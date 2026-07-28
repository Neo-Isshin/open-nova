#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Unified AI Source Collector (v1.13 - Pure Governance)
执行 GEMINI.md 宪法法则 II：叙事脱水。
1. 动作抽象：将 thoughts 原文转化为精炼标签。
2. 来源提纯：严格对齐 HKT 04:00 窗口。
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
import json
import sqlite3
import re
from pathlib import Path
from datetime import datetime
from data_foundation.paths import load_paths
from data_foundation.session_files import is_openclaw_session_file
from data_foundation.settings import (
    default_external_tool_path,
    external_tool_path,
    external_tool_path_list,
)
from data_foundation.time import business_today, business_window, parse_timestamp, resolve_timezone

SOURCES = {
    "openclaw": {"tool": "openclaw", "key": "agentsRoot", "pattern": "*.jsonl*", "engine": "openclaw_agents"},
    "claude-code": {"tool": "claudeCode", "key": "projectsRoot", "pattern": "**/*.jsonl", "engine": "jsonl_stream"},
    "gemini-cli":  {"tool": "geminiCli", "key": "chatsRoot", "pattern": "*.json*", "engine": "jsonl_stream"},
    "hermes":      {"tool": "hermes", "key": "sessionsRoot", "pattern": "*.json*", "engine": "json_messages"},
    "codex":       {"tool": "codex", "key": "sessionsRoot", "pattern": "rollout-*.jsonl", "engine": "jsonl_stream"},
    "opencode":    {"engine": "runtime_records"},
    "antigravity": {"engine": "runtime_records"},
    "cursor":      {"engine": "runtime_records"},
}
def _diary_root() -> Path:
    return load_paths().diary_dir

def source_path(cfg):
    tool = cfg.get("tool")
    key = cfg.get("key")
    if not tool or not key:
        raise ValueError("source config must declare externalTools tool/key")
    try:
        return external_tool_path(tool, key)
    except Exception:
        return default_external_tool_path(tool, key)

def get_hkt_window(target_date):
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    start_utc, end_utc = business_window(target)
    return start_utc.timestamp(), end_utc.timestamp() - start_utc.timestamp()

def parse_ts_any(val):
    if not val: return None
    try:
        if isinstance(val, (int, float)): return val / 1000.0 if val > 1e12 else val
        parsed = parse_timestamp(val)
        return parsed.timestamp() if parsed else None
    except: return None

def format_local_hhmm(ts):
    return datetime.fromtimestamp(ts, tz=resolve_timezone()).strftime("%H:%M")

# 文件名排除关键词（避免重复/临时/索引文件进入对话归档）
_EXCLUDE_PATTERNS = (".checkpoint.", ".trajectory", ".lock", ".tmp", "sessions.json")

def is_session_file(fname):
    if ".json" not in fname:
        return False
    for pat in _EXCLUDE_PATTERNS:
        if pat in fname:
            return False
    return True

def extract_text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text_parts = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text"}:
            text_parts.append(str(block.get("text", "")))
    return "\n".join([part for part in text_parts if part.strip()])

def strip_thinking(text):
    text = re.sub(r'<(thinking|思考)>[\s\S]*?</\1>', '', str(text))
    if 'Thought:' in text or '思考:' in text:
        text = re.sub(r'Thought:[\s\S]*?\n', '[动作: 内部思考] ', text)
    text = re.sub(r'【[\s\S]*?】', '', text)
    text = re.sub(r'<!--[\s\S]*?-->', '', text)
    return text.strip()

def extract_openclaw_dialogue(entry):
    if is_cron_entry(entry):
        return None
    if entry.get("type") != "message":
        return None
    msg = entry.get("message") or {}
    if not isinstance(msg, dict):
        return None
    role = msg.get("role", "")
    if role in ("toolResult", "tool"):
        return None
    content = strip_thinking(extract_text_content(msg.get("content", "")))
    if not content:
        return None
    ts = parse_ts_any(entry.get("timestamp") or msg.get("timestamp"))
    return {"role": role, "time": format_local_hhmm(ts) if ts else "", "content": content}

