#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import re
import sqlite3
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from collections import defaultdict, Counter
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from data_foundation.paths import load_paths
from data_foundation.settings import default_external_tool_path, external_tool_path, resolve_llm_provider, resolve_runtime_source
from data_foundation.llm_execution import ProviderChainError, execute_llm_message
from data_foundation.time import business_today, business_window
from data_foundation.weather import fetch_weather_for_date
from data_foundation.diary_paths import (
    diary_learning_report_path,
    diary_markdown_paths,
    diary_no_activity_report_path,
    diary_narrative_report_path,
    diary_technical_report_path,
)

_LLM_PROVIDER = resolve_llm_provider(redact_secrets=True)
THINKING_MODE = os.getenv("LLM_THINKING_MODE", "off").strip().lower()
def _runtime_diary_root() -> Path:
    return load_paths().diary_dir


def _thinking_instruction():
    if THINKING_MODE == "low":
        return "\n推理强度：low。任务是归纳整理，不需要深度多步推理；优先直接提炼事实与结构。"
    if THINKING_MODE == "medium":
        return "\n推理强度：medium。只在归并冲突信息和判断主线时使用适度推理，避免冗长思考。"
    if THINKING_MODE in {"off", "disabled", "disable"}:
        return "\n推理强度：off。不要进行深度思考或展开推理过程；直接完成摘要、归纳和格式化。"
    return ""


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


PIPELINE_CONCURRENCY = max(1, min(_positive_int(_LLM_PROVIDER.get("pipelineConcurrency"), 3), 16))
PIPELINE_GATE_TOKENS = max(1000, _positive_int(_LLM_PROVIDER.get("pipelineGateTokens"), 30000))

try:
    import tiktoken
except ImportError:
    tiktoken = None

# ===================== PROMPT SETTINGS (STRICTLY UNCHANGED) =====================

PROMPT_PARTIAL = """你是一个专业的技术日志提炼助手。请根据提供的日志数据（{agent_info}，时间范围限当日04:00至次日04:00HKT），提取核心内容。

【要求】
1. 提取任务描述、执行步骤、错误信息、性能数据和重要提醒。
2. 重要提醒提炼：必须包含严重性判定（严重/中等/低），并描述现象与潜在风险。
3. 必须保留具体的技术细节（如代码路径、修复逻辑、具体错误堆栈）。

日志数据：
{raw_text}
"""

PROMPT_INTEGRATION = """你是一个专业的技术日记整合助手。请将提供的不同Agent日志摘要整合为篇高质量日记。

【严格排版规则】
1. 输出必须包含以下5个二级标题：
## 今日概要
## Agent工作
## 重要提醒
## 定时任务情况
## 备注

2. 今日概要 [核心主权/严格嵌套模式]
   - 必须客观提炼全天的核心战略、架构决策和关键进展。
   - 【强制格式】：
     * 每条主干必须以 `* **[核心标题]**` 开头，后跟精炼的主叙述。
     * 每个主干下方必须嵌套至少 2-3 条以 `-` 开头的子条目，用于详细补充技术细节、哈希值或具体操作。
   - 【示例】：
     * **系统核心架构升级**：完成从 v5.0 到 v6.0 的平滑演进。
       - 核心变更：引入分布式统计引擎，支持多节点并发扫描。
       - 性能优化：单次请求延迟降低 40%，内存占用下降 150MB。


3. Agent工作 [时序硬核模式]
   - 必须按Agent分类 (### 【Agent名称】)，名称后严禁跟任何总结性文字。
   - 内部按时间段划分子结构，格式强制要求为：**[标签 HH:MM-HH:MM] - 业务总结**。
   - 时间段标签：凌晨 (00:00-04:00)、上午 (04:00-12:00)、下午 (12:00-18:00)、晚上 (18:00-24:00)。
   - 示例：**[凌晨 00:00-04:00] - 架构设计与调研**
   - 【严禁】：禁止使用“深夜”、“晚间”、“全天”等不规范标签。禁止在方括号内嵌套圆括号。
   - 列表项：必须且仅能使用 - 作为具体工作项的标识。

4. 重要提醒 [分级预警模式]
   - 按严重性 (严重 > 中等 > 低) 排序。格式：
   1. **【等级】— [标题]**
      - 现象: [具体异常表现]
      - 风险: [潜在影响]
      - 建议: [后续操作]
   无提醒则写"无"。

5. 定时任务情况: 列表展示所有定时任务执行结果。无则写"无"。

局部日志摘要数据：
{raw_text}
"""

# ===================== CORE UTILS =====================

def parse_ts(val):
    if not val: return None
    try:
        if isinstance(val, (int, float)): return val / 1000.0 if val > 1e12 else val
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except: return None

def get_hkt_window(target_date):
    target = date.fromisoformat(target_date)
    start_utc, end_utc = business_window(target)
    return start_utc.timestamp(), int((end_utc - start_utc).total_seconds())

