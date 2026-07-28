"""Read-model aggregation for shadow usage facts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from .db import connect
from .paths import RuntimePaths


def refresh_daily_usage(paths: RuntimePaths, business_date: date, run_id: int) -> None:
    day = business_date.isoformat()
    with connect(paths) as connection:
        connection.execute("DELETE FROM daily_tool_usage WHERE business_date = ?", (day,))
        connection.execute("DELETE FROM daily_model_usage WHERE business_date = ?", (day,))
        connection.execute("DELETE FROM daily_project_usage WHERE business_date = ?", (day,))
        connection.execute(
            """
            INSERT INTO daily_tool_usage(
                business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
            )
            SELECT
                business_date,
                tool_key,
                SUM(protocol_total_tokens),
                SUM(message_count),
                COUNT(DISTINCT session_id),
                SUM(message_count),
                ?
            FROM usage_events
            WHERE business_date = ?
            GROUP BY business_date, tool_key
            """,
            (run_id, day),
        )
        if connection.execute(
            "SELECT 1 FROM daily_tool_usage WHERE business_date = ? LIMIT 1",
            (day,),
        ).fetchone() is None:
            registered_tool_keys = [
                row["tool_key"]
                for row in connection.execute(
                    "SELECT tool_key FROM tool_sources WHERE tool_key IN ({}) ORDER BY tool_key".format(
                        ",".join("?" for _ in DIARY_TOOL_KEYS)
                    ),
                    DIARY_TOOL_KEYS,
                )
            ]
            connection.executemany(
                """
                INSERT INTO daily_tool_usage(
                    business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
                ) VALUES (?, ?, 0, 0, 0, 0, ?)
                """,
                [(day, tool_key, run_id) for tool_key in registered_tool_keys],
            )
        project_roots = [
            (row["id"], Path(row["canonical_root"]).expanduser().absolute())
            for row in connection.execute("SELECT id, canonical_root FROM projects WHERE enabled = 1")
        ]
        activity = connection.execute(
            """
            SELECT ue.tool_key, ue.protocol_total_tokens, ue.message_count, ue.session_id, s.initial_cwd
            FROM usage_events ue
            JOIN sessions s ON s.id = ue.session_id
            WHERE ue.business_date = ?
            """,
            (day,),
        ).fetchall()
        project_rollup: dict[tuple[str, str], dict[str, object]] = defaultdict(
            lambda: {"tokens": 0, "messages": 0, "sessions": set(), "confidence": "none"}
        )
        for row in activity:
            bucket = "unattributed"
            confidence = "none"
            if row["initial_cwd"]:
                observed = Path(row["initial_cwd"]).expanduser().absolute()
                candidates = [
                    (project_id, root)
                    for project_id, root in project_roots
                    if observed == root or root in observed.parents
                ]
                if candidates:
                    project_id, _ = max(candidates, key=lambda candidate: len(candidate[1].parts))
                    bucket = f"project:{project_id}"
                    confidence = "high"
            rollup = project_rollup[(bucket, row["tool_key"])]
            rollup["tokens"] += row["protocol_total_tokens"]
            rollup["messages"] += row["message_count"]
            rollup["sessions"].add(row["session_id"])
            rollup["confidence"] = confidence
        for (bucket, tool_key), rollup in project_rollup.items():
            connection.execute(
                """
                INSERT INTO daily_project_usage(
                    business_date, project_id_or_bucket, tool_key, tokens,
                    messages, active_sessions, evidence_confidence, source_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day,
                    bucket,
                    tool_key,
                    rollup["tokens"],
                    rollup["messages"],
                    len(rollup["sessions"]),
                    rollup["confidence"],
                    run_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO daily_model_usage(
                business_date, model_key, tool_key, tokens, messages, sessions, source_run_id
            )
            SELECT
                business_date,
                COALESCE(model_key, 'unknown'),
                tool_key,
                SUM(protocol_total_tokens),
                SUM(message_count),
                COUNT(DISTINCT session_id),
                ?
            FROM usage_events
            WHERE business_date = ?
            GROUP BY business_date, COALESCE(model_key, 'unknown'), tool_key
            """,
            (run_id, day),
        )


def daily_tool_totals(paths: RuntimePaths, business_date: date) -> dict[str, dict[str, int]]:
    with connect(paths, read_only=True) as connection:
        rows = connection.execute(
            "SELECT tool_key, tokens, messages, sessions, api_calls FROM daily_tool_usage WHERE business_date = ?",
            (business_date.isoformat(),),
        ).fetchall()
    return {
        row["tool_key"]: {
            "tokens": row["tokens"],
            "messages": row["messages"],
            "sessions": row["sessions"],
            "api_calls": row["api_calls"],
        }
        for row in rows
    }


def daily_project_totals(paths: RuntimePaths, business_date: date) -> list[dict[str, object]]:
    with connect(paths, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT project_id_or_bucket, tool_key, tokens, messages, active_sessions, evidence_confidence
            FROM daily_project_usage
            WHERE business_date = ?
            ORDER BY tokens DESC, project_id_or_bucket, tool_key
            """,
            (business_date.isoformat(),),
        ).fetchall()
    return [dict(row) for row in rows]


