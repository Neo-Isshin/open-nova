"""Materialized Dashboard projections that exclude deferred RAG fields."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .db import connect
from .external_tool_catalog import detect_external_tools
from .paths import RuntimePaths
from .time import parse_timestamp, resolve_timezone
from .usage_attribution import TOOL_EMOJI, WORKSPACE_USAGE_MIN_TOKENS, resolve_usage_group, usage_group_display_allowed

AI_ASSETS_NON_RAG_PROJECTION = "foundation-ai-assets-non-rag-v2"
AI_ASSETS_SNAPSHOT_KEY = "ai-assets:latest:non-rag"
DIARY_MEMORY_PROJECTION = "legacy-diary-memory-stats-v1"
DIARY_TASKS_PROJECTION = "nova-task-sqlite-diary-tasks-v1"
RAG_DAILY_STATUS_PROJECTION = "rag-daily-status-v1"
TOOL_DISPLAY = {
    "openclaw": ("OpenClaw", TOOL_EMOJI["OpenClaw"]),
    "claude-code": ("Claude Code", TOOL_EMOJI["Claude Code"]),
    "gemini-cli": ("Gemini CLI", TOOL_EMOJI["Gemini CLI"]),
    "codex": ("Codex", TOOL_EMOJI["Codex"]),
    "hermes": ("Hermes", TOOL_EMOJI["Hermes"]),
    "opencode": ("OpenCode", TOOL_EMOJI["OpenCode"]),
    "antigravity": ("Antigravity", TOOL_EMOJI["Antigravity"]),
    "cursor": ("Cursor", TOOL_EMOJI["Cursor"]),
}
DISPLAY_ORDER = (
    "openclaw",
    "claude-code",
    "gemini-cli",
    "codex",
    "hermes",
    "opencode",
    "antigravity",
    "cursor",
)

CATALOG_TOOL_IDS = (
    "openclaw",
    "claudeCode",
    "codex",
    "geminiCli",
    "hermes",
    "opencode",
    "antigravity",
    "cursor",
)
CATALOG_ID_BY_FOUNDATION_KEY = {
    "openclaw": "openclaw",
    "claude-code": "claudeCode",
    "codex": "codex",
    "gemini-cli": "geminiCli",
    "hermes": "hermes",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "cursor": "cursor",
}
CATALOG_ID_BY_DISPLAY_NAME = {
    TOOL_DISPLAY[foundation_key][0]: catalog_id
    for foundation_key, catalog_id in CATALOG_ID_BY_FOUNDATION_KEY.items()
}
_TOOL_ID_ALIASES = {
    **CATALOG_ID_BY_FOUNDATION_KEY,
    **CATALOG_ID_BY_DISPLAY_NAME,
    **{catalog_id: catalog_id for catalog_id in CATALOG_TOOL_IDS},
}


def apply_external_tool_visibility(
    payload: dict,
    detection: dict,
) -> dict:
    """Return an AI Assets payload containing cards for detected tools only."""

    detected = _normalized_detected_tool_ids(detection)
    detected_set = set(detected)
    result = dict(payload)
    presence = detection.get("toolPresence")
    result["detectedToolKeys"] = detected
    result["toolPresence"] = dict(presence) if isinstance(presence, dict) else {}

    for field in ("tools", "agentTree"):
        values = result.get(field)
        if isinstance(values, list):
            result[field] = [
                item
                for item in values
                if _tool_id_for_item(item) in detected_set
            ]

    tool_configs = result.get("toolConfigs")
    if isinstance(tool_configs, list):
        visible_configs = []
        for item in tool_configs:
            tool_id = _tool_id_for_item(item)
            if tool_id not in detected_set or not isinstance(item, dict):
                continue
            normalized = dict(item)
            details = presence.get(tool_id) if isinstance(presence, dict) else None
            normalized["status"] = (
                str(details.get("status") or "detected")
                if isinstance(details, dict)
                else "detected"
            )
            visible_configs.append(normalized)
        result["toolConfigs"] = visible_configs

    visible_tools = result.get("tools")
    if isinstance(visible_tools, list):
        result["totalTokens"] = sum(
            _nonnegative_int(item.get("allTimeTokens"))
            for item in visible_tools
            if isinstance(item, dict)
        )
        result["totalMessages"] = sum(
            _nonnegative_int(item.get("allTimeMessages"))
            for item in visible_tools
            if isinstance(item, dict)
        )
        result["totalSessions"] = sum(
            _nonnegative_int(item.get("sessionCount"))
            for item in visible_tools
            if isinstance(item, dict)
        )

    agents = result.get("agents")
    if isinstance(agents, list):
        result["agents"] = [
            item
            for item in agents
            if _tool_id_for_agent(item) in detected_set
        ]
        result["agentCount"] = len(result["agents"])

    storage = result.get("storage")
    if isinstance(storage, dict):
        filtered_storage = dict(storage)
        storage_tools = storage.get("tools")
        if isinstance(storage_tools, list):
            filtered_storage["tools"] = [
                item
                for item in storage_tools
                if _tool_id_for_item(item) in detected_set
            ]
        result["storage"] = filtered_storage

    skills = result.get("skills")
    if isinstance(skills, dict):
        filtered_skills = dict(skills)
        by_tool = skills.get("byTool")
        if isinstance(by_tool, dict):
            filtered_by_tool = {
                name: records
                for name, records in by_tool.items()
                if _tool_id_from_value(name) in detected_set
            }
            filtered_skills["byTool"] = filtered_by_tool
            filtered_skills["total"] = sum(
                len(records)
                for records in filtered_by_tool.values()
                if isinstance(records, list)
            )
        result["skills"] = filtered_skills

    return result


def _normalized_detected_tool_ids(detection: dict) -> list[str]:
    raw = detection.get("detectedToolKeys")
    if not isinstance(raw, list):
        raw = detection.get("detectedToolIds")
    if not isinstance(raw, list):
        presence = detection.get("toolPresence")
        raw = [
            tool_id
            for tool_id, details in presence.items()
            if isinstance(details, dict) and details.get("detected") is True
        ] if isinstance(presence, dict) else []
    selected = {
        canonical
        for value in raw
        if (canonical := _tool_id_from_value(value)) is not None
    }
    return [tool_id for tool_id in CATALOG_TOOL_IDS if tool_id in selected]


def _tool_id_from_value(value: object) -> str | None:
    return _TOOL_ID_ALIASES.get(str(value or "").strip())


def _tool_id_for_item(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    for field in ("toolKey", "tool", "id", "name"):
        if tool_id := _tool_id_from_value(item.get(field)):
            return tool_id
    return None


def _tool_id_for_agent(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    for field in ("source", "toolKey", "tool", "name", "displayName"):
        if tool_id := _tool_id_from_value(item.get(field)):
            return tool_id
    return None


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _diary_memory_snapshot_key(business_date: date) -> str:
    return f"diary:memory-stats:{business_date.isoformat()}:non-rag"


def _diary_tasks_snapshot_key(business_date: date) -> str:
    return f"diary:tasks:{business_date.isoformat()}:non-rag"


def _rag_daily_status_snapshot_key(business_date: date) -> str:
    return f"rag:daily-status:{business_date.isoformat()}"


def write_dashboard_snapshot(
    paths: RuntimePaths,
    payload: dict,
    *,
    source_run_id: int | None,
    projection_type: str = AI_ASSETS_NON_RAG_PROJECTION,
    status: str = "ready",
) -> None:
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO dashboard_snapshots(
                snapshot_key, projection_type, payload_json, generated_at, source_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                projection_type=excluded.projection_type,
                payload_json=excluded.payload_json,
                generated_at=excluded.generated_at,
                source_run_id=excluded.source_run_id,
                status=excluded.status
            """,
            (
                AI_ASSETS_SNAPSHOT_KEY,
                projection_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                datetime.now().astimezone().isoformat(),
                source_run_id,
                status,
            ),
        )


