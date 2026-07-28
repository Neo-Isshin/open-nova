"""Read-only usage adapters for normalized ingestion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ..paths import RuntimePaths
from ..session_files import is_openclaw_session_file
from ..settings import default_external_tool_path, external_tool_path, resolve_external_tool_paths
from ..runtime_sources.antigravity import AntigravityRuntime
from ..runtime_sources.base import SessionRecord, UsageRecord
from ..runtime_sources.cursor import CursorRuntime
from ..runtime_sources.opencode import OpenCodeRuntime
from ..time import parse_timestamp
from ..token_semantics import normalize_cached_input_detail
from .base import Cursor, NormalizedEvent, SourceArtifact


class UsageAdapter:
    adapter_version = "shadow-usage-v1"
    capabilities = {"usage_events", "session_inventory"}

    def fingerprint(self, artifact: SourceArtifact) -> str:
        stat = artifact.path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def _event_key(self, artifact: SourceArtifact, locator: str) -> str:
        raw = f"{artifact.path.absolute()}:{locator}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _usage_event(
        self,
        artifact: SourceArtifact,
        *,
        locator: str,
        session_key: str,
        occurred_at: object,
        model: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        message_count: int = 1,
        metadata: dict | None = None,
    ) -> NormalizedEvent | None:
        timestamp = parse_timestamp(occurred_at)
        if timestamp is None:
            return None
        payload = {
            "model_key": model or "unknown",
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
            "reasoning_tokens": int(reasoning_tokens or 0),
            "message_count": int(message_count or 1),
            "raw_locator": {"path": str(artifact.path), "locator": locator},
            "metadata": metadata or {},
        }
        return NormalizedEvent(
            tool_key=self.tool_key,
            external_event_key=self._event_key(artifact, locator),
            external_session_key=session_key,
            occurred_at=timestamp,
            event_type="usage",
            payload=payload,
        )

    @staticmethod
    def _jsonl(path: Path) -> Iterator[tuple[int, dict]]:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield number, value


class OpenClawAdapter(UsageAdapter):
    tool_key = "openclaw"
    capabilities = UsageAdapter.capabilities | {"message_metadata"}

    def __init__(self, root: Path | None = None):
        self.root = root or _external_tool_path("openclaw", "agentsRoot", default_external_tool_path("openclaw", "agentsRoot"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        if not self.root.exists():
            return ()
        return (
            SourceArtifact(self.tool_key, path, "session_jsonl")
            for path in self.root.glob("*/sessions/*.jsonl*")
            if is_openclaw_session_file(path.name)
        )

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        agent = artifact.path.parent.parent.name
        for line, value in self._jsonl(artifact.path):
            message = value.get("message", {})
            usage = message.get("usage", {})
            if value.get("type") != "message" or message.get("role") != "assistant" or not usage:
                continue
            event = self._usage_event(
                artifact,
                locator=f"line:{line}",
                session_key=str(value.get("sessionId") or f"{agent}:{artifact.path.stem}"),
                occurred_at=value.get("timestamp") or message.get("timestamp"),
                model=value.get("model") or message.get("model"),
                input_tokens=usage.get("input") or usage.get("input_tokens"),
                output_tokens=usage.get("output") or usage.get("output_tokens"),
                cache_read_tokens=usage.get("cacheRead") or usage.get("cache_read") or usage.get("cache_read_input_tokens"),
                cache_write_tokens=usage.get("cacheWrite") or usage.get("cache_write"),
                metadata={"agent_key": agent},
            )
            if event:
                yield event


class ClaudeCodeAdapter(UsageAdapter):
    tool_key = "claude-code"
    capabilities = UsageAdapter.capabilities | {"workspace_metadata"}

    def __init__(self, root: Path | None = None):
        self.root = root or _external_tool_path("claudeCode", "projectsRoot", default_external_tool_path("claudeCode", "projectsRoot"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        return () if not self.root.exists() else (
            SourceArtifact(self.tool_key, path, "session_jsonl") for path in self.root.rglob("*.jsonl")
        )

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        for line, value in self._jsonl(artifact.path):
            message = value.get("message", {})
            usage = message.get("usage", {})
            if value.get("type") != "assistant" or not usage:
                continue
            event = self._usage_event(
                artifact,
                locator=f"line:{line}",
                session_key=str(value.get("sessionId") or artifact.path.stem),
                occurred_at=value.get("timestamp"),
                model=message.get("model"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_input_tokens"),
                cache_write_tokens=usage.get("cache_creation_input_tokens"),
                metadata={"cwd": value.get("cwd")},
            )
            if event:
                yield event


class CodexAdapter(UsageAdapter):
    tool_key = "codex"
    capabilities = UsageAdapter.capabilities | {"workspace_metadata"}

    def __init__(self, root: Path | None = None):
        self.root = root or _external_tool_path("codex", "sessionsRoot", default_external_tool_path("codex", "sessionsRoot"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        return () if not self.root.exists() else (
            SourceArtifact(self.tool_key, path, "rollout_jsonl") for path in self.root.rglob("rollout-*.jsonl")
        )

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        session_key = artifact.path.stem
        cwd = None
        current_model = None
        for line, value in self._jsonl(artifact.path):
            payload = value.get("payload", {})
            if value.get("type") == "session_meta":
                session_key = str(payload.get("id") or session_key)
                cwd = payload.get("cwd")
                continue
            if value.get("type") == "turn_context":
                current_model = _codex_model_from_payload(payload) or current_model
                continue
            if value.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            usage = info.get("last_token_usage")
            if not usage:
                continue
            raw_input = usage.get("input_tokens") or 0
            output = usage.get("output_tokens") or 0
            cache_read = usage.get("cached_input_tokens") or 0
            input_tokens, cache_read_tokens, cache_semantics = normalize_cached_input_detail(
                input_tokens=raw_input,
                output_tokens=output,
                cache_read_tokens=cache_read,
                reported_total_tokens=usage.get("total_tokens"),
            )
            event = self._usage_event(
                artifact,
                locator=f"line:{line}",
                session_key=session_key,
                occurred_at=value.get("timestamp"),
                model=_codex_model_from_payload(payload) or current_model or _codex_model_from_context_window(info.get("model_context_window")),
                input_tokens=input_tokens,
                output_tokens=output,
                cache_read_tokens=cache_read_tokens,
                reasoning_tokens=usage.get("reasoning_output_tokens"),
                metadata={
                    "cwd": cwd,
                    "token_semantics": "last_token_usage",
                    "cache_input_semantics": cache_semantics,
                    "raw_input_tokens": int(raw_input or 0),
                    "reported_total_tokens": usage.get("total_tokens"),
                },
            )
            if event:
                yield event


def _codex_model_from_payload(payload: dict) -> str | None:
    for key in ("model", "model_key", "model_slug"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            value = settings.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    settings = payload.get("settings")
    if isinstance(settings, dict):
        value = settings.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _codex_model_from_context_window(window: object) -> str | None:
    try:
        parsed = int(window)
    except (TypeError, ValueError):
        return None
    if parsed == 258400:
        return "gpt-5.5"
    return None


class GeminiCliAdapter(UsageAdapter):
    tool_key = "gemini-cli"

    def __init__(self, root: Path | None = None):
        self.root = root or _external_tool_path("geminiCli", "chatsRoot", default_external_tool_path("geminiCli", "chatsRoot"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        if not self.root.exists():
            return ()
        return (
            SourceArtifact(self.tool_key, path, "session_json")
            for path in self.root.glob("session-*")
            if path.suffix in {".json", ".jsonl"}
        )

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        if artifact.path.suffix == ".jsonl":
            records = self._jsonl(artifact.path)
            default_timestamp = None
        else:
            value = json.loads(artifact.path.read_text(encoding="utf-8"))
            default_timestamp = value.get("startTime")
            records = enumerate(value.get("messages", []), 1)
        for line, value in records:
            if value.get("type") != "gemini":
                continue
            tokens = value.get("tokens") or {}
            event = self._usage_event(
                artifact,
                locator=f"record:{line}",
                session_key=artifact.path.stem,
                occurred_at=value.get("timestamp") or default_timestamp,
                model=value.get("model"),
                input_tokens=tokens.get("input"),
                output_tokens=tokens.get("output"),
                cache_read_tokens=tokens.get("cached"),
            )
            if event:
                yield event


class HermesAdapter(UsageAdapter):
    tool_key = "hermes"

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _external_tool_path("hermes", "stateDbPath", default_external_tool_path("hermes", "stateDbPath"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        return () if not self.db_path.exists() else (SourceArtifact(self.tool_key, self.db_path, "sqlite_sessions"),)

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        connection = sqlite3.connect(artifact.path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            cwd_select = "cwd" if "cwd" in columns else "'' AS cwd"
            rows = connection.execute(
                f"""
                SELECT id, started_at, model, input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, reasoning_tokens, api_call_count, {cwd_select}
                FROM sessions
                """
            )
            for row in rows:
                event = self._usage_event(
                    artifact,
                    locator=f"session:{row[0]}",
                    session_key=str(row[0]),
                    occurred_at=row[1],
                    model=row[2],
                    input_tokens=row[3],
                    output_tokens=row[4],
                    cache_read_tokens=row[5],
                    cache_write_tokens=row[6],
                    reasoning_tokens=row[7],
                    message_count=row[8] or 1,
                    metadata={
                        "aggregation": "session",
                        "message_semantics": "api_call_count",
                        "cwd": str(row[9] or ""),
                    },
                )
                if event:
                    yield event
        finally:
            connection.close()


class CronAdapter(UsageAdapter):
    tool_key = "cron"

    def __init__(self, root: Path | None = None):
        self.root = root or _external_tool_path("openclaw", "cronRunsRoot", default_external_tool_path("openclaw", "cronRunsRoot"))

    def discover_sources(self) -> Iterable[SourceArtifact]:
        return () if not self.root.exists() else (
            SourceArtifact(self.tool_key, path, "cron_jsonl") for path in _cron_run_files(self.root)
        )

    def read_incremental(self, artifact: SourceArtifact, cursor: Cursor | None = None) -> Iterable[NormalizedEvent]:
        del cursor
        for line, value in self._jsonl(artifact.path):
            usage = value.get("usage", {})
            if value.get("action") != "finished" or not usage:
                continue
            event = self._usage_event(
                artifact,
                locator=f"line:{line}",
                session_key=str(value.get("jobId") or value.get("sessionId") or artifact.path.stem),
                occurred_at=value.get("ts"),
                model=value.get("model"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cacheRead"),
            )
            if event:
                yield event


class LocalRuntimeAdapter(UsageAdapter):
    """Bridge one normalized local-runtime parser into Foundation ingestion."""

    adapter_version = "local-runtime-v1"
    capabilities = UsageAdapter.capabilities | {"workspace_metadata", "dialogue"}

    def __init__(self, runtime: Any, source_root: Path):
        self.runtime = runtime
        self.source_root = Path(source_root).expanduser()
        runtime_capabilities = getattr(runtime, "capabilities", ())
        self.capabilities = set(self.capabilities) | set(runtime_capabilities)
        if getattr(runtime, "usage_status", "") == "unavailable":
            self.capabilities.discard("usage_events")
            self.capabilities.add("usage_unavailable")

    def discover_sources(self) -> Iterable[SourceArtifact]:
        artifacts = tuple(self.runtime.artifacts())
        if not artifacts:
            return ()
        coordinator = self.source_root if self.source_root.exists() else artifacts[0]
        return (SourceArtifact(self.tool_key, coordinator, "local_runtime_inventory"),)

    def read_incremental(
        self,
        artifact: SourceArtifact,
        cursor: Cursor | None = None,
    ) -> Iterable[NormalizedEvent]:
        del cursor
        sessions = tuple(self.runtime.sessions())
        sessions_by_key = {record.external_session_key: record for record in sessions}
        for record in sessions:
            event = self._session_event(artifact, record)
            if event is not None:
                yield event
        for record in self.runtime.usage():
            event = self._normalized_usage_event(
                artifact,
                record,
                sessions_by_key.get(record.external_session_key),
            )
            if event is not None:
                yield event

    def _session_event(
        self,
        artifact: SourceArtifact,
        record: SessionRecord,
    ) -> NormalizedEvent | None:
        occurred_at = record.last_active_at or record.started_at or _artifact_timestamp(artifact.path)
        if occurred_at is None:
            return None
        metadata = {
            **record.metadata,
            "source_variant": record.source_variant,
            "usage_status": getattr(self.runtime, "usage_status", "available"),
        }
        return NormalizedEvent(
            tool_key=self.tool_key,
            external_event_key=self._event_key(
                artifact,
                f"session:{record.external_session_key}",
            ),
            external_session_key=record.external_session_key,
            occurred_at=occurred_at,
            event_type="session",
            payload={
                "started_at": record.started_at,
                "last_active_at": record.last_active_at,
                "initial_cwd": record.initial_cwd,
                "title": record.title,
                "agent_key": record.agent_key,
                "model_key": record.model_key,
                "source_variant": record.source_variant,
                "usage_status": getattr(self.runtime, "usage_status", "available"),
                "raw_locator": record.raw_locator,
                "metadata": metadata,
            },
        )

    def _normalized_usage_event(
        self,
        artifact: SourceArtifact,
        record: UsageRecord,
        session: SessionRecord | None,
    ) -> NormalizedEvent | None:
        occurred_at = parse_timestamp(record.occurred_at)
        if occurred_at is None:
            return None
        metadata = {
            **record.metadata,
            "source_variant": record.source_variant,
            "tool_tokens": int(record.tool_tokens or 0),
        }
        if session is not None:
            if session.initial_cwd:
                metadata.setdefault("cwd", session.initial_cwd)
            if session.title:
                metadata.setdefault("session_title", session.title)
        return NormalizedEvent(
            tool_key=self.tool_key,
            external_event_key=record.external_event_key,
            external_session_key=record.external_session_key,
            occurred_at=occurred_at,
            event_type="usage",
            payload={
                "model_key": record.model_key or (session.model_key if session else None) or "unknown",
                "input_tokens": int(record.input_tokens or 0),
                "output_tokens": int(record.output_tokens or 0),
                "cache_read_tokens": int(record.cache_read_tokens or 0),
                "cache_write_tokens": int(record.cache_write_tokens or 0),
                "reasoning_tokens": int(record.reasoning_tokens or 0),
                "protocol_total_tokens": record.protocol_total_tokens,
                "message_count": int(record.message_count or 1),
                "raw_locator": record.raw_locator,
                "metadata": metadata,
            },
        )


class OpenCodeAdapter(LocalRuntimeAdapter):
    tool_key = "opencode"

    def __init__(self, home: Path | None = None):
        selected = home or default_external_tool_path("opencode", "home")
        super().__init__(OpenCodeRuntime(selected), selected)


class AntigravityAdapter(LocalRuntimeAdapter):
    tool_key = "antigravity"

    def __init__(self, variant_homes: Mapping[str, Path] | None = None):
        selected = dict(variant_homes or {
            "cli": default_external_tool_path("antigravity", "cliHome"),
            "ide": default_external_tool_path("antigravity", "ideHome"),
            "app": default_external_tool_path("antigravity", "appHome"),
        })
        common_home = _common_runtime_home(selected.values())
        super().__init__(AntigravityRuntime(selected), common_home)


class CursorRuntimeAdapter(LocalRuntimeAdapter):
    tool_key = "cursor"

    def __init__(
        self,
        home: Path | None = None,
        *,
        ide_state_dbs: Iterable[Path] = (),
        workspace_storage_roots: Iterable[Path] = (),
    ):
        selected = home or default_external_tool_path("cursor", "home")
        super().__init__(
            CursorRuntime(
                selected,
                ide_state_dbs=ide_state_dbs,
                workspace_storage_roots=workspace_storage_roots,
            ),
            selected,
        )


def default_usage_adapters(paths: RuntimePaths | None = None) -> tuple[UsageAdapter, ...]:
    external_paths = _external_tool_paths(paths)
    return (
        OpenClawAdapter(_tool_path(external_paths, "openclaw", "agentsRoot")),
        ClaudeCodeAdapter(_tool_path(external_paths, "claudeCode", "projectsRoot")),
        CodexAdapter(_tool_path(external_paths, "codex", "sessionsRoot")),
        GeminiCliAdapter(_tool_path(external_paths, "geminiCli", "chatsRoot")),
        HermesAdapter(_tool_path(external_paths, "hermes", "stateDbPath")),
        OpenCodeAdapter(_tool_path(external_paths, "opencode", "home")),
        AntigravityAdapter({
            variant: path
            for variant, key in (
                ("cli", "cliHome"),
                ("ide", "ideHome"),
                ("app", "appHome"),
            )
            if (path := _tool_path(external_paths, "antigravity", key)) is not None
        }),
        CursorRuntimeAdapter(
            _tool_path(external_paths, "cursor", "home"),
            ide_state_dbs=_tool_path_list(external_paths, "cursor", "ideStateDbCandidates"),
            workspace_storage_roots=_tool_path_list(external_paths, "cursor", "workspaceStorageRoots"),
        ),
        CronAdapter(_tool_path(external_paths, "openclaw", "cronRunsRoot")),
    )


def _external_tool_paths(paths: RuntimePaths | None = None) -> dict[str, dict]:
    try:
        return resolve_external_tool_paths(paths)
    except Exception:
        return {}


def _tool_path(paths: dict[str, dict], tool: str, key: str) -> Path | None:
    value = paths.get(tool, {}).get(key)
    return value if isinstance(value, Path) else None


def _tool_path_list(paths: dict[str, dict], tool: str, key: str) -> tuple[Path, ...]:
    value = paths.get(tool, {}).get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Path))


def _common_runtime_home(paths: Iterable[Path]) -> Path:
    selected = tuple(Path(path).expanduser() for path in paths)
    if not selected:
        return default_external_tool_path("antigravity", "home")
    parents = {path.parent for path in selected}
    return next(iter(parents)) if len(parents) == 1 else selected[0]


def _artifact_timestamp(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _external_tool_path(tool: str, key: str, fallback: Path) -> Path:
    try:
        return external_tool_path(tool, key)
    except Exception:
        return fallback


def _cron_run_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in ("*.jsonl", "*.jsonl.migrated"):
        for path in sorted(root.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            yield path
