#!/usr/bin/env python3
"""AI Assets — 统一财务审计数据中心 (v3.0 - Unified Truth)"""

import json
import sqlite3
import shutil
import subprocess
import logging
import time
import re
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
import config
from typing import Any
from collections import defaultdict
from data_foundation.paths import load_paths
from data_foundation.diary_paths import iter_diary_markdown_files
from data_foundation.external_tool_catalog import detect_external_tools
from data_foundation.settings import (
    default_external_tool_settings,
    external_tool_path,
    external_tool_path_list,
    resolve_dashboard_settings,
    resolve_runtime_source,
)
from data_foundation.time import resolve_timezone
from data_foundation.network import host_for_url
from data_foundation.token_semantics import normalize_cached_input_detail
from data_foundation.snapshots import apply_external_tool_visibility
from data_foundation.runtime_sources import (
    AntigravityRuntime,
    CursorRuntime,
    OpenCodeRuntime,
)
from data_foundation.usage_attribution import (
    CONTAINER_WORKSPACE_NAMES,
    TOOL_EMOJI,
    WORKSPACE_USAGE_MIN_TOKENS,
    canonical_workspace_name,
    resolve_usage_group,
    usage_group_display_allowed,
)
from data_foundation.workspace_attribution import workspace_display_name
from agentic_rag.rag_active_source import resolve_active_rag_index
from agentic_rag.rag_settings import (
    DEFAULT_RAG_SERVER_HEALTH_PATH,
    DEFAULT_RAG_SERVER_HOST,
    DEFAULT_RAG_SERVER_PORT,
    resolve_rag_settings,
)

from .dashboard_state import attach_dashboard_state, dashboard_failure, source_error
from .ui_text import dashboard_language_profile, is_english_profile

logger = logging.getLogger("dashboard.ai_assets")

# ── 缓存 (30s TTL) ──
_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 30
AI_ASSETS_CONTAINER_WORKSPACE_NAMES = CONTAINER_WORKSPACE_NAMES
_RUNTIME_SESSION_RECORDS: dict[str, tuple] = {}
_RUNTIME_DIALOGUE_ACTIVITY: dict[str, tuple[tuple[str, datetime | None], ...]] = {}

# ── 工具定义 ──
TOOL_DEFS = [
    {"name": "OpenClaw", "emoji": TOOL_EMOJI["OpenClaw"]},
    {"name": "Claude Code", "emoji": TOOL_EMOJI["Claude Code"]},
    {"name": "Gemini CLI", "emoji": TOOL_EMOJI["Gemini CLI"]},
    {"name": "Codex", "emoji": TOOL_EMOJI["Codex"]},
    {"name": "Hermes", "emoji": TOOL_EMOJI["Hermes"]},
    {"name": "OpenCode", "emoji": TOOL_EMOJI["OpenCode"], "usageStatus": "available"},
    {"name": "Antigravity", "emoji": TOOL_EMOJI["Antigravity"], "usageStatus": "local-partial"},
    {"name": "Cursor", "emoji": TOOL_EMOJI["Cursor"], "usageStatus": "unavailable"},
]

# ── 基础路径 ──
HOME = Path.home()
_DEFAULT_HOME = HOME
_DEFAULT_EXTERNAL_TOOLS = default_external_tool_settings(HOME)
OPENCLAW_DIR = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["home"]))
AGENTS_DIR = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["agentsRoot"]))
DIARY_DIR = config.DIARY_OUTPUT_DIR
MEMORY_DIR = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["memoryRoot"]))
SKILLS_DIR = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["systemSkillsRoot"]))
OPENCLAW_CONFIG = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["configPath"]))
BUILTIN_SKILLS_DIR = Path(os.getenv("OPENCLAW_BUILTIN_SKILLS_DIR", str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["systemSkillsRoot"])))
TOOL_CONFIG_SNAPSHOT = Path(str(_DEFAULT_EXTERNAL_TOOLS["openclaw"]["toolConfigSnapshotPath"]))
_DEFAULT_TOOL_CONFIG_SNAPSHOT = TOOL_CONFIG_SNAPSHOT

def _local_tz():
    return resolve_timezone()

TZ = _local_tz()

def _now_local() -> datetime: return datetime.now(_local_tz())
def _utc_to_local(ts_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_local_tz())
    except:
        try: return datetime.fromtimestamp(float(ts_str), tz=_local_tz())
        except: return _now_local()

def _safe_read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except: return None

def _count_lines(path: Path) -> int:
    if not path.exists(): return 0
    try:
        r = subprocess.run(["wc", "-l", str(path)], capture_output=True, text=True, timeout=5)
        return int(r.stdout.split()[0])
    except: return 0

def _external_tool_path(tool: str, key: str) -> Path:
    fallback = default_external_tool_settings(HOME).get(tool, {}).get(key)
    if HOME != _DEFAULT_HOME:
        return Path(str(fallback)).expanduser().absolute()
    try:
        return external_tool_path(tool, key)
    except Exception:
        pass
    return Path(str(fallback)).expanduser().absolute()

def _external_tool_list(tool: str, key: str) -> list[Path]:
    fallback = default_external_tool_settings(HOME).get(tool, {}).get(key, [])
    if HOME != _DEFAULT_HOME:
        value = fallback
    else:
        try:
            return external_tool_path_list(tool, key)
        except Exception:
            value = fallback
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, Path) else Path(str(item)).expanduser().absolute() for item in value]

def _dashboard_settings() -> dict[str, Any]:
    try:
        return resolve_dashboard_settings(load_paths())
    except Exception:
        return {}

def _workspace_dir() -> Path:
    value = _dashboard_settings().get("projectRoot") or config.WORKSPACE_DIR
    return Path(str(value)).expanduser().absolute()

def _dashboard_app_dir() -> Path:
    settings = _dashboard_settings()
    value = settings.get("appDir") or (_workspace_dir() / "src" / "dashboard")
    return Path(str(value)).expanduser().absolute()

def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0

def _ai_assets_cache_key(source: str) -> dict[str, Any]:
    paths = load_paths()
    settings_path = paths.config_dir / "settings.json"
    runtime_path = paths.config_dir / "runtime.json"
    return {
        "source": source,
        "actanaraHome": str(paths.home),
        "database": str(paths.db_path),
        "projectRoot": str(_workspace_dir()),
        "dashboardAppDir": str(_dashboard_app_dir()),
        "settingsMtime": _mtime_or_zero(settings_path),
        "runtimeMtime": _mtime_or_zero(runtime_path),
    }


def _current_external_tool_detection() -> dict[str, Any]:
    try:
        return detect_external_tools(load_paths())
    except Exception as error:
        logger.warning("AI Assets external tool detection failed: %s", error)
        return {"detectedToolKeys": [], "toolPresence": {}}


def _tool_homes_by_name() -> dict[str, Path]:
    return {
        "Claude Code": _external_tool_path("claudeCode", "home"),
        "Gemini CLI": _external_tool_path("geminiCli", "home"),
        "Codex": _external_tool_path("codex", "home"),
        "Hermes": _external_tool_path("hermes", "home"),
        "OpenCode": _external_tool_path("opencode", "home"),
        "Antigravity": _external_tool_path("antigravity", "home"),
        "Cursor": _external_tool_path("cursor", "home"),
    }

def _tool_key_files_by_name() -> dict[str, list[tuple[str, Path]]]:
    homes = _tool_homes_by_name()
    return {
        "Claude Code": [
            ("settings.json", homes["Claude Code"] / "settings.json"),
            (".claude.json", homes["Claude Code"].parent / ".claude.json"),
            ("CLAUDE.md", homes["Claude Code"] / "CLAUDE.md"),
        ],
        "Gemini CLI": [
            ("GEMINI.md", homes["Gemini CLI"] / "GEMINI.md"),
            ("settings.json", homes["Gemini CLI"] / "settings.json"),
        ],
        "Codex": [
            ("AGENTS.md", homes["Codex"] / "AGENTS.md"),
            ("config.toml", homes["Codex"] / "config.toml"),
        ],
        "Hermes": [
            ("SOUL.md", homes["Hermes"] / "SOUL.md"),
            ("config.yaml", homes["Hermes"] / "config.yaml"),
            ("MEMORY.md", homes["Hermes"] / "memories" / "MEMORY.md"),
        ],
    }

def _tool_config_snapshot_path() -> Path:
    if TOOL_CONFIG_SNAPSHOT != _DEFAULT_TOOL_CONFIG_SNAPSHOT:
        return TOOL_CONFIG_SNAPSHOT
    return _external_tool_path("openclaw", "toolConfigSnapshotPath")

def _diary_dir() -> Path:
    try:
        return load_paths().diary_dir
    except Exception:
        return DIARY_DIR

def _rag_index_path() -> Path | None:
    try:
        active = resolve_active_rag_index(resolve_rag_settings())
        if active.source == "v2" and active.ready and active.index_path:
            return active.index_path
    except Exception:
        pass
    return None

def _embedding_health_url() -> str:
    try:
        settings = resolve_rag_settings()
        return f"http://{host_for_url(settings.server_host)}:{settings.server_port}{settings.server_health_path}"
    except Exception:
        return f"http://{DEFAULT_RAG_SERVER_HOST}:{DEFAULT_RAG_SERVER_PORT}{DEFAULT_RAG_SERVER_HEALTH_PATH}"

def _codex_model_from_payload(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("model", "model_key", "model_slug"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            value = settings.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    settings = payload.get("settings")
    if isinstance(settings, dict):
        value = settings.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

def _codex_model_from_context_window(window: object) -> str | None:
    try:
        parsed = int(window)
    except (TypeError, ValueError):
        return None
    if parsed == 258400:
        return "gpt-5.5"
    return None

def _workspace_group_from_path(path: str | Path) -> str:
    return workspace_display_name(path)


def _gemini_project_labels() -> dict[str, str]:
    data = _safe_read_json(_external_tool_path("geminiCli", "projectsPath")) or {}
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    result = {}
    for real_path, slug in projects.items():
        result[slug] = Path(real_path).name or real_path
    return result

def _ping(ip: str, timeout: int = 2) -> bool:
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip], capture_output=True, timeout=timeout + 1)
        return r.returncode == 0
    except: return False

# ── 1. Scanners (直插源头，包含全量历史) ──

def _scan_all_openclaw():
    """扫描所有 OpenClaw session (排除冗余备份和轨迹文件)"""
    from .token_clock import _collect_openclaw_session_files, _parse_openclaw_jsonl_file, _file_cache
    base = _external_tool_path("openclaw", "agentsRoot"); session_files = _collect_openclaw_session_files(base)
    entries = []; session_count = len(session_files)
    for sid, f in session_files.items():
        fp = str(f); fname = f.name
        # 严格过滤 redundant 文件
        if any(x in fname for x in ['.bak', '.trajectory.jsonl', '.checkpoint.', '.lock', '.jsonl.tmp']):
            continue
        try:
            mtime = f.stat().st_mtime
            cached = _file_cache.get(fp)
            if cached and cached[0] == mtime: file_entries = cached[1]
            else:
                file_entries = _parse_openclaw_jsonl_file(f)
                _file_cache[fp] = (mtime, file_entries)
            group = f.parent.parent.name
            entries.extend({**entry, "usageGroup": group} for entry in file_entries)
        except: continue
    return entries, session_count

def _scan_all_claude():
    base = _external_tool_path("claudeCode", "projectsRoot")
    entries = []; session_count = 0
    if not base.exists(): return entries, 0
    for f in base.rglob("*.jsonl"):
        is_subagent = "/subagents/" in str(f)
        # Tiny top-level sessions are generally probes; subagent usage is billed work.
        try:
            if not is_subagent and f.stat().st_size < 5120: continue
        except: continue
        if not is_subagent:
            session_count += 1
        try:
            project_dir = f.parents[2] if is_subagent else f.parent
            decoded = _decode_claude_project_path(project_dir)
            resolved_group = resolve_usage_group(
                "claude-code",
                raw_path=str(f),
                cwd=str(decoded) if decoded else "",
                fallback=_workspace_label(project_dir.name),
            )
            group = resolved_group.group or _workspace_label(project_dir.name)
            with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                for line in fin:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "assistant":
                            u = obj.get("message", {}).get("usage", {})
                            entries.append({
                                "input": u.get("input_tokens") or 0,
                                "output": u.get("output_tokens") or 0,
                                "cacheRead": u.get("cache_read_input_tokens") or 0,
                                "cacheWrite": u.get("cache_creation_input_tokens") or 0,
                                "timestamp": obj.get("timestamp", ""),
                                "model": obj.get("message", {}).get("model", ""),
                                "usageGroup": group,
                                "usageGroupSource": resolved_group.source,
                                "usageGroupConfidence": resolved_group.confidence,
                            })
                    except: continue
        except: continue
    return entries, session_count

def _scan_all_gemini():
    base = _external_tool_path("geminiCli", "chatsRoot")
    entries = []; session_count = 0
    if not base.exists(): return entries, 0
    labels = _gemini_project_labels()
    session_files = {}
    for f in base.glob("session-*"):
        if f.suffix not in (".json", ".jsonl"):
            continue
        current = session_files.get(f.stem)
        if current is None or (current.suffix == ".json" and f.suffix == ".jsonl"):
            session_files[f.stem] = f
    for f in session_files.values():
        session_count += 1
        try:
            file_entries = []
            fallback_group = labels.get(f.parent.parent.name, f.parent.parent.name)
            resolved_group = resolve_usage_group("gemini-cli", raw_path=str(f), fallback=fallback_group)
            group = resolved_group.group or fallback_group
            if f.suffix == ".jsonl":
                with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                    for line in fin:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") != "gemini": continue
                            t = obj.get("tokens", {})
                            file_entries.append({
                                "input": t.get("input", 0), "output": t.get("output", 0),
                                "cacheRead": t.get("cached", 0), "cacheWrite": 0,
                                "timestamp": obj.get("timestamp", ""), "model": obj.get("model", ""),
                                "usageGroup": group,
                                "usageGroupSource": resolved_group.source,
                                "usageGroupConfidence": resolved_group.confidence,
                            })
                        except: continue
            else:
                raw = f.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw)
                for msg in (data.get("messages", []) if isinstance(data, dict) else []):
                    if msg.get("type") != "gemini": continue
                    t = msg.get("tokens", {})
                    file_entries.append({
                        "input": t.get("input", 0), "output": t.get("output", 0),
                        "cacheRead": t.get("cached", 0), "cacheWrite": 0,
                        "timestamp": msg.get("timestamp", ""), "model": msg.get("model", ""),
                        "usageGroup": group,
                        "usageGroupSource": resolved_group.source,
                        "usageGroupConfidence": resolved_group.confidence,
                    })
            entries.extend(file_entries)
        except: continue
    return entries, session_count