def read_dashboard_snapshot(paths: RuntimePaths) -> dict | None:
    with connect(paths, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT projection_type, payload_json, generated_at, source_run_id, status
            FROM dashboard_snapshots
            WHERE snapshot_key = ? AND status = 'ready'
            """,
            (AI_ASSETS_SNAPSHOT_KEY,),
        ).fetchone()
    if row is None:
        return None
    return {
        "payload": json.loads(row["payload_json"]),
        "projectionType": row["projection_type"],
        "generatedAt": row["generated_at"],
        "sourceRunId": row["source_run_id"],
        "status": row["status"],
    }


def write_rag_daily_status_snapshot(
    paths: RuntimePaths,
    business_date: date,
    payload: dict,
    *,
    source_run_id: int | None,
    status: str = "ready",
) -> None:
    dated_payload = {"businessDate": business_date.isoformat(), **payload}
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO dashboard_snapshots(
                snapshot_key, projection_type, payload_json, generated_at, source_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                projection_type=excluded.projection_type,
                payload_json=excluded.payload_json,
                generated_at=excluded.generated_at,
                source_run_id=excluded.source_run_id,
                status=excluded.status
            """,
            (
                _rag_daily_status_snapshot_key(business_date),
                RAG_DAILY_STATUS_PROJECTION,
                json.dumps(dated_payload, ensure_ascii=False, sort_keys=True),
                datetime.now().astimezone().isoformat(),
                source_run_id,
                status,
            ),
        )


