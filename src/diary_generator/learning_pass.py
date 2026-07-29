#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
高阶技术洞察助手 (v2.0 - Dual-Track Intelligence)
1. 提炼技术教训 (Lessons)
2. 自动感知并更新基础设施 (Infra Updates)
"""

import os
import json
import sys
import urllib.request
import re
import hashlib
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
import config
from data_foundation.diary_paths import (
    diary_learning_report_path,
    diary_markdown_paths,
    diary_narrative_report_path,
    diary_report_paths,
    diary_technical_report_path,
)
from data_foundation.infrastructure import apply_infrastructure_updates, render_infrastructure_graph_context
from data_foundation.time import business_today, business_now
from data_foundation.llm_json import LLMJsonParseError, parse_llm_json_object
from data_foundation.memory_corpus import merge_lessons_into_canonical
from data_foundation.paths import load_paths
from data_foundation.llm_execution import execute_llm_message

THINKING_MODE = os.getenv("LLM_THINKING_MODE", "off").strip().lower()
def _runtime_diary_root() -> Path:
    return load_paths().diary_dir


def _thinking_instruction():
    if THINKING_MODE == "low":
        return "\n推理强度：low。任务是提炼经验教训，不需要深度发散推理；直接抽取问题、根因和建议。"
    if THINKING_MODE == "medium":
        return "\n推理强度：medium。只在归纳根因和长期经验时使用适度推理，避免冗长思考。"
    if THINKING_MODE in {"off", "disabled", "disable"}:
        return "\n推理强度：off。不要展开深度思考；直接输出结构化经验教训。"
    return ""

PROMPT_LEARNING = """你是一个高阶技术洞察助手。请分析提供的日志摘要与技术报告，生成一份结构化 Markdown 智慧沉淀报告，并严格抽取基础设施变更事件。

目标日期：{date}

1. **教训提炼 (Lessons)**：
   - 寻找：由于操作不当导致的严重Bug、架构设计上的重大失误、API调用的隐形陷阱、性能瓶颈。
   - 每条教训必须严格拆成 `问题`、`根因`、`建议` 三个子标题。
   - 标题层级必须严格区分：报告标题用 `#`，主 section 用 `##`，每条教训用 `###`，三段字段用 `####`。

2. **基础设施变更识别 (Infrastructure Updates)**：
   - 只允许两类对象：
     - `device`：实体设备、路由器、服务器、PC、主机、云服务器、VPS、远程实例、局域网实例。
     - `service`：Docker 容器、二进制服务、launchd/systemd 服务、API 服务、数据库服务、embedding server、dashboard server、占用端口的监听服务。
   - 只记录外部 agent/用户实际部署、修复、变更、上下线、端口/endpoint/path/连接信息变更的基础设施对象。
   - 不要把代码模块、函数、文档、任务、抽象子系统、配置项本身误判为基础设施；除非它是实际运行的设备或服务。
   - 优先匹配 active graph 中已有 `entityId`；同一对象的别名、端口、endpoint 或宿主相符时必须更新已有对象，不要创造新对象。
   - 只有技术报告或日志摘要有直接证据时，才输出新对象或变更；证据不足时写 `无`。
   - 凭证安全：不得输出 password、token、API key、cookie、私钥、Bearer、Authorization 等明文值。凭证变化只写 `credential_rotated`、`secretRef_changed` 或 `[redacted]`。

请只输出以下结构的 Markdown，不要 JSON，不要代码块，不要前言或解释：

# {date} 智慧沉淀与基建审计

## 🧠 黄金教训 (Lessons)
### 【agent-name】简短问题标题
#### 问题
具体问题。
#### 根因
具体根因。
#### 建议
具体建议。

## 📡 基建变动 (Infrastructure)
| 实体ID | 类型 | 对象 | 宿主/位置 | 变动类型 | 字段 | 变动描述 | 当前值 | 证据 | 置信度 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 既有entityId或new | device/service | 对象名称 | 宿主或位置 | created/updated/port_changed/endpoint_changed/path_changed/status_changed/deployed/fixed/credential_rotated | port/endpoint/path/status/credential/other | 变动描述 | 当前非敏感值或[redacted] | 技术报告中的证据短语 | high/medium/low |

