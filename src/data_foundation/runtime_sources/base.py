"""Normalized records and safe helpers for local agent runtimes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from ..time import parse_timestamp


@dataclass(frozen=True)
class SessionRecord:
    """One source-runtime session independent of whether usage is available."""

    external_session_key: str
    started_at: datetime | None = None
    last_active_at: datetime | None = None
    initial_cwd: str | None = None
    title: str | None = None
    agent_key: str | None = None
    model_key: str | None = None
    source_variant: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageRecord:
    """One stable, non-cumulative usage event."""

    external_event_key: str
    external_session_key: str
    occurred_at: datetime
    model_key: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    tool_tokens: int = 0
    protocol_total_tokens: int | None = None
    message_count: int = 1
    source_variant: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DialogueRecord:
    """Narrative-safe user/assistant text from one source session."""

    external_message_key: str
    external_session_key: str
    role: str
    content: str
    occurred_at: datetime | None = None
    source_variant: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_locator: dict[str, Any] = field(default_factory=dict)


@contextmanager
def connect_sqlite_read_only(path: Path, *, timeout_seconds: float = 2.0) -> Iterator[sqlite3.Connection]:
    """Open SQLite without creating or modifying the source database.

    ``immutable=1`` is intentionally not used because these runtimes commonly
    keep their latest rows in WAL files.  ``mode=ro`` observes that state while
    preventing writes to the database itself.
    """

    absolute = path.expanduser().absolute()
    uri = f"file:{quote(str(absolute), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout_seconds * 1000))}")
        yield connection
    finally:
        connection.close()


def sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def timestamp(value: Any) -> datetime | None:
    """Parse ISO values or Unix seconds/milliseconds into an aware datetime."""

    if isinstance(value, (int, float)):
        numeric = float(value)
        if abs(numeric) > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    return parse_timestamp(value)


def nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def text_content(value: Any) -> str:
    """Extract only explicit text blocks; never surface thinking/tool payloads."""

    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def namespaced_session_key(variant: str, native_id: str) -> str:
    normalized_variant = str(variant or "default").strip().lower() or "default"
    normalized_id = str(native_id or "").strip()
    return f"{normalized_variant}:{normalized_id}" if normalized_id else ""