def read_rag_daily_status_snapshot(paths: RuntimePaths, business_date: date) -> dict | None:
    with connect(paths, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT projection_type, payload_json, generated_at, source_run_id, status
            FROM dashboard_snapshots
            WHERE snapshot_key = ? AND status = 'ready'
            """,
            (_rag_daily_status_snapshot_key(business_date),),
        ).fetchone()
    if row is None:
        return None
    return {
        "payload": json.loads(row["payload_json"]),
        "projectionType": row["projection_type"],
        "generatedAt": row["generated_at"],
        "sourceRunId": row["source_run_id"],
        "status": row["status"],
    }


def materialize_ai_assets_non_rag_snapshot(
    paths: RuntimePaths,
    source_run_id: int,
    *,
    builder: Callable[[], dict] | None = None,
    business_date: date | None = None,
) -> str:
    """Materialize the full non-RAG Dashboard inventory in the background job."""
    if builder is None:
        dashboard_root = Path(__file__).resolve().parents[1] / "dashboard"
        if str(dashboard_root) not in sys.path:
            sys.path.insert(0, str(dashboard_root))
        from app.services import ai_assets

        def builder() -> dict:
            payload = ai_assets.get_ai_assets_incremental(include_rag=False)
            payload["storage"] = ai_assets._get_detailed_storage(include_rag=True)
            payload["rag"] = ai_assets._get_rag_stats()
            return payload

    payload = builder()
    detection = (
        {
            "detectedToolKeys": payload.get("detectedToolKeys"),
            "toolPresence": payload.get("toolPresence"),
        }
        if isinstance(payload.get("detectedToolKeys"), list)
        and isinstance(payload.get("toolPresence"), dict)
        else detect_external_tools(paths)
    )
    payload = apply_external_tool_visibility(payload, detection)
    write_dashboard_snapshot(paths, payload, source_run_id=source_run_id)
    if business_date is not None and isinstance(payload.get("rag"), dict):
        write_rag_daily_status_snapshot(
            paths,
            business_date,
            payload["rag"],
            source_run_id=source_run_id,
        )
    return AI_ASSETS_SNAPSHOT_KEY


def _foundation_ai_assets_non_rag_payload(paths: RuntimePaths) -> dict:
    """Build a Foundation-only usage rollup payload.

    This helper intentionally does not scan live inventory sections such as
    diary, memory, skills, storage, cron jobs, and tool configs. Ready
    AI Assets snapshots must be written through
    materialize_ai_assets_non_rag_snapshot(), whose default builder assembles
    the full non-RAG Dashboard inventory.
    """
    generated_at = datetime.now().astimezone()
    with connect(paths, read_only=True) as connection:
        tool_rows = connection.execute(
            """
            SELECT tool_key, SUM(tokens) AS tokens, SUM(messages) AS messages,
                   SUM(sessions) AS sessions, MAX(business_date) AS latest_date,
                   COUNT(DISTINCT business_date) AS active_days
            FROM daily_tool_usage
            GROUP BY tool_key
            """
        ).fetchall()
        model_rows = connection.execute(
            """
            SELECT model_key, SUM(tokens) AS tokens, SUM(messages) AS messages,
                   SUM(sessions) AS sessions
            FROM daily_model_usage
            GROUP BY model_key
            ORDER BY tokens DESC, model_key
            """
        ).fetchall()
        project_rows = connection.execute(
            """
            SELECT project_id_or_bucket, tool_key, SUM(tokens) AS tokens,
                   SUM(messages) AS messages, SUM(active_sessions) AS sessions
            FROM daily_project_usage
            GROUP BY project_id_or_bucket, tool_key
            ORDER BY tokens DESC, project_id_or_bucket, tool_key
            """
        ).fetchall()
        workspace_usage = _foundation_workspace_usage_from_events(connection)
        latest_usage_day = connection.execute(
            "SELECT MAX(business_date) FROM daily_tool_usage WHERE tokens > 0 OR messages > 0"
        ).fetchone()[0]
        active_day_count = int(connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT business_date
                FROM daily_tool_usage
                WHERE tool_key != 'cron'
                GROUP BY business_date
                HAVING SUM(tokens) > 0 OR SUM(messages) > 0 OR SUM(sessions) > 0 OR SUM(api_calls) > 0
            )
            """
        ).fetchone()[0] or 0)
        trend_rows = []
        if latest_usage_day:
            start_day = (date.fromisoformat(str(latest_usage_day)) - timedelta(days=29)).isoformat()
            trend_rows = connection.execute(
                """
                SELECT business_date, occurred_at, protocol_total_tokens
                FROM usage_events
                WHERE business_date BETWEEN ? AND ?
                ORDER BY business_date, occurred_at
                """,
                (start_day, str(latest_usage_day)),
            ).fetchall()

    by_tool = {row["tool_key"]: row for row in tool_rows}
    latest_date = max((str(row["latest_date"]) for row in tool_rows if row["latest_date"]), default="")
    tools = []
    total_tokens = 0
    total_messages = 0
    total_sessions = 0
    for key in DISPLAY_ORDER:
        row = by_tool.get(key)
        name, emoji = TOOL_DISPLAY[key]
        tokens = int(row["tokens"] or 0) if row else 0
        messages = int(row["messages"] or 0) if row else 0
        sessions = int(row["sessions"] or 0) if row else 0
        total_tokens += tokens
        total_messages += messages
        total_sessions += sessions
        tools.append(
            {
                "name": name,
                "emoji": emoji,
                "allTimeTokens": tokens,
                "allTimeMessages": messages,
                "todayTokens": tokens if row and row["latest_date"] == latest_date else 0,
                "todayMessages": messages if row and row["latest_date"] == latest_date else 0,
                "sessionCount": sessions,
                "firstActivity": "",
                "lastActivity": str(row["latest_date"] or "") if row else "",
                "activeDays": int(row["active_days"] or 0) if row else 0,
                "usageStatus": (
                    "unavailable"
                    if key == "cursor"
                    else "local-partial"
                    if key == "antigravity"
                    else "available"
                ),
            }
        )
    models = [
        {
            "name": row["model_key"],
            "tokens": int(row["tokens"] or 0),
            "messages": int(row["messages"] or 0),
            "sessions": int(row["sessions"] or 0),
        }
        for row in model_rows
    ]
    if not workspace_usage:
        workspace_usage = [
            item
            for item in (_workspace_usage_item(row) for row in project_rows)
            if int(item.get("tokens") or 0) >= WORKSPACE_USAGE_MIN_TOKENS
            and usage_group_display_allowed(str(item.get("name") or ""), str(item.get("tool") or ""))
        ]
    agents = [
        {
            "name": row["tool_key"],
            "displayName": TOOL_DISPLAY.get(row["tool_key"], (row["tool_key"], ""))[0],
            "model": "mixed",
            "sessionCount": int(row["sessions"] or 0),
            "totalMessages": int(row["messages"] or 0),
            "lastActive": str(row["latest_date"] or ""),
            "source": "foundation-rollup",
        }
        for row in sorted(tool_rows, key=lambda item: int(item["tokens"] or 0), reverse=True)
        if int(row["tokens"] or 0) > 0 or int(row["messages"] or 0) > 0
    ]
    trend30d = _trend30d_from_usage_rows(trend_rows, str(latest_usage_day or ""))
    payload = {
        "timestamp": generated_at.isoformat(),
        "tools": tools,
        "totalTokens": total_tokens,
        "totalMessages": total_messages,
        "totalSessions": total_sessions,
        "agents": agents,
        "agentCount": len(agents),
        "activeDayCount": active_day_count,
        "diary": {},
        "memory": {},
        "skills": {},
        "git": {},
        "mattermost": {"bots": 0, "status": "foundation-snapshot"},
        "cronJobs": {},
        "storage": {"tools": [], "categories": []},
        "infrastructure": _foundation_infrastructure_payload(paths),
        "toolConfigs": [],
        "trend30d": trend30d,
        "models": models,
        "workspaceUsage": workspace_usage,
        "agentTree": {},
        "updatedAt": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "dataAuthority": {
            "usage": "foundation-sqlite-rollups",
            "inventory": "not-materialized",
            "rag": "excluded",
        },
    }
    return apply_external_tool_visibility(
        payload,
        detect_external_tools(paths),
    )


