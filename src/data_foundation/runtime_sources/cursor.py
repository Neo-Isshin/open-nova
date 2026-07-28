"""Read-only local session inventory for Cursor IDE and Cursor Agent CLI.

Cursor's authoritative usage data is remote.  This parser deliberately does
not read authentication rows or make network requests: local state contributes
session/workspace/model metadata and explicit user/assistant dialogue only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, unquote, urlparse

from .base import (
    DialogueRecord,
    SessionRecord,
    UsageRecord,
    connect_sqlite_read_only,
    json_object,
    sqlite_columns,
    sqlite_tables,
    text_content,
    timestamp,
)


_COMPOSER_HEADERS_KEY = "composer.composerHeaders"
_COMPOSER_DATA_PREFIX = "composerData:"
_CLI_META_KEYS = ("0", "agent", "conversation", "meta", "session")
_UUID_ID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_MAX_JSON_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class _SessionCandidate:
    native_id: str
    source_variant: str
    priority: int
    started_at: datetime | None = None
    last_active_at: datetime | None = None
    time_quality: int = 0
    initial_cwd: str | None = None
    title: str | None = None
    model_key: str | None = None
    mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _DialogueCandidate:
    record: DialogueRecord
    native_id: str
    source_variant: str
    sort_key: tuple[str, int, int]


class CursorRuntime:
    """Normalize Cursor's local-only state without touching credentials."""

    tool_key = "cursor"
    capabilities = frozenset(
        {
            "session_inventory",
            "dialogue",
            "workspace_metadata",
            "model_metadata",
        }
    )
    usage_status = "unavailable"

    def __init__(
        self,
        home: Path,
        ide_state_dbs: Iterable[Path] = (),
        workspace_storage_roots: Iterable[Path] = (),
    ):
        self.home = Path(home).expanduser()
        self._configured_ide_state_dbs = tuple(Path(path).expanduser() for path in ide_state_dbs)
        self._configured_workspace_roots = tuple(
            Path(path).expanduser() for path in workspace_storage_roots
        )

    def artifacts(self) -> tuple[Path, ...]:
        """Return all extant local files consulted by this runtime."""

        paths: set[Path] = set()
        paths.update(path for path in self._ide_state_dbs() if path.is_file())
        paths.update(path for path in self._workspace_json_files() if path.is_file())
        paths.update(path for path in self._cli_store_dbs() if path.is_file())
        paths.update(path for path in self._transcript_files() if path.is_file())
        return tuple(sorted(paths, key=lambda path: str(path)))

    def sessions(self) -> Iterable[SessionRecord]:
        """Return one merged session per native Cursor session identifier."""

        workspace_by_id, workspace_by_db = self._workspace_mappings()
        candidates: list[_SessionCandidate] = []
        for path in self._ide_state_dbs():
            candidates.extend(
                self._ide_candidates(
                    path,
                    workspace_by_id=workspace_by_id,
                    db_workspace=workspace_by_db.get(_path_key(path)),
                )
            )
        candidates.extend(self._cli_candidates())
        candidates.extend(self._transcript_session_candidates())

        grouped: dict[str, list[_SessionCandidate]] = {}
        for candidate in candidates:
            if _native_id(candidate.native_id):
                grouped.setdefault(candidate.native_id, []).append(candidate)

        records = [
            self._merge_session(native_id, grouped[native_id])
            for native_id in sorted(grouped)
        ]
        return tuple(records)

    def usage(self) -> Iterable[UsageRecord]:
        """Cursor local files do not contain authoritative token usage."""

        return ()

    def dialogue(self) -> Iterable[DialogueRecord]:
        """Return explicit user/assistant text, excluding tools and thinking."""

        transcript = list(self._transcript_dialogue())
        transcript_text = {
            (item.native_id, item.record.role, item.record.content)
            for item in transcript
        }
        cli = [
            item
            for item in self._cli_dialogue()
            if (item.native_id, item.record.role, item.record.content) not in transcript_text
        ]
        merged = sorted(
            (*transcript, *cli),
            key=lambda item: (item.native_id, item.sort_key, item.record.external_message_key),
        )
        return tuple(item.record for item in merged)

    def _merge_session(
        self,
        native_id: str,
        candidates: list[_SessionCandidate],
    ) -> SessionRecord:
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                candidate.priority,
                candidate.time_quality,
                str(candidate.raw_locator.get("path") or ""),
            ),
            reverse=True,
        )
        primary = ordered[0]
        started_at = _best_time(candidates, "started_at", earliest=True)
        last_active_at = _best_time(candidates, "last_active_at", earliest=False)
        initial_cwd = _first_value(ordered, "initial_cwd")
        title = _first_value(ordered, "title")
        model_key = _first_value(ordered, "model_key")
        mode = _first_value(ordered, "mode")
        variants = sorted(
            {candidate.source_variant for candidate in candidates},
            key=lambda value: {"composer": 0, "agent-cli": 1, "transcript": 2}.get(value, 9),
        )
        time_quality = max(
            (
                candidate.time_quality
                for candidate in candidates
                if candidate.started_at is not None or candidate.last_active_at is not None
            ),
            default=0,
        )
        locators = [
            candidate.raw_locator
            for candidate in ordered
            if candidate.raw_locator
        ]
        metadata: dict[str, Any] = {
            "family": "Cursor",
            "native_session_id": native_id,
            "source_variants": variants,
            "usage_status": "unavailable",
            "time_confidence": {2: "metadata", 1: "mtime"}.get(time_quality, "unknown"),
        }
        if mode:
            metadata["mode"] = mode
        for candidate in ordered:
            for key, value in candidate.metadata.items():
                if key not in metadata and value is not None:
                    metadata[key] = value

        return SessionRecord(
            external_session_key=_session_key(native_id),
            started_at=started_at,
            last_active_at=last_active_at,
            initial_cwd=initial_cwd,
            title=title,
            agent_key="cursor",
            model_key=model_key,
            source_variant=primary.source_variant,
            metadata=metadata,
            raw_locator={
                "native_session_id": native_id,
                "sources": locators,
            },
        )

    def _ide_candidates(
        self,
        path: Path,
        *,
        workspace_by_id: dict[str, str],
        db_workspace: str | None,
    ) -> list[_SessionCandidate]:
        if not path.is_file():
            return []
        try:
            with _connect_cursor_db(path) as connection:
                tables = sqlite_tables(connection)
                candidates: list[_SessionCandidate] = []
                header_candidates: list[_SessionCandidate] = []
                if (
                    "ItemTable" in tables
                    and {"key", "value"} <= sqlite_columns(connection, "ItemTable")
                ):
                    header_candidates = self._composer_header_candidates(
                        connection,
                        path,
                        workspace_by_id=workspace_by_id,
                        db_workspace=db_workspace,
                    )
                    candidates.extend(header_candidates)
                if (
                    "cursorDiskKV" in tables
                    and {"key", "value"} <= sqlite_columns(connection, "cursorDiskKV")
                ):
                    candidates.extend(
                        self._composer_data_candidates(
                            connection,
                            path,
                            db_workspace=db_workspace,
                            allowed_native_ids=(
                                {candidate.native_id for candidate in header_candidates}
                                if header_candidates
                                else None
                            ),
                        )
                    )
                return candidates
        except (OSError, sqlite3.DatabaseError):
            return []

    def _composer_header_candidates(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        workspace_by_id: dict[str, str],
        db_workspace: str | None,
    ) -> list[_SessionCandidate]:
        # The exact ItemTable key plus explicit JSON paths are intentional:
        # credential rows are never selected and unlisted fields never leave
        # SQLite.
        query = """
            SELECT
                json_extract(header.value, '$.composerId') AS composer_id,
                json_extract(header.value, '$.createdAt') AS created_at,
                json_extract(header.value, '$.lastUpdatedAt') AS last_updated_at,
                json_extract(
                    header.value,
                    '$.conversationCheckpointLastUpdatedAt'
                ) AS checkpoint_updated_at,
                json_extract(header.value, '$.name') AS name,
                json_extract(header.value, '$.unifiedMode') AS unified_mode,
                json_extract(header.value, '$.forceMode') AS force_mode,
                json_extract(header.value, '$.workspaceIdentifier.id') AS workspace_id,
                json_extract(
                    header.value,
                    '$.workspaceIdentifier.uri.fsPath'
                ) AS workspace_fs_path,
                json_extract(
                    header.value,
                    '$.workspaceIdentifier.uri.external'
                ) AS workspace_external,
                json_extract(header.value, '$.isArchived') AS is_archived,
                json_extract(header.value, '$.isDraft') AS is_draft,
                json_extract(header.value, '$.isWorktree') AS is_worktree
            FROM ItemTable AS item
            JOIN json_each(CAST(item.value AS TEXT), '$.allComposers') AS header
            WHERE item.key = ?
              AND json_valid(CAST(item.value AS TEXT))
              AND json_type(CAST(item.value AS TEXT), '$.allComposers') = 'array'
        """
        try:
            rows = connection.execute(query, (_COMPOSER_HEADERS_KEY,))
        except sqlite3.DatabaseError:
            return []

        candidates: list[_SessionCandidate] = []
        for row in rows:
            native_id = _native_id(row["composer_id"])
            if not native_id:
                continue
            started = timestamp(row["created_at"])
            last = _latest_timestamp(
                row["last_updated_at"],
                row["checkpoint_updated_at"],
                row["created_at"],
            )
            workspace_id = _clean_text(row["workspace_id"])
            cwd = (
                _local_path(row["workspace_fs_path"])
                or _local_path(row["workspace_external"])
                or workspace_by_id.get(workspace_id or "")
                or db_workspace
            )
            mode = _clean_text(row["unified_mode"]) or _clean_text(row["force_mode"])
            metadata: dict[str, Any] = {
                "metadata_source": "composer_headers",
            }
            if workspace_id:
                metadata["workspace_id"] = workspace_id
            if row["is_archived"] is not None:
                metadata["archived"] = bool(row["is_archived"])
            if row["is_draft"] is not None:
                metadata["draft"] = bool(row["is_draft"])
            if row["is_worktree"] is not None:
                metadata["worktree"] = bool(row["is_worktree"])
            candidates.append(
                _SessionCandidate(
                    native_id=native_id,
                    source_variant="composer",
                    priority=400,
                    started_at=started,
                    last_active_at=last,
                    time_quality=2 if started or last else 0,
                    initial_cwd=cwd,
                    title=_clean_text(row["name"]),
                    mode=mode,
                    metadata=metadata,
                    raw_locator={
                        "path": str(path),
                        "kind": "composer_headers",
                        "key": _COMPOSER_HEADERS_KEY,
                    },
                )
            )
        return candidates

    def _composer_data_candidates(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        db_workspace: str | None,
        allowed_native_ids: set[str] | None,
    ) -> list[_SessionCandidate]:
        # composerData currently contains a blobEncryptionKey.  Do not fetch
        # the JSON value itself: project only the small, non-secret allowlist.
        query = """
            SELECT
                substr(key, ?) AS key_id,
                json_extract(CAST(value AS TEXT), '$.composerId') AS composer_id,
                json_extract(CAST(value AS TEXT), '$.createdAt') AS created_at,
                json_extract(CAST(value AS TEXT), '$.lastUpdatedAt') AS last_updated_at,
                json_extract(
                    CAST(value AS TEXT),
                    '$.conversationCheckpointLastUpdatedAt'
                ) AS checkpoint_updated_at,
                json_extract(CAST(value AS TEXT), '$.name') AS name,
                json_extract(CAST(value AS TEXT), '$.unifiedMode') AS unified_mode,
                json_extract(CAST(value AS TEXT), '$.forceMode') AS force_mode,
                json_extract(CAST(value AS TEXT), '$.isAgentic') AS is_agentic,
                json_extract(CAST(value AS TEXT), '$.status') AS status,
                json_extract(
                    CAST(value AS TEXT),
                    '$.modelConfig.modelName'
                ) AS model_name
            FROM cursorDiskKV
            WHERE key GLOB ?
              AND json_valid(CAST(value AS TEXT))
              AND json_type(CAST(value AS TEXT), '$') = 'object'
        """
        try:
            rows = connection.execute(
                query,
                (len(_COMPOSER_DATA_PREFIX) + 1, f"{_COMPOSER_DATA_PREFIX}*"),
            )
        except sqlite3.DatabaseError:
            return []

        candidates: list[_SessionCandidate] = []
        for row in rows:
            native_id = _native_id(row["composer_id"]) or _native_id(row["key_id"])
            if not native_id:
                continue
            if allowed_native_ids is not None and native_id not in allowed_native_ids:
                continue
            started = timestamp(row["created_at"])
            last = _latest_timestamp(
                row["last_updated_at"],
                row["checkpoint_updated_at"],
                row["created_at"],
            )
            mode = _clean_text(row["unified_mode"]) or _clean_text(row["force_mode"])
            metadata: dict[str, Any] = {"metadata_source": "composer_data"}
            if row["is_agentic"] is not None:
                metadata["agentic"] = bool(row["is_agentic"])
            status = _clean_text(row["status"])
            if status:
                metadata["status"] = status
            candidates.append(
                _SessionCandidate(
                    native_id=native_id,
                    source_variant="composer",
                    priority=300,
                    started_at=started,
                    last_active_at=last,
                    time_quality=2 if started or last else 0,
                    initial_cwd=db_workspace,
                    title=_clean_text(row["name"]),
                    model_key=_clean_text(row["model_name"]),
                    mode=mode,
                    metadata=metadata,
                    raw_locator={
                        "path": str(path),
                        "kind": "composer_data",
                        "key": f"{_COMPOSER_DATA_PREFIX}{native_id}",
                    },
                )
            )
        return candidates

    def _cli_candidates(self) -> list[_SessionCandidate]:
        candidates: list[_SessionCandidate] = []
        for path in self._cli_store_dbs():
            path_id = _native_id(path.parent.name)
            if not path_id:
                continue
            metadata = self._cli_meta(path)
            native_id = (
                _native_id(metadata.get("agentId"))
                or _native_id(metadata.get("sessionId"))
                or _native_id(metadata.get("conversationId"))
                or _native_id(metadata.get("id"))
                or path_id
            )
            created = timestamp(metadata.get("createdAt"))
            last = _latest_timestamp(
                metadata.get("lastUpdatedAt"),
                metadata.get("updatedAt"),
                metadata.get("createdAt"),
            )
            time_quality = 2 if created or last else 0
            if not created and not last:
                fallback = _mtime(path)
                created = fallback
                last = fallback
                time_quality = 1 if fallback else 0
            model = (
                _clean_text(metadata.get("lastUsedModel"))
                or _clean_text(metadata.get("modelName"))
                or _clean_text(metadata.get("model"))
            )
            cwd = (
                _local_path(metadata.get("cwd"))
                or _local_path(metadata.get("workspacePath"))
                or _local_path(metadata.get("rootPath"))
            )
            title = _clean_text(metadata.get("name")) or _clean_text(metadata.get("title"))
            mode = _clean_text(metadata.get("mode"))
            candidates.append(
                _SessionCandidate(
                    native_id=native_id,
                    source_variant="agent-cli",
                    priority=350,
                    started_at=created,
                    last_active_at=last,
                    time_quality=time_quality,
                    initial_cwd=cwd,
                    title=title,
                    model_key=model,
                    mode=mode,
                    metadata={
                        "metadata_source": "agent_cli_meta" if metadata else "path",
                        "cwd_hash": path.parent.parent.name,
                    },
                    raw_locator={
                        "path": str(path),
                        "kind": "agent_cli_store",
                    },
                )
            )
        return candidates

    def _cli_meta(self, path: Path) -> dict[str, Any]:
        allowed = {
            "agentId",
            "sessionId",
            "conversationId",
            "id",
            "name",
            "title",
            "createdAt",
            "updatedAt",
            "lastUpdatedAt",
            "lastUsedModel",
            "model",
            "modelName",
            "mode",
            "cwd",
            "workspacePath",
            "rootPath",
        }
        try:
            with _connect_cursor_db(path) as connection:
                if (
                    "meta" not in sqlite_tables(connection)
                    or not {"key", "value"} <= sqlite_columns(connection, "meta")
                ):
                    return {}
                placeholders = ",".join("?" for _ in _CLI_META_KEYS)
                rows = connection.execute(
                    f"SELECT key, value FROM meta WHERE key IN ({placeholders})",
                    _CLI_META_KEYS,
                )
                merged: dict[str, Any] = {}
                for row in rows:
                    value = _json_or_hex_object(row["value"])
                    for key in allowed:
                        if key in value and key not in merged:
                            merged[key] = value[key]
                return merged
        except (OSError, sqlite3.DatabaseError):
            return {}

    def _transcript_session_candidates(self) -> list[_SessionCandidate]:
        grouped: dict[str, list[Path]] = {}
        for path in self._transcript_files():
            native_id = _transcript_native_id(path)
            if native_id:
                grouped.setdefault(native_id, []).append(path)

        candidates: list[_SessionCandidate] = []
        for native_id, paths in grouped.items():
            explicit_times: list[datetime] = []
            cwds: list[str] = []
            for path in paths:
                for _, value in _jsonl_objects(path):
                    explicit = _message_timestamp(value)
                    if explicit:
                        explicit_times.append(explicit)
                    cwd = (
                        _local_path(value.get("cwd"))
                        or _local_path(value.get("workspacePath"))
                        or _local_path(value.get("rootPath"))
                    )
                    if cwd:
                        cwds.append(cwd)
            if explicit_times:
                started = min(explicit_times)
                last = max(explicit_times)
                quality = 2
            else:
                mtimes = [value for value in (_mtime(path) for path in paths) if value]
                started = min(mtimes) if mtimes else None
                last = max(mtimes) if mtimes else None
                quality = 1 if mtimes else 0
            project_slug = _project_slug(paths[0])
            candidates.append(
                _SessionCandidate(
                    native_id=native_id,
                    source_variant="transcript",
                    priority=100,
                    started_at=started,
                    last_active_at=last,
                    time_quality=quality,
                    initial_cwd=cwds[0] if cwds else None,
                    metadata={
                        "metadata_source": "transcript",
                        "project_slug": project_slug,
                    },
                    raw_locator={
                        "path": str(paths[0]),
                        "kind": "agent_transcript",
                        "file_count": len(paths),
                    },
                )
            )
        return candidates

    def _transcript_dialogue(self) -> Iterator[_DialogueCandidate]:
        for path in self._transcript_files():
            native_id = _transcript_native_id(path)
            if not native_id:
                continue
            for line_number, value in _jsonl_objects(path):
                role, content = _explicit_dialogue(value)
                if not role or not content:
                    continue
                message_time = _message_timestamp(value)
                occurred_at = message_time or _mtime(path)
                message_key = _message_key(
                    native_id,
                    "transcript",
                    f"{path.name}:{line_number}",
                    role,
                    content,
                )
                yield _DialogueCandidate(
                    record=DialogueRecord(
                        external_message_key=message_key,
                        external_session_key=_session_key(native_id),
                        role=role,
                        content=content,
                        occurred_at=occurred_at,
                        source_variant="transcript",
                        metadata={
                            "family": "Cursor",
                            "content_source": "explicit_text",
                            "timestamp_source": (
                                "message" if message_time is not None else "file_mtime"
                            ),
                            "timestamp_confidence": (
                                "high" if message_time is not None else "low"
                            ),
                        },
                        raw_locator={
                            "path": str(path),
                            "line": line_number,
                        },
                    ),
                    native_id=native_id,
                    source_variant="transcript",
                    sort_key=(str(path), line_number, 0),
                )

    def _cli_dialogue(self) -> Iterator[_DialogueCandidate]:
        for path in self._cli_store_dbs():
            native_id = _native_id(path.parent.name)
            if not native_id:
                continue
            metadata = self._cli_meta(path)
            native_id = (
                _native_id(metadata.get("agentId"))
                or _native_id(metadata.get("sessionId"))
                or _native_id(metadata.get("conversationId"))
                or _native_id(metadata.get("id"))
                or native_id
            )
            try:
                fallback_time = _mtime(path)
                with _connect_cursor_db(path) as connection:
                    table, id_column, data_column = _blob_table(connection)
                    if not table:
                        continue
                    rows = connection.execute(
                        f"SELECT {id_column}, {data_column} FROM {table} ORDER BY rowid"
                    )
                    for index, row in enumerate(rows, 1):
                        value = _direct_json_object(row[1])
                        role, content = _explicit_dialogue(value)
                        if not role or not content:
                            continue
                        blob_id = _clean_text(row[0]) or f"row:{index}"
                        message_key = _message_key(
                            native_id,
                            "agent-cli",
                            blob_id,
                            role,
                            content,
                        )
                        model = _provider_model(value)
                        message_time = _message_timestamp(value)
                        message_metadata: dict[str, Any] = {
                            "family": "Cursor",
                            "content_source": "explicit_text",
                            "timestamp_source": (
                                "message" if message_time is not None else "database_mtime"
                            ),
                            "timestamp_confidence": (
                                "high" if message_time is not None else "low"
                            ),
                        }
                        if model:
                            message_metadata["model_key"] = model
                        yield _DialogueCandidate(
                            record=DialogueRecord(
                                external_message_key=message_key,
                                external_session_key=_session_key(native_id),
                                role=role,
                                content=content,
                                occurred_at=message_time or fallback_time,
                                source_variant="agent-cli",
                                metadata=message_metadata,
                                raw_locator={
                                    "path": str(path),
                                    "blob_id": blob_id,
                                },
                            ),
                            native_id=native_id,
                            source_variant="agent-cli",
                            sort_key=(str(path), index, 1),
                        )
            except (OSError, sqlite3.DatabaseError):
                continue

    def _agent_home(self) -> Path:
        if self.home.name == ".cursor":
            return self.home
        nested = self.home / ".cursor"
        if nested.is_dir() and not (self.home / "chats").exists():
            return nested
        return self.home

    def _user_home(self) -> Path:
        agent_home = self._agent_home()
        return agent_home.parent if agent_home.name == ".cursor" else self.home

    def _ide_state_dbs(self) -> tuple[Path, ...]:
        paths: set[Path] = set(self._configured_ide_state_dbs)
        user_home = self._user_home()
        paths.add(
            user_home
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
        paths.add(
            user_home
            / ".config"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
        for root in self._workspace_roots():
            if (root / "state.vscdb").is_file():
                paths.add(root / "state.vscdb")
            if root.is_dir():
                paths.update(path for path in root.glob("*/state.vscdb") if path.is_file())
        return tuple(sorted(paths, key=lambda path: str(path)))

    def _workspace_roots(self) -> tuple[Path, ...]:
        roots: set[Path] = set(self._configured_workspace_roots)
        user_home = self._user_home()
        roots.add(
            user_home
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "workspaceStorage"
        )
        roots.add(user_home / ".config" / "Cursor" / "User" / "workspaceStorage")
        for path in self._configured_ide_state_dbs:
            if path.parent.name == "globalStorage":
                roots.add(path.parent.parent / "workspaceStorage")
            elif (path.parent / "workspace.json").is_file():
                roots.add(path.parent)
        return tuple(sorted(roots, key=lambda path: str(path)))

    def _workspace_json_files(self) -> tuple[Path, ...]:
        files: set[Path] = set()
        for root in self._workspace_roots():
            if (root / "workspace.json").is_file():
                files.add(root / "workspace.json")
            if root.is_dir():
                files.update(path for path in root.glob("*/workspace.json") if path.is_file())
        return tuple(sorted(files, key=lambda path: str(path)))

    def _workspace_mappings(self) -> tuple[dict[str, str], dict[str, str]]:
        by_id: dict[str, str] = {}
        by_db: dict[str, str] = {}
        for path in self._workspace_json_files():
            value = _read_json_object(path)
            cwd = _local_path(value.get("folder")) or _local_path(value.get("workspace"))
            if not cwd:
                continue
            workspace_id = path.parent.name
            by_id[workspace_id] = cwd
            by_db[_path_key(path.parent / "state.vscdb")] = cwd
        return by_id, by_db

    def _cli_store_dbs(self) -> tuple[Path, ...]:
        root = self._agent_home() / "chats"
        if not root.is_dir():
            return ()
        return tuple(sorted(root.glob("*/*/store.db"), key=lambda path: str(path)))

    def _transcript_files(self) -> tuple[Path, ...]:
        root = self._agent_home() / "projects"
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                root.glob("*/agent-transcripts/**/*.jsonl"),
                key=lambda path: str(path),
            )
        )


