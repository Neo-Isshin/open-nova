"""Shadow ingestion service for normalized facts; it is not a production reader."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .adapters.registry import ToolRegistry
from .adapters.usage import UsageAdapter, default_usage_adapters
from .aggregate import refresh_daily_usage
from .db import connect, migrate, seed_projects_from_registry
from .jobs import begin_ingestion_run, finish_ingestion_run
from .observations import observe_non_rag_assets
from .paths import RuntimePaths
from .time import business_date_for

DISPLAY_NAMES = {
    "openclaw": "OpenClaw",
    "claude-code": "Claude Code",
    "codex": "Codex",
    "gemini-cli": "Gemini CLI",
    "hermes": "Hermes",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
    "cron": "Cron",
}


@dataclass(frozen=True)
class ShadowIngestionResult:
    run_id: int
    business_date: date
    artifacts_seen: int
    events_seen: int
    events_in_window: int
    errors: int


def _business_dates(start_date: date, end_date: date) -> tuple[date, ...]:
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    return tuple(start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1))


def _upsert_artifact(paths: RuntimePaths, adapter: UsageAdapter, artifact, run_id: int) -> int:
    stat = artifact.path.stat()
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO source_artifacts(
                tool_key, canonical_path, artifact_type, fingerprint, byte_size,
                modified_at, last_ingestion_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'seen')
            ON CONFLICT(tool_key, canonical_path) DO UPDATE SET
                artifact_type=excluded.artifact_type,
                fingerprint=excluded.fingerprint,
                byte_size=excluded.byte_size,
                modified_at=excluded.modified_at,
                last_ingestion_run_id=excluded.last_ingestion_run_id,
                status=excluded.status
            """,
            (
                adapter.tool_key,
                str(artifact.path.absolute()),
                artifact.artifact_type,
                adapter.fingerprint(artifact),
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                run_id,
            ),
        )
        return int(
            connection.execute(
                "SELECT id FROM source_artifacts WHERE tool_key = ? AND canonical_path = ?",
                (adapter.tool_key, str(artifact.path.absolute())),
            ).fetchone()["id"]
        )


def _write_error(paths: RuntimePaths, run_id: int, tool_key: str, artifact_id: int | None, error: Exception) -> None:
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_errors(
                ingestion_run_id, tool_key, artifact_id, error_code, message, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tool_key,
                artifact_id,
                error.__class__.__name__,
                str(error)[:1000],
                datetime.now().astimezone().isoformat(),
            ),
        )