def _foundation_infrastructure_payload(paths: RuntimePaths) -> dict:
    try:
        from .infrastructure import dashboard_infrastructure_payload

        return dashboard_infrastructure_payload(paths)
    except Exception:
        return {"devices": [], "services": [], "recentActivity": [], "dataAuthority": "foundation-infrastructure-graph-v1", "redacted": True}


def _workspace_usage_item(row) -> dict:
    tool_name, emoji = TOOL_DISPLAY.get(row["tool_key"], (row["tool_key"], ""))
    bucket = str(row["project_id_or_bucket"] or "")
    name = bucket
    if bucket == "unattributed":
        name = f"{tool_name} unattributed"
    elif bucket.startswith("project:"):
        name = bucket.removeprefix("project:")
    return {
        "name": name,
        "tool": tool_name,
        "emoji": emoji,
        "tokens": int(row["tokens"] or 0),
        "messages": int(row["messages"] or 0),
        "sessions": int(row["sessions"] or 0),
        "attribution": bucket,
    }


def _foundation_workspace_usage_from_events(connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT ue.tool_key, ue.session_id, ue.protocol_total_tokens, ue.message_count,
               ue.raw_locator_json, ue.metadata_json, s.initial_cwd
        FROM usage_events ue
        JOIN sessions s ON s.id = ue.session_id
        """
    ).fetchall()
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        tool_key = str(row["tool_key"] or "")
        tool_name, emoji = TOOL_DISPLAY.get(tool_key, (tool_key, ""))
        raw_locator = _json_dict(row["raw_locator_json"])
        metadata = _json_dict(row["metadata_json"])
        group = _event_workspace_group(
            tool_key,
            raw_path=str(raw_locator.get("path") or ""),
            cwd=str(metadata.get("cwd") or ""),
            initial_cwd=str(row["initial_cwd"] or ""),
        )
        if not group:
            group = tool_name
        key = (tool_name, group)
        item = buckets.setdefault(
            key,
            {
                "name": group,
                "tool": tool_name,
                "emoji": emoji,
                "tokens": 0,
                "messages": 0,
                "sessions": set(),
                "attribution": "usage-event",
            },
        )
        item["tokens"] += int(row["protocol_total_tokens"] or 0)
        item["messages"] += int(row["message_count"] or 0)
        item["sessions"].add(row["session_id"])
    result = []
    for item in buckets.values():
        tokens = int(item["tokens"] or 0)
        if tokens < WORKSPACE_USAGE_MIN_TOKENS:
            continue
        if not usage_group_display_allowed(str(item.get("name") or ""), str(item.get("tool") or "")):
            continue
        sessions = item.pop("sessions")
        item["sessions"] = len(sessions)
        result.append(item)
    result.sort(key=lambda item: (-int(item["tokens"] or 0), str(item["tool"]), str(item["name"])))
    return result


def _json_dict(value: object) -> dict:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_workspace_group(tool_key: str, *, raw_path: str, cwd: str, initial_cwd: str) -> str:
    return resolve_usage_group(tool_key, raw_path=raw_path, cwd=cwd, initial_cwd=initial_cwd).group


def _trend30d_from_usage_rows(rows, latest_day: str) -> list[dict]:
    if not latest_day:
        return []
    start_day = date.fromisoformat(latest_day) - timedelta(days=29)
    by_day = {
        (start_day + timedelta(days=offset)).isoformat(): {"上午": 0, "下午": 0, "晚上": 0, "凌晨": 0}
        for offset in range(30)
    }
    timezone = resolve_timezone()
    for row in rows:
        business_day = str(row["business_date"])
        slots = by_day.get(business_day)
        if slots is None:
            continue
        parsed = parse_timestamp(row["occurred_at"])
        if parsed is None:
            continue
        hour = parsed.astimezone(timezone).hour
        tokens = int(row["protocol_total_tokens"] or 0)
        if 4 <= hour < 12:
            slots["上午"] += tokens
        elif 12 <= hour < 18:
            slots["下午"] += tokens
        elif 18 <= hour < 24:
            slots["晚上"] += tokens
        else:
            slots["凌晨"] += tokens
    return [{"date": day, "slots": slots} for day, slots in sorted(by_day.items())]


def write_diary_memory_snapshot(
    paths: RuntimePaths,
    business_date: date,
    payload: dict,
    *,
    source_run_id: int | None,
    status: str = "ready",
) -> None:
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO dashboard_snapshots(
                snapshot_key, projection_type, payload_json, generated_at, source_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                projection_type=excluded.projection_type,
                payload_json=excluded.payload_json,
                generated_at=excluded.generated_at,
                source_run_id=excluded.source_run_id,
                status=excluded.status
            """,
            (
                _diary_memory_snapshot_key(business_date),
                DIARY_MEMORY_PROJECTION,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                datetime.now().astimezone().isoformat(),
                source_run_id,
                status,
            ),
        )