def is_cron_entry(entry):
    if entry.get("source") == "cron" or str(entry.get("sessionId", "")).startswith("cron-"):
        return True
    msg = entry.get("message") or {}
    if not isinstance(msg, dict):
        return False
    for text in _iter_text_blocks(msg.get("content", [])):
        if text.strip().startswith("[cron:"):
            return True
    return False

def _iter_text_blocks(content):
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, str):
            yield block
        elif isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}:
            yield str(block.get("text", ""))

def summarize_tool_call(name, args):
    if not name:
        return ""
    arg_snippet = ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            arg_snippet = args[:160]
    if isinstance(args, dict):
        for key in ("cmd", "command", "query", "path", "file_path", "url", "search", "pattern"):
            if args.get(key):
                arg_snippet = str(args[key])[:160]
                break
        if not arg_snippet:
            for value in args.values():
                if isinstance(value, str) and value.strip():
                    arg_snippet = value[:160]
                    break
    return f"[动作: {name}] {arg_snippet}".strip()

def is_codex_runtime_metadata(content):
    text = str(content or "").strip()
    return (
        text.startswith("<environment_context>")
        or text.startswith("<permissions instructions>")
        or text.startswith("<turn_aborted>")
        or text.startswith("<developer")
        or text.startswith("<system")
    )

