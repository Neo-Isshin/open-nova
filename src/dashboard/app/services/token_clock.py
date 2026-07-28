#!/usr/bin/env python3
"""Token Clock — 实时 token 使用监控，读取与 TokenClock macOS app 相同的数据源"""

import json
import sqlite3
import logging
import time
import re
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections import defaultdict
import config
from data_foundation.external_tool_catalog import detect_external_tools
from data_foundation.session_files import is_openclaw_session_file
from data_foundation.time import DEFAULT_BUSINESS_DAY_START_HOUR, business_date_for, resolve_timezone
from data_foundation.settings import (
    default_external_tool_settings,
    external_tool_path,
    resolve_external_tool_paths,
)
from data_foundation.runtime_sources import AntigravityRuntime, OpenCodeRuntime
from data_foundation.token_semantics import (
    authoritative_semantics,
    cache_hit_rate,
    legacy_operational_total,
    normalize_cached_input_detail,
    protocol_total,
)
from data_foundation.usage_attribution import TOOL_EMOJI, resolve_usage_group, usage_group_display_allowed
from data_foundation.workspace_attribution import (
    infer_workspace_name_from_text,
    workspace_display_name,
)

logger = logging.getLogger("dashboard.token_clock")


def local_timezone():
    return resolve_timezone()


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

# ── 工具定义 ──
TOOL_DEFS = [
    {"name": "OpenClaw", "emoji": TOOL_EMOJI["OpenClaw"], "color": "#FF6B35"},
    {"name": "Claude Code", "emoji": TOOL_EMOJI["Claude Code"], "color": "#D97706"},
    {"name": "Gemini CLI", "emoji": TOOL_EMOJI["Gemini CLI"], "color": "#3B82F6"},
    {"name": "Codex", "emoji": TOOL_EMOJI["Codex"], "color": "#10B981"},
    {"name": "Hermes", "emoji": TOOL_EMOJI["Hermes"], "color": "#F59E0B"},
    {"name": "OpenCode", "emoji": TOOL_EMOJI["OpenCode"], "color": "#0EA5A4"},
    {"name": "Antigravity", "emoji": TOOL_EMOJI["Antigravity"], "color": "#6366F1"},
    {"name": "Cursor", "emoji": TOOL_EMOJI["Cursor"], "color": "#7C3AED", "usageStatus": "unavailable"},
]

# ── OpenClaw session 文件扫描公共函数 ──