def read_diary_memory_snapshot(paths: RuntimePaths, business_date: date) -> dict | None:
    with connect(paths, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT projection_type, payload_json, generated_at, source_run_id, status
            FROM dashboard_snapshots
            WHERE snapshot_key = ? AND status = 'ready'
            """,
            (_diary_memory_snapshot_key(business_date),),
        ).fetchone()
    if row is None:
        return None
    return {
        "payload": json.loads(row["payload_json"]),
        "projectionType": row["projection_type"],
        "generatedAt": row["generated_at"],
        "sourceRunId": row["source_run_id"],
        "status": row["status"],
    }


def materialize_diary_memory_snapshot(
    paths: RuntimePaths,
    business_date: date,
    source_run_id: int,
    *,
    builder: Callable[[], dict] | None = None,
) -> None:
    """Snapshot the current diary memoryStats contract without reading RAG."""
    if builder is None:
        diary_generator_root = Path(__file__).resolve().parents[1]
        if str(diary_generator_root) not in sys.path:
            sys.path.insert(0, str(diary_generator_root))
        from diary_generator import narrative_pass

        builder = narrative_pass._get_memory_stats_legacy
    write_diary_memory_snapshot(paths, business_date, builder(), source_run_id=source_run_id)


def write_diary_tasks_snapshot(
    paths: RuntimePaths,
    business_date: date,
    payload: dict,
    *,
    source_run_id: int | None,
    status: str = "ready",
) -> None:
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO dashboard_snapshots(
                snapshot_key, projection_type, payload_json, generated_at, source_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                projection_type=excluded.projection_type,
                payload_json=excluded.payload_json,
                generated_at=excluded.generated_at,
                source_run_id=excluded.source_run_id,
                status=excluded.status
            """,
            (
                _diary_tasks_snapshot_key(business_date),
                DIARY_TASKS_PROJECTION,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                datetime.now().astimezone().isoformat(),
                source_run_id,
                status,
            ),
        )


def read_diary_tasks_snapshot(paths: RuntimePaths, business_date: date) -> dict | None:
    with connect(paths, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT projection_type, payload_json, generated_at, source_run_id, status
            FROM dashboard_snapshots
            WHERE snapshot_key = ? AND status = 'ready'
            """,
            (_diary_tasks_snapshot_key(business_date),),
        ).fetchone()
    if row is None:
        return None
    return {
        "payload": json.loads(row["payload_json"]),
        "projectionType": row["projection_type"],
        "generatedAt": row["generated_at"],
        "sourceRunId": row["source_run_id"],
        "status": row["status"],
    }


def materialize_diary_tasks_snapshot(
    paths: RuntimePaths,
    business_date: date,
    source_run_id: int,
    *,
    board_path: Path | None = None,
    builder: Callable[[], dict] | None = None,
) -> None:
    """Snapshot diary task counts from Nova-Task SQLite authority."""
    del board_path
    if builder is None:
        from .nova_task import diary_tasks_snapshot

        builder = lambda: diary_tasks_snapshot(paths)
    write_diary_tasks_snapshot(paths, business_date, builder(), source_run_id=source_run_id)