@contextmanager
def _connect_cursor_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Use WAL-aware read-only mode, with immutable fallback for orphaned WAL DBs."""

    try:
        with connect_sqlite_read_only(path) as connection:
            # Force the first read inside this context so WAL/open failures can
            # fall back before the caller starts consuming rows.
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            yield connection
            return
    except sqlite3.OperationalError:
        pass

    absolute = path.expanduser().absolute()
    uri = f"file:{quote(str(absolute), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        yield connection
    finally:
        connection.close()


def _blob_table(connection: sqlite3.Connection) -> tuple[str, str, str]:
    tables = sqlite_tables(connection)
    for table in ("blobs", "blob"):
        if table not in tables:
            continue
        columns = sqlite_columns(connection, table)
        if {"id", "data"} <= columns:
            return table, "id", "data"
        if {"key", "value"} <= columns:
            return table, "key", "value"
    return "", "", ""


def _explicit_dialogue(value: dict[str, Any]) -> tuple[str, str]:
    role = _clean_text(value.get("role"))
    if role not in {"user", "assistant"}:
        return "", ""
    message = value.get("message")
    if isinstance(message, dict):
        nested_role = _clean_text(message.get("role"))
        if nested_role in {"user", "assistant"}:
            role = nested_role
        content_value = message.get("content")
    else:
        content_value = value.get("content")
    content = text_content(content_value)
    return (role, content) if content else ("", "")


def _message_timestamp(value: dict[str, Any]) -> datetime | None:
    message = value.get("message")
    nested = message if isinstance(message, dict) else {}
    for candidate in (
        value.get("timestamp"),
        value.get("createdAt"),
        value.get("created_at"),
        nested.get("timestamp"),
        nested.get("createdAt"),
        nested.get("created_at"),
    ):
        parsed = timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def _provider_model(value: dict[str, Any]) -> str | None:
    provider_options = value.get("providerOptions")
    if not isinstance(provider_options, dict):
        return None
    cursor = provider_options.get("cursor")
    if not isinstance(cursor, dict):
        return None
    return _clean_text(cursor.get("modelName"))


def _jsonl_objects(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if len(line.encode("utf-8", errors="ignore")) > _MAX_JSON_BYTES:
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    yield line_number, value
    except OSError:
        return


def _json_or_hex_object(value: Any) -> dict[str, Any]:
    direct = _direct_json_object(value)
    if direct:
        return direct
    if isinstance(value, bytes):
        try:
            raw = value.decode("ascii")
        except UnicodeDecodeError:
            return {}
    elif isinstance(value, str):
        raw = value
    else:
        return {}
    if len(raw) > _MAX_JSON_BYTES * 2 or len(raw) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", raw):
        return {}
    try:
        decoded = bytes.fromhex(raw)
    except ValueError:
        return {}
    return _direct_json_object(decoded)


def _direct_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes) and len(value) > _MAX_JSON_BYTES:
        return {}
    if isinstance(value, str) and len(value.encode("utf-8", errors="ignore")) > _MAX_JSON_BYTES:
        return {}
    return json_object(value)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        return _direct_json_object(path.read_bytes())
    except OSError:
        return {}


def _native_id(value: Any) -> str:
    text = _clean_text(value)
    return text.lower() if text and _UUID_ID.fullmatch(text) else ""


def _session_key(native_id: str) -> str:
    return f"cursor:{native_id}"


def _message_key(
    native_id: str,
    source_variant: str,
    locator: str,
    role: str,
    content: str,
) -> str:
    raw = "\0".join(("cursor", native_id, source_variant, locator, role, content))
    return f"cursor-message:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned:
        return None
    return cleaned


def _local_path(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme:
        if parsed.scheme.lower() != "file":
            return None
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path = f"//{parsed.netloc}{path}"
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path or None
    return cleaned


def _latest_timestamp(*values: Any) -> datetime | None:
    parsed = [item for item in (timestamp(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def _best_time(
    candidates: list[_SessionCandidate],
    field_name: str,
    *,
    earliest: bool,
) -> datetime | None:
    available = [
        candidate
        for candidate in candidates
        if getattr(candidate, field_name) is not None
    ]
    if not available:
        return None
    rank = max((candidate.time_quality, candidate.priority) for candidate in available)
    values = [
        getattr(candidate, field_name)
        for candidate in available
        if (candidate.time_quality, candidate.priority) == rank
    ]
    return min(values) if earliest else max(values)


def _first_value(candidates: list[_SessionCandidate], field_name: str) -> Any:
    for candidate in candidates:
        value = getattr(candidate, field_name)
        if value is not None and value != "":
            return value
    return None


def _transcript_native_id(path: Path) -> str:
    parent_id = _native_id(path.parent.name)
    return parent_id or _native_id(path.stem)


def _project_slug(path: Path) -> str | None:
    parts = path.parts
    try:
        index = parts.index("agent-transcripts")
    except ValueError:
        return None
    return parts[index - 1] if index else None


def _path_key(path: Path) -> str:
    return str(path.expanduser().absolute())