def normalize_codex_content(content):
    text = str(content or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_codex_noise_text(content):
    text = str(content or "").strip()
    if not text:
        return True
    if is_codex_runtime_metadata(text):
        return True
    if text.startswith("[动作:"):
        return True
    if "Sender (untrusted metadata)" in text:
        return True
    if "base_instructions" in text and "developer_instructions" in text:
        return True
    if re.match(r"^\s*[\[{]", text) and len(text) > 240:
        return True
    return False

def extract_codex_dialogue(d):
    """
    Convert Codex rollout records into narrative-safe dialogue entries.

    Codex rollout logs are OpenAI-style event streams, not plain chat
    transcripts. Keep only explicit user/assistant text messages. Tool calls,
    compacted history, turn context, token counts and runtime metadata are not
    dialogue and must not enter _filtered.
    """
    payload = d.get("payload") or {}
    record_type = d.get("type")

    if record_type == "response_item":
        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role") or "system"
            if role not in {"user", "assistant"}:
                return None
            content = extract_text_content(payload.get("content"))
            if is_codex_noise_text(content):
                return None
            return {"role": role, "content": content}
        return None

    if record_type == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            content = payload.get("message") or ""
            if is_codex_noise_text(content):
                return None
            return {"role": "user", "content": str(content)} if str(content).strip() else None
        if payload_type == "agent_message":
            phase = payload.get("phase")
            if phase and phase != "final_answer":
                return None
            content = payload.get("message") or ""
            if is_codex_noise_text(content):
                return None
            return {"role": "assistant", "content": str(content)} if str(content).strip() else None
        return None

    return None

def abstract_thought(thought_obj):
    """
    将复杂的 thoughts 转化为精炼的动作标签。
    依据 GEMINI.md 法则 II：thoughts 严禁原文录入叙事流。
    基于实际 5806+ 条 thought subjects 统计归类。
    """
    if not isinstance(thought_obj, dict): return ""
    subj = thought_obj.get("subject", "").lower()
    desc = thought_obj.get("description", "").lower()
    combined = subj + " " + desc

    # ── 动词优先匹配（按频率排序）──
    if any(w in subj for w in ("analyzing", "examining", "investigating", "dissecting")):
        return "[动作: 分析]"
    if any(w in subj for w in ("implementing", "coding", "building", "constructing", "developing", "engineering", "scripting")):
        return "[动作: 编码]"
    if any(w in subj for w in ("fixing", "correcting", "debugging", "resolving", "rectifying", "troubleshooting")):
        return "[动作: 修复]"
    if any(w in subj for w in ("reading", "inspecting", "scrutinizing")):
        return "[动作: 读取]"
    if any(w in subj for w in ("writing", "modifying", "updating", "editing", "adjusting", "revising", "refactoring", "refining", "improving")):
        return "[动作: 修改]"
    if any(w in subj for w in ("searching", "finding", "locating", "exploring", "navigating", "discovering", "scouting")):
        return "[动作: 搜索]"
    if any(w in subj for w in ("reviewing", "evaluating", "assessing", "reassessing")):
        return "[动作: 审查]"
    if any(w in subj for w in ("planning", "formulating", "designing", "outlining", "defining", "mapping", "charting")):
        return "[动作: 规划]"
    if any(w in subj for w in ("testing", "verifying", "validating", "confirming", "checking")):
        return "[动作: 验证]"
    if any(w in subj for w in ("removing", "deleting", "clearing", "cleaning")):
        return "[动作: 删除]"
    if any(w in subj for w in ("executing", "running", "initiating", "deploying", "starting", "restarting", "switching")):
        return "[动作: 执行]"
    if any(w in subj for w in ("integrating", "connecting", "combining", "merging", "synchronizing", "unifying")):
        return "[动作: 集成]"
    if any(w in subj for w in ("extracting", "processing", "collecting", "filtering", "aggregating", "parsing")):
        return "[动作: 处理数据]"
    if any(w in subj for w in ("generating", "creating", "producing", "compiling", "summarizing")):
        return "[动作: 生成]"
    if any(w in subj for w in ("restoring", "reverting", "recovering", "resetting", "reconstructing")):
        return "[动作: 回滚]"
    if any(w in subj for w in ("optimizing", "simplifying", "compressing", "reorganizing")):
        return "[动作: 优化]"
    if any(w in subj for w in ("considering", "reflecting", "contemplating", "rethinking", "revisiting", "re-evaluating")):
        return "[动作: 思考]"
    if any(w in subj for w in ("identifying", "pinpointing", "detecting", "recognizing", "isolating", "tracing")):
        return "[动作: 定位问题]"
    if any(w in subj for w in ("comparing", "differentiating", "reconciling")):
        return "[动作: 对比]"
    if any(w in subj for w in ("organizing", "structuring", "standardizing", "consolidating")):
        return "[动作: 整理]"
    if any(w in subj for w in ("monitoring", "observing", "tracking")):
        return "[动作: 监控]"
    if any(w in subj for w in ("interpreting", "clarifying", "understanding", "deciphering", "grasping")):
        return "[动作: 理解]"
    if any(w in subj for w in ("transitioning", "continuing", "resuming", "moving", "wrapping", "concluding", "finalizing", "completing")):
        return "[动作: 收尾]"

    return "[动作: 内部操作]"

def collect_engine(name, cfg, target_date):
    start_ts, duration = get_hkt_window(target_date); end_ts = start_ts + duration
    if cfg["engine"] == "runtime_records":
        return collect_runtime_records(name, target_date, start_ts, end_ts)
    path = source_path(cfg)
    if not path.exists(): return 0
    if cfg["engine"] == "openclaw_agents":
        return collect_openclaw_agents(path, cfg, target_date, start_ts, end_ts)
    daily_unified = []
    seen_entries = set()

    if cfg["engine"] == "jsonl_stream":
        for fpath in path.rglob(cfg["pattern"]):
            if not is_session_file(fpath.name): continue
            try:
                with open(fpath, "r", errors="ignore") as f:
                    first_char = f.read(1)
                    f.seek(0)
                    if first_char == '{':
                        for line in f:
                            line = line.strip()
                            if not line: continue
                            try:
                                d = json.loads(line); ts = parse_ts_any(d.get("timestamp") or d.get("ts"))
                                if ts and start_ts <= ts < end_ts:
                                    # ── Cron 铁幕：跳过 cron 来源 ──
                                    if d.get("source") == "cron" or str(d.get("sessionId", "")).startswith("cron-"):
                                        continue

                                    if name == "codex":
                                        codex_entry = extract_codex_dialogue(d)
                                        if not codex_entry:
                                            continue
                                        msg = codex_entry
                                    else:
                                        msg = d.get("message") or d
                                    raw_msg_content = msg.get("content") or msg.get("text") or ""
                                    # ── Cron 铁幕 II：跳过注入到普通 session 的 cron 消息 ──
                                    _cron_skip = False
                                    if isinstance(raw_msg_content, str) and raw_msg_content.strip().startswith("[cron:"):
                                        _cron_skip = True
                                    elif isinstance(raw_msg_content, list):
                                        for block in raw_msg_content:
                                            if isinstance(block, dict) and block.get("type") == "text":
                                                if str(block.get("text", "")).strip().startswith("[cron:"):
                                                    _cron_skip = True; break
                                    if _cron_skip: continue

                                    role = msg.get("role") or d.get("type") or "system"

                                    # ── 叙事脱水：只留对话点 + 动作标签 ──
                                    content_parts = []

                                    # 1. 处理 Thoughts (抽象化)
                                    thoughts = d.get("thoughts", [])
                                    if isinstance(thoughts, list):
                                        actions = [abstract_thought(th) for th in thoughts if abstract_thought(th)]
                                        if actions: content_parts.append(" ".join(list(dict.fromkeys(actions)))) # 去重

                                    # 2. 处理 Content (对话原文，排除 thinking 块)
                                    raw_content = msg.get("content") or msg.get("text") or ""
                                    content_text = extract_text_content(raw_content)

                                    # 物理屏蔽 <thinking> 标签内容 (针对某些工具原生输出)
                                    content_text = re.sub(r'<(thinking|思考)>[\s\S]*?</\\1>', '', content_text).strip()
                                    if content_text: content_parts.append(content_text)

                                    # 3. 处理 ToolCalls (提取工具名+参数作为动作记录)
                                    tool_calls = d.get("toolCalls") or []
                                    if isinstance(tool_calls, list) and tool_calls:
                                        tc_summaries = []
                                        for tc in tool_calls:
                                            if not isinstance(tc, dict): continue
                                            tc_name = tc.get("name", "")
                                            tc_args = tc.get("args", {})
                                            tc_desc = tc.get("description", "")
                                            if tc_name:
                                                # 构建动作摘要
                                                if isinstance(tc_args, dict) and tc_args:
                                                    # 提取关键参数（优先 command, query, path, file_path 等）
                                                    key_arg_keys = ("command", "query", "path", "file_path", "url", "search", "pattern")
                                                    arg_snippet = ""
                                                    for k in key_arg_keys:
                                                        if k in tc_args and tc_args[k]:
                                                            arg_snippet = str(tc_args[k])[:120]
                                                            break
                                                    if not arg_snippet:
                                                        # 兜底：取第一个 string/number 值
                                                        for v in tc_args.values():
                                                            if isinstance(v, str) and v.strip():
                                                                arg_snippet = v[:120]; break
                                                    if arg_snippet:
                                                        tc_summaries.append(f"[动作: {tc_name}] {arg_snippet}")
                                                    else:
                                                        tc_summaries.append(f"[动作: {tc_name}]")
                                                else:
                                                    tc_summaries.append(f"[动作: {tc_name}]")
                                        if tc_summaries:
                                            content_parts.append("\n".join(tc_summaries))

                                    final_content = "\n".join(content_parts).strip()
                                    if final_content:
                                        hhmm = format_local_hhmm(ts)
                                        if name == "codex":
                                            entry_key = (role, normalize_codex_content(final_content))
                                        else:
                                            entry_key = (role, hhmm, final_content)
                                        if entry_key not in seen_entries:
                                            seen_entries.add(entry_key)
                                            daily_unified.append({"role": role, "content": final_content, "time": hhmm, "agent": name})
                            except: continue
            except: continue

    elif cfg["engine"] == "json_messages":
        for fpath in path.glob(cfg["pattern"]):
            if not is_session_file(fpath.name): continue
            try:
                with open(fpath, "r") as f: data = json.load(f)
                all_raw_msgs = data.get("messages", []) + data.get("transcript", [])
                if isinstance(data, list): all_raw_msgs += data

                for msg in all_raw_msgs:
                    ts = parse_ts_any(msg.get("timestamp") or msg.get("ts") or data.get("startTime"))
                    if ts and start_ts <= ts < end_ts:
                        # ── Cron 铁幕：跳过 cron 来源 ──
                        if msg.get("source") == "cron" or str(msg.get("sessionId", "")).startswith("cron-"):
                            continue

                        raw_msg_content = msg.get("content") or msg.get("text") or ""
                        # ── Cron 铁幕 II：跳过注入到普通 session 的 cron 消息 ──
                        _skip = False
                        if isinstance(raw_msg_content, str) and raw_msg_content.strip().startswith("[cron:"):
                            _skip = True
                        elif isinstance(raw_msg_content, list):
                            for block in raw_msg_content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    if str(block.get("text", "")).strip().startswith("[cron:"):
                                        _skip = True; break
                        if _skip: continue

                        role = msg.get("role") or msg.get("type") or "system"
                        content = raw_msg_content
                        if isinstance(content, list):
                            # 只提取 text 类型，跳过 thinking 类型
                            content = "\n".join([str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"])

                        content = re.sub(r'<(thinking|思考)>[\s\S]*?</\1>', '', str(content)).strip()
                        if content:
                            hhmm = format_local_hhmm(ts)
                            entry_key = (role, hhmm, content)
                            if entry_key not in seen_entries:
                                seen_entries.add(entry_key)
                                daily_unified.append({"role": role, "content": content, "time": hhmm, "agent": name})
            except: continue

    if daily_unified:
        diary_root = _diary_root()
        out_dir = diary_root / "__diary_daily" / target_date / name
        flt_dir = diary_root / "__diary_daily" / target_date / "_filtered" / name
        out_dir.mkdir(parents=True, exist_ok=True); flt_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "unified_daily.jsonl", "w") as f1, open(flt_dir / "unified_daily.jsonl", "w") as f2:
            for e in daily_unified:
                f1.write(json.dumps(e, ensure_ascii=False) + "\n")
                f2.write(json.dumps({"role": e["role"], "content": e["content"], "time": e["time"]}, ensure_ascii=False) + "\n")
    return len(daily_unified)


def collect_runtime_records(name, target_date, start_ts, end_ts):
    """Collect narrative-safe records from normalized local runtime parsers."""

    runtime = _local_runtime(name)
    if runtime is None:
        return 0
    daily_unified: list[tuple[float, int, dict]] = []
    seen_entries = set()
    try:
        records = runtime.dialogue()
        for source_order, record in enumerate(records):
            occurred_at = record.occurred_at
            if occurred_at is None:
                continue
            ts = occurred_at.timestamp()
            if not (start_ts <= ts < end_ts):
                continue
            role = str(record.role or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = strip_thinking(record.content)
            if not content:
                continue
            key = (record.external_message_key, role, content)
            if key in seen_entries:
                continue
            seen_entries.add(key)
            daily_unified.append(
                (
                    ts,
                    source_order,
                    {
                        "role": role,
                        "content": content,
                        "time": format_local_hhmm(ts),
                        "agent": name,
                        "source": name,
                        "sourceVariant": record.source_variant,
                        "session": record.external_session_key,
                    },
                )
            )
    except Exception:
        return 0
    if not daily_unified:
        return 0
    daily_unified.sort(key=lambda item: (item[0], item[1]))
    diary_root = _diary_root()
    out_dir = diary_root / "__diary_daily" / target_date / name
    flt_dir = diary_root / "__diary_daily" / target_date / "_filtered" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    flt_dir.mkdir(parents=True, exist_ok=True)
    with (
        (out_dir / "unified_daily.jsonl").open("w", encoding="utf-8") as raw_handle,
        (flt_dir / "unified_daily.jsonl").open("w", encoding="utf-8") as filtered_handle,
    ):
        for _timestamp, _source_order, entry in daily_unified:
            raw_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            filtered_handle.write(
                json.dumps(
                    {
                        "role": entry["role"],
                        "content": entry["content"],
                        "time": entry["time"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(daily_unified)


def _local_runtime(name):
    if name == "opencode":
        from data_foundation.runtime_sources import OpenCodeRuntime

        return OpenCodeRuntime(_configured_external_path("opencode", "home"))
    if name == "antigravity":
        from data_foundation.runtime_sources import AntigravityRuntime

        return AntigravityRuntime(
            {
                "cli": _configured_external_path("antigravity", "cliHome"),
                "ide": _configured_external_path("antigravity", "ideHome"),
                "app": _configured_external_path("antigravity", "appHome"),
            }
        )
    if name == "cursor":
        from data_foundation.runtime_sources import CursorRuntime

        return CursorRuntime(
            _configured_external_path("cursor", "home"),
            ide_state_dbs=_configured_external_path_list("cursor", "ideStateDbCandidates"),
            workspace_storage_roots=_configured_external_path_list("cursor", "workspaceStorageRoots"),
        )
    return None


def _configured_external_path(tool, key):
    try:
        return external_tool_path(tool, key)
    except Exception:
        return default_external_tool_path(tool, key)


def _configured_external_path_list(tool, key):
    try:
        return external_tool_path_list(tool, key)
    except Exception:
        return []

def collect_openclaw_agents(path, cfg, target_date, start_ts, end_ts):
    total = 0
    pattern = cfg.get("pattern") or "*.jsonl*"
    for agent_dir in sorted(directory for directory in path.iterdir() if directory.is_dir()):
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        daily_unified = []
        seen_entries = set()
        for fpath in sorted(sessions_dir.glob(pattern)):
            if not is_openclaw_session_file(fpath.name):
                continue
            entries = []
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except Exception:
                            continue
                        ts = parse_ts_any(payload.get("timestamp") or (payload.get("message") or {}).get("timestamp"))
                        if ts and start_ts <= ts < end_ts:
                            entries.append(payload)
            except Exception:
                continue
            if not entries or any(is_cron_entry(entry) for entry in entries):
                continue
            for entry in entries:
                dialogue = extract_openclaw_dialogue(entry)
                if not dialogue:
                    continue
                key = (dialogue["role"], dialogue["time"], dialogue["content"])
                if key in seen_entries:
                    continue
                seen_entries.add(key)
                daily_unified.append(dialogue)
        if daily_unified:
            diary_root = _diary_root()
            out_dir = diary_root / "__diary_daily" / target_date / agent_dir.name
            flt_dir = diary_root / "__diary_daily" / target_date / "_filtered" / agent_dir.name
            out_dir.mkdir(parents=True, exist_ok=True)
            flt_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / "unified_daily.jsonl", "w") as f1, open(flt_dir / "unified_daily.jsonl", "w") as f2:
                for entry in daily_unified:
                    raw = {**entry, "agent": agent_dir.name, "source": "openclaw"}
                    f1.write(json.dumps(raw, ensure_ascii=False) + "\n")
                    f2.write(json.dumps(entry, ensure_ascii=False) + "\n")
            total += len(daily_unified)
    return total

if __name__ == "__main__":
    import sys; d = sys.argv[1] if len(sys.argv) > 1 else business_today().isoformat()
    total = sum(collect_engine(n, c, d) for n, c in SOURCES.items())
    print(f"🏁 Final Complete Sync: {total} items captured (Action Abstracted).")