def _collect_openclaw_session_files(base: Path) -> dict[str, Path]:
    """收集所有 OpenClaw session JSONL 文件，按 session ID 去重。

    规则：
    - 只纳入 .jsonl、.jsonl.reset.*、.jsonl.deleted.*
    - 排除 metadata sidecar、checkpoint、trajectory、lock、sessions.json
    - 同一 session ID 有多个文件时，取 mtime 最新的
    - 返回 {session_id: file_path}
    """
    sessions: dict[str, tuple[float, Path]] = {}  # session_id -> (mtime, path)
    if not base.exists():
        return {}
    for agent_dir in base.iterdir():
        if not agent_dir.is_dir():
            continue
        sess_dir = agent_dir / "sessions"
        if not sess_dir.exists():
            continue
        for f in sess_dir.iterdir():
            fn = f.name
            if not is_openclaw_session_file(fn):
                continue
            # Extract session ID (UUID before first .jsonl or .jsonl.)
            import re
            m = re.match(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", fn)
            if not m:
                continue
            sid = m.group(1)
            try:
                mtime = f.stat().st_mtime
            except OSError:
                raise
            # Keep latest mtime per session ID
            if sid not in sessions or mtime > sessions[sid][0]:
                sessions[sid] = (mtime, f)
    return {sid: path for sid, (_, path) in sessions.items()}


def _parse_openclaw_jsonl_file(file_path: Path) -> list[dict]:
    """解析单个 OpenClaw session JSONL 文件，提取 assistant usage entries."""
    entries = []
    nonblank_lines = 0
    valid_json_lines = 0
    for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        nonblank_lines += 1
        try:
            obj = json.loads(line)
            valid_json_lines += 1
            msg = obj.get("message", {})
            if msg.get("role") == "assistant":
                usage = msg.get("usage", {})
                if not usage:
                    continue
                entries.append({
                    "input": usage.get("input", 0) or 0,
                    "output": usage.get("output", 0) or 0,
                    "cacheRead": usage.get("cacheRead", 0) or 0,
                    "cacheWrite": usage.get("cacheWrite", 0) or 0,
                    "timestamp": obj.get("timestamp", ""),
                    "model": msg.get("model", ""),
                })
        except (json.JSONDecodeError, KeyError):
            # Tolerate a partial/truncated JSONL tail; file I/O failures still
            # propagate to the source-level degraded state.
            continue
    if nonblank_lines and not valid_json_lines:
        raise ValueError("session file contains no valid JSON records")
    return entries


# ── 增量扫描缓存 ──
# key: file path (str), value: (mtime, parsed_entries_list)
_file_cache: dict[str, tuple[float, list]] = {}
# key: db path, value: (db_mtime, wal_mtime, entries)
_db_cache: dict[str, tuple[float, float, list]] = {}
# Route handlers run this scanner in a worker thread.  Serialize complete
# snapshots so concurrent requests cannot interleave mutations of the shared
# incremental caches or observe a partially refreshed snapshot.
_snapshot_lock = threading.Lock()


def _now_local() -> datetime:
    return datetime.now(local_timezone())


def _file_mtime_before_today(path: Path, today_str: str) -> bool:
    try:
        business_start = datetime.strptime(today_str, "%Y-%m-%d").replace(
            hour=DEFAULT_BUSINESS_DAY_START_HOUR,
            tzinfo=local_timezone(),
        )
        return datetime.fromtimestamp(path.stat().st_mtime, tz=local_timezone()) < business_start
    except Exception:
        return False


def _utc_to_local(ts_str: str) -> datetime:
    """Parse ISO8601 UTC timestamp string to local datetime."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(local_timezone())
    except Exception:
        # Try Unix timestamp as string
        try:
            return datetime.fromtimestamp(float(ts_str), tz=local_timezone())
        except Exception:
            return _now_local()


def _primary_referenced_project(text: str) -> str | None:
    return infer_workspace_name_from_text(text)


def _referenced_group(project: str | None, fallback: str) -> str:
    return project if project and project != fallback else fallback


def _workspace_group_from_path(path: str | Path) -> str:
    return resolve_usage_group(cwd=str(path)).group or workspace_display_name(path)


def _workspace_usage_visible(group: str, tool_name: str = "") -> bool:
    return usage_group_display_allowed(group, tool_name)


def _claude_workspace_label(encoded_name: str) -> str:
    parts = [part for part in encoded_name.split("-") if part]
    return parts[-1] if parts else encoded_name


def _usage_group_for_source(tool_key: str, *, raw_path: str | Path = "", cwd: str | Path = "", fallback: str = "") -> str:
    return resolve_usage_group(
        tool_key,
        raw_path=str(raw_path or ""),
        cwd=str(cwd or ""),
        fallback=fallback,
    ).group or fallback


def _external_tool_path(tool: str, key: str) -> Path:
    try:
        return external_tool_path(tool, key)
    except Exception:
        fallback = default_external_tool_settings(Path.home()).get(tool, {}).get(key)
        return Path(str(fallback)).expanduser().absolute()


def _gemini_project_labels() -> dict[str, str]:
    path = _external_tool_path("geminiCli", "projectsPath")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    return {slug: (Path(real_path).name or real_path) for real_path, slug in projects.items()}


def _scan_openclaw(today_str: str, current_hour: int) -> list[dict]:
    """Scan all OpenClaw session JSONL files (including reset/deleted/bak)."""
    base = _external_tool_path("openclaw", "agentsRoot")
    session_files = _collect_openclaw_session_files(base)
    entries = []
    for sid, f in session_files.items():
        try:
            if _file_mtime_before_today(f, today_str):
                continue
            mtime = f.stat().st_mtime
            cached = _file_cache.get(str(f))
            if cached and cached[0] == mtime:
                file_entries = cached[1]
            else:
                file_entries = _parse_openclaw_jsonl_file(f)
                _file_cache[str(f)] = (mtime, file_entries)
            group = _usage_group_for_source("openclaw", raw_path=f, fallback=f.parent.parent.name)
            entries.extend({**entry, "usageGroup": group} for entry in file_entries)
        except Exception:
            raise
    return entries


def _scan_claude_code(today_str: str, current_hour: int) -> list[dict]:
    """Scan Claude Code project session JSONL files."""
    base = _external_tool_path("claudeCode", "projectsRoot")
    entries = []
    if not base.exists():
        return entries
    for f in base.rglob("*.jsonl"):
        is_subagent = "/subagents/" in str(f)
        try:
            if _file_mtime_before_today(f, today_str):
                continue
            mtime = f.stat().st_mtime
            cached = _file_cache.get(str(f))
            if cached and cached[0] == mtime:
                entries.extend(cached[1])
                continue
            project_dir = f.parents[2] if is_subagent else f.parent
            group = _usage_group_for_source(
                "claude-code",
                raw_path=project_dir,
                fallback=_claude_workspace_label(project_dir.name),
            )
            file_entries = []
            nonblank_lines = 0
            valid_json_lines = 0
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                nonblank_lines += 1
                project = _primary_referenced_project(line)
                if project:
                    group = _referenced_group(project, group)
                try:
                    obj = json.loads(line)
                    valid_json_lines += 1
                    if obj.get("type") == "assistant":
                        usage = obj.get("message", {}).get("usage", {})
                        ts_raw = obj.get("timestamp", "")
                        file_entries.append({
                            "input": usage.get("input_tokens", 0) or 0,
                            "output": usage.get("output_tokens", 0) or 0,
                            "cacheRead": usage.get("cache_read_input_tokens", 0) or 0,
                            "cacheWrite": usage.get("cache_creation_input_tokens", 0) or 0,
                            "timestamp": ts_raw,
                            "usageGroup": group,
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
            if nonblank_lines and not valid_json_lines:
                raise ValueError("session file contains no valid JSON records")
            _file_cache[str(f)] = (mtime, file_entries)
            entries.extend(file_entries)
        except Exception:
            raise
    return entries


def _scan_gemini(today_str: str, current_hour: int) -> list[dict]:
    """Scan Gemini sessions across registered and temporary workspaces."""
    base = _external_tool_path("geminiCli", "chatsRoot")
    entries = []
    if not base.exists():
        return entries
    labels = _gemini_project_labels()
    session_files: dict[str, Path] = {}
    for f in base.glob("session-*"):
        if f.suffix in (".json", ".jsonl"):
            current = session_files.get(f.stem)
            if current is None or (current.suffix == ".json" and f.suffix == ".jsonl"):
                session_files[f.stem] = f
    for f in session_files.values():
        try:
            if _file_mtime_before_today(f, today_str):
                continue
            mtime = f.stat().st_mtime
            cached = _file_cache.get(str(f))
            if cached and cached[0] == mtime:
                entries.extend(cached[1])
                continue
            group = labels.get(f.parent.parent.name, f.parent.parent.name)
            file_entries = []
            if f.suffix == ".jsonl":
                messages = []
                nonblank_lines = 0
                valid_json_lines = 0
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    nonblank_lines += 1
                    try:
                        messages.append(json.loads(line))
                        valid_json_lines += 1
                    except json.JSONDecodeError:
                        continue
                if nonblank_lines and not valid_json_lines:
                    raise ValueError("session file contains no valid JSON records")
            else:
                data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
                messages = data.get("messages", []) if isinstance(data, dict) else []
            for msg in messages:
                project = _primary_referenced_project(json.dumps(msg, ensure_ascii=False))
                if project:
                    group = _referenced_group(project, group)
                if msg.get("type") != "gemini":
                    continue
                tokens = msg.get("tokens", {})
                file_entries.append({
                    "input": tokens.get("input", 0) or 0,
                    "output": tokens.get("output", 0) or 0,
                    "cacheRead": tokens.get("cached", 0) or 0,
                    "cacheWrite": 0,
                    "timestamp": msg.get("timestamp", ""),
                    "usageGroup": group,
                })
            _file_cache[str(f)] = (mtime, file_entries)
            entries.extend(file_entries)
        except Exception:
            raise
    return entries


def _scan_codex(today_str: str, current_hour: int) -> list[dict]:
    """Scan Codex sessions using incremental last_token_usage events."""
    base = _external_tool_path("codex", "sessionsRoot")
    entries = []
    if not base.exists():
        return entries
    for f in base.rglob("rollout-*.jsonl"):
        try:
            if _file_mtime_before_today(f, today_str):
                continue
            mtime = f.stat().st_mtime
            cached = _file_cache.get(str(f))
            if cached and cached[0] == mtime:
                entries.extend(cached[1])
                continue
            group = "unknown"
            current_model = None
            file_entries = []
            nonblank_lines = 0
            valid_json_lines = 0
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                nonblank_lines += 1
                project = _primary_referenced_project(line)
                if project:
                    group = _referenced_group(project, group)
                try:
                    obj = json.loads(line)
                    valid_json_lines += 1
                    if not isinstance(obj, dict):
                        continue
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    if obj.get("type") == "session_meta":
                        cwd = payload.get("cwd", "")
                        if cwd:
                            group = _usage_group_for_source("codex", raw_path=f, cwd=cwd, fallback=group)
                        continue
                    if obj.get("type") == "turn_context":
                        current_model = _codex_model_from_payload(payload) or current_model
                        continue
                    if obj.get("type") != "event_msg":
                        continue
                    if payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    usage = info.get("last_token_usage")
                    if not usage:
                        continue
                    raw_input = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cached = usage.get("cached_input_tokens", 0) or 0
                    inp, cached, cache_semantics = normalize_cached_input_detail(
                        input_tokens=raw_input,
                        output_tokens=out,
                        cache_read_tokens=cached,
                        reported_total_tokens=usage.get("total_tokens"),
                    )
                    reasoning = usage.get("reasoning_output_tokens", 0) or 0
                    if raw_input == 0 and out == 0 and cached == 0 and reasoning == 0:
                        continue
                    file_entries.append({
                        "input": inp,
                        "output": out,
                        "cacheRead": cached,
                        "cacheWrite": 0,
                        "reasoning": reasoning,
                        "rawInput": raw_input,
                        "cacheInputSemantics": cache_semantics,
                        "legacyOutputWithReasoning": out + reasoning,
                        "timestamp": obj.get("timestamp", ""),
                        "usageGroup": group,
                        "model": _codex_model_from_payload(payload) or current_model or _codex_model_from_context_window(info.get("model_context_window")) or "",
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
            if nonblank_lines and not valid_json_lines:
                raise ValueError("session file contains no valid JSON records")
            _file_cache[str(f)] = (mtime, file_entries)
            entries.extend(file_entries)
        except Exception:
            raise
    return entries


def _scan_hermes(today_str: str, current_hour: int) -> list[dict]:
    """Scan Hermes state database."""
    db_path = _external_tool_path("hermes", "stateDbPath")
    entries = []
    if not db_path.exists():
        return entries
    wal_path = db_path.with_suffix(".db-wal")
    db_mtime = db_path.stat().st_mtime
    wal_mtime = wal_path.stat().st_mtime if wal_path.exists() else 0
    cached = _db_cache.get(str(db_path))
    if cached and cached[0] == db_mtime and cached[1] == wal_mtime:
        return cached[2]
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT started_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, message_count FROM sessions"
        )
        for row in cur.fetchall():
            started_at, inp, out, cr, cw, mc = row
            entries.append({
                "input": inp or 0,
                "output": out or 0,
                "cacheRead": cr or 0,
                "cacheWrite": cw or 0,
                "timestamp": str(started_at),  # Unix timestamp REAL
                "message_count": mc or 0,
                "usageGroup": "Hermes",
            })
    finally:
        conn.close()
    _db_cache[str(db_path)] = (db_mtime, wal_mtime, entries)
    return entries


def _scan_opencode(today_str: str, current_hour: int) -> list[dict]:
    del today_str, current_hour
    runtime = OpenCodeRuntime(_external_tool_path("opencode", "home"))
    return _normalized_runtime_usage_entries(runtime)


def _scan_antigravity(today_str: str, current_hour: int) -> list[dict]:
    del today_str, current_hour
    runtime = AntigravityRuntime(
        {
            "cli": _external_tool_path("antigravity", "cliHome"),
            "ide": _external_tool_path("antigravity", "ideHome"),
            "app": _external_tool_path("antigravity", "appHome"),
        }
    )
    return _normalized_runtime_usage_entries(runtime)


def _normalized_runtime_usage_entries(runtime) -> list[dict]:
    sessions = {record.external_session_key: record for record in runtime.sessions()}
    entries = []
    for record in runtime.usage():
        session = sessions.get(record.external_session_key)
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
    return entries


def _filter_today(entries: list[dict], today_str: str) -> list[dict]:
    """Filter entries to the configured 04:00 business date, parse timestamps, add local hour."""
    result = []
    now = _now_local()
    ten_min_ago = now.timestamp() - 600
    for e in entries:
        ts_raw = e.get("timestamp", "")
        if not ts_raw:
            continue
        dt = _utc_to_local(ts_raw)
        if business_date_for(dt, tz=local_timezone()).isoformat() != today_str:
            continue
        e["_dt"] = dt
        e["_hour"] = dt.hour
        e["_ts"] = dt.timestamp()
        e["_recent"] = dt.timestamp() >= ten_min_ago
        result.append(e)
    return result


def _aggregate(entries: list[dict], current_hour: int) -> dict:
    """Aggregate filtered entries into stats."""
    total_tokens = 0
    messages = len(entries)
    hourly_tokens = 0
    hourly_timeline = [{"hour": f"{h:02d}", "tokens": 0, "messages": 0} for h in range(24)]
    is_active = False
    total_input = 0
    total_cache_read = 0
    cache_write = 0
    legacy_total = 0

    for e in entries:
        inp = e.get("input", 0)
        out = e.get("output", 0)
        cr = e.get("cacheRead", 0)
        cw = e.get("cacheWrite", 0)
        t = _entry_protocol_total(e)
        total_tokens += t
        total_input += int(inp or 0)
        total_cache_read += int(cr or 0)
        cache_write += int(cw or 0)
        legacy_total += _entry_legacy_operational_total(e)
        hourly_timeline[e["_hour"]]["tokens"] += t
        hourly_timeline[e["_hour"]]["messages"] += 1
        if e["_hour"] == current_hour:
            hourly_tokens += t
        if e.get("_recent"):
            is_active = True

    cache_rate = cache_hit_rate({"input": total_input, "cacheRead": total_cache_read})
    payload = {
        "tokens": total_tokens,
        "legacyOperationalTokens": legacy_total,
        "cacheWrite": cache_write,
        "messages": messages,
        "cacheRate": cache_rate,
        "isActive": is_active,
        "hourlyTokens": hourly_tokens,
        "hourlyTimeline": hourly_timeline,
    }
    return payload


def _aggregate_workspace_usage(all_filtered: list[tuple[str, list[dict], dict]], current_hour: int) -> list[dict]:
    usage = defaultdict(lambda: {"tokens": 0, "legacyOperationalTokens": 0, "cacheWrite": 0, "messages": 0, "hourlyTokens": 0, "isActive": False, "lastActive": ""})
    for tool_name, entries, _stats in all_filtered:
        for entry in entries:
            group = entry.get("usageGroup") or tool_name
            row = usage[(tool_name, group)]
            tokens = _entry_protocol_total(entry)
            row["tokens"] += tokens
            row["legacyOperationalTokens"] += _entry_legacy_operational_total(entry)
            row["cacheWrite"] += int(entry.get("cacheWrite", 0) or 0)
            row["messages"] += entry.get("message_count", 1) or 1
            if entry.get("_hour") == current_hour:
                row["hourlyTokens"] += tokens
            row["isActive"] = row["isActive"] or bool(entry.get("_recent"))
            timestamp = entry.get("_dt")
            if timestamp and timestamp.isoformat() > row["lastActive"]:
                row["lastActive"] = timestamp.isoformat()
    result = [
        {"name": group, "tool": tool_name, "emoji": TOOL_EMOJI.get(tool_name, ""), **metrics}
        for (tool_name, group), metrics in usage.items()
        if metrics["tokens"] > 0 and _workspace_usage_visible(group, tool_name)
    ]
    result.sort(key=lambda item: item["tokens"], reverse=True)
    return result


def _entry_protocol_total(entry: dict) -> int:
    explicit = entry.get("protocolTotal")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError, OverflowError):
            pass
    return protocol_total(entry)


def _entry_legacy_operational_total(entry: dict) -> int:
    return _entry_protocol_total(entry) + int(entry.get("cacheWrite", 0) or 0)


def _rate_emoji(hourly_tokens: int) -> str:
    if hourly_tokens > 10_000_000:
        return "💥"
    elif hourly_tokens > 2_000_000:
        return "🔥"
    elif hourly_tokens > 400_000:
        return "🏃‍♂️"
    elif hourly_tokens > 10_000:
        return "☕"
    return "🛌"


# ── Scanner registry ──
_SCANNERS = [
    ("OpenClaw", _scan_openclaw),
    ("Claude Code", _scan_claude_code),
    ("Gemini CLI", _scan_gemini),
    ("Codex", _scan_codex),
    ("Hermes", _scan_hermes),
    ("OpenCode", _scan_opencode),
    ("Antigravity", _scan_antigravity),
]

_TOOL_ID_BY_NAME = {
    "OpenClaw": "openclaw",
    "Claude Code": "claudeCode",
    "Gemini CLI": "geminiCli",
    "Codex": "codex",
    "Hermes": "hermes",
    "OpenCode": "opencode",
    "Antigravity": "antigravity",
    "Cursor": "cursor",
}
_USAGE_UNAVAILABLE_TOOL_IDS = {"cursor"}


def _live_token_semantics() -> dict:
    tz = local_timezone()
    semantics = authoritative_semantics(
        scope="multi-tool live operational status",
        day_boundary=f"{getattr(tz, 'key', str(tz))} business day 04:00-03:59",
        live=True,
    )
    semantics["runtimeOverrides"] = {
        "OpenCode": "source protocol total includes separately reported reasoning and excludes cache writes",
        "Antigravity": "source protocol total also includes local reasoning and tool token fields",
    }
    semantics["usageUnavailable"] = ["Cursor"]
    return semantics


def get_token_clock_data() -> dict:
    with _snapshot_lock:
        return _get_token_clock_data_unlocked()


def _get_token_clock_data_unlocked() -> dict:
    from .dashboard_state import attach_dashboard_state, source_error

    now = _now_local()
    today_str = business_date_for(now, tz=local_timezone()).isoformat()
    current_hour = now.hour

    tools = []
    total_tokens = 0
    total_messages = 0
    total_cache = 0
    merged_timeline = [{"hour": f"{h:02d}", "tokens": 0, "messages": 0} for h in range(24)]

    TOOL_MAP = {t["name"]: t for t in TOOL_DEFS}

    all_filtered = []
    source_errors: list[dict[str, str]] = []
    try:
        detection = detect_external_tools()
        detected_values = detection.get(
            "detectedToolKeys",
            detection.get("detectedToolIds", ()),
        )
        detected_tool_ids = {
            str(tool_id)
            for tool_id in detected_values
        }
    except Exception as exc:
        logger.warning("Token clock external tool detection failed: %s", exc)
        detected_tool_ids = set()
        source_errors.append(source_error("external-tool-detection"))

    for name, scanner_fn in _SCANNERS:
        tool_id = _TOOL_ID_BY_NAME.get(name)
        if (
            tool_id is None
            or tool_id not in detected_tool_ids
            or tool_id in _USAGE_UNAVAILABLE_TOOL_IDS
        ):
            continue
        try:
            raw_entries = scanner_fn(today_str, current_hour)
        except Exception as exc:
            logger.warning("Token clock scanner failed for %s: %s", name, exc)
            source_errors.append(source_error(name))
            raw_entries = []
        filtered = _filter_today(raw_entries, today_str)
        stats = _aggregate(filtered, current_hour)
        all_filtered.append((name, filtered, stats))

    for name, filtered, stats in all_filtered:
        td = TOOL_MAP[name]
        # Hermes: use message_count from DB instead of entry count
        if name == "Hermes" and filtered:
            total_mc = sum(e.get("message_count", 0) or 0 for e in filtered)
            if total_mc > 0:
                stats["messages"] = total_mc

        tools.append({
            "name": td["name"],
            "emoji": td["emoji"],
            "tokens": stats["tokens"],
            "legacyOperationalTokens": stats["legacyOperationalTokens"],
            "cacheWrite": stats["cacheWrite"],
            "messages": stats["messages"],
            "cacheRate": stats["cacheRate"],
            "isActive": stats["isActive"],
            "hourlyTokens": stats["hourlyTokens"],
        })

        total_tokens += stats["tokens"]
        total_messages += stats["messages"]

        # Merge hourly timeline
        for i, ht in enumerate(stats["hourlyTimeline"]):
            merged_timeline[i]["tokens"] += ht["tokens"]
            merged_timeline[i]["messages"] += ht["messages"]

    total_cache_read = 0
    total_input_for_rate = 0
    for name, filtered, stats in all_filtered:
        for e in filtered:
            total_cache_read += int(e.get("cacheRead", 0) or 0)
            total_input_for_rate += int(e.get("input", 0) or 0)

    overall_cache_rate = cache_hit_rate({"input": total_input_for_rate, "cacheRead": total_cache_read})
    current_hour_tokens = merged_timeline[current_hour]["tokens"]

    payload = {
        "timestamp": now.isoformat(),
        "today": today_str,
        "semantics": _live_token_semantics(),
        "tools": tools,
        "totalTokens": total_tokens,
        "totalMessages": total_messages,
        "overallCacheRate": overall_cache_rate,
        "rateEmoji": _rate_emoji(current_hour_tokens),
        "hourlyTimeline": merged_timeline,
        "workspaceUsage": _aggregate_workspace_usage(all_filtered, current_hour),
        "degraded": bool(source_errors),
        "sourceErrors": source_errors,
    }

    return attach_dashboard_state(
        payload,
        empty=total_tokens == 0 and total_messages == 0,
        source_errors=source_errors,
    )