def _scan_all_codex():
    """Scan Codex JSONL sessions using last_token_usage (incremental values).
    total_token_usage is cumulative within a session and MUST NOT be summed.
    """
    base = _external_tool_path("codex", "sessionsRoot")
    entries = []; session_count = 0
    if not base.exists(): return entries, 0
    for f in base.rglob("rollout-*.jsonl"):
        session_count += 1
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                last_ts = ""
                resolved_group = resolve_usage_group("codex", raw_path=str(f), fallback="unknown")
                usage_group = resolved_group.group or "unknown"
                usage_group_source = resolved_group.source
                usage_group_confidence = resolved_group.confidence
                current_model = None
                for line in fin:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "session_meta":
                            cwd = obj.get("payload", {}).get("cwd", "")
                            if cwd:
                                resolved_group = resolve_usage_group("codex", cwd=cwd, raw_path=str(f), fallback=usage_group)
                                usage_group = resolved_group.group or usage_group
                                usage_group_source = resolved_group.source
                                usage_group_confidence = resolved_group.confidence
                            continue
                        if obj.get("type") == "turn_context":
                            current_model = _codex_model_from_payload(obj.get("payload", {})) or current_model
                            continue
                        if obj.get("type") != "event_msg":
                            continue
                        payload = obj.get("payload", {})
                        if payload.get("type") != "token_count": continue
                        info = payload.get("info", {})
                        ltu = info.get("last_token_usage")
                        if not ltu: continue
                        raw_input = ltu.get("input_tokens", 0) or 0
                        out = ltu.get("output_tokens", 0) or 0
                        cached = ltu.get("cached_input_tokens", 0) or 0
                        inp, cached, cache_semantics = normalize_cached_input_detail(
                            input_tokens=raw_input,
                            output_tokens=out,
                            cache_read_tokens=cached,
                            reported_total_tokens=ltu.get("total_tokens"),
                        )
                        reasoning = ltu.get("reasoning_output_tokens", 0) or 0
                        # Skip zero-increment events
                        if raw_input == 0 and out == 0 and cached == 0 and reasoning == 0:
                            continue
                        ts = obj.get("timestamp", "")
                        if ts: last_ts = ts
                        entries.append({
                            "input": inp,
                            "output": out,
                            "cacheRead": cached,
                            "cacheWrite": 0,
                            "reasoning": reasoning,
                            "rawInput": raw_input,
                            "cacheInputSemantics": cache_semantics,
                            "legacyInputWithCache": raw_input if cache_semantics == "input_includes_cached_input" else inp + cached,
                            "legacyOutputWithReasoning": out + reasoning,
                            "timestamp": ts or last_ts,
                            "model": _codex_model_from_payload(payload) or current_model or _codex_model_from_context_window(info.get("model_context_window")) or "",
                            "message_count": 1,
                            "usageGroup": usage_group,
                            "usageGroupSource": usage_group_source,
                            "usageGroupConfidence": usage_group_confidence,
                        })
                    except: continue
        except: continue
    return entries, session_count

def _scan_all_hermes():
    db_path = _external_tool_path("hermes", "stateDbPath")
    entries = []; session_count = 0
    if not db_path.exists(): return entries, 0
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sessions"); session_count = cur.fetchone()[0] or 0
        columns = {row[1] for row in cur.execute("PRAGMA table_info(sessions)").fetchall()}
        model_select = "model" if "model" in columns else "'' AS model"
        cwd_select = "cwd" if "cwd" in columns else "'' AS cwd"
        cur.execute(
            f"SELECT started_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, message_count, {model_select}, {cwd_select} FROM sessions"
        )
        for r in cur.fetchall():
            resolved = resolve_usage_group("hermes", cwd=str(r[7] or ""), fallback="Hermes")
            entries.append({
                "input": r[1] or 0, "output": r[2] or 0, "cacheRead": r[3] or 0,
                "cacheWrite": r[4] or 0, "timestamp": str(r[0]), "message_count": r[5] or 1,
                "model": r[6] or "", "usageGroup": resolved.group or "Hermes",
                "usageGroupSource": resolved.source,
                "usageGroupConfidence": resolved.confidence,
            })
    finally:
        conn.close()
    return entries, session_count


def _scan_all_opencode():
    return _scan_all_normalized_runtime(
        "OpenCode",
        OpenCodeRuntime(_external_tool_path("opencode", "home")),
    )


def _scan_all_antigravity():
    return _scan_all_normalized_runtime(
        "Antigravity",
        AntigravityRuntime(
            {
                "cli": _external_tool_path("antigravity", "cliHome"),
                "ide": _external_tool_path("antigravity", "ideHome"),
                "app": _external_tool_path("antigravity", "appHome"),
            }
        ),
    )


def _scan_all_cursor():
    return _scan_all_normalized_runtime(
        "Cursor",
        CursorRuntime(
            _external_tool_path("cursor", "home"),
            ide_state_dbs=_external_tool_list("cursor", "ideStateDbCandidates"),
            workspace_storage_roots=_external_tool_list("cursor", "workspaceStorageRoots"),
        ),
    )


def _scan_all_normalized_runtime(name: str, runtime) -> tuple[list[dict], int]:
    sessions = tuple(runtime.sessions())
    _RUNTIME_SESSION_RECORDS[name] = sessions
    _RUNTIME_DIALOGUE_ACTIVITY[name] = tuple(
        (record.external_session_key, record.occurred_at)
        for record in runtime.dialogue()
    )
    sessions_by_key = {record.external_session_key: record for record in sessions}
    entries = []
    for record in runtime.usage():
        session = sessions_by_key.get(record.external_session_key)
        cwd = session.initial_cwd if session is not None else None
        resolved = resolve_usage_group(
            runtime.tool_key,
            cwd=cwd,
            initial_cwd=cwd,
            metadata=record.metadata,
            fallback="",
        )
        entries.append(
            {
                "input": int(record.input_tokens or 0),
                "output": int(record.output_tokens or 0),
                "cacheRead": int(record.cache_read_tokens or 0),
                "cacheWrite": int(record.cache_write_tokens or 0),
                "reasoning": int(record.reasoning_tokens or 0),
                "toolTokens": int(record.tool_tokens or 0),
                "protocolTotal": record.protocol_total_tokens,
                "timestamp": record.occurred_at.isoformat(),
                "message_count": int(record.message_count or 1),
                "model": record.model_key or (session.model_key if session else None) or "",
                "usageGroup": resolved.group,
                "usageGroupSource": resolved.source,
                "usageGroupConfidence": resolved.confidence,
                "sourceVariant": record.source_variant,
            }
        )
    return entries, len(sessions)

_ALL_SCANNERS = [
    ("OpenClaw", _scan_all_openclaw),
    ("Claude Code", _scan_all_claude),
    ("Gemini CLI", _scan_all_gemini),
    ("Codex", _scan_all_codex),
    ("Hermes", _scan_all_hermes),
    ("OpenCode", _scan_all_opencode),
    ("Antigravity", _scan_all_antigravity),
    ("Cursor", _scan_all_cursor),
]

AI_ASSET_USAGE_PARSER_VERSION = "ai-assets-usage-cache-v9"


def _is_openclaw_redundant_session_file(path: Path) -> bool:
    return any(x in path.name for x in ['.bak', '.trajectory.jsonl', '.checkpoint.', '.lock', '.jsonl.tmp'])


def _iter_openclaw_usage_sources() -> list[dict]:
    from .token_clock import _collect_openclaw_session_files

    base = _external_tool_path("openclaw", "agentsRoot")
    sources = []
    for sid, path in sorted(_collect_openclaw_session_files(base).items(), key=lambda item: str(item[1])):
        if _is_openclaw_redundant_session_file(path):
            continue
        sources.append({
            "tool": "OpenClaw",
            "path": path,
            "sessionId": sid,
            "usageGroup": resolve_usage_group("openclaw", raw_path=str(path), fallback=path.parent.parent.name).group,
            "sessionCountUnit": 1,
        })
    return sources


def _iter_claude_usage_sources() -> list[dict]:
    base = _external_tool_path("claudeCode", "projectsRoot")
    sources = []
    if not base.exists():
        return sources
    for path in sorted(base.rglob("*.jsonl")):
        is_subagent = "/subagents/" in str(path)
        try:
            if not is_subagent and path.stat().st_size < 5120:
                continue
        except OSError:
            continue
        project_dir = path.parents[2] if is_subagent else path.parent
        sources.append({
            "tool": "Claude Code",
            "path": path,
            "sessionId": path.stem,
            "usageGroup": _claude_usage_group(project_dir),
            "sessionCountUnit": 0 if is_subagent else 1,
        })
    return sources


def _claude_usage_group(project_dir: Path) -> str:
    decoded = _decode_claude_project_path(project_dir)
    if decoded is not None:
        resolved = resolve_usage_group("claude-code", raw_path=str(project_dir), cwd=str(decoded))
        if resolved.group:
            return resolved.group
    return resolve_usage_group("claude-code", raw_path=str(project_dir), fallback=_workspace_label(project_dir.name)).group


def _iter_gemini_usage_sources() -> list[dict]:
    base = _external_tool_path("geminiCli", "chatsRoot")
    sources = []
    if not base.exists():
        return sources
    labels = _gemini_project_labels()
    session_files = {}
    for path in base.glob("session-*"):
        if path.suffix not in (".json", ".jsonl"):
            continue
        current = session_files.get(path.stem)
        if current is None or (current.suffix == ".json" and path.suffix == ".jsonl"):
            session_files[path.stem] = path
    for path in sorted(session_files.values()):
        sources.append({
            "tool": "Gemini CLI",
            "path": path,
            "sessionId": path.stem,
            "usageGroup": labels.get(path.parent.parent.name, path.parent.parent.name),
            "sessionCountUnit": 1,
        })
    return sources


def _iter_codex_usage_sources() -> list[dict]:
    base = _external_tool_path("codex", "sessionsRoot")
    if not base.exists():
        return []
    return [
        {
            "tool": "Codex",
            "path": path,
            "sessionId": path.stem,
            "usageGroup": "unknown",
            "sessionCountUnit": 1,
        }
        for path in sorted(base.rglob("rollout-*.jsonl"))
    ]


def _iter_incremental_usage_sources() -> list[dict]:
    sources = []
    for discover in (
        _iter_openclaw_usage_sources,
        _iter_claude_usage_sources,
        _iter_gemini_usage_sources,
        _iter_codex_usage_sources,
    ):
        try:
            sources.extend(discover())
        except Exception as error:
            logger.warning("AI Assets source discovery failed: %s", error)
    return sources


def _parse_openclaw_usage_file(path: Path, usage_group: str) -> list[dict]:
    from .token_clock import _parse_openclaw_jsonl_file

    return [{**entry, "usageGroup": usage_group} for entry in _parse_openclaw_jsonl_file(path)]


def _parse_claude_usage_file(path: Path, usage_group: str) -> list[dict]:
    entries = []
    nonblank_lines = 0
    valid_json_lines = 0
    project_dir = path.parents[2] if "/subagents/" in str(path) else path.parent
    decoded = _decode_claude_project_path(project_dir)
    resolved_group = resolve_usage_group(
        "claude-code",
        raw_path=str(path),
        cwd=str(decoded) if decoded else "",
        fallback=usage_group,
    )
    group = resolved_group.group or usage_group
    with open(path, "r", encoding="utf-8", errors="ignore") as fin:
        for line in fin:
            if not line.strip():
                continue
            nonblank_lines += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            valid_json_lines += 1
            if obj.get("type") != "assistant":
                continue
            usage = obj.get("message", {}).get("usage", {})
            entries.append({
                "input": usage.get("input_tokens") or 0,
                "output": usage.get("output_tokens") or 0,
                "cacheRead": usage.get("cache_read_input_tokens") or 0,
                "cacheWrite": usage.get("cache_creation_input_tokens") or 0,
                "timestamp": obj.get("timestamp", ""),
                "model": obj.get("message", {}).get("model", ""),
                "usageGroup": group,
                "usageGroupSource": resolved_group.source,
                "usageGroupConfidence": resolved_group.confidence,
            })
    if nonblank_lines and not valid_json_lines:
        raise ValueError("session file contains no valid JSON records")
    return entries


def _parse_gemini_usage_file(path: Path, usage_group: str) -> list[dict]:
    entries = []
    resolved_group = resolve_usage_group("gemini-cli", raw_path=str(path), fallback=usage_group)
    group = resolved_group.group or usage_group
    if path.suffix == ".jsonl":
        nonblank_lines = 0
        valid_json_lines = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as fin:
            for line in fin:
                if not line.strip():
                    continue
                nonblank_lines += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                valid_json_lines += 1
                if obj.get("type") != "gemini":
                    continue
                tokens = obj.get("tokens", {})
                entries.append({
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0),
                    "cacheRead": tokens.get("cached", 0),
                    "cacheWrite": 0,
                    "timestamp": obj.get("timestamp", ""),
                    "model": obj.get("model", ""),
                    "usageGroup": group,
                    "usageGroupSource": resolved_group.source,
                    "usageGroupConfidence": resolved_group.confidence,
                })
        if nonblank_lines and not valid_json_lines:
            raise ValueError("session file contains no valid JSON records")
        return entries
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    for msg in (data.get("messages", []) if isinstance(data, dict) else []):
        if msg.get("type") != "gemini":
            continue
        tokens = msg.get("tokens", {})
        entries.append({
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
            "cacheRead": tokens.get("cached", 0),
            "cacheWrite": 0,
            "timestamp": msg.get("timestamp", ""),
            "model": msg.get("model", ""),
            "usageGroup": group,
            "usageGroupSource": resolved_group.source,
            "usageGroupConfidence": resolved_group.confidence,
        })
    return entries