def get_token_count(text):
    if tiktoken:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except: pass
    return len(text) // 2


def _llm_chunk_id(label):
    raw = str(label or "narrative call").strip()
    slug = re.sub(r"[^\w.-]+", "-", raw, flags=re.UNICODE).strip("-_.").casefold()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{(slug[:80] or 'narrative-call')}-{digest}"

def call_llm(prompt, is_int=False, label=None, max_tokens=None):
    sys_content = "你是一个专业的AI技术日记助手。"
    if is_int: sys_content = "你是一个专业的技术日记整合助手。直接从'## 今日概要'开始输出。"
    sys_content += _thinking_instruction()
    call_label = label or ("final integration" if is_int else "partial")
    token_estimate = get_token_count(prompt)
    output_budget = max_tokens or (16384 if is_int else 6144)
    started = time.time()
    print(
        f"   [LLM-START] {call_label}: prompt≈{token_estimate:,} tokens, max_tokens={output_budget}",
        flush=True,
    )
    try:
        content = execute_llm_message(
            system=sys_content,
            prompt=prompt,
            temperature=0.05,
            max_tokens=output_budget,
            thinking_mode=THINKING_MODE,
            paths=load_paths(),
            pass_id="narrative",
            label=call_label,
            chunk_id=_llm_chunk_id(call_label),
        ).text.strip()
        cleaned = re.sub(r'<(think|思考)>[\s\S]*?</\1>|```json[\s\S]*?```', '', content).strip()
        print(
            f"   [LLM-END] {call_label}: {time.time() - started:.1f}s, chars={len(cleaned):,}",
            flush=True,
        )
        return cleaned
    except Exception as e:
        print(f"   [LLM-ERROR] {call_label}: {time.time() - started:.1f}s, {e}", flush=True)
        raise

def load_filtered_entries(date_str):
    base_dir = _runtime_diary_root() / "__diary_daily" / date_str / "_filtered"
    all_entries = {}
    if not base_dir.exists(): return all_entries
    for agent in os.listdir(base_dir):
        path = base_dir / agent
        if path.is_dir():
            ents = []
            for f in sorted(os.listdir(path)):
                if f.endswith('.jsonl'):
                    with open(path / f, "r", encoding="utf-8") as fin:
                        for line in fin:
                            try: ents.append(json.loads(line))
                            except: pass
            if ents: all_entries[agent] = ents
    return all_entries

def extract_top_topics(all_entries_dict):
    words = []
    stop_words = {"this", "that", "with", "from", "system", "agent", "task", "done", "success", "file", "path", "user", "assistant", "local", "command", "skill", "usage", "commands", "running", "response", "unknown", "caveat", "message", "messages"}
    for agent, entries in all_entries_dict.items():
        for e in entries:
            if e.get("role") not in ("user", "assistant"): continue
            content = str(e.get("content", ""))
            content = re.sub(r'<local-command-caveat>.*?</local-command-caveat>', '', content, flags=re.DOTALL)
            content = re.sub(r'Sender \(untrusted metadata\):.*?```json.*?```', '', content, flags=re.DOTALL)
            found = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-z0-9\-]{3,25}', content.lower())
            words.extend([w for w in found if w not in stop_words and not re.match(r'^[0-9\-]+$', w)])
    counts = Counter(words).most_common(12)
    return [{"topic": w[0], "count": w[1]} for w in counts if len(w[0]) > 1][:10]

# ===================== CORE LOGIC: STATS =====================

def _calculate_stats_legacy(date_str):
    """通过权威 Token Engine 获取统计数据并归类（支持追溯）"""
    import sys, os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from ai_assets_center.token_engine import scan_tokens

    # 获取指定日期的脱水统计
    engine_stats, model_usage = scan_tokens(date_str)

    all_stats = defaultdict(lambda: defaultdict(int))
    # 映射回日记生成器期望的 Provider 聚合结构
    for name, s in engine_stats.items():
        # 根据 source 进行聚合：openclaw(含多个agent), gemini-cli, claude-code, etc.
        source = s.get("source", name)

        all_stats[source]["input_tokens"] += s["input"]
        all_stats[source]["output_tokens"] += s["output"]
        all_stats[source]["cache_read"] += s["cacheRead"]
        all_stats[source]["total_tokens"] += s["total"]
        all_stats[source]["api_calls"] += s["api_calls"]
        all_stats[source]["messages_count"] += s["messages_count"]
        all_stats[source]["active_sessions"] += s["active_sessions"]
        all_stats[source]["sessions_total"] += s["sessions_total"]

    # 汇总 Total
    tot = defaultdict(int)
    for source in ["openclaw", "gemini-cli", "claude-code", "hermes", "codex", "opencode", "antigravity", "cron"]:
        for k, v in (all_stats.get(source) or {}).items(): tot[k] += v
    all_stats["total"] = tot

    # 映射模型分布
    all_stats["model_usage_list"] = [{"model": m, "calls": d["calls"], "tokens": d["tokens"]} for m, d in model_usage.items()]
    return all_stats