DIARY_TOOL_KEYS = (
    "openclaw",
    "gemini-cli",
    "claude-code",
    "hermes",
    "codex",
    "opencode",
    "antigravity",
    "cron",
)


def daily_diary_usage_metrics(paths: RuntimePaths, business_date: date) -> dict | None:
    """Return the existing diary token/model shape from materialized Foundation facts."""
    day = business_date.isoformat()
    with connect(paths, read_only=True) as connection:
        materialized = connection.execute(
            "SELECT 1 FROM daily_tool_usage WHERE business_date = ? LIMIT 1",
            (day,),
        ).fetchone()
        if materialized is None:
            return None
        usage_rows = connection.execute(
            """
            SELECT
                tool_key,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cache_read_tokens) AS cache_read,
                SUM(protocol_total_tokens) AS total_tokens,
                SUM(message_count) AS api_calls,
                SUM(message_count) AS messages_count,
                COUNT(DISTINCT session_id) AS active_sessions
            FROM usage_events
            WHERE business_date = ?
            GROUP BY tool_key
            """,
            (day,),
        ).fetchall()
        model_rows = connection.execute(
            """
            SELECT model_key, SUM(messages) AS calls, SUM(tokens) AS tokens
            FROM daily_model_usage
            WHERE business_date = ?
            GROUP BY model_key
            ORDER BY tokens DESC, model_key
            """,
            (day,),
        ).fetchall()

    by_tool = {row["tool_key"]: row for row in usage_rows}
    metrics: dict[str, object] = {}
    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "total_tokens": 0,
        "api_calls": 0,
        "messages_count": 0,
        "active_sessions": 0,
        "sessions_total": 0,
    }
    for tool_key in DIARY_TOOL_KEYS:
        row = by_tool.get(tool_key)
        if row is None and tool_key in {"opencode", "antigravity"}:
            continue
        values = {
            "input_tokens": int(row["input_tokens"] or 0) if row else 0,
            "output_tokens": int(row["output_tokens"] or 0) if row else 0,
            "cache_read": int(row["cache_read"] or 0) if row else 0,
            "total_tokens": int(row["total_tokens"] or 0) if row else 0,
            "api_calls": int(row["api_calls"] or 0) if row else 0,
            "messages_count": int(row["messages_count"] or 0) if row else 0,
            "active_sessions": int(row["active_sessions"] or 0) if row else 0,
            "sessions_total": int(row["active_sessions"] or 0) if row else 0,
        }
        metrics[tool_key] = values
        for key, value in values.items():
            total[key] += value
    metrics["total"] = total
    metrics["model_usage_list"] = [
        {"model": row["model_key"], "calls": int(row["calls"]), "tokens": int(row["tokens"])}
        for row in model_rows
    ]
    return metrics