def _parse_codex_usage_file(path: Path, usage_group: str) -> list[dict]:
    entries = []
    last_ts = ""
    nonblank_lines = 0
    valid_json_lines = 0
    resolved_group = resolve_usage_group("codex", raw_path=str(path), fallback=usage_group)
    group = resolved_group.group or usage_group
    group_source = resolved_group.source
    group_confidence = resolved_group.confidence
    current_model = None
    with open(path, "r", encoding="utf-8", errors="ignore") as fin:
        for line in fin:
            if not line.strip():
                continue
            nonblank_lines += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            valid_json_lines += 1
            if obj.get("type") == "session_meta":
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                cwd = payload.get("cwd", "")
                if cwd:
                    resolved_group = resolve_usage_group("codex", cwd=cwd, raw_path=str(path), fallback=group)
                    group = resolved_group.group or group
                    group_source = resolved_group.source
                    group_confidence = resolved_group.confidence
                continue
            if obj.get("type") == "turn_context":
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                current_model = _codex_model_from_payload(payload) or current_model
                continue
            if obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            last_token_usage = info.get("last_token_usage")
            if not last_token_usage:
                continue
            raw_input = last_token_usage.get("input_tokens", 0) or 0
            output = last_token_usage.get("output_tokens", 0) or 0
            cached = last_token_usage.get("cached_input_tokens", 0) or 0
            inp, cached, cache_semantics = normalize_cached_input_detail(
                input_tokens=raw_input,
                output_tokens=output,
                cache_read_tokens=cached,
                reported_total_tokens=last_token_usage.get("total_tokens"),
            )
            reasoning = last_token_usage.get("reasoning_output_tokens", 0) or 0
            if raw_input == 0 and output == 0 and cached == 0 and reasoning == 0:
                continue
            ts = obj.get("timestamp", "")
            if ts:
                last_ts = ts
            entries.append({
                "input": inp,
                "output": output,
                "cacheRead": cached,
                "cacheWrite": 0,
                "reasoning": reasoning,
                "rawInput": raw_input,
                "cacheInputSemantics": cache_semantics,
                "legacyInputWithCache": raw_input if cache_semantics == "input_includes_cached_input" else inp + cached,
                "legacyOutputWithReasoning": output + reasoning,
                "timestamp": ts or last_ts,
                "model": _codex_model_from_payload(payload) or current_model or _codex_model_from_context_window(info.get("model_context_window")) or "",
                "message_count": 1,
                "usageGroup": group,
                "usageGroupSource": group_source,
                "usageGroupConfidence": group_confidence,
            })
    if nonblank_lines and not valid_json_lines:
        raise ValueError("session file contains no valid JSON records")
    return entries


def _parse_usage_source(source: dict) -> list[dict]:
    tool = source["tool"]
    path = source["path"]
    usage_group = source.get("usageGroup") or tool
    if tool == "OpenClaw":
        return _parse_openclaw_usage_file(path, usage_group)
    if tool == "Claude Code":
        return _parse_claude_usage_file(path, usage_group)
    if tool == "Gemini CLI":
        return _parse_gemini_usage_file(path, usage_group)
    if tool == "Codex":
        return _parse_codex_usage_file(path, usage_group)
    return []


def _entry_metadata(entry: dict) -> dict:
    return {
        key: value
        for key, value in entry.items()
        if key not in {
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "reasoning",
            "rawInput",
            "timestamp",
            "model",
            "message_count",
            "usageGroup",
        }
    }