def _calculate_stats_foundation(date_str):
    try:
        foundation_src = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if foundation_src not in sys.path:
            sys.path.append(foundation_src)
        from data_foundation.aggregate import daily_diary_usage_metrics
        from data_foundation.paths import load_paths

        return daily_diary_usage_metrics(load_paths(), datetime.strptime(date_str, "%Y-%m-%d").date())
    except Exception as exc:
        print(f"   WARNING: Foundation diary metrics unavailable: {exc}")
        return None


def calculate_stats_raw(date_str):
    """Read daily token/model metrics without changing the rendered diary contract."""
    if resolve_runtime_source("DIARY_METRICS_SOURCE", load_paths()) == "foundation":
        metrics = _calculate_stats_foundation(date_str)
        if metrics is not None:
            return metrics
        raise RuntimeError(f"Foundation diary metrics missing for {date_str}; run Foundation refresh/backfill before generation.")
    return _calculate_stats_legacy(date_str)


def _empty_stats():
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read",
        "total_tokens",
        "api_calls",
        "messages_count",
        "active_sessions",
        "sessions_total",
    )
    stats = {source: {key: 0 for key in keys} for source in ["openclaw", "gemini-cli", "claude-code", "hermes", "codex", "cron", "total"]}
    stats["model_usage_list"] = []
    return stats

# ===================== COMPONENT SCRAPERS =====================

def get_lessons_structured(date_str):
    root = _runtime_diary_root()
    wf = next(iter(diary_markdown_paths(root, date_str, "智慧沉淀-*.md")), diary_learning_report_path(root, date_str))
    ls = []
    if wf.exists():
        cnt = wf.read_text(); ms = re.findall(r'- \*\*【(.+?)】\*\*: (.+?): (.+)', cnt)
        for a, p, s in ms: ls.append({"agent": a, "problem": p, "suggestion": s})
    return ls

def get_new_skills_structured(date_str):
    root = _runtime_diary_root()
    tf = next(iter(diary_markdown_paths(root, date_str, "技术进展-*.md")), diary_technical_report_path(root, date_str))
    sk = []
    if tf.exists():
        cnt = tf.read_text(); sn = re.search(r'### 新技能\n(.*?)\n#', cnt, re.DOTALL) or re.search(r'### 新技能\n(.*)', cnt, re.DOTALL)
        if sn: sk = [s.strip('- ').strip() for s in sn.group(1).strip().split('\n') if s.strip()]
    return sk

def get_cron_structured(date_str):
    from ai_assets_center import cron_run_reporter
    try:
        raw = cron_run_reporter.generate_cron_report(date_str); tsks = []
        for l in raw.split('\n'):
            if '|' in l and '`' in l:
                ps = [p.strip() for p in l.split('|')]
                if len(ps) >= 6: tsks.append({"time": ps[1], "taskId": ps[2].replace('`', ''), "status": "Success" if "OK" in ps[3] else "Failed", "duration": ps[4], "conclusion": ps[5]})
        return tsks
    except: return []

def render_cron_tasks_section(cron_tasks):
    if not cron_tasks:
        return "无"
    lines = ["| 时间 | 任务ID | 状态 | 耗时 | 执行结论 |", "| :--- | :--- | :--- | :--- | :--- |"]
    for task in cron_tasks:
        lines.append(
            f"| {task.get('time', '')} | `{task.get('taskId', '')}` | {task.get('status', '')} | "
            f"{task.get('duration', '')} | {task.get('conclusion', '')} |"
        )
    return "\n".join(lines)

def _get_task_board_snapshot_legacy():
    try:
        bp = load_paths().task_board_path
    except Exception:
        bp = _runtime_diary_root() / "TASK_BOARD.md"
    st = {"InProgress": 0, "Completed": 0}
    if bp.exists():
        cnt = bp.read_text(); st["InProgress"] = len(re.findall(r'\[\s*\]', cnt)); st["Completed"] = len(re.findall(r'\[x\]', cnt))
    return st

def _get_task_board_snapshot_foundation(date_str):
    try:
        foundation_src = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if foundation_src not in sys.path:
            sys.path.append(foundation_src)
        from data_foundation.paths import load_paths
        from data_foundation.snapshots import read_diary_tasks_snapshot

        snapshot = read_diary_tasks_snapshot(load_paths(), datetime.strptime(date_str, "%Y-%m-%d").date())
        return snapshot["payload"] if snapshot is not None else None
    except Exception as exc:
        print(f"   WARNING: Foundation diary tasks snapshot unavailable: {exc}")
        return None

