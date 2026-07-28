#!/usr/bin/env python3
"""
Token Engine (v4.0 - Universal Logic)
智慧 Agent 系统的统一财务统计引擎。
支持：OpenClaw(🦞), Gemini-CLI, Claude-Code, Codex, Hermes, OpenCode, Antigravity.
功能：支持指定日期追溯统计 & 历史累计统计。
协议：v5.9.3 Master (Total = Input + Output + CacheRead)
"""

import json
import os
import sqlite3
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from data_foundation.session_files import is_openclaw_session_file
from data_foundation.settings import default_external_tool_path, external_tool_path
from data_foundation.time import business_window
from data_foundation.token_semantics import normalize_cached_input_detail
from data_foundation.runtime_sources import AntigravityRuntime, OpenCodeRuntime

# ── 基础路径定义 ──
HOME = Path.home()
AGENTS_DIR = default_external_tool_path("openclaw", "agentsRoot", HOME)
GEMINI_DIR = default_external_tool_path("geminiCli", "chatsRoot", HOME)
CLAUDE_DIR = default_external_tool_path("claudeCode", "projectsRoot", HOME)
CODEX_DIR = default_external_tool_path("codex", "sessionsRoot", HOME)
HERMES_DB = default_external_tool_path("hermes", "stateDbPath", HOME)
_DEFAULT_AGENTS_DIR = AGENTS_DIR
_DEFAULT_GEMINI_DIR = GEMINI_DIR
_DEFAULT_CLAUDE_DIR = CLAUDE_DIR
_DEFAULT_CODEX_DIR = CODEX_DIR
_DEFAULT_HERMES_DB = HERMES_DB


def _configured_path(tool, key, current, default):
    if current != default:
        return current
    try:
        return external_tool_path(tool, key)
    except Exception:
        return default

def parse_ts(val):
    if not val: return None
    try:
        if isinstance(val, (int, float)): return val / 1000.0 if val > 1e12 else val
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except: return None

def _codex_model_from_payload(payload):
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

def _codex_model_from_context_window(window):
    try:
        parsed = int(window)
    except (TypeError, ValueError):
        return None
    if parsed == 258400:
        return "gpt-5.5"
    return None

def get_hkt_window(target_date_str=None):
    """
    获取配置业务时区 04:00 窗口。
    如果 target_date_str 为 None，返回 None (表示全量历史)。
    """
    if not target_date_str: return None, None
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        start_utc, end_utc = business_window(target)
        return start_utc.timestamp(), (end_utc - start_utc).total_seconds()
    except: return None, None

def is_session_file(fname):
    """权威过滤：只纳入受支持的 OpenClaw session JSONL 文件。"""
    return is_openclaw_session_file(fname)

