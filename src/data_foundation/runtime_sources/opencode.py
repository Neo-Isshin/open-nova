"""Read-only normalization for OpenCode's local runtime state.

OpenCode has used two local persistence layouts:

* recent releases keep sessions in ``opencode.db`` and publish repeated
  ``message.updated.1`` / ``message.part.updated.1`` events;
* some database versions expose only aggregate ``tokens_*`` session columns;
* older releases keep one JSON object per project, session, message, and part
  below ``storage``.

The event stream contains successive materializations, not token deltas.  This
module therefore keeps only the greatest sequence number for each logical
message/part before exposing records.  The SQLite materialized tables and the
legacy JSON tree are lower-priority fallbacks for IDs not represented by the
event stream. Session aggregates are emitted only when positive message usage
is absent for that session.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .base import (
    DialogueRecord,
    SessionRecord,
    UsageRecord,
    connect_sqlite_read_only,
    json_object,
    namespaced_session_key,
    nonnegative_int,
    sqlite_tables,
    text_content,
    timestamp,
)


_VARIANT = "default"
_DB_RANK = 30
_EVENT_RANK = 40
_STORAGE_RANK = 10


@dataclass
class _Entry:
    data: dict[str, Any]
    rank: int
    order: int = 0
    raw_locator: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Snapshot:
    projects: dict[str, _Entry] = field(default_factory=dict)
    sessions: dict[str, _Entry] = field(default_factory=dict)
    messages: dict[str, _Entry] = field(default_factory=dict)
    parts: dict[str, _Entry] = field(default_factory=dict)


class OpenCodeRuntime:
    """Parse one OpenCode data home without accessing credentials or the network."""

    tool_key = "opencode"
    source_variant = _VARIANT
    usage_status = "available"
    capabilities = frozenset(
        {"session_inventory", "usage_events", "dialogue", "workspace_metadata", "model_metadata"}
    )

    def __init__(self, home: Path):
        self.home = Path(home).expanduser()
        self.db_path = self.home / "opencode.db"
        self.storage_path = self.home / "storage"
        self._snapshot_cache: _Snapshot | None = None

    def artifacts(self) -> tuple[Path, ...]:
        """Return only known non-credential OpenCode artifacts."""

        artifacts: list[Path] = []
        if self.db_path.is_file():
            artifacts.append(self.db_path)
        if self.storage_path.is_dir():
            artifacts.append(self.storage_path)
        return tuple(artifacts)

    def sessions(self) -> Iterable[SessionRecord]:
        snapshot = self._snapshot()
        records: list[SessionRecord] = []
        messages_by_session = _messages_by_session(snapshot)

        for native_id, entry in snapshot.sessions.items():
            data = entry.data
            session_messages = messages_by_session.get(native_id, ())
            started_at = timestamp(data.get("created_at"))
            if started_at is None:
                started_at = _earliest_timestamp(message.data.get("created_at") for message in session_messages)

            last_active_at = _latest_timestamp(
                (
                    data.get("updated_at"),
                    *(
                        _first(
                            message.data.get("completed_at"),
                            message.data.get("updated_at"),
                            message.data.get("created_at"),
                        )
                        for message in session_messages
                    ),
                )
            )
            if last_active_at is None:
                last_active_at = started_at

            model_key = _clean_string(data.get("model_key"))
            agent_key = _clean_string(data.get("agent_key"))
            if model_key is None or agent_key is None:
                for message in reversed(
                    sorted(session_messages, key=lambda item: (item.order, str(item.data.get("id") or "")))
                ):
                    if model_key is None:
                        model_key = _clean_string(message.data.get("model_key"))
                    if agent_key is None:
                        agent_key = _clean_string(message.data.get("agent_key"))
                    if model_key is not None and agent_key is not None:
                        break

            project_id = _clean_string(data.get("project_id"))
            project = snapshot.projects.get(project_id or "")
            project_worktree = _clean_string(project.data.get("worktree")) if project else None
            session_directory = _clean_string(data.get("directory"))
            initial_cwd = _workspace_path(project_worktree, session_directory)

            metadata: dict[str, Any] = {"native_session_id": native_id}
            if project_id:
                metadata["project_id"] = project_id
            if project_worktree:
                metadata["project_worktree"] = project_worktree
            if session_directory and session_directory != initial_cwd:
                metadata["session_directory"] = session_directory
            provider = _clean_string(data.get("model_provider"))
            model_id = _clean_string(data.get("model_id"))
            if provider:
                metadata["model_provider"] = provider
            if model_id:
                metadata["model_id"] = model_id

            records.append(
                SessionRecord(
                    external_session_key=namespaced_session_key(_VARIANT, native_id),
                    started_at=started_at,
                    last_active_at=last_active_at,
                    initial_cwd=initial_cwd,
                    title=_clean_string(data.get("title")),
                    agent_key=agent_key,
                    model_key=model_key,
                    source_variant=_VARIANT,
                    metadata=metadata,
                    raw_locator=dict(entry.raw_locator),
                )
            )

        records.sort(key=lambda item: (_datetime_sort_key(item.started_at), item.external_session_key))
        return tuple(records)

    def usage(self) -> Iterable[UsageRecord]:
        snapshot = self._snapshot()
        records: list[UsageRecord] = []
        sessions_with_message_usage: set[str] = set()

        for native_id, entry in snapshot.messages.items():
            data = entry.data
            if _clean_string(data.get("role")) != "assistant" or not data.get("has_usage"):
                continue
            native_session_id = _clean_string(data.get("session_id"))
            occurred_at = timestamp(
                _first(data.get("completed_at"), data.get("updated_at"), data.get("created_at"))
            )
            if native_session_id is None or occurred_at is None:
                continue

            input_tokens = nonnegative_int(data.get("input_tokens"))
            output_tokens = nonnegative_int(data.get("output_tokens"))
            cache_read_tokens = nonnegative_int(data.get("cache_read_tokens"))
            cache_write_tokens = nonnegative_int(data.get("cache_write_tokens"))
            reasoning_tokens = nonnegative_int(data.get("reasoning_tokens"))
            if (
                input_tokens
                + output_tokens
                + cache_read_tokens
                + cache_write_tokens
                + reasoning_tokens
                <= 0
            ):
                continue
            protocol_total = (
                input_tokens
                + output_tokens
                + cache_read_tokens
                + reasoning_tokens
            )
            metadata: dict[str, Any] = {
                "native_message_id": native_id,
                # OpenCode reports cache reads separately from uncached input.
                # Its normalized output excludes the separately persisted
                # reasoning count. Cache writes remain outside Actanara's
                # cross-tool protocol total.
                "protocol_total_tokens": protocol_total,
                "cache_input_semantics": "separate",
                "usage_granularity": "message",
            }
            provider = _clean_string(data.get("model_provider"))
            model_id = _clean_string(data.get("model_id"))
            if provider:
                metadata["model_provider"] = provider
            if model_id:
                metadata["model_id"] = model_id

            records.append(
                UsageRecord(
                    external_event_key=f"message:{native_id}",
                    external_session_key=namespaced_session_key(_VARIANT, native_session_id),
                    occurred_at=occurred_at,
                    model_key=_clean_string(data.get("model_key")),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    protocol_total_tokens=protocol_total,
                    message_count=1,
                    source_variant=_VARIANT,
                    metadata=metadata,
                    raw_locator=dict(entry.raw_locator),
                )
            )
            sessions_with_message_usage.add(native_session_id)

        # Some OpenCode versions retain only aggregate token columns on the
        # session row. Use that lower-granularity source only when no positive
        # message usage is available for the same session, preventing double
        # counting when both schemas coexist.
        for native_session_id, entry in snapshot.sessions.items():
            if native_session_id in sessions_with_message_usage:
                continue
            data = entry.data
            input_tokens = nonnegative_int(data.get("input_tokens"))
            output_tokens = nonnegative_int(data.get("output_tokens"))
            cache_read_tokens = nonnegative_int(data.get("cache_read_tokens"))
            cache_write_tokens = nonnegative_int(data.get("cache_write_tokens"))
            reasoning_tokens = nonnegative_int(data.get("reasoning_tokens"))
            if (
                input_tokens
                + output_tokens
                + cache_read_tokens
                + cache_write_tokens
                + reasoning_tokens
                <= 0
            ):
                continue
            occurred_at = timestamp(_first(data.get("updated_at"), data.get("created_at")))
            if occurred_at is None:
                continue
            protocol_total = (
                input_tokens
                + output_tokens
                + cache_read_tokens
                + reasoning_tokens
            )
            records.append(
                UsageRecord(
                    external_event_key=f"session:{native_session_id}:aggregate",
                    external_session_key=namespaced_session_key(
                        _VARIANT,
                        native_session_id,
                    ),
                    occurred_at=occurred_at,
                    model_key=_clean_string(data.get("model_key")),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    protocol_total_tokens=protocol_total,
                    message_count=1,
                    source_variant=_VARIANT,
                    metadata={
                        "native_session_id": native_session_id,
                        "protocol_total_tokens": protocol_total,
                        "cache_input_semantics": "separate",
                        "usage_granularity": "session_aggregate",
                        "message_count_semantics": "aggregate_record",
                    },
                    raw_locator=dict(entry.raw_locator),
                )
            )

        records.sort(key=lambda item: (item.occurred_at, item.external_event_key))
        return tuple(records)

    def dialogue(self) -> Iterable[DialogueRecord]:
        snapshot = self._snapshot()
        parts_by_message: dict[str, list[_Entry]] = {}
        for part in snapshot.parts.values():
            message_id = _clean_string(part.data.get("message_id"))
            if message_id:
                parts_by_message.setdefault(message_id, []).append(part)

        records: list[DialogueRecord] = []
        for native_id, entry in snapshot.messages.items():
            data = entry.data
            role = (_clean_string(data.get("role")) or "").lower()
            if role not in {"user", "assistant"}:
                continue
            native_session_id = _clean_string(data.get("session_id"))
            if native_session_id is None:
                continue

            message_parts = parts_by_message.get(native_id, ())
            highest_part_rank = max((part.rank for part in message_parts), default=-1)
            text_parts: list[tuple[int, str, _Entry]] = []
            for part in message_parts:
                # A storage tree can coexist with a migrated database.  Part
                # IDs changed in some migrations, so native part-ID dedupe
                # alone is insufficient; use only the highest-fidelity source
                # represented for this logical message.
                if part.rank != highest_part_rank:
                    continue
                if (_clean_string(part.data.get("type")) or "").lower() != "text":
                    continue
                content = text_content(part.data.get("text"))
                if content:
                    text_parts.append((part.order, content, part))
            text_parts.sort(key=lambda value: (value[0], str(value[2].data.get("id") or "")))

            if text_parts:
                content = "\n".join(value[1] for value in text_parts).strip()
                locator = dict(text_parts[0][2].raw_locator)
                part_ids = [
                    str(value[2].data.get("id"))
                    for value in text_parts
                    if value[2].data.get("id")
                ]
            else:
                # A few early storage versions embedded explicit text blocks in
                # the message object rather than writing separate part files.
                content = text_content(data.get("content"))
                locator = dict(entry.raw_locator)
                part_ids = []
            if not content:
                continue

            metadata: dict[str, Any] = {"native_message_id": native_id}
            if part_ids:
                metadata["native_part_ids"] = part_ids
            records.append(
                DialogueRecord(
                    external_message_key=f"message:{native_id}",
                    external_session_key=namespaced_session_key(_VARIANT, native_session_id),
                    role=role,
                    content=content,
                    occurred_at=timestamp(data.get("created_at")),
                    source_variant=_VARIANT,
                    metadata=metadata,
                    raw_locator=locator,
                )
            )

        records.sort(
            key=lambda item: (
                _datetime_sort_key(item.occurred_at),
                item.external_session_key,
                item.external_message_key,
            )
        )
        return tuple(records)

    def _snapshot(self) -> _Snapshot:
        if self._snapshot_cache is not None:
            return self._snapshot_cache
        snapshot = _Snapshot()
        if self.db_path.is_file():
            self._load_database(snapshot)
        if self.storage_path.is_dir():
            self._load_storage(snapshot)
        _synthesize_missing_sessions(snapshot)
        self._snapshot_cache = snapshot
        return snapshot

    def _load_database(self, snapshot: _Snapshot) -> None:
        try:
            with connect_sqlite_read_only(self.db_path) as connection:
                tables = sqlite_tables(connection)

                if "project" in tables:
                    for index, row in enumerate(_table_rows(connection, "project")):
                        project = _normalize_project(row)
                        native_id = _clean_string(project.get("id"))
                        if native_id:
                            _upsert(
                                snapshot.projects,
                                native_id,
                                project,
                                rank=_DB_RANK,
                                order=index,
                                raw_locator=_db_locator(self.db_path, f"project:{native_id}"),
                            )

                if "session" in tables:
                    for index, row in enumerate(_table_rows(connection, "session")):
                        session = _normalize_session(row)
                        native_id = _clean_string(session.get("id"))
                        if native_id:
                            _upsert(
                                snapshot.sessions,
                                native_id,
                                session,
                                rank=_DB_RANK,
                                order=index,
                                raw_locator=_db_locator(self.db_path, f"session:{native_id}"),
                            )

                if "event" in tables:
                    self._load_events(connection, snapshot)

                # Materialized tables are intentionally lower priority than the
                # final event for the same logical ID.
                if "message" in tables:
                    for index, row in enumerate(_table_rows(connection, "message")):
                        message = _normalize_message(
                            row.get("data"),
                            id_hint=row.get("id"),
                            session_hint=row.get("session_id"),
                            created_hint=row.get("time_created"),
                            updated_hint=row.get("time_updated"),
                        )
                        native_id = _clean_string(message.get("id"))
                        if native_id:
                            _upsert(
                                snapshot.messages,
                                native_id,
                                message,
                                rank=_DB_RANK,
                                order=index,
                                raw_locator=_db_locator(self.db_path, f"message:{native_id}"),
                            )

                if "part" in tables:
                    for index, row in enumerate(_table_rows(connection, "part")):
                        part = _normalize_part(
                            row.get("data"),
                            id_hint=row.get("id"),
                            message_hint=row.get("message_id"),
                            session_hint=row.get("session_id"),
                            created_hint=row.get("time_created"),
                            updated_hint=row.get("time_updated"),
                        )
                        native_id = _clean_string(part.get("id"))
                        if native_id:
                            _upsert(
                                snapshot.parts,
                                native_id,
                                part,
                                rank=_DB_RANK,
                                order=index,
                                raw_locator=_db_locator(self.db_path, f"part:{native_id}"),
                            )
        except (OSError, sqlite3.Error, ValueError):
            # A concurrently replaced, locked, truncated, or otherwise invalid
            # database must not prevent the legacy storage fallback.
            return

    def _load_events(self, connection: sqlite3.Connection, snapshot: _Snapshot) -> None:
        latest_sessions: dict[str, tuple[int, int, dict[str, Any], str]] = {}
        latest_messages: dict[str, tuple[int, int, dict[str, Any], str]] = {}
        latest_parts: dict[str, tuple[int, int, dict[str, Any], str]] = {}

        for index, row in enumerate(_table_rows(connection, "event")):
            event_type = (_clean_string(row.get("type")) or "").lower()
            payload = json_object(row.get("data"))
            if not payload:
                continue
            sequence = _integer(row.get("seq"), default=index)
            event_id = _clean_string(row.get("id")) or f"seq:{sequence}"

            if event_type.startswith(("session.created", "session.updated")):
                info = json_object(payload.get("info"))
                native_id = _clean_string(_first(info.get("id"), payload.get("sessionID")))
                if native_id:
                    _keep_latest(latest_sessions, native_id, sequence, index, payload, event_id)
            elif event_type.startswith("message.updated"):
                info = json_object(payload.get("info"))
                native_id = _clean_string(info.get("id"))
                if native_id:
                    _keep_latest(latest_messages, native_id, sequence, index, payload, event_id)
            elif event_type.startswith("message.part.updated"):
                part = json_object(payload.get("part"))
                native_id = _clean_string(part.get("id"))
                if native_id:
                    _keep_latest(latest_parts, native_id, sequence, index, payload, event_id)

        for native_id, (sequence, _, payload, event_id) in latest_sessions.items():
            info = json_object(payload.get("info"))
            session = _normalize_session(
                info,
                id_hint=native_id,
                session_hint=payload.get("sessionID"),
            )
            _upsert(
                snapshot.sessions,
                native_id,
                session,
                rank=_EVENT_RANK,
                order=sequence,
                raw_locator=_db_locator(self.db_path, f"event:{event_id}"),
            )

        for native_id, (sequence, _, payload, event_id) in latest_messages.items():
            message = _normalize_message(
                payload.get("info"),
                id_hint=native_id,
                session_hint=payload.get("sessionID"),
            )
            _upsert(
                snapshot.messages,
                native_id,
                message,
                rank=_EVENT_RANK,
                order=sequence,
                raw_locator=_db_locator(self.db_path, f"event:{event_id}"),
            )

        for native_id, (sequence, _, payload, event_id) in latest_parts.items():
            part = _normalize_part(
                payload.get("part"),
                id_hint=native_id,
                session_hint=payload.get("sessionID"),
                updated_hint=payload.get("time"),
            )
            _upsert(
                snapshot.parts,
                native_id,
                part,
                rank=_EVENT_RANK,
                order=sequence,
                raw_locator=_db_locator(self.db_path, f"event:{event_id}"),
            )

    def _load_storage(self, snapshot: _Snapshot) -> None:
        for index, (path, raw) in enumerate(_json_objects(self.storage_path / "project")):
            project = _normalize_project(raw)
            native_id = _clean_string(_first(project.get("id"), path.stem))
            if native_id:
                project["id"] = native_id
                _upsert(
                    snapshot.projects,
                    native_id,
                    project,
                    rank=_STORAGE_RANK,
                    order=index,
                    raw_locator=_file_locator(path),
                )

        for index, (path, raw) in enumerate(_json_objects(self.storage_path / "session")):
            session = _normalize_session(raw, id_hint=path.stem)
            native_id = _clean_string(session.get("id"))
            if native_id:
                _upsert(
                    snapshot.sessions,
                    native_id,
                    session,
                    rank=_STORAGE_RANK,
                    order=index,
                    raw_locator=_file_locator(path),
                )
            for embedded_index, embedded in enumerate(_object_list(raw.get("messages"))):
                self._load_embedded_message(
                    snapshot,
                    embedded,
                    session_id=native_id,
                    path=path,
                    order=(index * 10_000) + embedded_index,
                )

        for index, (path, raw) in enumerate(_json_objects(self.storage_path / "message")):
            message = _normalize_message(raw, id_hint=path.stem)
            native_id = _clean_string(message.get("id"))
            if not native_id:
                continue
            _upsert(
                snapshot.messages,
                native_id,
                message,
                rank=_STORAGE_RANK,
                order=index,
                raw_locator=_file_locator(path),
            )
            for embedded_index, embedded in enumerate(_object_list(raw.get("parts"))):
                part = _normalize_part(
                    embedded,
                    message_hint=native_id,
                    session_hint=message.get("session_id"),
                )
                part_id = _clean_string(part.get("id"))
                if part_id:
                    _upsert(
                        snapshot.parts,
                        part_id,
                        part,
                        rank=_STORAGE_RANK,
                        order=(index * 10_000) + embedded_index,
                        raw_locator=_file_locator(path, f"part:{part_id}"),
                    )

        for index, (path, raw) in enumerate(_json_objects(self.storage_path / "part")):
            part = _normalize_part(raw, id_hint=path.stem)
            native_id = _clean_string(part.get("id"))
            if native_id:
                _upsert(
                    snapshot.parts,
                    native_id,
                    part,
                    rank=_STORAGE_RANK,
                    order=index,
                    raw_locator=_file_locator(path),
                )

    @staticmethod
    def _load_embedded_message(
        snapshot: _Snapshot,
        raw: dict[str, Any],
        *,
        session_id: str | None,
        path: Path,
        order: int,
    ) -> None:
        message = _normalize_message(raw, session_hint=session_id)
        native_id = _clean_string(message.get("id"))
        if native_id is None:
            return
        _upsert(
            snapshot.messages,
            native_id,
            message,
            rank=_STORAGE_RANK,
            order=order,
            raw_locator=_file_locator(path, f"message:{native_id}"),
        )
        for part_index, embedded in enumerate(_object_list(raw.get("parts"))):
            part = _normalize_part(
                embedded,
                message_hint=native_id,
                session_hint=session_id,
            )
            part_id = _clean_string(part.get("id"))
            if part_id:
                _upsert(
                    snapshot.parts,
                    part_id,
                    part,
                    rank=_STORAGE_RANK,
                    order=(order * 10_000) + part_index,
                    raw_locator=_file_locator(path, f"part:{part_id}"),
                )


def _table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"project", "session", "event", "message", "part"}:
        return []
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    except sqlite3.Error:
        return []


def _keep_latest(
    target: dict[str, tuple[int, int, dict[str, Any], str]],
    native_id: str,
    sequence: int,
    index: int,
    payload: dict[str, Any],
    event_id: str,
) -> None:
    previous = target.get(native_id)
    if previous is None or (sequence, index) >= (previous[0], previous[1]):
        target[native_id] = (sequence, index, payload, event_id)


def _upsert(
    target: dict[str, _Entry],
    native_id: str,
    data: dict[str, Any],
    *,
    rank: int,
    order: int,
    raw_locator: dict[str, Any],
) -> None:
    existing = target.get(native_id)
    if existing is None:
        target[native_id] = _Entry(dict(data), rank, order, dict(raw_locator))
        return

    if (rank, order) >= (existing.rank, existing.order):
        merged = dict(existing.data)
        merged.update({key: value for key, value in data.items() if value is not None})
        target[native_id] = _Entry(merged, rank, order, dict(raw_locator))
        return

    # A lower-priority source may safely fill absent metadata, but it must not
    # replace the event/materialized representation of the same native ID.
    merged = dict(existing.data)
    for key, value in data.items():
        if value is not None and _is_missing(merged.get(key)):
            merged[key] = value
    existing.data = merged


def _normalize_project(raw: Any) -> dict[str, Any]:
    value = _unwrap(raw)
    time_value = json_object(value.get("time"))
    return {
        "id": _first(value.get("id"), value.get("projectID"), value.get("project_id")),
        "worktree": _first(value.get("worktree"), value.get("directory")),
        "name": value.get("name"),
        "created_at": _first(value.get("time_created"), time_value.get("created")),
        "updated_at": _first(value.get("time_updated"), time_value.get("updated")),
    }


def _normalize_session(
    raw: Any,
    *,
    id_hint: Any = None,
    session_hint: Any = None,
) -> dict[str, Any]:
    value = _unwrap(raw)
    time_value = json_object(value.get("time"))
    model_key, provider, model_id = _model_parts(
        _first(
            value.get("model"),
            value.get("model_key"),
            {
                "providerID": _first(value.get("providerID"), value.get("provider_id")),
                "modelID": _first(value.get("modelID"), value.get("model_id")),
            },
        )
    )
    return {
        "id": _first(value.get("id"), value.get("sessionID"), value.get("session_id"), id_hint, session_hint),
        "project_id": _first(value.get("projectID"), value.get("project_id")),
        "directory": _first(value.get("directory"), value.get("cwd")),
        "title": value.get("title"),
        "created_at": _first(value.get("time_created"), value.get("created_at"), time_value.get("created")),
        "updated_at": _first(
            value.get("time_updated"),
            value.get("updated_at"),
            time_value.get("updated"),
            time_value.get("completed"),
        ),
        "agent_key": _first(value.get("agent"), value.get("agent_key"), value.get("mode")),
        "model_key": model_key,
        "model_provider": provider,
        "model_id": model_id,
        "input_tokens": value.get("tokens_input"),
        "output_tokens": value.get("tokens_output"),
        "reasoning_tokens": value.get("tokens_reasoning"),
        "cache_read_tokens": value.get("tokens_cache_read"),
        "cache_write_tokens": value.get("tokens_cache_write"),
    }


def _normalize_message(
    raw: Any,
    *,
    id_hint: Any = None,
    session_hint: Any = None,
    created_hint: Any = None,
    updated_hint: Any = None,
) -> dict[str, Any]:
    value = _unwrap(raw)
    time_value = json_object(value.get("time"))

    raw_tokens = value.get("tokens")
    token_value = json_object(raw_tokens)
    cache_value = json_object(_first(token_value.get("cache"), value.get("cache")))
    has_usage = isinstance(raw_tokens, Mapping) or (
        isinstance(raw_tokens, (str, bytes)) and bool(token_value)
    )

    explicit_model: Any = value.get("model")
    if _is_missing(explicit_model):
        explicit_model = {
            "providerID": _first(value.get("providerID"), value.get("provider_id")),
            "modelID": _first(value.get("modelID"), value.get("model_id")),
        }
    model_key, provider, model_id = _model_parts(explicit_model)

    content: Any = _first(value.get("content"), value.get("text"))
    if content is None and isinstance(value.get("parts"), list):
        content = value.get("parts")

    return {
        "id": _first(value.get("id"), value.get("messageID"), value.get("message_id"), id_hint),
        "session_id": _first(value.get("sessionID"), value.get("session_id"), session_hint),
        "role": (_clean_string(value.get("role")) or "").lower() or None,
        "created_at": _first(
            value.get("time_created"),
            value.get("created_at"),
            time_value.get("created"),
            created_hint,
        ),
        "updated_at": _first(
            value.get("time_updated"),
            value.get("updated_at"),
            time_value.get("updated"),
            updated_hint,
        ),
        "completed_at": _first(
            value.get("time_completed"),
            value.get("completed_at"),
            time_value.get("completed"),
            time_value.get("end"),
        ),
        "agent_key": _first(value.get("agent"), value.get("agent_key"), value.get("mode")),
        "model_key": model_key,
        "model_provider": provider,
        "model_id": model_id,
        "has_usage": has_usage,
        "input_tokens": _first(
            token_value.get("input"),
            token_value.get("input_tokens"),
            value.get("tokens_input"),
        ),
        "output_tokens": _first(
            token_value.get("output"),
            token_value.get("output_tokens"),
            value.get("tokens_output"),
        ),
        "reasoning_tokens": _first(
            token_value.get("reasoning"),
            token_value.get("reasoning_tokens"),
            value.get("tokens_reasoning"),
        ),
        "cache_read_tokens": _first(
            cache_value.get("read"),
            cache_value.get("read_tokens"),
            token_value.get("cacheRead"),
            token_value.get("cache_read"),
            token_value.get("cache_read_tokens"),
            value.get("tokens_cache_read"),
        ),
        "cache_write_tokens": _first(
            cache_value.get("write"),
            cache_value.get("write_tokens"),
            token_value.get("cacheWrite"),
            token_value.get("cache_write"),
            token_value.get("cache_write_tokens"),
            value.get("tokens_cache_write"),
        ),
        "content": content,
    }


def _normalize_part(
    raw: Any,
    *,
    id_hint: Any = None,
    message_hint: Any = None,
    session_hint: Any = None,
    created_hint: Any = None,
    updated_hint: Any = None,
) -> dict[str, Any]:
    value = _unwrap(raw)
    time_value = json_object(value.get("time"))
    return {
        "id": _first(value.get("id"), value.get("partID"), value.get("part_id"), id_hint),
        "message_id": _first(
            value.get("messageID"),
            value.get("message_id"),
            message_hint,
        ),
        "session_id": _first(
            value.get("sessionID"),
            value.get("session_id"),
            session_hint,
        ),
        "type": (_clean_string(value.get("type")) or "").lower() or None,
        "text": _first(value.get("text"), value.get("content")),
        "created_at": _first(
            value.get("time_created"),
            value.get("created_at"),
            time_value.get("created"),
            time_value.get("start"),
            created_hint,
        ),
        "updated_at": _first(
            value.get("time_updated"),
            value.get("updated_at"),
            time_value.get("updated"),
            time_value.get("end"),
            updated_hint,
        ),
    }


def _model_parts(value: Any) -> tuple[str | None, str | None, str | None]:
    model_value = json_object(value)
    if model_value:
        provider = _clean_string(
            _first(
                model_value.get("providerID"),
                model_value.get("providerId"),
                model_value.get("provider_id"),
                model_value.get("provider"),
            )
        )
        model_id = _clean_string(
            _first(
                model_value.get("modelID"),
                model_value.get("modelId"),
                model_value.get("model_id"),
                model_value.get("id"),
                model_value.get("name"),
            )
        )
    else:
        provider = None
        model_id = _clean_string(value)

    if model_id and "/" in model_id and provider is None:
        provider, model_id = model_id.split("/", 1)
    if provider and model_id:
        if model_id.startswith(f"{provider}/"):
            model_id = model_id[len(provider) + 1 :]
        return f"{provider}/{model_id}", provider, model_id
    return model_id, provider, model_id


def _messages_by_session(snapshot: _Snapshot) -> dict[str, tuple[_Entry, ...]]:
    grouped: dict[str, list[_Entry]] = {}
    for message in snapshot.messages.values():
        session_id = _clean_string(message.data.get("session_id"))
        if session_id:
            grouped.setdefault(session_id, []).append(message)
    return {
        session_id: tuple(sorted(messages, key=lambda item: (item.order, str(item.data.get("id") or ""))))
        for session_id, messages in grouped.items()
    }


def _synthesize_missing_sessions(snapshot: _Snapshot) -> None:
    for message in snapshot.messages.values():
        session_id = _clean_string(message.data.get("session_id"))
        if session_id is None or session_id in snapshot.sessions:
            continue
        data = {
            "id": session_id,
            "created_at": message.data.get("created_at"),
            "updated_at": _first(
                message.data.get("completed_at"),
                message.data.get("updated_at"),
                message.data.get("created_at"),
            ),
            "agent_key": message.data.get("agent_key"),
            "model_key": message.data.get("model_key"),
            "model_provider": message.data.get("model_provider"),
            "model_id": message.data.get("model_id"),
        }
        snapshot.sessions[session_id] = _Entry(
            data=data,
            rank=message.rank,
            order=message.order,
            raw_locator=dict(message.raw_locator),
        )


def _json_objects(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not root.is_dir():
        return []
    result: list[tuple[Path, dict[str, Any]]] = []
    try:
        paths = sorted(path for path in root.rglob("*.json") if path.is_file())
    except OSError:
        return []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(raw, dict):
            result.append((path, raw))
    return result


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _unwrap(raw: Any) -> dict[str, Any]:
    value = json_object(raw)
    info = json_object(value.get("info"))
    return info or value


def _workspace_path(project_worktree: str | None, session_directory: str | None) -> str | None:
    # ``global`` projects use "/" as a sentinel, while their session directory
    # still contains the actual working directory.
    if project_worktree and not (project_worktree == "/" and session_directory):
        return project_worktree
    return session_directory or project_worktree


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _clean_string(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _earliest_timestamp(values: Iterable[Any]) -> datetime | None:
    parsed = [item for item in (timestamp(value) for value in values) if item is not None]
    return min(parsed) if parsed else None


def _latest_timestamp(values: Iterable[Any]) -> datetime | None:
    parsed = [item for item in (timestamp(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


def _datetime_sort_key(value: datetime | None) -> float:
    return value.timestamp() if value is not None else float("inf")


def _db_locator(path: Path, locator: str) -> dict[str, Any]:
    return {"path": str(path), "locator": locator, "format": "sqlite"}


def _file_locator(path: Path, locator: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "format": "json"}
    if locator:
        result["locator"] = locator
    return result


__all__ = ["OpenCodeRuntime"]