def _write_usage_source_cache(connection, source: dict, entries: list[dict], stat_result) -> None:
    now = _now_local().isoformat()
    tool = source["tool"]
    source_path = str(source["path"])
    connection.execute(
        """
        INSERT INTO ai_asset_usage_source_files(
            tool_name, source_path, file_mtime, file_size, session_id,
            usage_group, session_count_unit, parser_version, parsed_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
        ON CONFLICT(tool_name, source_path) DO UPDATE SET
            file_mtime=excluded.file_mtime,
            file_size=excluded.file_size,
            session_id=excluded.session_id,
            usage_group=excluded.usage_group,
            session_count_unit=excluded.session_count_unit,
            parser_version=excluded.parser_version,
            parsed_at=excluded.parsed_at,
            status=excluded.status
        """,
        (
            tool,
            source_path,
            stat_result.st_mtime,
            stat_result.st_size,
            source.get("sessionId"),
            source.get("usageGroup"),
            int(source.get("sessionCountUnit", 1)),
            AI_ASSET_USAGE_PARSER_VERSION,
            now,
        ),
    )
    connection.execute(
        "DELETE FROM ai_asset_usage_records WHERE tool_name = ? AND source_path = ?",
        (tool, source_path),
    )
    rows = []
    for index, entry in enumerate(entries):
        rows.append((
            tool,
            source_path,
            index,
            int(entry.get("input") or 0),
            int(entry.get("output") or 0),
            int(entry.get("cacheRead") or 0),
            int(entry.get("cacheWrite") or 0),
            int(entry.get("reasoning") or 0),
            int(entry.get("rawInput")) if entry.get("rawInput") is not None else None,
            entry.get("timestamp", ""),
            entry.get("model", ""),
            int(entry.get("message_count", 1) or 1),
            entry.get("usageGroup") or source.get("usageGroup") or tool,
            json.dumps(_entry_metadata(entry), ensure_ascii=False, sort_keys=True),
        ))
    connection.executemany(
        """
        INSERT INTO ai_asset_usage_records(
            tool_name, source_path, event_index, input_tokens, output_tokens,
            cache_read_tokens, cache_write_tokens, reasoning_tokens, raw_input_tokens,
            timestamp, model, message_count, usage_group, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _usage_records_from_cache(connection) -> tuple[dict[str, list[dict]], dict[str, int]]:
    all_entries: dict[str, list[dict]] = defaultdict(list)
    session_counts = {
        row["tool_name"]: row["count"] or 0
        for row in connection.execute(
            """
            SELECT tool_name, SUM(session_count_unit) AS count
            FROM ai_asset_usage_source_files
            WHERE status = 'ready'
            GROUP BY tool_name
            """
        )
    }
    for row in connection.execute(
        """
        SELECT tool_name, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
               reasoning_tokens, raw_input_tokens, timestamp, model, message_count,
               usage_group, metadata_json
        FROM ai_asset_usage_records
        ORDER BY tool_name, source_path, event_index
        """
    ):
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        entry = {
            **metadata,
            "input": row["input_tokens"],
            "output": row["output_tokens"],
            "cacheRead": row["cache_read_tokens"],
            "cacheWrite": row["cache_write_tokens"],
            "timestamp": row["timestamp"] or "",
            "model": row["model"] or "",
            "message_count": row["message_count"] or 1,
            "usageGroup": row["usage_group"] or row["tool_name"],
        }
        if row["reasoning_tokens"]:
            entry["reasoning"] = row["reasoning_tokens"]
        if row["raw_input_tokens"] is not None:
            entry["rawInput"] = row["raw_input_tokens"]
        all_entries[row["tool_name"]].append(entry)
    return dict(all_entries), dict(session_counts)


def _scan_usage_incremental() -> tuple[dict[str, list[dict]], dict[str, int], dict]:
    from data_foundation.db import connect, migrate

    paths = load_paths()
    migrate(paths)
    sources = _iter_incremental_usage_sources()
    active_keys = {(source["tool"], str(source["path"])) for source in sources}
    stats = {"sources": len(sources), "reparsed": 0, "cached": 0, "removed": 0, "errors": 0}
    with connect(paths) as connection:
        for tool_name in ("OpenClaw", "Claude Code", "Gemini CLI", "Codex"):
            known_paths = {
                row["source_path"]
                for row in connection.execute(
                    "SELECT source_path FROM ai_asset_usage_source_files WHERE tool_name = ?",
                    (tool_name,),
                )
            }
            active_paths = {path for tool, path in active_keys if tool == tool_name}
            stale_paths = known_paths - active_paths
            stats["removed"] += len(stale_paths)
            for source_path in stale_paths:
                connection.execute(
                    "DELETE FROM ai_asset_usage_source_files WHERE tool_name = ? AND source_path = ?",
                    (tool_name, source_path),
                )
        for source in sources:
            tool = source["tool"]
            path = source["path"]
            source_path = str(path)
            try:
                stat_result = path.stat()
            except OSError:
                stats["errors"] += 1
                continue
            row = connection.execute(
                """
                SELECT file_mtime, file_size, parser_version
                FROM ai_asset_usage_source_files
                WHERE tool_name = ? AND source_path = ? AND status = 'ready'
                """,
                (tool, source_path),
            ).fetchone()
            if (
                row is not None
                and row["file_mtime"] == stat_result.st_mtime
                and row["file_size"] == stat_result.st_size
                and row["parser_version"] == AI_ASSET_USAGE_PARSER_VERSION
            ):
                stats["cached"] += 1
                continue
            try:
                entries = _parse_usage_source(source)
                _write_usage_source_cache(connection, source, entries, stat_result)
                stats["reparsed"] += 1
            except Exception as error:
                stats["errors"] += 1
                logger.warning("AI Assets incremental parse failed for %s: %s", source_path, error)
        all_entries, session_counts = _usage_records_from_cache(connection)
    return all_entries, session_counts, stats

def _workspace_label(encoded_name: str) -> str:
    parts = [part for part in encoded_name.split("-") if part]
    return parts[-1] if parts else encoded_name

def _aggregate_tool(name: str, entries: list[dict], session_count: int) -> dict:
    td = next((t for t in TOOL_DEFS if t["name"] == name), {})
    total_tokens = 0; messages = 0; active_dates = set(); timestamps = []
    today_str = _now_local().strftime("%Y-%m-%d"); today_tokens = 0; today_messages = 0

    for e in entries:
        # v5.9.3 协议：Total 不含 cacheWrite
        t = _entry_total_tokens(e)
        total_tokens += t
        mc = e.get("message_count", 1) or 1
        messages += mc
        ts_raw = e.get("timestamp", "")
        if ts_raw:
            try:
                dt = _utc_to_local(ts_raw); ds = dt.strftime("%Y-%m-%d"); active_dates.add(ds); timestamps.append(dt)
                if ds == today_str: today_tokens += t; today_messages += mc
            except: pass

    for session in _RUNTIME_SESSION_RECORDS.get(name, ()):
        for occurred_at in (session.started_at, session.last_active_at):
            if occurred_at is None:
                continue
            dt = occurred_at.astimezone(_local_tz())
            active_dates.add(dt.strftime("%Y-%m-%d"))
            timestamps.append(dt)

    if td.get("usageStatus") == "unavailable":
        dialogue_activity = _RUNTIME_DIALOGUE_ACTIVITY.get(name, ())
        messages = len(dialogue_activity)
        today_messages = 0
        for _session_key, occurred_at in dialogue_activity:
            if occurred_at is None:
                continue
            dt = occurred_at.astimezone(_local_tz())
            if dt.strftime("%Y-%m-%d") == today_str:
                today_messages += 1

    timestamps.sort()
    return {
        "name": name, "emoji": td.get("emoji", ""),
        "allTimeTokens": total_tokens, "allTimeMessages": messages,
        "todayTokens": today_tokens, "todayMessages": today_messages,
        "sessionCount": session_count,
        "firstActivity": timestamps[0].strftime("%Y-%m-%d") if timestamps else "",
        "lastActivity": timestamps[-1].strftime("%Y-%m-%d") if timestamps else "",
        "activeDays": len(active_dates),
        "usageStatus": td.get("usageStatus", "available"),
    }

def _model_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("primary", "name", "id", "model"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _latest_models_from_entries(all_entries: dict[str, list[dict]]) -> dict[str, dict[str, str]]:
    latest: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for tool_name, entries in all_entries.items():
        for entry in entries:
            model = _model_name(entry.get("model"))
            if not model:
                continue
            timestamp = str(entry.get("timestamp") or "")
            for bucket in ("__tool__", str(entry.get("usageGroup") or "")):
                if not bucket:
                    continue
                current = latest[tool_name].get(bucket)
                if current is None or timestamp >= current[0]:
                    latest[tool_name][bucket] = (timestamp, model)
    return {
        tool_name: {bucket: model for bucket, (_timestamp, model) in buckets.items()}
        for tool_name, buckets in latest.items()
    }


def _get_agents_enhanced(tool_stats_list: list[dict], all_entries: dict[str, list[dict]] | None = None) -> list[dict]:
    """获取 Agents 列表，并注入 Gemini/Claude/Codex/Hermes 作为 Agent"""
    agents = []
    latest_models = _latest_models_from_entries(all_entries or {})
    agents_root = _external_tool_path("openclaw", "agentsRoot")
    openclaw_home = _external_tool_path("openclaw", "home")
    if agents_root.exists():
        from .token_clock import _collect_openclaw_session_files
        all_session_map = _collect_openclaw_session_files(agents_root)
        config = _safe_read_json(_external_tool_path("openclaw", "configPath")); agent_configs = {}; defaults_model = ""
        if config and isinstance(config, dict):
            acfg = config.get("agents", {}); defaults_model = acfg.get("defaults", {}).get("model", {}).get("primary", "")
            for a in acfg.get("list", []):
                if isinstance(a, dict) and a.get("id"): agent_configs[a["id"]] = a

        for adir in agents_root.iterdir():
            if not adir.is_dir(): continue
            aid = adir.name; sdir = adir / "sessions"
            scnt = 0; msgs = 0; last_a = None
            if sdir.exists():
                a_map = {sid: f for sid, f in all_session_map.items() if str(f).startswith(str(sdir) + '/')}
                scnt = len(a_map); latest_mt = 0
                for sid, f in list(a_map.items())[:50]:
                    mt = f.stat().st_mtime; latest_mt = max(latest_mt, mt)
                    try:
                        with open(f, "r", encoding="utf-8", errors="ignore") as f_in:
                            for l in f_in:
                                if '"role":"assistant"' in l: msgs += 1
                    except: pass
                if latest_mt: last_a = datetime.fromtimestamp(latest_mt, tz=_local_tz()).strftime("%Y-%m-%d %H:%M")

            ac = agent_configs.get(aid, {}); iden = ac.get("identity", {}) if isinstance(ac.get("identity"), dict) else {}
            wpath = Path(ac.get("workspace", "")) if ac.get("workspace") else (openclaw_home / f"workspace-{aid}")
            docs = {n: {"lines": _count_lines(wpath / n), "exists": (wpath/n).exists()} for n in ["SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "USER.md"]}

            # OpenClaw agent
            display_name = f"{TOOL_EMOJI['OpenClaw']} {iden.get('name') or aid}"

            agents.append({
                "id": aid, "name": aid, "displayName": display_name,
                "model": _model_name(ac.get("model")) or latest_models.get("OpenClaw", {}).get(aid, "") or _model_name(defaults_model) or "unknown",
                "workspace": str(wpath), "sessionCount": scnt, "totalMessages": msgs,
                "lastActive": last_a or "unknown", "documents": docs, "identity": iden,
                "source": "OpenClaw"
            })

    # 注入外部工具作为 Agent（含关键文件）
    _tool_key_files = _tool_key_files_by_name()
    _tool_home = _tool_homes_by_name()
    for ts in tool_stats_list:
        if ts["name"] == "OpenClaw": continue
        tname = ts["name"]
        docs = {}
        for fn, fp in _tool_key_files.get(tname, []):
            docs[fn] = {"exists": fp.exists(), "lines": _count_lines(fp), "path": str(fp)}
        agents.append({
            "id": tname.lower().replace(" ", "-"), "name": tname,
            "displayName": f"{ts['emoji']} {ts['name']}", "model": latest_models.get(tname, {}).get("__tool__", ""),
            "workspace": str(_tool_home.get(tname, "")),
            "sessionCount": ts["sessionCount"], "totalMessages": ts["allTimeMessages"],
            "lastActive": ts["lastActivity"], "documents": docs, "identity": {}, "source": tname,
        })

    agents.sort(key=lambda x: x["sessionCount"], reverse=True)
    return agents

# ── 3. File Content API Helpers (Preserved) ──

MAX_FILE_CONTENT_BYTES = 512 * 1024
_ALLOWED_TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst"}
_SAFE_TOP_LEVEL_TOOL_DOCS = {
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "GEMINI.md",
    "SOUL.md",
    "IDENTITY.md",
    "TOOLS.md",
    "MEMORY.md",
    "USER.md",
}
_SENSITIVE_FILENAMES = {
    ".claude.json",
    ".mcp.json",
    "authorized_keys",
    "config.json",
    "config.toml",
    "config.yaml",
    "config.yml",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
    "openclaw.json",
    "settings.json",
    "settings.local.json",
}
_SENSITIVE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".jsonl",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
}
FILE_WRITE_CONFIRMATION = "SAVE ACTANARA FILE"

def _allowed_document_roots() -> list[Path]:
    openclaw_home = _external_tool_path("openclaw", "home")
    claude_home = _external_tool_path("claudeCode", "home")
    codex_home = _external_tool_path("codex", "home")
    hermes_home = _external_tool_path("hermes", "home")
    roots = [
        _workspace_dir(),
        _external_tool_path("openclaw", "workspaceCoderRoot"),
        _external_tool_path("openclaw", "skillsRoot"),
        _external_tool_path("openclaw", "systemSkillsRoot"),
        _external_tool_path("claudeCode", "commandsRoot"),
        _external_tool_path("claudeCode", "pluginsRoot"),
        _external_tool_path("claudeCode", "skillsRoot"),
        _external_tool_path("codex", "skillsRoot"),
        codex_home / "skills",
        codex_home / "memories",
        hermes_home / "memories",
    ]
    try:
        roots.extend(path for path in openclaw_home.glob("workspace-*") if path.is_dir())
    except OSError:
        pass
    return [path.expanduser().resolve() for path in roots]


def _safe_top_level_tool_doc_paths() -> set[Path]:
    homes = list(_tool_homes_by_name().values())
    homes.append(_external_tool_path("openclaw", "home"))
    paths: set[Path] = set()
    for home in homes:
        base = home.expanduser()
        for name in _SAFE_TOP_LEVEL_TOOL_DOCS:
            paths.add((base / name).resolve())
    return paths

def _validate_file_path(file_path: str) -> tuple[bool, str]:
    raw_path = Path(str(file_path or "")).expanduser()
    if any(part == ".." for part in raw_path.parts):
        return False, "Path traversal not allowed"
    p = raw_path.resolve()
    if _is_sensitive_file(p):
        return False, "Sensitive file not allowed"
    if not _is_safe_text_document(p):
        return False, "File type not allowed"
    matched_root = next((root for root in _allowed_document_roots() if _path_is_relative_to(p, root)), None)
    if matched_root is None and p not in _safe_top_level_tool_doc_paths():
        return False, "Path not in whitelist"
    if matched_root is not None and _has_hidden_relative_component(p, matched_root):
        return False, "Hidden paths are not allowed"
    try:
        if p.exists() and p.stat().st_size > MAX_FILE_CONTENT_BYTES:
            return False, f"File exceeds {MAX_FILE_CONTENT_BYTES} byte limit"
    except OSError as error:
        return False, str(error)
    return True, ""


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _has_hidden_relative_component(path: Path, parent: Path) -> bool:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return True
    return any(part.startswith(".") for part in relative.parts)


def _is_safe_text_document(path: Path) -> bool:
    return path.name in _SAFE_TOP_LEVEL_TOOL_DOCS or path.suffix.lower() in _ALLOWED_TEXT_EXTENSIONS


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".env"):
        return True
    if name.startswith("config."):
        return True
    if name in _SENSITIVE_FILENAMES:
        return True
    return any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def read_file_content(file_path: str) -> dict:
    ok, err = _validate_file_path(file_path)
    if not ok: return {"error": err, "status": 403}
    p = Path(file_path).expanduser()
    if not p.exists(): return {"error": "File not found", "status": 404}
    try:
        content = p.read_text(encoding="utf-8")
        return {"path": str(p), "content": content, "size": p.stat().st_size, "lastModified": datetime.fromtimestamp(p.stat().st_mtime, tz=_local_tz()).strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e: return {"error": str(e), "status": 500}

def write_file_content(file_path: str, content: str, *, confirmation_text: str = "", dry_run: bool = False) -> dict:
    ok, err = _validate_file_path(file_path)
    if not ok: return {"error": err, "status": 403}
    p = Path(file_path).expanduser()
    try:
        if len(str(content).encode("utf-8")) > MAX_FILE_CONTENT_BYTES:
            return {"error": f"Content exceeds {MAX_FILE_CONTENT_BYTES} byte limit", "status": 413}
        backup_path = None
        if dry_run:
            return {
                "success": True,
                "dryRun": True,
                "path": str(p),
                "exists": p.exists(),
                "wouldCreateParent": not p.parent.exists(),
                "wouldBackup": p.exists(),
                "confirmationTextRequired": FILE_WRITE_CONFIRMATION,
            }
        if confirmation_text != FILE_WRITE_CONFIRMATION:
            return {"error": f"confirmationText must be exactly: {FILE_WRITE_CONFIRMATION}", "status": 400}
        if p.exists():
            selected = load_paths()
            stamp = datetime.now(_local_tz()).strftime("%Y%m%d-%H%M%S")
            backup_dir = selected.state_dir / "backups" / "file-content" / stamp
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / p.name
            backup_path.write_bytes(p.read_bytes())
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, encoding="utf-8")
        return {"success": True, "path": str(p), "size": p.stat().st_size, "backupPath": str(backup_path) if backup_path else None, "message": _ai_assets_ui("fileSaved")}
    except Exception as e: return {"error": str(e), "status": 500}

def update_file_content(file_path: str, content: str, *, confirmation_text: str = "", dry_run: bool = False) -> dict:
    return write_file_content(file_path, content, confirmation_text=confirmation_text, dry_run=dry_run)

# ── 4. Diary, Memory, Skills (Stat Wrappers) ──

def _get_diary_stats():
    count = 0; first = ""; last = ""; words = 0
    diary_dir = _diary_dir()
    if diary_dir.exists():
        files = iter_diary_markdown_files(diary_dir)
        dates = sorted(
            {
                match.group(1)
                for path in files
                for match in [re.search(r"-(\d{6})\.md$", path.name)]
                if match
            }
        )
        count = len(dates)
        if dates:
            first = f"20{dates[0][0:2]}-{dates[0][2:4]}-{dates[0][4:6]}"
            last = f"20{dates[-1][0:2]}-{dates[-1][2:4]}-{dates[-1][4:6]}"
            for md in files[-21:]:
                try: words += len(md.read_text(encoding="utf-8", errors="ignore").split())
                except: pass
    return {"count": count, "firstDate": first, "lastDate": last, "totalWords": words}

def _get_memory_stats():
    fc = 0; sz = 0; dc = 0; dnc = 0
    agents_root = _external_tool_path("openclaw", "agentsRoot")
    for adir in (agents_root.iterdir() if agents_root.exists() else []):
        mdir = adir / "memory"
        if mdir.exists():
            for f in mdir.rglob("*"):
                if f.is_file(): fc += 1; sz += f.stat().st_size
    memory_root = _external_tool_path("openclaw", "memoryRoot")
    if memory_root.exists():
        for f in memory_root.rglob("*"):
            if f.is_file():
                fc += 1; sz += f.stat().st_size
                if f.suffix == ".md":
                    if "diary" in f.name.lower(): dc += 1
                    elif re.match(r"\d{4}-\d{2}-\d{2}", f.name): dnc += 1
    return {"sessionFiles": fc, "totalSizeMB": round(sz / 1024 / 1024, 1), "diaryCount": dc, "dailyNoteCount": dnc}


def _get_git_stats() -> dict:
    try:
        commits = subprocess.run(
            ["git", "-C", str(_workspace_dir()), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        size = subprocess.run(
            ["du", "-sm", str(_workspace_dir() / ".git")],
            capture_output=True, text=True, timeout=5,
        )
        return {
            "commits": int(commits.stdout.strip() or 0) if commits.returncode == 0 else 0,
            "repoSizeMB": float(size.stdout.split()[0]) if size.returncode == 0 and size.stdout.split() else 0,
        }
    except Exception:
        return {"commits": 0, "repoSizeMB": 0}


def _get_cron_job_stats() -> dict:
    jobs_path = _first_existing_path(
        _external_tool_path("openclaw", "cronJobsPath"),
        _external_tool_path("openclaw", "cronJobsMigratedPath"),
    )
    runs_dir = _external_tool_path("openclaw", "cronRunsRoot")
    total = 0
    success = 0
    failed = 0
    if jobs_path:
        data = _safe_read_json(jobs_path) or {}
        total = len(data.get("jobs", []))
    if runs_dir.exists():
        for f in _iter_cron_run_files(runs_dir):
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
            except Exception:
                continue
            for line in lines:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = obj.get("status", "")
                if status == "ok":
                    success += 1
                elif status == "error":
                    failed += 1
    denom = success + failed
    return {"total": total, "success": success, "failed": failed, "successRate": round(success / denom * 100, 1) if denom else 0}


def _first_existing_path(*paths: Path) -> Path | None:
    return next((path for path in paths if path.exists()), None)


def _iter_cron_run_files(root: Path):
    seen = set()
    for pattern in ("*.jsonl", "*.jsonl.migrated"):
        for path in sorted(root.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            yield path


def _extract_skill_description(path: Path) -> str:
    """Extract description from SKILL.md YAML frontmatter or first non-frontmatter line."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        # Try YAML frontmatter description field
        m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if m:
            for line in m.group(1).split("\n"):
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
        # Fallback: first non-empty, non-heading line after frontmatter
        lines = content.split("\n")
        in_fm = False
        for line in lines:
            if line.strip() == "---":
                in_fm = not in_fm; continue
            if not in_fm and line.strip() and not line.startswith("#"):
                return line.strip().lstrip("# ").strip()[:200]
    except: pass
    return ""


SKILL_LEVEL_LABELS = {
    "global": "全局",
    "workspace": "Workspace/Project",
    "project": "Workspace/Project",
    "agent": "Agent",
    "system": "系统/只读",
}

SKILL_SOURCE_KIND_LABELS = {
    "global": "全局目录",
    "workspace": "工作区目录",
    "project": "项目目录",
    "agent": "Agent 目录",
    "profile": "Profile 目录",
    "command": "命令",
    "system": "系统",
    "bundled": "内置",
    "optional": "可选",
    "plugin": "插件",
    "marketplace": "外部库",
}


AI_ASSETS_UI = {
    "zh": {
        "storageRagIndex": "RAG 索引",
        "storageDiary": "正式日记",
        "storageIntermediate": "归档 / 清洗中间产物",
        "storageArchive": "历史归档",
        "serverStopped": "未运行",
        "serverRunning": "运行中",
        "commandPrefix": "命令",
        "pluginPrefix": "插件",
        "memoryPrefix": "记忆",
        "claudeCustomCommand": "Claude Code 自定义命令",
        "fileSaved": "File saved",
        "skillLevelLabels": SKILL_LEVEL_LABELS,
        "skillSourceKindLabels": SKILL_SOURCE_KIND_LABELS,
    },
    "en": {
        "storageRagIndex": "RAG Index",
        "storageDiary": "Diary Documents",
        "storageIntermediate": "Archive / Processing Intermediates",
        "storageArchive": "Historical Archive",
        "serverStopped": "Stopped",
        "serverRunning": "Running",
        "commandPrefix": "Command",
        "pluginPrefix": "Plugin",
        "memoryPrefix": "Memory",
        "claudeCustomCommand": "Claude Code custom command",
        "fileSaved": "File saved",
        "skillLevelLabels": {
            "global": "Global",
            "workspace": "Workspace/Project",
            "project": "Workspace/Project",
            "agent": "Agent",
            "system": "System/Read-only",
        },
        "skillSourceKindLabels": {
            "global": "Global Directory",
            "workspace": "Workspace Directory",
            "project": "Project Directory",
            "agent": "Agent Directory",
            "profile": "Profile Directory",
            "command": "Command",
            "system": "System",
            "bundled": "Bundled",
            "optional": "Optional",
            "plugin": "Plugin",
            "marketplace": "External Repository",
        },
    },
}


def _ai_assets_english() -> bool:
    return is_english_profile(dashboard_language_profile())


def _ai_assets_ui(key: str) -> str:
    table = AI_ASSETS_UI["en" if _ai_assets_english() else "zh"]
    return str(table[key])


def _skill_level_label(level: str) -> str:
    if _ai_assets_english():
        return AI_ASSETS_UI["en"]["skillLevelLabels"].get(level, level)
    return SKILL_LEVEL_LABELS.get(level, level)


def _skill_source_kind_label(source_kind: str) -> str:
    if _ai_assets_english():
        return AI_ASSETS_UI["en"]["skillSourceKindLabels"].get(source_kind, source_kind)
    return SKILL_SOURCE_KIND_LABELS.get(source_kind, source_kind)


def _skill_search_text(path: Path, desc: str = "") -> str:
    """Return searchable text for a skill without exposing it in the card UI."""
    try:
        fp = path
        if fp.is_dir():
            for name in ("SKILL.md", "DESCRIPTION.md"):
                candidate = fp / name
                if candidate.exists():
                    fp = candidate
                    break
        if fp.exists() and fp.is_file() and fp.suffix.lower() in {".md", ".txt"}:
            return (desc + "\n" + fp.read_text(encoding="utf-8", errors="ignore"))[:50000]
    except Exception:
        pass
    return desc


def _skill_record(
    path: Path,
    source: str,
    type_: str,
    level: str,
    *,
    name: str | None = None,
    id_: str | None = None,
    description_default: str = "Skill",
    category: str = "",
    source_kind: str = "",
    profile: str = "",
) -> dict:
    desc = _extract_skill_description(path)
    skill_id = id_ or path.parent.name
    source_kind = source_kind or level
    return {
        "name": name or skill_id.replace("-", " ").replace("_", " "),
        "id": skill_id,
        "description": desc or description_default,
        "searchText": _skill_search_text(path, desc or description_default),
        "path": str(path),
        "source": source,
        "type": type_,
        "level": level,
        "levelLabel": _skill_level_label(level),
        "sourceKind": source_kind,
        "sourceKindLabel": _skill_source_kind_label(source_kind),
        "category": category,
        "profile": profile,
        "lastModified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"),
    }


