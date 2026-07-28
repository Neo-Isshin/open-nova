"""Read-only Antigravity CLI, IDE, and app local-runtime parsing."""

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
    namespaced_session_key,
    sqlite_columns,
    sqlite_tables,
    timestamp,
)

_VARIANTS = ("cli", "ide", "app")


@dataclass
class _SessionState:
    native_id: str
    variant: str
    history_started_at: datetime | None = None
    history_last_active_at: datetime | None = None
    fallback_started_at: datetime | None = None
    fallback_last_active_at: datetime | None = None
    initial_cwd: str | None = None
    title: str | None = None
    history_records: int = 0
    conversation_paths: list[Path] = field(default_factory=list)
    conversation_formats: set[str] = field(default_factory=set)
    readable_databases: int = 0
    unreadable_databases: int = 0
    brain_root: Path | None = None
    brain_artifacts: list[Path] = field(default_factory=list)

    def observe_history(
        self,
        *,
        occurred_at: datetime | None,
        workspace: str | None,
        display: str | None,
    ) -> None:
        self.history_records += 1
        if occurred_at is not None:
            if self.history_started_at is None or occurred_at < self.history_started_at:
                self.history_started_at = occurred_at
                if workspace:
                    self.initial_cwd = workspace
                if display:
                    self.title = display
            if self.history_last_active_at is None or occurred_at > self.history_last_active_at:
                self.history_last_active_at = occurred_at
        if self.initial_cwd is None and workspace:
            self.initial_cwd = workspace
        if self.title is None and display:
            self.title = display

    def observe_fallback_time(self, occurred_at: datetime | None) -> None:
        if occurred_at is None:
            return
        if self.fallback_started_at is None or occurred_at < self.fallback_started_at:
            self.fallback_started_at = occurred_at
        if self.fallback_last_active_at is None or occurred_at > self.fallback_last_active_at:
            self.fallback_last_active_at = occurred_at


@dataclass(frozen=True)
class _HistoryEntry:
    line_number: int
    native_id: str
    display: str | None
    occurred_at: datetime | None
    workspace: str | None
    entry_type: str | None
    path: Path


@dataclass(frozen=True)
class _ProtobufField:
    number: int
    wire_type: int
    value: int | bytes