def _upsert_session(paths: RuntimePaths, event, artifact_id: int) -> tuple[int, str, str | None]:
    payload = event.payload
    metadata = dict(payload.get("metadata") or {})
    for key in ("title", "model_key", "source_variant", "usage_status", "dialogue_status"):
        if payload.get(key) not in (None, ""):
            metadata[key] = payload[key]
    if payload.get("raw_locator"):
        metadata["raw_locator"] = payload["raw_locator"]
    occurred = event.occurred_at.isoformat()
    started_at = _event_timestamp(payload.get("started_at")) or occurred
    last_active_at = _event_timestamp(payload.get("last_active_at")) or occurred
    cwd = str(payload.get("initial_cwd") or metadata.get("cwd") or "").strip() or None
    agent_key = str(payload.get("agent_key") or metadata.get("agent_key") or "").strip() or None
    with connect(paths) as connection:
        existing = connection.execute(
            "SELECT metadata_json FROM sessions WHERE tool_key = ? AND external_session_key = ?",
            (event.tool_key, event.external_session_key),
        ).fetchone()
        if existing is not None:
            try:
                prior_metadata = json.loads(existing["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                prior_metadata = {}
            if isinstance(prior_metadata, dict):
                metadata = {**prior_metadata, **metadata}
        connection.execute(
            """
            INSERT INTO sessions(
                tool_key, external_session_key, started_at, last_active_at,
                initial_cwd, agent_key, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_key, external_session_key) DO UPDATE SET
                started_at=CASE
                    WHEN sessions.started_at IS NULL OR excluded.started_at < sessions.started_at
                    THEN excluded.started_at ELSE sessions.started_at END,
                last_active_at=CASE
                    WHEN sessions.last_active_at IS NULL OR excluded.last_active_at > sessions.last_active_at
                    THEN excluded.last_active_at ELSE sessions.last_active_at END,
                initial_cwd=COALESCE(sessions.initial_cwd, excluded.initial_cwd),
                agent_key=COALESCE(sessions.agent_key, excluded.agent_key),
                metadata_json=excluded.metadata_json
            """,
            (
                event.tool_key,
                event.external_session_key,
                started_at,
                last_active_at,
                cwd,
                agent_key,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        session_id = connection.execute(
            "SELECT id FROM sessions WHERE tool_key = ? AND external_session_key = ?",
            (event.tool_key, event.external_session_key),
        ).fetchone()["id"]
    return int(session_id), last_active_at, cwd


def _write_event(paths: RuntimePaths, event, artifact_id: int) -> None:
    payload = event.payload
    session_id, session_last_active, cwd = _upsert_session(paths, event, artifact_id)
    occurred = event.occurred_at.isoformat()
    occurred_dt = event.occurred_at
    day = business_date_for(occurred_dt, paths=paths).isoformat()
    metadata = payload.get("metadata") or {}
    if event.event_type == "session":
        _write_activity_evidence(
            paths,
            session_id=session_id,
            occurred_at=session_last_active,
            business_date=day,
            cwd=cwd,
            artifact_id=artifact_id,
            raw_locator=payload.get("raw_locator") or {},
            confidence=str(payload.get("workspace_confidence") or "high"),
        )
        return
    if event.event_type != "usage":
        raise ValueError(f"unsupported normalized event type: {event.event_type}")
    with connect(paths) as connection:
        explicit_protocol_total = payload.get("protocol_total_tokens")
        protocol_total = (
            int(explicit_protocol_total)
            if explicit_protocol_total is not None
            else payload["input_tokens"] + payload["output_tokens"] + payload["cache_read_tokens"]
        )
        connection.execute(
            """
            INSERT INTO usage_events(
                tool_key, session_id, external_event_key, occurred_at, business_date, model_key,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                reasoning_tokens, protocol_total_tokens, message_count, source_artifact_id,
                raw_locator_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_key, external_event_key) DO UPDATE SET
                session_id=excluded.session_id,
                occurred_at=excluded.occurred_at,
                business_date=excluded.business_date,
                model_key=excluded.model_key,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cache_read_tokens=excluded.cache_read_tokens,
                cache_write_tokens=excluded.cache_write_tokens,
                reasoning_tokens=excluded.reasoning_tokens,
                protocol_total_tokens=excluded.protocol_total_tokens,
                message_count=excluded.message_count,
                source_artifact_id=excluded.source_artifact_id,
                raw_locator_json=excluded.raw_locator_json,
                metadata_json=excluded.metadata_json
            """,
            (
                event.tool_key,
                session_id,
                event.external_event_key,
                occurred,
                day,
                payload["model_key"],
                payload["input_tokens"],
                payload["output_tokens"],
                payload["cache_read_tokens"],
                payload["cache_write_tokens"],
                payload["reasoning_tokens"],
                protocol_total,
                payload["message_count"],
                artifact_id,
                json.dumps(payload["raw_locator"], sort_keys=True),
                json.dumps(metadata, sort_keys=True),
            ),
        )
    _write_activity_evidence(
        paths,
        session_id=session_id,
        occurred_at=occurred,
        business_date=day,
        cwd=cwd,
        artifact_id=artifact_id,
        raw_locator=payload.get("raw_locator") or {},
        confidence="high",
    )


def _write_activity_evidence(
    paths: RuntimePaths,
    *,
    session_id: int,
    occurred_at: str,
    business_date: str,
    cwd: str | None,
    artifact_id: int,
    raw_locator: dict,
    confidence: str,
) -> None:
    if not cwd or not Path(cwd).is_absolute():
        return
    normalized = str(Path(cwd).expanduser().absolute())
    with connect(paths) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO activity_evidence(
                session_id, occurred_at, business_date, evidence_type,
                observed_path, normalized_path, source_artifact_id,
                raw_locator_json, confidence
            ) VALUES (?, ?, ?, 'cwd', ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                occurred_at,
                business_date,
                cwd,
                normalized,
                artifact_id,
                json.dumps(raw_locator, sort_keys=True),
                confidence if confidence in {"high", "medium", "low", "fallback"} else "low",
            ),
        )


def _event_timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def run_shadow_ingestion(
    paths: RuntimePaths,
    business_date: date,
    *,
    adapters: Iterable[UsageAdapter] | None = None,
    trigger: str = "shadow-manual",
    observe_assets: bool = True,
) -> ShadowIngestionResult:
    return run_shadow_period_ingestion(
        paths,
        business_date,
        business_date,
        adapters=adapters,
        trigger=trigger,
        observe_assets=observe_assets,
    )


def run_shadow_period_ingestion(
    paths: RuntimePaths,
    start_date: date,
    end_date: date,
    *,
    adapters: Iterable[UsageAdapter] | None = None,
    trigger: str = "shadow-period",
    observe_assets: bool = True,
) -> ShadowIngestionResult:
    """Ingest one inclusive business-date interval with a single source scan."""
    dates = _business_dates(start_date, end_date)
    selected_dates = set(dates)
    migrate(paths)
    seed_projects_from_registry(paths)
    selected = tuple(adapters or default_usage_adapters(paths))
    registry = ToolRegistry(paths)
    for adapter in selected:
        registry.register(
            tool_key=adapter.tool_key,
            display_name=DISPLAY_NAMES.get(adapter.tool_key, adapter.tool_key),
            adapter_version=adapter.adapter_version,
            capabilities=adapter.capabilities,
            enabled=True,
        )
    run_id = begin_ingestion_run(
        paths,
        trigger_type=trigger,
        business_date=end_date,
        adapter_versions={adapter.tool_key: adapter.adapter_version for adapter in selected},
    )
    artifacts_seen = events_seen = events_in_window = errors = 0
    try:
        for adapter in selected:
            for artifact in adapter.discover_sources():
                artifacts_seen += 1
                artifact_id = None
                try:
                    artifact_id = _upsert_artifact(paths, adapter, artifact, run_id)
                    for event in adapter.read_incremental(artifact, None):
                        events_seen += 1
                        if business_date_for(event.occurred_at, paths=paths) not in selected_dates:
                            continue
                        _write_event(paths, event, artifact_id)
                        events_in_window += 1
                except Exception as error:
                    errors += 1
                    _write_error(paths, run_id, adapter.tool_key, artifact_id, error)
        for business_date in dates:
            refresh_daily_usage(paths, business_date, run_id)
        if observe_assets:
            observe_non_rag_assets(paths, end_date, run_id)
        finish_ingestion_run(paths, run_id, status="completed" if not errors else "completed_with_errors")
    except Exception as error:
        finish_ingestion_run(paths, run_id, status="failed", error_summary=str(error))
        raise
    return ShadowIngestionResult(run_id, end_date, artifacts_seen, events_seen, events_in_window, errors)