def get_task_board_snapshot(date_str):
    if resolve_runtime_source("DIARY_TASKS_SOURCE", load_paths()) == "foundation":
        tasks = _get_task_board_snapshot_foundation(date_str)
        if tasks is not None:
            return tasks
        raise RuntimeError(f"Foundation diary task snapshot missing for {date_str}; run Foundation refresh/backfill before generation.")
    return _get_task_board_snapshot_legacy()

def _get_memory_stats_legacy():
    try:
        md = external_tool_path("openclaw", "agentsRoot")
    except Exception:
        md = default_external_tool_path("openclaw", "agentsRoot")
    memory = {"sessionFiles": 0, "totalSizeMB": 0, "diaryCount": 0}
    if md.exists():
        c, s = 0, 0
        for r, _, fs in os.walk(md):
            for f in fs:
                if f.endswith('.jsonl'): c += 1; s += os.path.getsize(os.path.join(r, f))
        diary_root = _runtime_diary_root()
        memory["sessionFiles"] = c; memory["totalSizeMB"] = round(s/(1024*1024), 2); memory["diaryCount"] = len([d for d in os.listdir(diary_root) if d.startswith("diary-")])
    return memory


def _get_memory_stats_foundation(date_str):
    try:
        foundation_src = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if foundation_src not in sys.path:
            sys.path.append(foundation_src)
        from data_foundation.paths import load_paths
        from data_foundation.snapshots import read_diary_memory_snapshot

        snapshot = read_diary_memory_snapshot(load_paths(), datetime.strptime(date_str, "%Y-%m-%d").date())
        return snapshot["payload"] if snapshot is not None else None
    except Exception as exc:
        print(f"   WARNING: Foundation diary memory snapshot unavailable: {exc}")
        return None


def get_memory_stats(date_str):
    if resolve_runtime_source("DIARY_MEMORY_SOURCE", load_paths()) == "foundation":
        memory = _get_memory_stats_foundation(date_str)
        if memory is not None:
            return memory
        raise RuntimeError(f"Foundation diary memory snapshot missing for {date_str}; run Foundation refresh/backfill before generation.")
    return _get_memory_stats_legacy()


def get_rag_memory_stats(date_str):
    return {"rag": _get_active_rag_stats(), "memory": get_memory_stats(date_str)}


def _get_active_rag_stats():
    try:
        foundation_src = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if foundation_src not in sys.path:
            sys.path.append(foundation_src)
        from agentic_rag.rag_status import read_rag_status

        status = read_rag_status(count_legacy_entries=False, probe_server=False)
        v2 = status.get("v2") if isinstance(status.get("v2"), dict) else {}
        active_index = status.get("activeIndex") if isinstance(status.get("activeIndex"), dict) else {}
        index_path_value = v2.get("activeIndexPath") or active_index.get("indexPath")
        if not index_path_value:
            return {"entries": 0, "sizeMB": 0, "source": "rag-v2-unavailable", "reason": "active-v2-index-missing"}
        index_path = Path(str(index_path_value))
        if v2.get("ready") and index_path.exists():
            return {
                "entries": int(v2.get("chunkCount") or 0),
                "sizeMB": round(index_path.stat().st_size / (1024 * 1024), 2),
                "source": "rag-v2-active",
                "indexPath": str(index_path),
                "updatedAt": v2.get("updatedAt"),
            }
        return {"entries": 0, "sizeMB": 0, "source": "rag-v2-unavailable", "reason": "active-v2-index-not-ready"}
    except Exception as exc:
        print(f"   WARNING: Active nova-RAG status unavailable: {exc}")
        return {"entries": 0, "sizeMB": 0, "source": "rag-v2-unavailable", "reason": "rag-status-unavailable"}