class AntigravityRuntime:
    """Normalize all local Antigravity variants under one tool family."""

    tool_key = "antigravity"
    usage_status = "local-partial"
    capabilities = frozenset(
        {"session_inventory", "usage_events_partial", "dialogue_partial", "workspace_metadata"}
    )

    def __init__(self, variant_homes: Mapping[str, Path]):
        self.variant_homes = {
            variant: Path(variant_homes[variant]).expanduser()
            for variant in _VARIANTS
            if variant in variant_homes
        }

    def artifacts(self) -> tuple[Path, ...]:
        """Return existing primary sources and brain assets without reading them."""

        discovered: list[Path] = []
        for variant in _VARIANTS:
            home = self.variant_homes.get(variant)
            if home is None:
                continue
            history_path = home / "history.jsonl"
            if history_path.is_file():
                discovered.append(history_path)
            conversations = home / "conversations"
            if conversations.is_dir():
                discovered.extend(self._conversation_paths(conversations))
            brain = home / "brain"
            if brain.is_dir():
                try:
                    discovered.extend(
                        sorted(
                            (path for path in brain.rglob("*") if path.is_file()),
                            key=str,
                        )
                    )
                except OSError:
                    pass
        return tuple(dict.fromkeys(discovered))

    def sessions(self) -> Iterable[SessionRecord]:
        states: dict[tuple[str, str], _SessionState] = {}

        for variant in _VARIANTS:
            home = self.variant_homes.get(variant)
            if home is None:
                continue

            for entry in self._history_entries(variant, home):
                state = states.setdefault(
                    (variant, entry.native_id),
                    _SessionState(native_id=entry.native_id, variant=variant),
                )
                state.observe_history(
                    occurred_at=entry.occurred_at,
                    workspace=entry.workspace,
                    display=entry.display,
                )

            conversations = home / "conversations"
            if conversations.is_dir():
                for path in self._conversation_paths(conversations):
                    native_id = path.stem.strip()
                    if not native_id:
                        continue
                    state = states.setdefault(
                        (variant, native_id),
                        _SessionState(native_id=native_id, variant=variant),
                    )
                    state.conversation_paths.append(path)
                    state.conversation_formats.add(path.suffix.removeprefix(".").lower())
                    if path.suffix.lower() == ".db":
                        started_at, last_active_at, readable = self._database_time_bounds(
                            path
                        )
                        if readable:
                            state.readable_databases += 1
                        else:
                            state.unreadable_databases += 1
                        state.observe_fallback_time(started_at)
                        state.observe_fallback_time(last_active_at)
                        if started_at is None and last_active_at is None:
                            state.observe_fallback_time(self._file_modified_at(path))
                    else:
                        state.observe_fallback_time(self._file_modified_at(path))

            brain = home / "brain"
            if brain.is_dir():
                try:
                    brain_sessions = sorted(
                        (path for path in brain.iterdir() if path.is_dir()),
                        key=lambda path: path.name,
                    )
                except OSError:
                    brain_sessions = []
                for session_root in brain_sessions:
                    native_id = session_root.name.strip()
                    if not native_id:
                        continue
                    state = states.setdefault(
                        (variant, native_id),
                        _SessionState(native_id=native_id, variant=variant),
                    )
                    state.brain_root = session_root
                    try:
                        state.brain_artifacts = sorted(
                            (path for path in session_root.rglob("*") if path.is_file()),
                            key=str,
                        )
                    except OSError:
                        state.brain_artifacts = []
                    brain_times = [
                        observed
                        for observed in (
                            self._file_modified_at(path) for path in state.brain_artifacts
                        )
                        if observed is not None
                    ]
                    if brain_times:
                        state.observe_fallback_time(min(brain_times))
                        state.observe_fallback_time(max(brain_times))
                    else:
                        state.observe_fallback_time(self._file_modified_at(session_root))

        for variant, native_id in sorted(states):
            state = states[(variant, native_id)]
            formats = sorted(state.conversation_formats)
            metadata: dict[str, Any] = {
                "runtime_family": self.tool_key,
                "native_session_id": native_id,
                "source_variant": variant,
                "history_records": state.history_records,
                "conversation_formats": formats,
                "brain_asset_count": len(state.brain_artifacts),
                "usage_status": (
                    "available" if state.readable_databases else "unavailable"
                ),
            }
            if "pb" in formats:
                metadata["encrypted_conversation"] = True
            if state.unreadable_databases:
                metadata["unreadable_database_count"] = state.unreadable_databases
            raw_locator: dict[str, Any] = {}
            history_path = self.variant_homes[variant] / "history.jsonl"
            if state.history_records and history_path.is_file():
                raw_locator["history_path"] = str(history_path)
            if state.conversation_paths:
                raw_locator["conversation_paths"] = [
                    str(path) for path in sorted(state.conversation_paths, key=str)
                ]
            if state.brain_root is not None:
                raw_locator["brain_root"] = str(state.brain_root)

            yield SessionRecord(
                external_session_key=namespaced_session_key(variant, native_id),
                started_at=state.history_started_at or state.fallback_started_at,
                last_active_at=state.history_last_active_at or state.fallback_last_active_at,
                initial_cwd=state.initial_cwd,
                title=state.title,
                source_variant=variant,
                metadata=metadata,
                raw_locator=raw_locator,
            )

    def usage(self) -> Iterable[UsageRecord]:
        """Read non-cumulative telemetry, deduplicated across every variant."""

        seen_tracking_ids: set[str] = set()
        for variant in _VARIANTS:
            home = self.variant_homes.get(variant)
            if home is None:
                continue
            conversations = home / "conversations"
            if not conversations.is_dir():
                continue
            for path in self._conversation_paths(conversations):
                if path.suffix.lower() != ".db":
                    # Conversation protobufs may be encrypted.  Their names are
                    # useful session evidence, but their contents are opaque.
                    continue
                for record in self._database_usage(path, variant):
                    tracking_id = str(record.metadata.get("tracking_id") or "")
                    if not tracking_id or tracking_id in seen_tracking_ids:
                        continue
                    seen_tracking_ids.add(tracking_id)
                    yield record

    def dialogue(self) -> Iterable[DialogueRecord]:
        """Expose only CLI history display strings with a stable session link."""

        home = self.variant_homes.get("cli")
        if home is None:
            return
        for entry in self._history_entries("cli", home):
            if not entry.display:
                continue
            metadata: dict[str, Any] = {"source": "history_display"}
            if entry.entry_type:
                metadata["history_type"] = entry.entry_type
            if entry.workspace:
                metadata["workspace"] = entry.workspace
            yield DialogueRecord(
                external_message_key=f"cli:history:line:{entry.line_number}",
                external_session_key=namespaced_session_key("cli", entry.native_id),
                role="user",
                content=entry.display,
                occurred_at=entry.occurred_at,
                source_variant="cli",
                metadata=metadata,
                raw_locator={
                    "path": str(entry.path),
                    "line": entry.line_number,
                },
            )

    @staticmethod
    def _conversation_paths(conversations: Path) -> list[Path]:
        try:
            return sorted(
                (
                    path
                    for path in conversations.iterdir()
                    if path.is_file() and path.suffix.lower() in {".db", ".pb"}
                ),
                key=lambda path: (path.name, str(path)),
            )
        except OSError:
            return []

    @staticmethod
    def _history_entries(variant: str, home: Path) -> Iterable[_HistoryEntry]:
        if variant != "cli":
            return
        path = home / "history.jsonl"
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, dict):
                    continue
                native_id = str(
                    value.get("conversationId")
                    or value.get("conversation_id")
                    or value.get("sessionId")
                    or ""
                ).strip()
                if not native_id:
                    continue
                display = _optional_text(value.get("display"))
                workspace = _optional_text(value.get("workspace"))
                entry_type = _optional_text(value.get("type"))
                yield _HistoryEntry(
                    line_number=line_number,
                    native_id=native_id,
                    display=display,
                    occurred_at=timestamp(value.get("timestamp")),
                    workspace=workspace,
                    entry_type=entry_type,
                    path=path,
                )

    @staticmethod
    def _file_modified_at(path: Path) -> datetime | None:
        try:
            return timestamp(path.stat().st_mtime)
        except OSError:
            return None

    def _database_time_bounds(
        self, path: Path
    ) -> tuple[datetime | None, datetime | None, bool]:
        observed: list[datetime] = []
        try:
            with connect_sqlite_read_only(path) as connection:
                if "steps" not in sqlite_tables(connection):
                    return None, None, False
                columns = sqlite_columns(connection, "steps")
                if "metadata" not in columns:
                    return None, None, True
                for row in connection.execute(
                    "SELECT metadata FROM steps WHERE metadata IS NOT NULL"
                ):
                    occurred_at = _protobuf_timestamp(row[0])
                    if occurred_at is not None:
                        observed.append(occurred_at)
        except (OSError, sqlite3.Error):
            return None, None, False
        if not observed:
            return None, None, True
        return min(observed), max(observed), True

    def _database_usage(self, path: Path, variant: str) -> Iterable[UsageRecord]:
        fallback_time = self._file_modified_at(path)
        try:
            with connect_sqlite_read_only(path) as connection:
                if "steps" not in sqlite_tables(connection):
                    return
                columns = sqlite_columns(connection, "steps")
                if "step_payload" not in columns:
                    return
                metadata_expression = "metadata" if "metadata" in columns else "NULL"
                rows = connection.execute(
                    f"""
                    SELECT step_payload, {metadata_expression} AS metadata
                    FROM steps
                    WHERE step_payload IS NOT NULL
                    """
                )
                for row_number, row in enumerate(rows, 1):
                    occurred_at = _protobuf_timestamp(row["metadata"]) or fallback_time
                    if occurred_at is None:
                        continue
                    for telemetry_number, telemetry in enumerate(
                        _telemetry_blocks(row["step_payload"]), 1
                    ):
                        tracking_id = _length_delimited_text(telemetry, 11)
                        if not tracking_id:
                            continue
                        input_tokens = _varint_field(telemetry, 1)
                        output_tokens = _varint_field(telemetry, 2)
                        cache_read_tokens = _varint_field(telemetry, 3)
                        reasoning_tokens = _varint_field(telemetry, 9)
                        tool_tokens = _varint_field(telemetry, 10)
                        protocol_total = (
                            input_tokens
                            + output_tokens
                            + cache_read_tokens
                            + reasoning_tokens
                            + tool_tokens
                        )
                        if protocol_total <= 0:
                            continue
                        yield UsageRecord(
                            external_event_key=f"tracking:{tracking_id}",
                            external_session_key=namespaced_session_key(
                                variant, path.stem
                            ),
                            occurred_at=occurred_at,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            reasoning_tokens=reasoning_tokens,
                            tool_tokens=tool_tokens,
                            protocol_total_tokens=protocol_total,
                            source_variant=variant,
                            metadata={
                                "tracking_id": tracking_id,
                                "timestamp_source": (
                                    "protobuf_metadata"
                                    if _protobuf_timestamp(row["metadata"]) is not None
                                    else "database_mtime"
                                ),
                                "token_field_semantics": {
                                    "1": "input_tokens",
                                    "2": "output_tokens",
                                    "3": "cache_read_tokens",
                                    "5": "cumulative_prompt_tokens_ignored",
                                    "9": "reasoning_tokens",
                                    "10": "tool_tokens",
                                    "11": "tracking_id",
                                },
                            },
                            raw_locator={
                                "path": str(path),
                                "row": row_number,
                                "telemetry": telemetry_number,
                            },
                        )
        except (OSError, sqlite3.Error):
            return


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _read_varint(data: bytes, position: int) -> tuple[int, int] | None:
    result = 0
    shift = 0
    while position < len(data) and shift < 64:
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, position
        shift += 7
    return None