def _collect_skill_md_records(
    root: Path,
    source: str,
    level: str,
    *,
    type_: str = "skill",
    description_default: str = "Skill",
    max_count: int = 400,
    source_kind: str = "",
    profile: str = "",
) -> list[dict]:
    """Collect SKILL.md/DESCRIPTION.md files under a known skill root."""
    result: list[dict] = []
    if not root.exists(): return result
    seen_dirs: set[Path] = set()
    candidates = sorted(root.rglob("SKILL.md")) + sorted(root.rglob("DESCRIPTION.md"))
    for fp in candidates:
        if len(result) >= max_count: break
        if fp.parent in seen_dirs: continue
        try:
            if any(part.startswith(".") for part in fp.relative_to(root).parts):
                continue
        except Exception:
            continue
        if any(part in {"node_modules", "tests", "docs", "website"} for part in fp.parts):
            continue
        seen_dirs.add(fp.parent)
        try:
            rel_parent = fp.parent.relative_to(root)
            category = str(rel_parent.parent) if rel_parent.parent != Path(".") else ""
            result.append(_skill_record(
                fp, source, type_, level,
                description_default=description_default,
                category=category,
                source_kind=source_kind,
                profile=profile,
            ))
        except Exception:
            continue
    return result


def _hermes_profile_roots() -> list[tuple[str, Path]]:
    """Return configured Hermes profile homes."""
    roots: list[tuple[str, Path]] = [("default", _external_tool_path("hermes", "home"))]
    profiles_dir = _external_tool_path("hermes", "profilesRoot")
    if profiles_dir.exists():
        for p in sorted(profiles_dir.iterdir()):
            if not p.is_dir() or p.name.startswith("."): continue
            if (p / "config.yaml").exists() or (p / "skills").exists():
                roots.append((p.name, p))
    return roots


def _openclaw_agent_skill_roots() -> list[tuple[str, Path, str]]:
    """Return OpenClaw agent/workspace skill roots as (agent, skills_dir, source_kind)."""
    roots: list[tuple[str, Path, str]] = []
    seen: set[Path] = set()

    def add(agent: str, skills_dir: Path, source_kind: str = "workspace") -> None:
        try:
            resolved = skills_dir.resolve()
        except Exception:
            resolved = skills_dir
        if resolved in seen or not skills_dir.exists():
            return
        seen.add(resolved)
        roots.append((agent, skills_dir, source_kind))

    openclaw_home = _external_tool_path("openclaw", "home")
    agents_root = _external_tool_path("openclaw", "agentsRoot")
    add("main", _external_tool_path("openclaw", "skillsRoot"), "workspace")
    projects_dir = _external_tool_path("openclaw", "projectsRoot")
    if projects_dir.exists():
        for project_skills in sorted(projects_dir.glob("*/skills")):
            add(f"main/{project_skills.parent.name}", project_skills, "project")

    if agents_root.exists():
        for agent_dir in sorted(agents_root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."): continue
            agent_name = agent_dir.name
            candidates = [
                openclaw_home / f"workspace-{agent_name}" / "skills",
                agent_dir / "workspace" / "skills",
                agent_dir / "skills",
            ]
            for skills_dir in candidates:
                add(agent_name, skills_dir, "workspace")

    return roots


def _get_skills_stats() -> dict:
    result = {"byTool": {}, "total": 0}
    try:
        # ── OpenClaw ──
        from . import skills as skills_service
        data = skills_service.get_all_skills()
        oc_skills = []
        for s in data.get("system", []):
            spath = Path(str(s.get("path", ""))) if s.get("path") else _external_tool_path("openclaw", "systemSkillsRoot") / str(s.get("id", ""))
            oc_skills.append({**s, "source": "OpenClaw", "type": "skill", "level": "global", "levelLabel": _skill_level_label("global"), "sourceKind": "global", "sourceKindLabel": _skill_source_kind_label("global"), "searchText": _skill_search_text(spath, s.get("description", ""))})
        for agent_name, skills_dir, source_kind in _openclaw_agent_skill_roots():
            oc_skills.extend(_collect_skill_md_records(
                skills_dir, "OpenClaw", "agent",
                description_default=f"OpenClaw {agent_name} agent skill",
                source_kind=source_kind,
                profile=agent_name,
            ))
        oc_skills.extend(_collect_skill_md_records(
            _external_tool_path("openclaw", "home") / "MiniMax-skills" / "skills", "OpenClaw", "system",
            description_default="OpenClaw marketplace skill",
            source_kind="marketplace",
        ))
        result["byTool"]["OpenClaw"] = oc_skills

        # ── Claude Code ──
        cc_skills = []
        cc_skills.extend(_collect_skill_md_records(
            _external_tool_path("claudeCode", "skillsRoot"), "Claude Code", "global",
            description_default="Claude Code skill",
        ))
        cc_skills.extend(_collect_skill_md_records(
            _workspace_dir() / ".claude" / "skills", "Claude Code", "workspace",
            description_default="Claude Code project skill",
            source_kind="project",
        ))
        cc_skills.extend(_collect_skill_md_records(
            _external_tool_path("claudeCode", "pluginsRoot") / "marketplaces" / "minimax-skills" / "skills",
            "Claude Code", "system",
            description_default="Claude Code marketplace skill",
            source_kind="marketplace",
        ))
        cc_dir = _external_tool_path("claudeCode", "commandsRoot")
        if cc_dir.exists():
            for f in cc_dir.glob("*.md"):
                name = f.stem.replace("-", " ").replace("_", " ")
                cc_skills.append(_skill_record(
                    f, "Claude Code", "command", "global",
                    name=name, id_=f.stem, description_default=_ai_assets_ui("claudeCustomCommand"),
                    source_kind="command",
                ))
        result["byTool"]["Claude Code"] = cc_skills

        # ── Hermes ──
        hermes_skills = []
        hermes_profile_skill_ids: set[str] = set()
        for profile_name, profile_root in _hermes_profile_roots():
            profile_skills = _collect_skill_md_records(
                profile_root / "skills", "Hermes", "agent",
                description_default=f"Hermes {profile_name} profile skill",
                source_kind="profile",
                profile=profile_name,
            )
            hermes_profile_skill_ids.update(str(s.get("id", "")) for s in profile_skills)
            hermes_skills.extend(profile_skills)
        bundled_skills = _collect_skill_md_records(
            _external_tool_path("hermes", "skillsRoot"), "Hermes", "system",
            description_default="Hermes bundled skill",
            source_kind="bundled",
        )
        hermes_skills.extend([s for s in bundled_skills if str(s.get("id", "")) not in hermes_profile_skill_ids])
        hermes_skills.extend(_collect_skill_md_records(
            _external_tool_path("hermes", "optionalSkillsRoot"), "Hermes", "system",
            description_default="Hermes optional skill",
            source_kind="optional",
        ))
        plugins_dir = _external_tool_path("hermes", "pluginsRoot")
        if plugins_dir.exists():
            for fp in sorted(plugins_dir.glob("*/SKILL.md")):
                hermes_skills.append(_skill_record(
                    fp, "Hermes", "skill", "system",
                    description_default="Hermes plugin skill",
                    category=fp.parent.name,
                    source_kind="plugin",
                ))
        result["byTool"]["Hermes"] = hermes_skills

        # ── Codex ── (.system/skill-name/SKILL.md)
        codex_skills = []
        codex_skills_root = _external_tool_path("codex", "skillsRoot")
        codex_sys = codex_skills_root / ".system"
        codex_skills.extend(_collect_skill_md_records(
            codex_sys, "Codex", "system", type_="system",
            description_default="Codex system skill",
            source_kind="system",
        ))
        codex_skills.extend(_collect_skill_md_records(
            codex_skills_root, "Codex", "global",
            description_default="Codex global skill",
        ))
        codex_skills.extend(_collect_skill_md_records(
            _workspace_dir() / ".codex" / "skills", "Codex", "workspace",
            description_default="Codex project skill",
            source_kind="project",
        ))
        result["byTool"]["Codex"] = codex_skills

        # ── Gemini CLI ──
        gemini_skills = []
        gemini_skills.extend(_collect_skill_md_records(
            _external_tool_path("geminiCli", "skillsRoot"), "Gemini CLI", "global",
            description_default="Gemini skill",
        ))
        gemini_skills.extend(_collect_skill_md_records(
            _workspace_dir() / ".gemini" / "skills", "Gemini CLI", "workspace",
            description_default="Gemini project skill",
            source_kind="project",
        ))
        result["byTool"]["Gemini CLI"] = gemini_skills

        result["total"] = sum(len(v) for v in result["byTool"].values())
    except Exception:
        pass
    return result


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size / 1024 / 1024
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_symlink() or not item.is_file():
                    continue
                total += item.stat().st_size
            except OSError:
                continue
        return total / 1024 / 1024
    except OSError:
        pass
    return 0


def _sum_size_mb(paths: list[Path]) -> float:
    return sum(_dir_size_mb(path) for path in paths)


def _rag_indexes_path_for_storage(rag_index: Path) -> Path:
    try:
        indexes_path = resolve_rag_settings().v2_store_path / "indexes"
        if indexes_path.exists():
            return indexes_path
    except Exception:
        pass
    for parent in rag_index.parents:
        if parent.name == "indexes":
            return parent
    return rag_index.parent


def _rag_storage_category() -> dict:
    rag_index = _rag_index_path()
    if not rag_index:
        return {"label": _ai_assets_ui("storageRagIndex"), "sizeMB": 0, "source": "v2", "paths": []}
    indexes_path = _rag_indexes_path_for_storage(rag_index)
    return {
        "label": _ai_assets_ui("storageRagIndex"),
        "sizeMB": round(_dir_size_mb(indexes_path), 1),
        "source": "v2-indexes",
        "paths": [str(indexes_path)],
    }


def _get_detailed_storage(*, include_rag: bool = True) -> dict:
    tool_dirs = [
        ("OpenClaw", TOOL_EMOJI["OpenClaw"], _external_tool_path("openclaw", "home")),
        ("Claude Code", TOOL_EMOJI["Claude Code"], _external_tool_path("claudeCode", "home")),
        ("Gemini CLI", TOOL_EMOJI["Gemini CLI"], _external_tool_path("geminiCli", "home")),
        ("Codex", TOOL_EMOJI["Codex"], _external_tool_path("codex", "home")),
        ("Hermes", TOOL_EMOJI["Hermes"], _external_tool_path("hermes", "home")),
        ("OpenCode", TOOL_EMOJI["OpenCode"], _external_tool_path("opencode", "home")),
        ("Cursor", TOOL_EMOJI["Cursor"], _external_tool_path("cursor", "home")),
    ]
    tools = [{"name": n, "emoji": e, "sizeMB": round(_dir_size_mb(p), 1)} for n, e, p in tool_dirs]
    tools.append(
        {
            "name": "Antigravity",
            "emoji": TOOL_EMOJI["Antigravity"],
            "sizeMB": round(
                _sum_size_mb(
                    [
                        _external_tool_path("antigravity", "cliHome"),
                        _external_tool_path("antigravity", "ideHome"),
                        _external_tool_path("antigravity", "appHome"),
                    ]
                ),
                1,
            ),
        }
    )
    paths = None
    try:
        paths = load_paths()
    except Exception:
        paths = None
    diary_dir = paths.diary_dir if paths is not None else _diary_dir()
    intermediate_paths = [diary_dir / "__diary_daily"]
    if paths is not None and paths.archives_dir not in intermediate_paths:
        intermediate_paths.append(paths.archives_dir)
    categories = [
        {
            "label": _ai_assets_ui("storageDiary"),
            "sizeMB": round(_sum_size_mb(iter_diary_markdown_files(diary_dir)), 1),
            "source": "runtime-diary-markdown",
            "paths": [str(diary_dir)],
        },
        {
            "label": _ai_assets_ui("storageIntermediate"),
            "sizeMB": round(_sum_size_mb(intermediate_paths), 1),
            "source": "runtime-intermediate",
            "paths": [str(path) for path in intermediate_paths],
        },
        {
            "label": _ai_assets_ui("storageArchive"),
            "sizeMB": round(_dir_size_mb(diary_dir / "_archive"), 1),
            "source": "runtime-diary-archive",
            "paths": [str(diary_dir / "_archive")],
        },
    ]
    if include_rag:
        categories.append(_rag_storage_category())
    return {"tools": tools, "categories": categories}


def _get_rag_stats() -> dict:
    rag_index = _rag_index_path()
    index_exists = bool(rag_index and rag_index.exists())
    entries = _count_lines(rag_index) if index_exists else 0
    size_mb = round(_dir_size_mb(rag_index), 1) if index_exists else 0
    modified_at = ""
    if index_exists:
        try:
            modified_at = datetime.fromtimestamp(rag_index.stat().st_mtime, tz=_local_tz()).strftime("%Y-%m-%d %H:%M")
        except OSError:
            pass
    server_status = _ai_assets_ui("serverStopped")
    try:
        with urllib.request.urlopen(_embedding_health_url(), timeout=0.3) as response:
            if 200 <= response.status < 300:
                server_status = _ai_assets_ui("serverRunning")
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    embedding_running = server_status == _ai_assets_ui("serverRunning")
    if index_exists and embedding_running:
        health = "ready"
    elif index_exists:
        health = "index-only"
    elif embedding_running:
        health = "server-only"
    else:
        health = "missing"
    return {
        "indexFiles": 1 if index_exists else 0,
        "entries": entries,
        "sizeMB": size_mb,
        "updatedAt": modified_at,
        "embeddingStatus": server_status,
        "health": health,
        "indexReady": index_exists,
        "embeddingRunning": embedding_running,
        "source": "v2",
    }


_TIME_SLOTS = ["凌晨", "上午", "下午", "晚上"]

def _time_slot(hour: int) -> str:
    if 0 <= hour < 4: return "凌晨"
    if 4 <= hour < 12: return "上午"
    if 12 <= hour < 18: return "下午"
    return "晚上"

def _get_30day_trend(all_entries: dict) -> list[dict]:
    today = _now_local().date()
    sorted_days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    grid = {d: {s: 0 for s in _TIME_SLOTS} for d in sorted_days}
    for entries in all_entries.values():
        for e in entries:
            ts = e.get("timestamp", "")
            if not ts: continue
            try:
                dt = _utc_to_local(ts)
                slot = _time_slot(dt.hour)
                # 凌晨(00-03)归入前一天，遵循 04:00~03:59 统计周期
                if dt.hour < 4:
                    ds = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    ds = dt.strftime("%Y-%m-%d")
                if ds in grid:
                    t = _entry_total_tokens(e)
                    grid[ds][slot] += t
            except: pass
    return [{"date": d, "slots": {s: grid[d][s] for s in _TIME_SLOTS}} for d in sorted_days]


def _aggregate_by_model(all_entries: dict) -> list[dict]:
    model_map = defaultdict(lambda: {"tokens": 0, "messages": 0, "sessions": 0})
    for name, entries in all_entries.items():
        session_models = defaultdict(set)
        for e in entries:
            m = e.get("model", "") or "unknown"
            t = _entry_total_tokens(e)
            mc = e.get("message_count", 1) or 1
            model_map[m]["tokens"] += t
            model_map[m]["messages"] += mc
            ts = e.get("timestamp", "")
            if ts:
                try:
                    dt = _utc_to_local(ts)
                    key = dt.strftime("%Y-%m-%d") + "|" + name
                    session_models[m].add(key)
                except: pass
        for model, session_keys in session_models.items():
            model_map[model]["sessions"] += len(session_keys)
    result = [{"name": m, **d} for m, d in model_map.items()]
    result.sort(key=lambda x: x["tokens"], reverse=True)
    return result


def _aggregate_by_workspace(all_entries: dict) -> list[dict]:
    usage = defaultdict(lambda: {"tokens": 0, "messages": 0})
    emojis = {item["name"]: item["emoji"] for item in TOOL_DEFS}
    for tool_name, entries in all_entries.items():
        for entry in entries:
            group = canonical_workspace_name(entry.get("usageGroup") or tool_name)
            key = (tool_name, group)
            usage[key]["tokens"] += _entry_total_tokens(entry)
            usage[key]["messages"] += entry.get("message_count", 1) or 1
    result = [
        {
            "name": group,
            "tool": tool_name,
            "emoji": emojis.get(tool_name, ""),
            **metrics,
        }
        for (tool_name, group), metrics in usage.items()
        if metrics["tokens"] >= WORKSPACE_USAGE_MIN_TOKENS
        and _ai_assets_workspace_usage_visible(group, tool_name)
    ]
    result.sort(key=lambda row: row["tokens"], reverse=True)
    return result


def _entry_total_tokens(entry: dict) -> int:
    explicit = entry.get("protocolTotal")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError, OverflowError):
            pass
    return int((entry.get("input") or 0) + (entry.get("output") or 0) + (entry.get("cacheRead") or 0))