def build_matrix_table(all_stats):
    rows = [("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("cache_read", "cache_read"), ("api_calls", "api_calls"), ("sessions_total", "sessions_total"), ("active_sessions", "active_sessions"), ("messages_count", "messages_count"), ("**total_tokens**", "total_tokens")]
    # 固定列顺序，确保日记格式稳定且包含 codex
    srcs = ["openclaw", "gemini-cli", "claude-code", "hermes", "codex"]
    srcs.extend(source for source in ("opencode", "antigravity") if source in all_stats)
    srcs.append("cron")
    matrix_str = "## 本日统计\n| 指标 | " + " | ".join(srcs) + " | **合计** |\n| :--- | " + " | ".join([":---"] * (len(srcs) + 1)) + " |\n"
    for label, key in rows:
        row_str = f"| {label} | "
        for s in srcs: row_str += f"{all_stats[s].get(key, 0):,} | "
        matrix_str += row_str + f"**{all_stats['total'].get(key, 0):,}** |\n"
    return matrix_str

def _get_weather_for_date(target_date_str):
    """Fetch weather using the runtime weather settings and location resolver."""
    return fetch_weather_for_date(target_date_str)

# ===================== ASSEMBLY =====================

def _metric_value(stats, source, key):
    try:
        return int((stats.get(source) or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _is_blank_diary_activity(all_stats, filtered_entries):
    non_cron_sources = ["openclaw", "gemini-cli", "claude-code", "hermes", "codex", "opencode", "antigravity"]
    has_non_cron_usage = any(
        _metric_value(all_stats, source, "messages_count") > 0 or _metric_value(all_stats, source, "total_tokens") > 0
        for source in non_cron_sources
    )
    return not filtered_entries and not has_non_cron_usage


def assemble_final_markdown(date_str, llm_content, all_stats, filtered_entries):
    target_dt = datetime.strptime(date_str, "%Y-%m-%d"); title = f"# {target_dt.strftime('%Y年%m月%d日')} 日记"
    weather = _get_weather_for_date(date_str)
    sec_map = {"今日概要": [], "Agent工作": [], "重要提醒": [], "定时任务情况": [], "备注": []}
    curr = None
    for line in llm_content.split('\n'):
        m = re.match(r'^##\s*(今日概要|Agent工作|重要提醒|定时任务情况|备注)', line, re.I)
        if m: curr = m.group(1).strip(); sec_map[curr] = []; continue
        if curr in sec_map: sec_map[curr].append(line)

    matrix = build_matrix_table(all_stats)
    cron_tasks = get_cron_structured(date_str)
    cron_section = render_cron_tasks_section(cron_tasks)
    if _is_blank_diary_activity(all_stats, filtered_entries):
        meta = {"date": date_str, "metrics": dict(all_stats), "agents": [], "cronTasks": cron_tasks, "modelUsage": all_stats["model_usage_list"], "activityState": "empty"}
        visible_sources = ["openclaw", "gemini-cli", "claude-code", "hermes", "codex"]
        visible_sources.extend(source for source in ("opencode", "antigravity") if source in all_stats)
        visible_sources.append("cron")
        for s in visible_sources:
            meta["agents"].append({"name": s, "messages": all_stats[s]["messages_count"], "tokens": all_stats[s]["total_tokens"], "active_sessions": all_stats[s].get("active_sessions", 0), "sessions_total": all_stats[s].get("sessions_total", 0)})
        meta["activityState"] = "empty"
        md = [
            title,
            "",
            "## 天气",
            weather,
            "",
            "## 今日概要",
            "今日无活动",
            "",
            "## 定时任务情况",
            cron_section,
            "",
            "```json\n" + json.dumps(meta, indent=2, ensure_ascii=False) + "\n```",
        ]
        return "\n".join(md)
    meta = {"date": date_str, "metrics": dict(all_stats), "agents": [], "lessons": get_lessons_structured(date_str), "newSkills": get_new_skills_structured(date_str), "cronTasks": cron_tasks, "topTopics": extract_top_topics(filtered_entries), "ragStats": get_rag_memory_stats(date_str)["rag"], "memoryStats": get_rag_memory_stats(date_str)["memory"], "tasks": get_task_board_snapshot(date_str), "modelUsage": all_stats["model_usage_list"]}

    # 元数据中的 Agents 列表也保持全量 key
    visible_sources = ["openclaw", "gemini-cli", "claude-code", "hermes", "codex"]
    visible_sources.extend(source for source in ("opencode", "antigravity") if source in all_stats)
    visible_sources.append("cron")
    for s in visible_sources:
        meta["agents"].append({"name": s, "messages": all_stats[s]["messages_count"], "tokens": all_stats[s]["total_tokens"], "active_sessions": all_stats[s].get("active_sessions", 0), "sessions_total": all_stats[s].get("sessions_total", 0)})

    md = [title, "", "## 天气", weather, "", "## 今日概要", "\n".join(sec_map["今日概要"]).strip(), "", matrix, "", "## Agent工作", "\n".join(sec_map["Agent工作"]).strip(), "", "## 重要提醒", "\n".join(sec_map["重要提醒"]).strip(), "", "## 定时任务情况", cron_section, "", "## 备注", "\n".join(sec_map["备注"]).strip() or "无", "", "```json\n" + json.dumps(meta, indent=2, ensure_ascii=False) + "\n```"]
    return "\n".join(md)

QUALITY_GATE_TOKENS = PIPELINE_GATE_TOKENS
MAX_GATE_SPLIT_CHUNKS = max(1, _positive_int(os.getenv("ACTANARA_NARRATIVE_MAX_GATE_SPLIT_CHUNKS"), 256))
MAX_FINAL_PRECOMPRESS_CHUNKS = max(1, _positive_int(os.getenv("ACTANARA_NARRATIVE_MAX_FINAL_PRECOMPRESS_CHUNKS"), 256))
TRUNCATE_SEQUENCE = (400, 300, 200, 100)
BROAD_TIME_SLOTS = (
    ("凌晨(00-04)", 0, 4),
    ("上午(04-12)", 4, 12),
    ("下午(12-18)", 12, 18),
    ("晚上(18-24)", 18, 24),
)
TWO_HOUR_TIME_SLOTS = tuple((f"{h:02d}:00-{h + 2:02d}:00", h, h + 2) for h in range(0, 24, 2))
ONE_HOUR_TIME_SLOTS = tuple((f"{h:02d}:00-{h + 1:02d}:00", h, h + 1) for h in range(0, 24))
SIGNAL_RE = re.compile(
    r"(/Volumes/|src/|docs/|tests/|advanced/|\.py\b|\.md\b|\.jsonl\b|python|pytest|unittest|git |curl|"
    r"passed|failed|error|traceback|exception|commit|hash|T-\d|RAG|Foundation|Dashboard|snapshot|"
    r"prompt|token|cache|registry|pipeline|sqlite)",
    re.IGNORECASE,
)


def _smart_truncate_content(content, max_chars):
    content = str(content)
    if len(content) <= max_chars:
        return content
    if max_chars <= 80:
        return content[:max_chars] + "..."

    head_budget = max(20, int(max_chars * 0.30))
    middle_budget = max(40, int(max_chars * 0.25))
    tail_budget = max(20, max_chars - head_budget - middle_budget)
    head = content[:head_budget].rstrip()
    tail = content[-tail_budget:].lstrip()
    middle = content[head_budget:-tail_budget]

    signals = []
    for line in middle.splitlines():
        stripped = line.strip()
        if stripped and SIGNAL_RE.search(stripped):
            signals.append(stripped)
        if sum(len(item) for item in signals) >= middle_budget:
            break
    if signals:
        middle_text = " ".join(signals)[:middle_budget].strip()
    else:
        center = max(0, len(middle) // 2 - middle_budget // 2)
        middle_text = middle[center:center + middle_budget].strip()

    return "\n[前文]\n" + head + "\n[中间关键信号摘录]\n" + middle_text + "\n[结尾]\n" + tail + "..."


def build_raw_text(entries, max_chars):
    text = ""
    for e in entries:
        role, t_str, cnt = e.get("role", ""), e.get("time", ""), e.get("content", "")
        cnt = _smart_truncate_content(cnt, max_chars)
        text += "[" + str(t_str) + "] " + str(role) + ": " + str(cnt) + "\n"
    return text


def _entry_hour(entry):
    try:
        return int(str(entry.get("time", "12")).split(":", 1)[0])
    except:
        return 12


def _entries_for_slot(entries, start_hour, end_hour):
    return [entry for entry in entries if start_hour <= _entry_hour(entry) < end_hour]


def _clean_partial_summary(summary):
    return re.sub(r'^###.*?\n', '', summary or "").strip()


def try_generate(entries, agent, truncate_list=TRUNCATE_SEQUENCE):
    plan = _plan_direct_summary(entries, agent, truncate_list)
    if not plan:
        return None
    return _execute_summary_plan(plan)


def _plan_direct_summary(entries, agent, truncate_list=TRUNCATE_SEQUENCE):
    for t in truncate_list:
        prompt = _partial_prompt(entries, agent, t)
        tokens = get_token_count(prompt)
        if tokens <= QUALITY_GATE_TOKENS:
            return {"agent": agent, "entries": entries, "truncate": t, "tokens": tokens}
        else:
            print(f"   [GATE] {agent} t={t} estimated {tokens:,} tokens > {QUALITY_GATE_TOKENS:,}; trying smaller slice.")
    return None


def _partial_prompt(entries, agent, truncate_chars):
    raw_text = build_raw_text(entries, truncate_chars)
    return PROMPT_PARTIAL.replace("{agent_info}", agent).replace("{raw_text}", raw_text)


def _execute_summary_plan(plan):
    prompt = _partial_prompt(plan["entries"], plan["agent"], plan["truncate"])
    return call_llm(prompt, label=plan["agent"])


def _plan_slot_summaries(agent, entries, slots):
    plans = []
    for slot_label, start_hour, end_hour in slots:
        slot_entries = _entries_for_slot(entries, start_hour, end_hour)
        if not slot_entries:
            continue
        plan = _plan_direct_summary(slot_entries, agent + " - " + slot_label)
        if not plan:
            print(f"   [GATE] {agent} {slot_label} could not fit quality gate.")
            return None
        plans.append({"slot": slot_label, "plan": plan})
    return plans


def _largest_gate_fitting_prefix(entries, agent):
    best = 0
    low, high = 1, len(entries)
    while low <= high:
        mid = (low + high) // 2
        raw_text = build_raw_text(entries[:mid], TRUNCATE_SEQUENCE[-1])
        prompt = PROMPT_PARTIAL.replace("{agent_info}", agent).replace("{raw_text}", raw_text)
        if get_token_count(prompt) <= QUALITY_GATE_TOKENS:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _split_entries_by_gate(entries, agent):
    chunks = []
    index = 0
    while index < len(entries):
        if len(chunks) + 1 >= MAX_GATE_SPLIT_CHUNKS:
            chunks.append(entries[index:])
            break
        size = _largest_gate_fitting_prefix(entries[index:], agent)
        if size <= 0:
            size = 1
        chunks.append(entries[index:index + size])
        index += size
    return chunks


def _plan_chunked_slot_summaries(agent, entries, slots=ONE_HOUR_TIME_SLOTS):
    plans = []
    for slot_label, start_hour, end_hour in slots:
        slot_entries = _entries_for_slot(entries, start_hour, end_hour)
        if not slot_entries:
            continue
        plan = _plan_direct_summary(slot_entries, agent + " - " + slot_label)
        if plan:
            plans.append({"slot": slot_label, "plan": plan})
            continue

        chunks = _split_entries_by_gate(slot_entries, agent + " - " + slot_label)
        print(f"   [SPLIT] {agent} {slot_label} requires {len(chunks)} message chunks.")
        for index, chunk in enumerate(chunks, start=1):
            chunk_label = slot_label + f" #{index}"
            plan = _plan_direct_summary(chunk, agent + " - " + chunk_label, truncate_list=(TRUNCATE_SEQUENCE[-1],))
            if not plan:
                print(f"   [GATE] {agent} {chunk_label} could not fit quality gate.")
                return None
            plans.append({"slot": chunk_label, "plan": plan})
    return plans


def _execute_slot_plans(slot_plans):
    summaries_by_index = {}
    max_workers = min(PIPELINE_CONCURRENCY, len(slot_plans)) or 1
    if max_workers == 1:
        for index, item in enumerate(slot_plans):
            result = _execute_summary_plan(item["plan"])
            if not result:
                return None
            summaries_by_index[index] = {"slot": item["slot"], "content": _clean_partial_summary(result)}
        return [summaries_by_index[index] for index in sorted(summaries_by_index)]

    print(f"   [PARALLEL] executing {len(slot_plans)} narrative partial calls with concurrency={max_workers}.")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_execute_summary_plan, item["plan"]): (index, item)
            for index, item in enumerate(slot_plans)
        }
        for future in as_completed(futures):
            index, item = futures[future]
            try:
                result = future.result()
            except ProviderChainError:
                raise
            except Exception as exc:
                print(f"   [PARALLEL] {item['slot']} failed during narrative partial summary: {exc}")
                result = None
            if not result:
                return None
            summaries_by_index[index] = {"slot": item["slot"], "content": _clean_partial_summary(result)}
    return [summaries_by_index[index] for index in sorted(summaries_by_index)]


def _integrate_agent_summary(agent, slot_summaries):
    if not slot_summaries:
        return ""
    if len(slot_summaries) == 1:
        only = slot_summaries[0]
        return "\n\n**[" + only["slot"] + "] - 业务复盘**\n\n" + only["content"]

    combined = "\n\n".join(
        "**[" + item["slot"] + "] - 业务复盘**\n\n" + item["content"] for item in slot_summaries
    )
    prompt = PROMPT_PARTIAL.replace("{agent_info}", agent + " - 全天连续整合").replace("{raw_text}", combined)
    tokens = get_token_count(prompt)
    if tokens <= QUALITY_GATE_TOKENS:
        result = call_llm(prompt, label=agent + " - agent integration", max_tokens=8192)
        if result:
            return _clean_partial_summary(result)
    print(f"   [AGENT-INTEGRATION] {agent} integration estimated {tokens:,} tokens; using ordered slot summaries.")
    return combined


def _generate_agent_summary(agent, sorted_entries):
    if len(sorted_entries) <= 40:
        plan = _plan_direct_summary(sorted_entries, agent)
        if plan:
            return _execute_summary_plan(plan)

    print("   [SPLIT] " + agent + " (" + str(len(sorted_entries)) + " msgs). Forcing time-segment refinement...")
    for label, slots in (
        ("4h/8h", BROAD_TIME_SLOTS),
        ("2h", TWO_HOUR_TIME_SLOTS),
        ("1h", ONE_HOUR_TIME_SLOTS),
    ):
        print(f"   [SPLIT] Trying {agent} with {label} windows...")
        slot_plans = _plan_slot_summaries(agent, sorted_entries, slots)
        if slot_plans:
            slot_summaries = _execute_slot_plans(slot_plans)
            if not slot_summaries:
                return None
            return _integrate_agent_summary(agent, slot_summaries)
    print(f"   [SPLIT] Trying {agent} with 1h message chunks...")
    chunk_plans = _plan_chunked_slot_summaries(agent, sorted_entries)
    if chunk_plans:
        chunk_summaries = _execute_slot_plans(chunk_plans)
        if not chunk_summaries:
            return None
        return _integrate_agent_summary(agent, chunk_summaries)
    return None


def _split_text_for_partial_gate(text, agent):
    lines = text.splitlines()
    chunks = []
    index = 0
    while index < len(lines):
        if len(chunks) + 1 >= MAX_FINAL_PRECOMPRESS_CHUNKS:
            chunks.append("\n".join(lines[index:]))
            break
        best = 0
        low, high = 1, len(lines) - index
        while low <= high:
            mid = (low + high) // 2
            raw_text = "\n".join(lines[index:index + mid])
            prompt = PROMPT_PARTIAL.replace("{agent_info}", agent).replace("{raw_text}", raw_text)
            if get_token_count(prompt) <= QUALITY_GATE_TOKENS:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        if best <= 0:
            best = 1
        chunks.append("\n".join(lines[index:index + best]))
        index += best
    return chunks


def _call_final_integration(agent_results):
    blocks = ["=== Agent: 【" + a + "】摘要 ===\n" + r for a, r in agent_results.items()]
    combined_summary = "\n\n".join(blocks)
    prompt = PROMPT_INTEGRATION.replace("{raw_text}", combined_summary)
    tokens = get_token_count(prompt)
    if tokens <= QUALITY_GATE_TOKENS:
        return call_llm(prompt, True, label="final integration")

    print(f"   [FINAL-GATE] final integration estimated {tokens:,} tokens; pre-compressing summaries.")
    reduced = []
    for index, chunk in enumerate(_split_text_for_partial_gate(combined_summary, "全日最终整合预压缩"), start=1):
        chunk_prompt = PROMPT_PARTIAL.replace("{agent_info}", f"全日最终整合预压缩 #{index}").replace("{raw_text}", chunk)
        result = call_llm(chunk_prompt, label=f"final pre-compress #{index}")
        reduced.append(result if result else chunk)

    reduced_summary = "\n\n".join(reduced)
    prompt = PROMPT_INTEGRATION.replace("{raw_text}", reduced_summary)
    tokens = get_token_count(prompt)
    if tokens <= QUALITY_GATE_TOKENS:
        return call_llm(prompt, True, label="final integration")

    for char_budget in (60000, 40000, 25000, 15000):
        bounded = _smart_truncate_content(reduced_summary, char_budget)
        prompt = PROMPT_INTEGRATION.replace("{raw_text}", bounded)
        tokens = get_token_count(prompt)
        if tokens <= QUALITY_GATE_TOKENS:
            print(f"   [FINAL-GATE] final integration bounded to {tokens:,} tokens.")
            return call_llm(prompt, True, label="final integration")
    print(f"   [FINAL-GATE] unable to fit final integration under {QUALITY_GATE_TOKENS:,} tokens.")
    return None


def generate_diary_with_fallback(all_entries_by_agent):
    print("\n>>> Narrative Pass: V4.0 Quality-Gated Detail Mode")
    print(f">>> Narrative Gate: max {QUALITY_GATE_TOKENS:,} tokens/call, concurrency={PIPELINE_CONCURRENCY}")
    agent_results = {}
    for agent, entries in all_entries_by_agent.items():
        if not entries: continue
        sorted_ents = sorted(entries, key=lambda x: x.get('time', '00:00'))
        result = _generate_agent_summary(agent, sorted_ents)
        if result:
            agent_results[agent] = result

    return _call_final_integration(agent_results)

def write_narrative_report(date_str):
    entries = load_filtered_entries(date_str)
    all_stats = calculate_stats_raw(date_str)
    llm_md = ""
    if entries:
        llm_md = generate_diary_with_fallback(entries)
        if llm_md is None:
            raise RuntimeError("llm_md is None. This means the LLM integration call failed or returned None.")
    final_md = assemble_final_markdown(date_str, llm_md, all_stats, entries)
    root = _runtime_diary_root()
    out_file = diary_no_activity_report_path(root, date_str) if _is_blank_diary_activity(all_stats, entries) else diary_narrative_report_path(root, date_str)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        f.write(final_md)
    return out_file


def write_blank_day_report(date_str, diary_root=None):
    try:
        all_stats = calculate_stats_raw(date_str)
    except Exception:
        all_stats = _empty_stats()
    final_md = assemble_final_markdown(date_str, "", all_stats, {})
    root = Path(diary_root) if diary_root is not None else _runtime_diary_root()
    out_file = diary_no_activity_report_path(root, date_str)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        f.write(final_md)
    return out_file


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else business_today().isoformat()
    try:
        out_file = write_narrative_report(d)
    except RuntimeError as exc:
        print(f"❌ ERROR: {exc}")
        sys.exit(1)
    print("✅ SUCCESS: " + str(out_file))