def _protobuf_fields(value: Any) -> list[_ProtobufField] | None:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    data = bytes(value)
    position = 0
    fields: list[_ProtobufField] = []
    while position < len(data):
        parsed_key = _read_varint(data, position)
        if parsed_key is None:
            return None
        key, position = parsed_key
        number = key >> 3
        wire_type = key & 0x07
        if number <= 0:
            return None
        if wire_type == 0:
            parsed_value = _read_varint(data, position)
            if parsed_value is None:
                return None
            field_value, position = parsed_value
        elif wire_type == 1:
            end = position + 8
            if end > len(data):
                return None
            field_value = data[position:end]
            position = end
        elif wire_type == 2:
            parsed_length = _read_varint(data, position)
            if parsed_length is None:
                return None
            length, position = parsed_length
            end = position + length
            if end > len(data):
                return None
            field_value = data[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            if end > len(data):
                return None
            field_value = data[position:end]
            position = end
        else:
            return None
        fields.append(_ProtobufField(number, wire_type, field_value))
    return fields


def _protobuf_timestamp(value: Any) -> datetime | None:
    outer = _protobuf_fields(value) or []
    for field_value in outer:
        if field_value.number != 1 or field_value.wire_type != 2:
            continue
        inner = _protobuf_fields(field_value.value) or []
        seconds = _varint_field(inner, 1)
        if seconds <= 0:
            continue
        nanoseconds = _varint_field(inner, 2)
        return timestamp(seconds + (nanoseconds / 1_000_000_000))
    return None


def _telemetry_blocks(value: Any) -> Iterable[list[_ProtobufField]]:
    outer = _protobuf_fields(value) or []
    for step_wrapper in outer:
        if step_wrapper.number != 5 or step_wrapper.wire_type != 2:
            continue
        wrapper = _protobuf_fields(step_wrapper.value) or []
        for telemetry in wrapper:
            if telemetry.number == 9 and telemetry.wire_type == 2:
                parsed = _protobuf_fields(telemetry.value)
                if parsed is not None:
                    yield parsed


def _varint_field(fields: Iterable[_ProtobufField], number: int) -> int:
    for field_value in fields:
        if field_value.number == number and field_value.wire_type == 0:
            return max(0, int(field_value.value))
    return 0


def _length_delimited_text(
    fields: Iterable[_ProtobufField], number: int
) -> str | None:
    for field_value in fields:
        if field_value.number != number or field_value.wire_type != 2:
            continue
        try:
            decoded = bytes(field_value.value).decode("utf-8").strip()
        except (UnicodeDecodeError, ValueError):
            return None
        return decoded or None
    return None