def scan_tokens(target_date_str=None):
    """
    核心扫描函数。
    target_date_str: "YYYY-MM-DD" 或 None(全量)
    """
    start_ts, duration = get_hkt_window(target_date_str)
    end_ts = (start_ts + duration) if start_ts else None

    # 结果容器
    stats = defaultdict(lambda: {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0, "count": 0, "api_calls": 0, "messages_count": 0})
    model_usage = defaultdict(lambda: {"calls": 0, "tokens": 0})
    active_sessions = defaultdict(set)

    agents_dir = _configured_path("openclaw", "agentsRoot", AGENTS_DIR, _DEFAULT_AGENTS_DIR)
    gemini_dir = _configured_path("geminiCli", "chatsRoot", GEMINI_DIR, _DEFAULT_GEMINI_DIR)
    claude_dir = _configured_path("claudeCode", "projectsRoot", CLAUDE_DIR, _DEFAULT_CLAUDE_DIR)
    codex_dir = _configured_path("codex", "sessionsRoot", CODEX_DIR, _DEFAULT_CODEX_DIR)
    hermes_db = _configured_path("hermes", "stateDbPath", HERMES_DB, _DEFAULT_HERMES_DB)

    # 1. OpenClaw (🦞)
    if agents_dir.exists():
        for adir in agents_dir.iterdir():
            if not adir.is_dir(): continue
            aid = adir.name; sdir = adir / "sessions"
            display_name = f"openclaw:{aid}" # 内部 ID，输出时转为 🦞
            if not sdir.exists(): continue
            for f in sdir.iterdir():
                if not is_session_file(f.name): continue
                try:
                    with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                        for line in fin:
                            try:
                                d = json.loads(line)
                                if d.get("type") != "message": continue
                                msg = d.get("message", {})
                                if msg.get("role") != "assistant": continue
                                u = msg.get("usage", {})
                                if not u: continue

                                ts = parse_ts(d.get("timestamp") or msg.get("timestamp"))
                                if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue

                                sid = d.get("sessionId") or f.name; active_sessions[display_name].add(sid)
                                inp = (u.get("input") or u.get("input_tokens") or 0)
                                out = (u.get("output") or u.get("output_tokens") or 0)
                                cr = (u.get("cacheRead") or u.get("cache_read") or u.get("cache_read_input_tokens") or 0)
                                cw = (u.get("cacheWrite") or u.get("cache_write") or 0)

                                total = inp + out + cr
                                stats[display_name]["input"] += inp
                                stats[display_name]["output"] += out
                                stats[display_name]["cacheRead"] += cr
                                stats[display_name]["cacheWrite"] += cw
                                stats[display_name]["total"] += total
                                stats[display_name]["api_calls"] += 1
                                stats[display_name]["messages_count"] += 1

                                m_real = d.get("model") or msg.get("model") or "unknown"
                                model_usage[m_real]["calls"] += 1; model_usage[m_real]["tokens"] += total
                            except: continue
                except: continue

    # 2. Gemini CLI
    if gemini_dir.exists():
        for f in gemini_dir.glob("session-*"):
            if f.suffix not in (".json", ".jsonl"): continue
            try:
                if f.suffix == ".jsonl":
                    with open(f, "r") as fin:
                        for line in fin:
                            d = json.loads(line)
                            if d.get("type") != "gemini": continue
                            ts = parse_ts(d.get("timestamp"))
                            if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue
                            u = d.get("tokens", {}); inp, out, cr = u.get("input", 0), u.get("output", 0), u.get("cached", 0)
                            total = inp+out+cr
                            stats["gemini-cli"]["input"] += inp; stats["gemini-cli"]["output"] += out; stats["gemini-cli"]["cacheRead"] += cr; stats["gemini-cli"]["total"] += total
                            stats["gemini-cli"]["api_calls"] += 1; stats["gemini-cli"]["messages_count"] += 1
                            active_sessions["gemini-cli"].add(f.name)
                else:
                    data = json.loads(f.read_text())
                    for m in data.get("messages", []):
                        if m.get("type") != "gemini": continue
                        ts = parse_ts(m.get("timestamp") or data.get("startTime"))
                        if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue
                        u = m.get("tokens", {}); inp, out, cr = u.get("input", 0), u.get("output", 0), u.get("cached", 0)
                        total = inp+out+cr
                        stats["gemini-cli"]["input"] += inp; stats["gemini-cli"]["output"] += out; stats["gemini-cli"]["cacheRead"] += cr; stats["gemini-cli"]["total"] += total
                        stats["gemini-cli"]["api_calls"] += 1; stats["gemini-cli"]["messages_count"] += 1
                        active_sessions["gemini-cli"].add(f.name)
            except: continue

    # 3. Claude Code
    if claude_dir.exists():
        for f in claude_dir.rglob("*.jsonl"):
            try:
                with open(f, "r") as fin:
                    for line in fin:
                        d = json.loads(line)
                        if d.get("type") != "assistant": continue
                        ts = parse_ts(d.get("timestamp"))
                        if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue
                        u = d.get("message", {}).get("usage", {})
                        inp, out, cr = u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0)
                        total = inp+out+cr
                        stats["claude-code"]["input"] += inp; stats["claude-code"]["output"] += out; stats["claude-code"]["cacheRead"] += cr; stats["claude-code"]["total"] += total
                        stats["claude-code"]["api_calls"] += 1; stats["claude-code"]["messages_count"] += 1
                        active_sessions["claude-code"].add(f.parent.name)
            except: continue

    # 4. Codex
    if codex_dir.exists():
        # 递归扫描 YYYY/MM/DD 结构
        for f in codex_dir.rglob("rollout-*.jsonl"):
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                    current_model = None
                    for line in fin:
                        try:
                            d = json.loads(line)
                            if d.get("type") == "turn_context":
                                current_model = _codex_model_from_payload(d.get("payload", {})) or current_model
                                continue
                            if d.get("type") == "event_msg":
                                p = d.get("payload", {})
                                if p.get("type") == "token_count":
                                    info = p.get("info")
                                    if not info: continue
                                    ts = parse_ts(d.get("timestamp"))
                                    if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue

                                    u = info.get("total_token_usage", {})
                                    inp, out, cr = u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cached_input_tokens", 0)
                                    total = inp+out+cr

                                    # Codex 统计去重逻辑：同一个 session 只取最后一条 token_count (累计值)
                                    # 但在按天统计时，我们取该时间窗口内的增量或最大值。
                                    # 为了简化，我们按 event 计数，但注意 Codex 的 rollout 是累计的。
                                    # 修正：按 message 增量计算（last_token_usage）
                                    last_u = info.get("last_token_usage", {})
                                    model_total = total
                                    if last_u:
                                        inp, out, cr = last_u.get("input_tokens", 0), last_u.get("output_tokens", 0), last_u.get("cached_input_tokens", 0)
                                        total = inp + out + cr
                                        model_input, model_cache_read, _ = normalize_cached_input_detail(
                                            input_tokens=inp,
                                            output_tokens=out,
                                            cache_read_tokens=cr,
                                            reported_total_tokens=last_u.get("total_tokens"),
                                        )
                                        model_total = model_input + out + model_cache_read

                                    stats["codex"]["input"] += inp; stats["codex"]["output"] += out; stats["codex"]["cacheRead"] += cr; stats["codex"]["total"] += total
                                    stats["codex"]["api_calls"] += 1; stats["codex"]["messages_count"] += 1
                                    active_sessions["codex"].add(f.name)
                                    model = _codex_model_from_payload(p) or current_model or _codex_model_from_context_window(info.get("model_context_window")) or "unknown"
                                    model_usage[model]["calls"] += 1; model_usage[model]["tokens"] += model_total
                        except: pass
            except: continue

    # 5. Hermes
    if hermes_db.exists():
        try:
            conn = sqlite3.connect(str(hermes_db)); cur = conn.cursor()
            cur.execute("SELECT started_at, input_tokens, output_tokens, cache_read_tokens, api_call_count, id FROM sessions")
            for r in cur.fetchall():
                ts = parse_ts(r[0])
                if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue
                inp, out, cr = (r[1] or 0), (r[2] or 0), (r[3] or 0)
                total = inp+out+cr
                stats["hermes"]["input"] += inp; stats["hermes"]["output"] += out; stats["hermes"]["cacheRead"] += cr; stats["hermes"]["total"] += total
                stats["hermes"]["api_calls"] += (r[4] or 1); stats["hermes"]["messages_count"] += (r[4] or 1)
                active_sessions["hermes"].add(r[5])
            conn.close()
        except: pass

    # 6. Normalized local runtimes
    normalized_runtimes = (
        (
            "opencode",
            OpenCodeRuntime(
                _configured_path(
                    "opencode",
                    "home",
                    default_external_tool_path("opencode", "home", HOME),
                    default_external_tool_path("opencode", "home", HOME),
                )
            ),
        ),
        (
            "antigravity",
            AntigravityRuntime(
                {
                    variant: _configured_path(
                        "antigravity",
                        key,
                        default_external_tool_path("antigravity", key, HOME),
                        default_external_tool_path("antigravity", key, HOME),
                    )
                    for variant, key in (
                        ("cli", "cliHome"),
                        ("ide", "ideHome"),
                        ("app", "appHome"),
                    )
                }
            ),
        ),
    )
    for tool_key, runtime in normalized_runtimes:
        try:
            for record in runtime.usage():
                ts = record.occurred_at.timestamp()
                if start_ts and not (start_ts <= ts < end_ts):
                    continue
                total = (
                    int(record.protocol_total_tokens)
                    if record.protocol_total_tokens is not None
                    else int(record.input_tokens + record.output_tokens + record.cache_read_tokens)
                )
                stats[tool_key]["input"] += int(record.input_tokens)
                stats[tool_key]["output"] += int(record.output_tokens)
                stats[tool_key]["cacheRead"] += int(record.cache_read_tokens)
                stats[tool_key]["cacheWrite"] += int(record.cache_write_tokens)
                stats[tool_key]["total"] += total
                stats[tool_key]["api_calls"] += int(record.message_count or 1)
                stats[tool_key]["messages_count"] += int(record.message_count or 1)
                active_sessions[tool_key].add(record.external_session_key)
                model = record.model_key or "unknown"
                model_usage[model]["calls"] += int(record.message_count or 1)
                model_usage[model]["tokens"] += total
        except Exception:
            continue

    # 7. OpenClaw Cron Runs
    cron_runs = _configured_path(
        "openclaw",
        "cronRunsRoot",
        default_external_tool_path("openclaw", "cronRunsRoot"),
        default_external_tool_path("openclaw", "cronRunsRoot"),
    )
    if cron_runs.exists():
        for f in _iter_cron_run_files(cron_runs):
            try:
                with open(f, "r") as fin:
                    for line in fin:
                        try:
                            d = json.loads(line)
                            if d.get("action") == "finished":
                                ts = parse_ts(d.get("ts"))
                                if start_ts and (not ts or not (start_ts <= ts < end_ts)): continue
                                u = d.get("usage", {})
                                if not u: continue
                                inp, out, cr = u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cacheRead", 0)
                                total = inp + out + cr
                                stats["cron"]["input"] += inp; stats["cron"]["output"] += out; stats["cron"]["cacheRead"] += cr; stats["cron"]["total"] += total
                                stats["cron"]["api_calls"] += 1; stats["cron"]["messages_count"] += 1
                                active_sessions["cron"].add(d.get("jobId"))
                        except: continue
            except: continue

    # ── 格式转换与汇总 ──
    final_stats = {}
    for s_id, v in stats.items():
        # 处理显示名称与来源标记
        display_name = s_id
        source = s_id
        if s_id.startswith("openclaw:"):
            display_name = s_id.split(":", 1)[1]
            source = "openclaw"

        final_stats[display_name] = {
            **v,
            "source": source,
            "active_sessions": len(active_sessions[s_id]),
            "sessions_total": len(active_sessions[s_id])
        }

    return final_stats, model_usage


def _iter_cron_run_files(root):
    seen = set()
    for pattern in ("*.jsonl", "*.jsonl.migrated"):
        for path in sorted(root.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            yield path

if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    res, _ = scan_tokens(d)
    print(json.dumps(res, indent=2, ensure_ascii=False))