def _workspace_attribution_qa(all_entries: dict, workspace_rows: list[dict] | None = None) -> dict:
    raw_groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "tokens": 0,
            "messages": 0,
            "canonicalName": "",
            "sources": set(),
            "confidences": set(),
        }
    )
    canonical_groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"tokens": 0, "rawNames": set(), "sources": set(), "confidences": set()}
    )
    total_tokens = 0
    for tool_name, entries in all_entries.items():
        for entry in entries:
            tokens = _entry_total_tokens(entry)
            if tokens <= 0:
                continue
            raw_group = str(entry.get("usageGroup") or tool_name).strip() or tool_name
            canonical = canonical_workspace_name(raw_group)
            source = str(entry.get("usageGroupSource") or "unknown").strip() or "unknown"
            confidence = str(entry.get("usageGroupConfidence") or "").strip()
            messages = int(entry.get("message_count", 1) or 1)
            total_tokens += tokens

            raw_key = (tool_name, raw_group)
            raw_groups[raw_key]["tokens"] += tokens
            raw_groups[raw_key]["messages"] += messages
            raw_groups[raw_key]["canonicalName"] = canonical
            raw_groups[raw_key]["sources"].add(source)
            if confidence:
                raw_groups[raw_key]["confidences"].add(confidence)

            canonical_key = (tool_name, canonical)
            canonical_groups[canonical_key]["tokens"] += tokens
            canonical_groups[canonical_key]["rawNames"].add(raw_group)
            canonical_groups[canonical_key]["sources"].add(source)
            if confidence:
                canonical_groups[canonical_key]["confidences"].add(confidence)

    visible_keys = {
        (str(row.get("tool") or ""), str(row.get("name") or ""))
        for row in (workspace_rows or _aggregate_by_workspace(all_entries))
    }
    findings = []
    hidden_tokens = 0
    low_confidence_tokens = 0
    alias_tokens = 0
    transcript_tokens = 0
    codex_transcript_tokens = 0
    transcript_tokens_by_source: dict[str, int] = defaultdict(int)
    for (tool_name, raw_name), metrics in raw_groups.items():
        canonical = metrics["canonicalName"]
        visible = (tool_name, canonical) in visible_keys
        sources = sorted(metrics["sources"])
        confidences = sorted(metrics["confidences"])
        tokens = metrics["tokens"]
        if not visible:
            hidden_tokens += tokens
            if tokens >= WORKSPACE_USAGE_MIN_TOKENS:
                findings.append({
                    "id": "hidden-workspace-usage",
                    "severity": "warning",
                    "tool": tool_name,
                    "workspace": canonical,
                    "rawWorkspace": raw_name,
                    "tokens": tokens,
                    "message": f"{tool_name} 的 {canonical} 有 {tokens:,} tokens，但被展示策略隐藏。",
                    "suggestion": "确认它是否是 home/SSD/unknown 等容器目录；若是实际项目，应补充路径归属证据或项目 catalog。",
                })
        if raw_name != canonical:
            alias_tokens += tokens
            findings.append({
                "id": "canonical-alias-merged",
                "severity": "info",
                "tool": tool_name,
                "workspace": canonical,
                "rawWorkspace": raw_name,
                "tokens": tokens,
                "message": f"{raw_name} 已规范合并为 {canonical}。",
                "suggestion": "无需修复；该项用于防止历史项目名拆分排行。",
            })
        for source in sources:
            if source.endswith("transcript-path"):
                transcript_tokens += tokens
                transcript_tokens_by_source[source] += tokens
        if "codex-transcript-path" in sources:
            codex_transcript_tokens += tokens
        if confidences and "high" not in confidences:
            low_confidence_tokens += tokens
            if tokens >= WORKSPACE_USAGE_MIN_TOKENS:
                findings.append({
                    "id": "low-confidence-attribution",
                    "severity": "warning",
                    "tool": tool_name,
                    "workspace": canonical,
                    "rawWorkspace": raw_name,
                    "tokens": tokens,
                    "message": f"{tool_name} 的 {canonical} 使用低置信或 fallback 归属。",
                    "suggestion": "检查 session cwd、工具项目路径或 transcript 中是否包含明确项目路径。",
                })

    for (tool_name, canonical), metrics in canonical_groups.items():
        raw_names = sorted(metrics["rawNames"])
        if len(raw_names) > 1:
            findings.append({
                "id": "split-workspace-names",
                "severity": "info",
                "tool": tool_name,
                "workspace": canonical,
                "rawWorkspaces": raw_names,
                "tokens": metrics["tokens"],
                "message": f"{canonical} 同时出现多个原始名称：{', '.join(raw_names)}。",
                "suggestion": "已按规范名称聚合；若仍有异常名称，补充 alias 或项目 catalog。",
            })

    severity_rank = {"info": 0, "warning": 1, "blocked": 2}
    findings.sort(key=lambda item: (-severity_rank.get(item["severity"], 0), -int(item.get("tokens") or 0), item["id"]))
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    blocked_count = sum(1 for item in findings if item["severity"] == "blocked")
    visible_tokens = sum(int(row.get("tokens") or 0) for row in (workspace_rows or []))
    if not workspace_rows:
        visible_tokens = total_tokens - hidden_tokens
    return {
        "schemaVersion": 1,
        "status": "blocked" if blocked_count else ("attention" if warning_count else "ready"),
        "totalTokens": total_tokens,
        "visibleTokens": visible_tokens,
        "hiddenTokens": hidden_tokens,
        "hiddenRate": round(hidden_tokens / total_tokens, 4) if total_tokens else 0,
        "lowConfidenceTokens": low_confidence_tokens,
        "aliasMergedTokens": alias_tokens,
        "transcriptInferredTokens": transcript_tokens,
        "transcriptInferredTokensBySource": dict(sorted(transcript_tokens_by_source.items())),
        "codexTranscriptInferredTokens": codex_transcript_tokens,
        "findingCount": len(findings),
        "warningCount": warning_count,
        "blockedCount": blocked_count,
        "findings": findings[:20],
    }


def _ai_assets_workspace_usage_visible(group: str, tool_name: str = "") -> bool:
    return usage_group_display_allowed(group, tool_name)


def _file_entry(name: str, path: Path, kind: str = "context", exists: bool | None = None, group: str | None = None) -> dict:
    exists = path.exists() and path.is_file() if exists is None else exists
    if group is None:
        group = "context" if kind in {"context", "memory"} else ("config" if kind == "config" else "tools")
    entry = {
        "name": name,
        "path": str(path),
        "size": path.stat().st_size if exists else 0,
        "kind": kind,
        "group": group,
        "exists": exists,
        "createable": not exists and kind in {"context", "config"},
    }
    if kind == "skill":
        entry["isSkill"] = True
    return entry


def _collect_key_files(paths: list[tuple], include_missing: bool = False) -> list[dict]:
    """Collect existing files from a list of (name, path) pairs."""
    result = []
    for item in paths:
        name, path = item[0], item[1]
        kind = item[2] if len(item) > 2 else "context"
        group = item[3] if len(item) > 3 else None
        if path.exists() and path.is_file():
            result.append(_file_entry(name, path, kind, group=group))
        elif include_missing:
            result.append(_file_entry(name, path, kind, exists=False, group=group))
    return result


def _collect_skill_files(skills_dir: Path, max_count: int = 30) -> list[dict]:
    """Collect skill directories/files from a skills directory (1 or 2 levels deep)."""
    result = []
    if not skills_dir.exists(): return result
    for d in sorted(skills_dir.iterdir()):
        if d.name.startswith('_'): continue
        if len(result) >= max_count: break
        if d.is_dir():
            # Try SKILL.md / DESCRIPTION.md directly
            found = False
            for fname in ("SKILL.md", "DESCRIPTION.md"):
                fp = d / fname
                if fp.exists():
                    result.append(_file_entry(d.name, fp, "skill"))
                    found = True; break
            # If not found, recurse one level (e.g. .system/skill-name/SKILL.md)
            if not found:
                for sub in sorted(d.iterdir()):
                    if not sub.is_dir(): continue
                    if len(result) >= max_count: break
                    for fname in ("SKILL.md", "DESCRIPTION.md"):
                        fp = sub / fname
                        if fp.exists():
                            result.append(_file_entry(sub.name, fp, "skill"))
                            break
        elif d.is_file() and d.suffix == '.md':
            result.append(_file_entry(d.stem, d, "skill"))
    return result


def _decode_claude_project_path(project_dir: Path) -> Path | None:
    """Claude stores project session dirs as absolute paths with / replaced by -."""
    name = project_dir.name
    if not name.startswith("-"):
        return None
    parts = [p for p in name.lstrip("-").split("-") if p]

    def walk(base: Path, idx: int) -> Path | None:
        if idx >= len(parts):
            return base if base.exists() else None
        for end in range(len(parts), idx, -1):
            chunk = parts[idx:end]
            for dirname in ("-".join(chunk), " ".join(chunk)):
                candidate = base / dirname
                if candidate.exists() and candidate.is_dir():
                    found = walk(candidate, end)
                    if found:
                        return found
        return None

    return walk(Path("/"), 0)


def _workspace_display_name(path: Path) -> str:
    try:
        if path == _workspace_dir():
            return path.name
    except Exception:
        pass
    return path.name or str(path)