如果没有黄金教训或基建变动，对应 section 下写 `无`。不要省略主 section。

基础设施 Active Graph（已脱敏，仅用于匹配已有对象）：
{infra_graph_context}

技术报告：
{technical_report}

日志摘要数据：
{summary}
"""

SYSTEM_LEARNING = "你是一个专业的技术审计助手。只输出结构化 Markdown，严禁任何前言或解释。"

class LearningPassError(RuntimeError):
    pass


def prepare_learning_summary(summary_text):
    text = re.sub(r'\n## 定时任务情况\n[\s\S]*?(?=\n## 备注|\n```json|\Z)', '\n## 定时任务情况\n无\n', summary_text)
    text = re.sub(r'\n```json\n[\s\S]*?\n```\s*$', '', text).rstrip()
    return text

def _llm_chunk_id(label):
    raw = str(label or "learning llm").strip()
    slug = re.sub(r"[^\w.-]+", "-", raw, flags=re.UNICODE).strip("-_.").casefold()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{(slug[:80] or 'learning-llm')}-{digest}"


def call_llm(prompt, label=None):
    call_label = label or "learning llm"
    return execute_llm_message(
        system=SYSTEM_LEARNING + _thinking_instruction(),
        prompt=prompt,
        temperature=0.1,
        max_tokens=16384,
        thinking_mode=THINKING_MODE,
        paths=load_paths(),
        pass_id="learning",
        label=call_label,
        chunk_id=_llm_chunk_id(call_label),
    ).text


def _debug_dir():
    return Path(config.ACTANARA_HOME) / "state" / "logs" / "learning-pass"


def save_learning_debug_output(date_str, raw_output, label="parse-failure"):
    directory = _debug_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = business_now().strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"{date_str}-{stamp}-{label}.txt"
    path.write_text(raw_output or "", encoding="utf-8")
    return path


def repair_learning_json(raw_output):
    prompt = "\n".join(
        [
            "下面是一段原始 LLM 输出，它本应是合法 JSON 对象。",
            "请只返回修复后的合法 JSON 对象，不要 Markdown 代码块，不要前言或解释。",
            "JSON 顶层必须是对象，并保留 lessons 和 infra 两个数组。",
            "",
            raw_output or "",
        ]
    )
    return call_llm(prompt, label="learning json repair")


def repair_learning_markdown(raw_output):
    prompt = "\n".join(
        [
            "下面是一段原始 LLM 输出，它本应是结构化 Markdown 智慧沉淀报告。",
            "请只返回修复后的 Markdown，不要 JSON，不要代码块，不要前言或解释。",
            "必须包含 `## 🧠 黄金教训 (Lessons)` 和 `## 📡 基建变动 (Infrastructure)` 两个主 section。",
            "每条黄金教训必须使用 `### 【agent-name】标题`，并包含 `#### 问题`、`#### 根因`、`#### 建议` 三个子标题。",
            "基建变动必须使用三列表格：对象、变动描述、当前值。没有内容时写 `无`。",
            "",
            raw_output or "",
        ]
    )
    return call_llm(prompt, label="learning markdown repair")


def _strip_code_fence(text):
    stripped = (text or "").strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*([\s\S]*?)\s*```", stripped)
    return fenced.group(1).strip() if fenced else stripped


def _section_body(markdown, heading_keyword):
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and heading_keyword in line:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _clean_markdown_value(value):
    return (value or "").strip().replace("`", "").strip()


def _stable_id(prefix, *parts):
    digest = hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _technical_report_text(date_str):
    root = _runtime_diary_root()
    existing = diary_report_paths(root, date_str, "technical")
    path = existing[0] if existing else diary_technical_report_path(root, date_str)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "无"


def _infrastructure_graph_context():
    try:
        return render_infrastructure_graph_context(load_paths())
    except Exception:
        return "Infrastructure active graph unavailable."


def build_learning_prompt(date_str, summary_text):
    return (
        PROMPT_LEARNING.replace("{date}", date_str)
        .replace("{summary}", prepare_learning_summary(summary_text))
        .replace("{technical_report}", prepare_learning_summary(_technical_report_text(date_str)))
        .replace("{infra_graph_context}", _infrastructure_graph_context())
    )


def _parse_learning_markdown_lessons(date_str, markdown):
    body = _section_body(markdown, "黄金教训")
    if not body or body.strip() == "无":
        return []
    lessons = []
    blocks = re.split(r"(?m)^###\s+", body)
    for block in blocks:
        block = block.strip()
        if not block or block == "无":
            continue
        lines = block.splitlines()
        title = lines[0].strip() if lines else ""
        title_match = re.match(r"^【([^】]+)】\s*(.*)$", title)
        agent = title_match.group(1).strip() if title_match else "unknown"
        problem_title = title_match.group(2).strip() if title_match else title
        fields = {}
        current = None
        buffer = []
        for line in lines[1:]:
            heading = re.match(r"^####\s*(问题|根因|建议)\s*$", line.strip())
            if heading:
                if current:
                    fields[current] = "\n".join(buffer).strip()
                current = heading.group(1)
                buffer = []
            elif current:
                buffer.append(line)
        if current:
            fields[current] = "\n".join(buffer).strip()
        problem = fields.get("问题") or problem_title
        root_cause = fields.get("根因") or ""
        suggestion = fields.get("建议") or ""
        if not any((problem, root_cause, suggestion)):
            continue
        text_parts = []
        if problem:
            text_parts.append(f"问题：{problem}")
        if root_cause:
            text_parts.append(f"根因：{root_cause}")
        if suggestion:
            text_parts.append(f"建议：{suggestion}")
        text = " ".join(text_parts)
        lessons.append(
            {
                "id": _stable_id("lesson", date_str, agent, problem, root_cause, suggestion),
                "text": text,
                "agent": agent,
                "date": date_str,
                "problem": problem,
                "rootCause": root_cause,
                "suggestion": suggestion,
            }
        )
    return lessons


def _parse_learning_markdown_infra(markdown):
    body = _section_body(markdown, "基建变动")
    if not body or body.strip() == "无":
        return []
    updates = []
    headers = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cols = [_clean_markdown_value(col) for col in stripped.strip("|").split("|")]
        if len(cols) < 3:
            continue
        if all(re.match(r"^:?-{3,}:?$", col or "") for col in cols):
            continue
        normalized_first = cols[0].lower()
        if normalized_first in {"对象", "object", "实体id", "entity id", "entityid"}:
            headers = [_infra_header_key(col) for col in cols]
            continue
        if headers and len(headers) >= len(cols):
            update = _infra_update_from_header_row(headers, cols)
            if update:
                updates.append(update)
            continue
        name, change, value = cols[:3]
        if not any((name, change, value)):
            continue
        updates.append(
            {
                "id": _stable_id("infra", "service", name, "updated", "", value, change),
                "entityType": "service",
                "type": "service",
                "name": name,
                "eventType": "updated",
                "change": change,
                "currentValue": value,
                "value": value,
                "confidence": "medium",
            }
        )
    return updates


def _infra_header_key(label):
    normalized = re.sub(r"[\s/_-]+", "", str(label or "").strip().lower())
    aliases = {
        "实体id": "entityId",
        "entityid": "entityId",
        "类型": "entityType",
        "type": "entityType",
        "entitytype": "entityType",
        "对象": "name",
        "object": "name",
        "name": "name",
        "名称": "name",
        "宿主位置": "host",
        "宿主": "host",
        "位置": "host",
        "host": "host",
        "变动类型": "eventType",
        "changetype": "eventType",
        "字段": "field",
        "field": "field",
        "变动描述": "change",
        "change": "change",
        "summary": "change",
        "当前值": "currentValue",
        "current": "currentValue",
        "currentvalue": "currentValue",
        "证据": "evidence",
        "evidence": "evidence",
        "置信度": "confidence",
        "confidence": "confidence",
    }
    return aliases.get(normalized, normalized)


def _infra_update_from_header_row(headers, cols):
    row = {headers[index]: cols[index] for index in range(min(len(headers), len(cols))) if headers[index]}
    name = row.get("name", "")
    if not name or name == "对象名称":
        return None
    entity_type = row.get("entityType", "")
    if entity_type not in {"device", "service"}:
        entity_type = "service"
    evidence = [item.strip() for item in re.split(r"[;；]\s*", row.get("evidence", "")) if item.strip()]
    current_value = row.get("currentValue", "")
    change = row.get("change", "")
    event_type = row.get("eventType", "") or "updated"
    field = row.get("field", "")
    return {
        "id": _stable_id("infra", entity_type, name, event_type, field, current_value, change),
        "entityId": "" if row.get("entityId") in {"new", "新增"} else row.get("entityId", ""),
        "entityType": entity_type,
        "type": entity_type,
        "name": name,
        "host": row.get("host", ""),
        "eventType": event_type,
        "field": field,
        "change": change,
        "currentValue": current_value,
        "value": current_value,
        "evidence": evidence,
        "confidence": row.get("confidence", "") or "medium",
    }


def parse_learning_markdown(date_str, raw_output):
    markdown = _strip_code_fence(raw_output)
    if "黄金教训" not in markdown and "基建变动" not in markdown:
        raise LLMJsonParseError("Learning Markdown missing required sections")
    lessons = _parse_learning_markdown_lessons(date_str, markdown)
    infra = _parse_learning_markdown_infra(markdown)
    return {"lessons": lessons, "infra": infra, "markdown": markdown}


def parse_learning_response(date_str, raw_output):
    try:
        return parse_learning_markdown(date_str, raw_output)
    except LLMJsonParseError as markdown_error:
        try:
            return parse_llm_json_object(raw_output).data
        except LLMJsonParseError:
            first_path = save_learning_debug_output(date_str, raw_output, "initial")
            print(f"   ⚠️ Initial Markdown parse failed; raw output saved: {first_path}")
            repaired = repair_learning_markdown(raw_output)
            try:
                return parse_learning_markdown(date_str, repaired)
            except LLMJsonParseError as second_error:
                second_path = save_learning_debug_output(date_str, repaired, "repair")
                raise LearningPassError(
                    f"Learning Pass Markdown parse failed after repair retry: {second_error}; "
                    f"initial error: {markdown_error}; debug files: {first_path}, {second_path}"
                ) from second_error


def parse_learning_response_json_compat(date_str, raw_output):
    try:
        return parse_llm_json_object(raw_output).data
    except LLMJsonParseError as first_error:
        first_path = save_learning_debug_output(date_str, raw_output, "initial")
        print(f"   ⚠️ Initial JSON parse failed; raw output saved: {first_path}")
        repaired = repair_learning_json(raw_output)
        try:
            return parse_llm_json_object(repaired).data
        except LLMJsonParseError as second_error:
            second_path = save_learning_debug_output(date_str, repaired, "repair")
            raise LearningPassError(
                f"Learning Pass JSON parse failed after repair retry: {second_error}; "
                f"initial error: {first_error}; debug files: {first_path}, {second_path}"
            ) from second_error

def process_learning(date_str, summary_text):
    print(f"🧠 Running Learning Pass for {date_str}...")
    prompt = build_learning_prompt(date_str, summary_text)
    res_raw = call_llm(prompt, label="learning generation")
    if not res_raw:
        raise LearningPassError("Learning Pass LLM returned empty output")

    data = parse_learning_response(date_str, res_raw)

    # 1. 处理教训 (Lessons)
    lessons = data.get('lessons', [])
    lessons_result = merge_lessons_into_canonical(load_paths(), lessons)
    if lessons or lessons_result["migrated"]:
        print(f"   ✅ Ingested {lessons_result['added']} new lessons.")
        if lessons_result["migrated"]:
            print(
                "   ♻️ Migrated "
                f"{lessons_result['migrated']} legacy lessons to {lessons_result['path']}."
            )

    # 2. 处理环境变动 (Infra)
    infra_updates = data.get('infra', [])
    if infra_updates:
        try:
            graph_result = apply_infrastructure_updates(load_paths(), date_str, infra_updates)
            print(
                "   📡 Updated infrastructure graph "
                f"(entities={graph_result['entities']}, events={graph_result['events']})."
            )
        except Exception as exc:
            print(f"   ⚠️ Infrastructure graph update failed: {exc}")
        infra_file = _runtime_diary_root() / "infrastructure.jsonl"
        infra_file.parent.mkdir(parents=True, exist_ok=True)
        current_infra = {}
        if infra_file.exists():
            with open(infra_file, "r") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        current_infra[d['id']] = d
                    except: pass

        for up in infra_updates:
            uid = up.get('id')
            if uid:
                # 增量 Merge
                current_infra[uid] = {**current_infra.get(uid, {}), **up, "last_updated": date_str}

        with open(infra_file, "w") as f:
            for item in current_infra.values():
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"   📡 Synchronized {len(infra_updates)} infrastructure updates.")

    # 🚀 3. 生成每日智慧沉淀 Markdown
    out_file = diary_learning_report_path(_runtime_diary_root(), date_str)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    md_text = data.get("markdown")
    if not md_text:
        md = [f"# {date_str} 智慧沉淀与基建审计", ""]
        md.append("## 🧠 黄金教训 (Lessons)")
        if lessons:
            for l in lessons:
                md.extend(
                    [
                        f"### 【{l.get('agent')}】{l.get('problem') or '技术教训'}",
                        "#### 问题",
                        l.get("problem") or l.get("text") or "",
                        "#### 根因",
                        l.get("rootCause") or "",
                        "#### 建议",
                        l.get("suggestion") or "",
                        "",
                    ]
                )
        else:
            md.append("无")
            md.append("")
        md.append("## 📡 基建变动 (Infrastructure)")
        if infra_updates:
            md.append("| 实体ID | 类型 | 对象 | 宿主/位置 | 变动类型 | 字段 | 变动描述 | 当前值 | 证据 | 置信度 |")
            md.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for up in infra_updates:
                evidence = "；".join(up.get("evidence") or [])
                md.append(
                    "| {entity_id} | {entity_type} | {name} | {host} | {event_type} | {field} | {change} | `{value}` | {evidence} | {confidence} |".format(
                        entity_id=up.get("entityId") or "new",
                        entity_type=up.get("entityType") or up.get("type") or "service",
                        name=up.get("name") or "",
                        host=up.get("host") or "",
                        event_type=up.get("eventType") or "updated",
                        field=up.get("field") or "",
                        change=up.get("change") or "",
                        value=up.get("currentValue") or up.get("value") or "",
                        evidence=evidence,
                        confidence=up.get("confidence") or "medium",
                    )
                )
        else:
            md.append("无")
        md_text = "\n".join(md).strip()

    with open(out_file, "w") as f: f.write(md_text + "\n")
    print(f"   ✅ Wisdom Report generated: {out_file}")

    return True

if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else business_today().isoformat()
    # 从日记文件中读取摘要 (作为学习输入)
    root = _runtime_diary_root()
    diary_path = next(iter(diary_markdown_paths(root, d, "日记-*.md")), diary_narrative_report_path(root, d))
    if diary_path.exists():
        with open(diary_path, "r") as f:
            summary = f.read()
        try:
            process_learning(d, summary)
        except Exception as exc:
            print(f"   ❌ Learning Pass failed: {exc}")
            sys.exit(1)
    else:
        print("   ⚠️ Diary not found, skipping learning pass.")