def _get_agent_tree(tools_stats: list[dict], all_entries: dict[str, list[dict]] | None = None) -> list[dict]:
    tree = []
    latest_models = _latest_models_from_entries(all_entries or {})
    # OpenClaw agents
    oc_item = {"name": "OpenClaw", "emoji": TOOL_EMOJI["OpenClaw"], "count": 0, "countLabel": "agents", "items": []}
    agents_root = _external_tool_path("openclaw", "agentsRoot")
    openclaw_home = _external_tool_path("openclaw", "home")
    if agents_root.exists():
        from .token_clock import _collect_openclaw_session_files
        all_sessions = _collect_openclaw_session_files(agents_root)
        cfg = _safe_read_json(_external_tool_path("openclaw", "configPath")) or {}
        acfg = cfg.get("agents", {}) if isinstance(cfg, dict) else {}
        defaults_model = acfg.get("defaults", {}).get("model", {}).get("primary", "")
        agent_list = acfg.get("list", [])

        for adir in sorted(agents_root.iterdir()):
            if not adir.is_dir(): continue
            aid = adir.name
            ac = next((a for a in agent_list if isinstance(a, dict) and a.get("id") == aid), {})
            iden = ac.get("identity", {}) if isinstance(ac.get("identity"), dict) else {}
            wpath = Path(ac.get("workspace", "")) if ac.get("workspace") else (openclaw_home / f"workspace-{aid}")
            sdir = adir / "sessions"
            scnt = 0; msgs = 0; last_a = ""; latest_mt = 0
            if sdir.exists():
                a_sessions = {sid: f for sid, f in all_sessions.items() if str(f).startswith(str(sdir) + '/')}
                scnt = len(a_sessions)
                for sid, f in list(a_sessions.items())[:50]:
                    mt = f.stat().st_mtime
                    latest_mt = max(latest_mt, mt)
                    try:
                        with open(f, "r", encoding="utf-8", errors="ignore") as f_in:
                            for l in f_in:
                                if '"role":"assistant"' in l: msgs += 1
                    except: pass
            if latest_mt: last_a = datetime.fromtimestamp(latest_mt, tz=_local_tz()).strftime("%Y-%m-%d %H:%M")
            display_name = iden.get("name") or aid
            key_files = []
            for fn in ["SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "USER.md"]:
                fp = wpath / fn
                if fp.exists():
                    key_files.append({"name": fn, "path": str(fp), "size": fp.stat().st_size})
            oc_item["items"].append({
                "name": aid, "displayName": display_name,
                "model": _model_name(ac.get("model")) or latest_models.get("OpenClaw", {}).get(aid, "") or _model_name(defaults_model) or "unknown",
                "sessions": scnt, "messages": msgs, "lastActive": last_a or "unknown",
                "workspace": str(wpath), "keyFiles": key_files, "level": "agent",
            })
    oc_item["count"] = len(oc_item["items"])
    tree.append(oc_item)

    # ── Helper: build global + workspace items for non-OpenClaw tools ──

    def _claude_code_items(ts):
        claude_home = _external_tool_path("claudeCode", "home")
        claude_projects = _external_tool_path("claudeCode", "projectsRoot")

        def _claude_workspace_group(path_str: str) -> str:
            p = Path(path_str)
            if p == _workspace_dir():
                return "current"
            if p in {claude_home, _workspace_dir().parent, _workspace_dir().parent.parent}:
                return "general"
            if "/Library/Application Support/" in path_str or "/Documents/Claude/" in path_str:
                return "external"
            return "project"

        items = []
        # Global level
        global_files = _collect_key_files([
            ("settings.json", claude_home / "settings.json", "config"),
            ("settings.local.json", claude_home / "settings.local.json", "config"),
            (".claude.json", claude_home.parent / ".claude.json", "config"),
            ("CLAUDE.md", claude_home / "CLAUDE.md", "context"),
        ], include_missing=True)
        # Claude slash commands are reusable prompts, distinct from skills.
        cc_cmd_dir = _external_tool_path("claudeCode", "commandsRoot")
        if cc_cmd_dir.exists():
            for f in sorted(cc_cmd_dir.glob("*.md")):
                global_files.append(_file_entry(_ai_assets_ui("commandPrefix") + "/" + f.stem, f, "command", group="tools"))
        plugin_dir = _external_tool_path("claudeCode", "pluginsRoot")
        if plugin_dir.exists():
            for harness in sorted(plugin_dir.glob("*/HARNESS.md"))[:20]:
                global_files.append(_file_entry(_ai_assets_ui("pluginPrefix") + "/" + harness.parent.name + "/HARNESS.md", harness, "reference", group="tools"))
        items.append({
            "name": "__global__", "displayName": "Global", "level": "global",
            "model": "", "sessions": 0, "messages": 0, "lastActive": "",
            "workspace": str(claude_home), "keyFiles": global_files,
        })
        # Workspace level — real project dirs, with sessions attached when present.
        base = claude_projects
        project_sessions = defaultdict(list)
        if base.exists():
            for f in base.rglob("*.jsonl"):
                if "/subagents/" in str(f): continue
                try:
                    if f.stat().st_size < 5120: continue
                except: continue
                real_dir = _decode_claude_project_path(f.parent) or f.parent
                project_sessions[str(real_dir)].append(f)
        candidate_paths = {str(_workspace_dir())}
        candidate_paths.update(project_sessions.keys())
        for proj_path_str in sorted(candidate_paths):
            proj_dir = Path(proj_path_str)
            if not proj_dir.exists(): continue
            sfiles = project_sessions.get(proj_path_str, [])
            ws_files = _collect_key_files([
                ("CLAUDE.md", proj_dir / "CLAUDE.md", "context"),
                (".claude/CLAUDE.md", proj_dir / ".claude" / "CLAUDE.md", "context"),
                ("CLAUDE.local.md", proj_dir / "CLAUDE.local.md", "context"),
                (".claude/settings.json", proj_dir / ".claude" / "settings.json", "config"),
                (".claude/settings.local.json", proj_dir / ".claude" / "settings.local.json", "config"),
                (".mcp.json", proj_dir / ".mcp.json", "config"),
            ], include_missing=(proj_dir == _workspace_dir()))
            if not sfiles and not ws_files and proj_dir != _workspace_dir():
                continue
            proj_name = _workspace_display_name(proj_dir)
            msgs = 0; model = ""; latest_mt = 0
            for f in sfiles[:20]:
                try:
                    mt = f.stat().st_mtime
                    if mt > latest_mt: latest_mt = mt
                    with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                        for line in fin:
                            try:
                                obj = json.loads(line)
                                if obj.get("type") == "assistant":
                                    msgs += 1
                                    if not model: model = obj.get("message", {}).get("model", "")
                            except: pass
                except: pass
            last_active = datetime.fromtimestamp(latest_mt, tz=_local_tz()).strftime("%Y-%m-%d %H:%M") if latest_mt else ""
            usage_group = _workspace_group_from_path(proj_dir)
            items.append({
                "name": proj_name, "displayName": proj_name, "level": "workspace",
                "model": model or latest_models.get("Claude Code", {}).get(usage_group, "") or latest_models.get("Claude Code", {}).get("__tool__", ""),
                "sessions": len(sfiles), "messages": msgs,
                "lastActive": last_active, "workspace": proj_path_str,
                "workspaceGroup": _claude_workspace_group(proj_path_str),
                "keyFiles": ws_files,
            })
        return items

    def _gemini_cli_items(ts):
        gemini_home = _external_tool_path("geminiCli", "home")
        items = []
        global_files = _collect_key_files([
            ("GEMINI.md", gemini_home / "GEMINI.md", "context"),
            ("settings.json", gemini_home / "settings.json", "config"),
        ], include_missing=True)
        items.append({
            "name": "__global__", "displayName": "Global", "level": "global",
            "model": "", "sessions": 0, "messages": 0, "lastActive": "",
            "workspace": str(gemini_home), "keyFiles": global_files,
        })
        # Workspace level — project GEMINI.md files.
        candidate_paths = {str(_workspace_dir())}
        pj = _safe_read_json(_external_tool_path("geminiCli", "projectsPath"))
        if pj and isinstance(pj, dict):
            candidate_paths.update(str(p) for p in list(pj.keys())[:20])
        for path_str in sorted(candidate_paths):
            pp = Path(path_str)
            if not pp.exists(): continue
            ws_files = _collect_key_files([
                ("GEMINI.md", pp / "GEMINI.md", "context"),
                (".gemini/settings.json", pp / ".gemini" / "settings.json", "config"),
                (".mcp.json", pp / ".mcp.json", "config"),
            ], include_missing=(pp == _workspace_dir()))
            if ws_files:
                display_name = pj.get(path_str) if isinstance(pj, dict) else ""
                items.append({
                    "name": display_name or _workspace_display_name(pp),
                    "displayName": display_name or _workspace_display_name(pp), "level": "workspace",
                    "model": latest_models.get("Gemini CLI", {}).get(display_name or _workspace_display_name(pp), "") or latest_models.get("Gemini CLI", {}).get("__tool__", ""),
                    "sessions": 0, "messages": 0, "lastActive": "",
                    "workspace": path_str, "keyFiles": ws_files,
                })
        return items

    def _codex_items(ts):
        codex_home = _external_tool_path("codex", "home")
        codex_sessions = _external_tool_path("codex", "sessionsRoot")

        def _codex_workspace_group(path_str: str) -> str:
            p = Path(path_str)
            if p == _workspace_dir():
                return "current"
            if p == codex_home:
                return "home"
            if "CodexBar" in path_str or "/Documents/Codex/" in path_str:
                return "external"
            return "project"

        items = []
        global_files = _collect_key_files([
            ("AGENTS.override.md", codex_home / "AGENTS.override.md", "context"),
            ("AGENTS.md", codex_home / "AGENTS.md", "context"),
            ("config.toml", codex_home / "config.toml", "config"),
        ], include_missing=True)
        # Codex memories
        mem_dir = codex_home / "memories"
        if mem_dir.exists():
            for f in sorted(mem_dir.glob("*.md")):
                global_files.append(_file_entry(_ai_assets_ui("memoryPrefix") + "/" + f.name, f, "memory"))
        items.append({
            "name": "__global__", "displayName": "Global", "level": "global",
            "model": "", "sessions": 0, "messages": 0, "lastActive": "",
            "workspace": str(codex_home), "keyFiles": global_files,
        })
        # Workspace level — real cwd from rollout metadata, plus the active dashboard repo.
        base = codex_sessions
        project_sessions = defaultdict(list)
        if base.exists():
            for sf in sorted(base.rglob("rollout-*.jsonl")):
                try:
                    with open(sf, "r", encoding="utf-8", errors="ignore") as fin:
                        first = fin.readline()
                    obj = json.loads(first) if first else {}
                    cwd = obj.get("payload", {}).get("cwd") if obj.get("type") == "session_meta" else ""
                    if cwd:
                        project_sessions[cwd].append(sf)
                except: pass
        candidate_paths = {str(_workspace_dir())}
        candidate_paths.update(project_sessions.keys())
        for dir_path_str in sorted(candidate_paths):
            dp = Path(dir_path_str)
            if not dp.exists(): continue
            sfiles = project_sessions.get(dir_path_str, [])
            key_files = _collect_key_files([
                ("AGENTS.override.md", dp / "AGENTS.override.md", "context"),
                ("AGENTS.md", dp / "AGENTS.md", "context"),
                (".mcp.json", dp / ".mcp.json", "config"),
            ], include_missing=(dp == _workspace_dir()))
            if not sfiles and not key_files and dp != _workspace_dir():
                continue
            latest_mt = max((f.stat().st_mtime for f in sfiles), default=0)
            last_active = datetime.fromtimestamp(latest_mt, tz=_local_tz()).strftime("%Y-%m-%d %H:%M") if latest_mt else ""
            usage_group = _workspace_group_from_path(dp)
            items.append({
                "name": _workspace_display_name(dp), "displayName": _workspace_display_name(dp), "level": "workspace",
                "model": latest_models.get("Codex", {}).get(usage_group, "") or latest_models.get("Codex", {}).get("__tool__", ""),
                "sessions": len(sfiles), "messages": 0,
                "lastActive": last_active, "workspace": dir_path_str,
                "workspaceGroup": _codex_workspace_group(dir_path_str),
                "keyFiles": key_files,
            })
        return items

    def _hermes_items(ts):
        hermes_home = _external_tool_path("hermes", "home")
        global_files = _collect_key_files([
            ("SOUL.md", hermes_home / "SOUL.md", "context"),
            ("config.yaml", hermes_home / "config.yaml", "config"),
            ("MEMORY.md", hermes_home / "memories" / "MEMORY.md", "memory"),
        ], include_missing=True)
        # Hermes is a single-agent system. Profiles configure that agent, but
        # sessions cannot be reliably assigned to a profile, so expose one
        # stable logical agent instead of presenting sessions as workspaces.
        return [{
            "name": "hermes", "displayName": "Hermes", "level": "agent",
            "model": latest_models.get("Hermes", {}).get("__tool__", ""),
            "sessions": ts.get("sessionCount", 0),
            "messages": ts.get("allTimeMessages", 0),
            "lastActive": ts.get("lastActivity", "") or "unknown",
            "workspace": str(hermes_home), "keyFiles": global_files,
        }]

    def _normalized_runtime_items(tname, ts):
        sessions = _RUNTIME_SESSION_RECORDS.get(tname, ())
        dialogue_counts = defaultdict(int)
        for session_key, _occurred_at in _RUNTIME_DIALOGUE_ACTIVITY.get(tname, ()):
            dialogue_counts[session_key] += 1
        grouped = defaultdict(list)
        for session in sessions:
            grouped[session.initial_cwd or "__local__"].append(session)
        items = []
        for workspace, records in sorted(grouped.items(), key=lambda item: item[0]):
            activity_times = [
                record.last_active_at or record.started_at
                for record in records
                if record.last_active_at is not None or record.started_at is not None
            ]
            last_active = max(activity_times, default=None)
            models = [
                record.model_key
                for record in sorted(
                    records,
                    key=lambda record: record.last_active_at or record.started_at or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                if record.model_key
            ]
            variants = sorted({record.source_variant for record in records if record.source_variant})
            display_name = (
                _workspace_display_name(Path(workspace))
                if workspace != "__local__"
                else "Local sessions"
            )
            items.append(
                {
                    "name": display_name,
                    "displayName": display_name,
                    "level": "workspace" if workspace != "__local__" else "session",
                    "model": models[0] if models else latest_models.get(tname, {}).get("__tool__", ""),
                    "sessions": len(records),
                    "messages": sum(
                        dialogue_counts.get(record.external_session_key, 0)
                        for record in records
                    ),
                    "lastActive": (
                        last_active.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M")
                        if last_active is not None
                        else "unknown"
                    ),
                    "workspace": (
                        workspace
                        if workspace != "__local__"
                        else str(_tool_homes_by_name().get(tname, ""))
                    ),
                    "keyFiles": [],
                    "sourceVariants": variants,
                    "usageStatus": ts.get("usageStatus", "available"),
                }
            )
        return items

    # Build tree for non-OpenClaw tools
    tool_builders = {
        "Claude Code": _claude_code_items,
        "Gemini CLI": _gemini_cli_items,
        "Codex": _codex_items,
        "Hermes": _hermes_items,
        "OpenCode": lambda ts: _normalized_runtime_items("OpenCode", ts),
        "Antigravity": lambda ts: _normalized_runtime_items("Antigravity", ts),
        "Cursor": lambda ts: _normalized_runtime_items("Cursor", ts),
    }
    for tname, builder in tool_builders.items():
        ts = next((t for t in tools_stats if t["name"] == tname), {})
        emoji = next((t["emoji"] for t in TOOL_DEFS if t["name"] == tname), "")
        items = builder(ts)
        if tname in {"Claude Code", "Gemini CLI", "Codex"}:
            card_count = sum(1 for item in items if item.get("level") == "workspace")
            count_label = "workspaces"
        elif tname == "Hermes":
            card_count = len(items)
            count_label = "agents"
        elif tname in {"OpenCode", "Antigravity", "Cursor"}:
            card_count = ts.get("sessionCount", 0)
            count_label = "sessions"
        else:
            card_count = ts.get("sessionCount", 0)
            count_label = "sessions"
        tree.append({
            "name": tname, "emoji": emoji,
            "count": card_count, "countLabel": count_label,
            "sessionCount": ts.get("sessionCount", 0),
            "items": items,
        })
    return tree


def _get_infrastructure() -> dict:
    """Read the safe Foundation infrastructure graph."""
    try:
        from data_foundation.infrastructure import dashboard_infrastructure_payload

        return dashboard_infrastructure_payload(load_paths())
    except Exception as exc:
        logger.warning("Unable to read infrastructure graph: %s", exc)
    return {"devices": [], "services": [], "recentActivity": [], "dataAuthority": "foundation-infrastructure-graph-v1", "redacted": True}


def _config_timestamp(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=_local_tz()).strftime("%Y-%m-%d %H:%M")


def _tool_version(executable: str | None) -> str:
    if not executable:
        return ""
    try:
        env = os.environ.copy()
        env["PATH"] = str(Path(executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=4, env=env)
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0][:80] if output else ""
    except Exception:
        return ""


def _find_tool_executable(binary: str, explicit_paths: list[Path] | None = None) -> str | None:
    if not binary:
        return None
    candidates = [Path(path) for path in (explicit_paths or [])]
    candidates.extend(sorted((HOME / ".nvm" / "versions" / "node").glob(f"*/bin/{binary}"), reverse=True))
    resolved = shutil.which(binary)
    if resolved:
        return resolved
    return next((str(path) for path in candidates if path.exists() and path.is_file()), None)


def discover_tool_configs(*, persist: bool = True) -> list[dict]:
    oc_config = _external_tool_path("openclaw", "configPath")
    oc_data = _safe_read_json(oc_config) or {}
    definitions = [
        ("OpenClaw", TOOL_EMOJI["OpenClaw"], _external_tool_path("openclaw", "home"), oc_config, "openclaw", [], [oc_data.get("gateway", {}).get("port")]),
        ("Claude Code", TOOL_EMOJI["Claude Code"], _external_tool_path("claudeCode", "home"), _external_tool_path("claudeCode", "configPath"), "claude", _external_tool_list("claudeCode", "binaryCandidates"), []),
        ("Codex", TOOL_EMOJI["Codex"], _external_tool_path("codex", "home"), _external_tool_path("codex", "configPath"), "codex", [], []),
        ("Gemini CLI", TOOL_EMOJI["Gemini CLI"], _external_tool_path("geminiCli", "home"), _external_tool_path("geminiCli", "configPath"), "gemini", [], []),
        ("Hermes", TOOL_EMOJI["Hermes"], _external_tool_path("hermes", "home"), _external_tool_path("hermes", "configPath"), "hermes", _external_tool_list("hermes", "binaryCandidates"), []),
        ("OpenCode", TOOL_EMOJI["OpenCode"], _external_tool_path("opencode", "home"), _external_tool_path("opencode", "configPath"), "", [], []),
        ("Antigravity", TOOL_EMOJI["Antigravity"], _external_tool_path("antigravity", "home"), _external_tool_path("antigravity", "cliHome"), "", [], []),
        ("Cursor", TOOL_EMOJI["Cursor"], _external_tool_path("cursor", "home"), _external_tool_path("cursor", "configPath"), "", [], []),
    ]
    checked_at = _now_local().strftime("%Y-%m-%d %H:%M:%S")
    configs = []
    for name, emoji, home_path, config_path, binary, explicit_paths, ports in definitions:
        executable = _find_tool_executable(binary, explicit_paths)
        configs.append({
            "name": name,
            "emoji": emoji,
            "status": "detected" if home_path.exists() or executable else "missing",
            "path": str(home_path),
            "executablePath": executable or "",
            "configPath": str(config_path),
            "ports": [str(port) for port in ports if port],
            "version": _tool_version(executable),
            "updatedAt": _config_timestamp(config_path),
            "checkedAt": checked_at,
        })
    if persist:
        try:
            snapshot_path = _tool_config_snapshot_path()
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Unable to write tool config snapshot: %s", exc)
    return configs


def _get_tool_configs(*, persist_discovery: bool = True) -> list[dict]:
    snapshot = _safe_read_json(_tool_config_snapshot_path())
    return snapshot if isinstance(snapshot, list) else discover_tool_configs(persist=persist_discovery)


def _foundation_active_day_count() -> int:
    """Count distinct non-blank business dates from Foundation rollups."""
    try:
        from data_foundation.db import connect

        with connect(load_paths(), read_only=True) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT business_date
                    FROM daily_tool_usage
                    WHERE tool_key != 'cron'
                    GROUP BY business_date
                    HAVING SUM(tokens) > 0 OR SUM(messages) > 0
                        OR SUM(sessions) > 0 OR SUM(api_calls) > 0
                )
                """
            ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def refresh_tool_configs() -> list[dict]:
    configs, _detection = _refresh_tool_configs_with_detection()
    return configs


def _refresh_tool_configs_with_detection() -> tuple[list[dict], dict[str, Any]]:
    configs = discover_tool_configs()
    detection = _current_external_tool_detection()
    visible = apply_external_tool_visibility(
        {"toolConfigs": configs},
        detection,
    ).get("toolConfigs", [])
    _cache["data"] = None
    _cache["ts"] = 0
    return visible, detection


def refresh_tool_configs_with_metadata() -> dict:
    configs, detection = _refresh_tool_configs_with_detection()
    return {
        "toolConfigs": configs,
        "detectedToolKeys": detection.get("detectedToolKeys", []),
        "toolPresence": detection.get("toolPresence", {}),
        "sideEffects": ["tool-config-snapshot-write", "ai-assets-cache-invalidation"],
        "snapshotPath": str(_tool_config_snapshot_path()),
    }

# ── 核心主函数 ──

def _build_ai_assets_payload(
    all_entries: dict[str, list[dict]],
    session_counts: dict[str, int],
    *,
    include_rag: bool = True,
    usage_cache: dict | None = None,
    tool_detection: dict[str, Any] | None = None,
) -> dict:
    now = _now_local()
    tools = []
    total_tt = 0
    total_tm = 0
    total_ts = 0
    for item in TOOL_DEFS:
        name = item["name"]
        entries = all_entries.get(name, [])
        session_count = session_counts.get(name, 0)
        try:
            tool_stat = _aggregate_tool(name, entries, session_count)
            tools.append(tool_stat)
            total_tt += tool_stat["allTimeTokens"]
            total_tm += tool_stat["allTimeMessages"]
            total_ts += session_count
        except Exception as error:
            logger.warning("Aggregate %s failed: %s", name, error)
            tools.append({"name": name, "emoji": item.get("emoji", ""), "allTimeTokens": 0, "allTimeMessages": 0, "todayTokens": 0, "todayMessages": 0, "sessionCount": 0, "firstActivity": "", "lastActivity": "", "activeDays": 0, "usageStatus": item.get("usageStatus", "available")})

    agents = _get_agents_enhanced(tools, all_entries)
    workspace_usage = _aggregate_by_workspace(all_entries)
    data = {
        "timestamp": now.isoformat(), "tools": tools,
        "totalTokens": total_tt, "totalMessages": total_tm, "totalSessions": total_ts,
        "agents": agents, "agentCount": len(agents),
        "activeDayCount": _foundation_active_day_count(),
        "diary": _get_diary_stats(), "memory": _get_memory_stats(),
        "skills": _get_skills_stats(),
        "git": _get_git_stats(),
        "mattermost": {"bots": 0, "status": "configured"},
        "cronJobs": _get_cron_job_stats(),
        "storage": _get_detailed_storage(include_rag=include_rag),
        "infrastructure": _get_infrastructure(),
        "toolConfigs": _get_tool_configs(persist_discovery=include_rag),
        "trend30d": _get_30day_trend(all_entries),
        "models": _aggregate_by_model(all_entries),
        "workspaceUsage": workspace_usage,
        "workspaceAttributionQa": _workspace_attribution_qa(all_entries, workspace_usage),
        "agentTree": _get_agent_tree(tools, all_entries),
        "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S")
    }
    if usage_cache is not None:
        data["usageCache"] = usage_cache
    if include_rag:
        data["rag"] = _get_rag_stats()
    return apply_external_tool_visibility(
        data,
        tool_detection if tool_detection is not None else _current_external_tool_detection(),
    )


def get_ai_assets(*, include_rag: bool = True) -> dict:
    all_entries = {}
    session_counts = {}
    for name, scanner_fn in _ALL_SCANNERS:
        try:
            entries, session_count = scanner_fn()
            all_entries[name] = entries
            session_counts[name] = session_count
        except Exception as e:
            logger.warning(f"Scan {name} failed: {e}")
            all_entries[name] = []
            session_counts[name] = 0
            _RUNTIME_SESSION_RECORDS.pop(name, None)
            _RUNTIME_DIALOGUE_ACTIVITY.pop(name, None)
    return _build_ai_assets_payload(all_entries, session_counts, include_rag=include_rag)


def get_ai_assets_incremental(*, include_rag: bool = True) -> dict:
    all_entries, session_counts, usage_cache = _scan_usage_incremental()
    try:
        hermes_entries, hermes_session_count = _scan_all_hermes()
        all_entries["Hermes"] = hermes_entries
        session_counts["Hermes"] = hermes_session_count
    except Exception as error:
        logger.warning("Scan Hermes failed: %s", error)
        all_entries["Hermes"] = []
        session_counts["Hermes"] = 0
        try:
            prior_errors = max(0, int(usage_cache.get("errors") or 0))
        except (TypeError, ValueError):
            prior_errors = 0
        usage_cache["errors"] = prior_errors + 1
    for name, scanner_fn in (
        ("OpenCode", _scan_all_opencode),
        ("Antigravity", _scan_all_antigravity),
        ("Cursor", _scan_all_cursor),
    ):
        try:
            entries, session_count = scanner_fn()
            all_entries[name] = entries
            session_counts[name] = session_count
        except Exception as error:
            logger.warning("Scan %s failed: %s", name, error)
            all_entries[name] = []
            session_counts[name] = 0
            _RUNTIME_SESSION_RECORDS.pop(name, None)
            _RUNTIME_DIALOGUE_ACTIVITY.pop(name, None)
            try:
                prior_errors = max(0, int(usage_cache.get("errors") or 0))
            except (TypeError, ValueError):
                prior_errors = 0
            usage_cache["errors"] = prior_errors + 1
    payload = _build_ai_assets_payload(
        all_entries,
        session_counts,
        include_rag=include_rag,
        usage_cache={**usage_cache, "mode": "incremental", "parserVersion": AI_ASSET_USAGE_PARSER_VERSION},
    )
    return payload


def _empty_ai_assets_payload(*, freshness: dict) -> dict:
    now = _now_local()
    return {
        "timestamp": now.isoformat(),
        "tools": [],
        "totalTokens": 0,
        "totalMessages": 0,
        "totalSessions": 0,
        "agents": [],
        "agentCount": 0,
        "activeDayCount": 0,
        "diary": {},
        "memory": {},
        "skills": {},
        "git": {},
        "mattermost": {},
        "cronJobs": {},
        "storage": {"tools": [], "categories": []},
        "infrastructure": {},
        "toolConfigs": [],
        "trend30d": [],
        "models": [],
        "workspaceUsage": [],
        "agentTree": {},
        "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "rag": {},
        "dataFreshness": {"aiAssets": freshness},
    }


def _ai_assets_snapshot_failure() -> dict:
    return dashboard_failure(
        "ai-assets-snapshot",
        fallback=_empty_ai_assets_payload(
            freshness={
                "source": "foundation",
                "status": "source_error",
                "refreshRequired": False,
                "staticSnapshotOnly": True,
            }
        ),
    )


def _get_ai_assets_foundation() -> dict:
    try:
        paths = load_paths()
        foundation_src = _workspace_dir() / "src"
        if str(foundation_src) not in sys.path:
            sys.path.insert(0, str(foundation_src))
        from data_foundation.snapshots import read_dashboard_snapshot

        snapshot = read_dashboard_snapshot(paths)
    except Exception:
        logger.exception("AI Assets Foundation snapshot read failed")
        return _ai_assets_snapshot_failure()
    if snapshot is None:
        missing = _empty_ai_assets_payload(
            freshness={
                "source": "snapshot-missing",
                "status": "snapshot_missing",
                "refreshRequired": True,
                "staticSnapshotOnly": True,
                "runtime": {
                    "actanaraHome": str(paths.home),
                    "database": str(paths.db_path),
                    "databaseExists": paths.db_path.exists(),
                },
            }
        )
        return attach_dashboard_state(missing, empty=True)
    try:
        payload = snapshot["payload"]
        if not isinstance(payload, dict):
            raise TypeError("AI Assets snapshot payload must be an object")
        data = dict(payload)
        # Presence is intentionally evaluated at read time as well. A snapshot
        # can outlive a local uninstall, so its embedded presence is audit
        # metadata rather than authority for current runtime cards.
        data = apply_external_tool_visibility(
            data,
            _current_external_tool_detection(),
        )
        data.setdefault("rag", {})
        data.setdefault("storage", {"tools": [], "categories": []})
        data["dataFreshness"] = {
            "aiAssets": {
                "source": "foundation",
                "projectionType": snapshot["projectionType"],
                "generatedAt": snapshot["generatedAt"],
                "status": snapshot["status"],
                "staticSnapshotOnly": True,
                "ragStatusSource": "snapshot",
                "diaryRoot": str(paths.diary_dir),
            }
        }
    except Exception:
        logger.exception("AI Assets Foundation snapshot payload failed validation")
        return _ai_assets_snapshot_failure()

    usage_cache = data.get("usageCache") if isinstance(data.get("usageCache"), dict) else {}
    try:
        usage_error_count = max(0, int(usage_cache.get("errors") or 0))
    except (TypeError, ValueError):
        usage_error_count = 1
    source_errors = []
    if usage_error_count:
        source_errors.append(source_error("ai-assets-usage-cache", code="incremental-source-read-failed"))
        data["degraded"] = True
        data["sourceErrors"] = source_errors
    return attach_dashboard_state(data, source_errors=source_errors)

def get_ai_assets_cached() -> dict:
    now = time.time()
    requested_source = resolve_runtime_source("DASHBOARD_READ_SOURCE", load_paths())
    cache_key = {**_ai_assets_cache_key("foundation"), "requestedSource": requested_source}
    cached = _cache.get("data")
    cached_state = cached.get("dashboardState") if isinstance(cached, dict) else {}
    cached_status = cached_state.get("status") if isinstance(cached_state, dict) else None
    if (
        cached is not None
        and cached_status not in {"error", "unavailable"}
        and _cache.get("key") == cache_key
        and (now - _cache["ts"]) < CACHE_TTL
    ):
        return cached
    data = _get_ai_assets_foundation()
    if requested_source != "foundation":
        data.setdefault("dataFreshness", {}).setdefault("aiAssets", {})["retiredSourceRequested"] = requested_source
    state = data.get("dashboardState") if isinstance(data, dict) else {}
    status = state.get("status") if isinstance(state, dict) else None
    if status not in {"error", "unavailable"}:
        _cache["data"] = data
        _cache["ts"] = now
        _cache["key"] = cache_key
        _cache["source"] = "foundation"
    return data
