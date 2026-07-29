"""Read-only adapters for Agent Runtime-managed memory Markdown.

The adapters in this module intentionally consume only narrow, documented
memory surfaces.  They do not inspect raw chat/session stores, private IDE
databases, extensions, or repository metadata.

The returned ``documents`` already follow Actanara's chunk-shaped evidence
contract so a lexical or semantic corpus can consume the same records.  The
``sources`` list is a manifest of the files that were actually read.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SOURCE_SET = "agent-native-memory"
INSTRUCTIONS_SOURCE_SET = "agent-native-instructions"
SCHEMA_VERSION = 1
PARSER_VERSION = "agent-native-memory-markdown-v1"
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = 2_000
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_DISCOVERY_ENTRIES = 20_000

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_FORBIDDEN_PATH_PARTS = {
    ".git",
    "extensions",
    "raw",
    "sessions",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_HEADER_FIELD_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_-]{1,63})\s*:\s*(?P<value>.*?)\s*$"
)
_CWD_RE = re.compile(r"(?:^|[;\s,(])cwd=(?P<cwd>/[^;\s,)]+)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(?P<title>.+?)\s*$")


class NativeMemorySourceError(RuntimeError):
    """A source-local discovery or secure-read failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class _NativeMemoryBudget:
    max_file_bytes: int
    max_files: int
    max_total_bytes: int
    max_discovery_entries: int
    files_considered: int = 0
    bytes_read: int = 0
    discovery_entries: int = 0

    def limits(self) -> dict[str, int]:
        return {
            "maxFileBytes": self.max_file_bytes,
            "maxFiles": self.max_files,
            "maxTotalBytes": self.max_total_bytes,
            "maxDiscoveryEntries": self.max_discovery_entries,
        }

    def usage(self) -> dict[str, int]:
        return {
            "filesConsidered": self.files_considered,
            "bytesRead": self.bytes_read,
            "discoveryEntries": self.discovery_entries,
        }


def collect_codex_native_memory(
    codex_home: Path | str | None = None,
    *,
    include_instructions: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_discovery_entries: int = DEFAULT_MAX_DISCOVERY_ENTRIES,
    _budget: _NativeMemoryBudget | None = None,
) -> dict[str, Any]:
    """Collect Codex's allowlisted native memory Markdown.

    The memory root comes from Actanara's configured Codex tool path. Only
    ``MEMORY.md``, ``memory_summary.md``, and Markdown files below
    ``rollout_summaries`` are eligible. ``AGENTS.md`` and
    ``instructions/*.md`` are considered only when ``include_instructions`` is
    explicitly true.
    """

    budget = _budget or _new_budget(
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_discovery_entries=max_discovery_entries,
    )
    home = (
        _absolute_path(codex_home)
        if codex_home is not None
        else _configured_external_tool_path("codex", "home")
    )
    memory_root = home / "memories"
    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    home_boundary = _resolved_directory(
        home,
        diagnostics=diagnostics,
        tool="codex",
        label="codex-home",
    )
    memory_boundary = _resolved_directory(
        memory_root,
        diagnostics=diagnostics,
        tool="codex",
        label="codex-memories",
    )
    if (
        home_boundary is not None
        and memory_boundary is not None
        and not _is_within(memory_boundary, home_boundary)
    ):
        diagnostics.append(
            _diagnostic(
                "codex",
                memory_root,
                "skipped",
                "codex-memories-symlink-target-outside-home",
            )
        )
        memory_boundary = None
    elif memory_boundary is not None and memory_root.is_symlink():
        diagnostics.append(
            _diagnostic(
                "codex",
                memory_root,
                "skipped",
                "codex-memories-symlink-rejected",
            )
        )
        memory_boundary = None
    if memory_boundary is not None:
        for name, native_kind in (
            ("MEMORY.md", "memory-index"),
            ("memory_summary.md", "memory-summary"),
        ):
            candidate = memory_root / name
            if candidate.exists() or candidate.is_symlink():
                _append_candidate(
                    candidates,
                    _candidate(
                        candidate,
                        boundary=memory_boundary,
                        root=memory_root,
                        producer_tool="codex",
                        native_kind=native_kind,
                        scope_type="runtime",
                        scope_key="codex:global",
                        scope_evidence="codex-global-memory-file",
                    ),
                    budget=budget,
                    diagnostics=diagnostics,
                )

        rollout_root = memory_root / "rollout_summaries"
        candidates.extend(
            _markdown_candidates(
                rollout_root,
                boundary=memory_boundary,
                root=memory_root,
                producer_tool="codex",
                native_kind="rollout-summary",
                scope_type="runtime",
                scope_key="codex:global",
                scope_evidence="codex-rollout-header-or-runtime",
                diagnostics=diagnostics,
                budget=budget,
                recursive=True,
            )
        )

    if include_instructions:
        if home_boundary is not None:
            agents_file = home / "AGENTS.md"
            if agents_file.exists() or agents_file.is_symlink():
                _append_candidate(
                    candidates,
                    _candidate(
                        agents_file,
                        boundary=home_boundary,
                        root=home,
                        producer_tool="codex",
                        native_kind="instructions",
                        scope_type="runtime",
                        scope_key="codex:global",
                        scope_evidence="codex-global-instructions",
                    ),
                    budget=budget,
                    diagnostics=diagnostics,
                )
            candidates.extend(
                _markdown_candidates(
                    home / "instructions",
                    boundary=home_boundary,
                    root=home,
                    producer_tool="codex",
                    native_kind="instructions",
                    scope_type="runtime",
                    scope_key="codex:global",
                    scope_evidence="codex-global-instructions",
                    diagnostics=diagnostics,
                    budget=budget,
                    recursive=False,
                )
            )

    return _collect_candidates(
        candidates,
        diagnostics=diagnostics,
        include_instructions=include_instructions,
        adapter="codex-native-memory",
        budget=budget,
    )


def collect_claude_native_memory(
    projects_root: Path | str | None = None,
    *,
    project_scope_map: Mapping[str, Path | str] | None = None,
    include_instructions: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_discovery_entries: int = DEFAULT_MAX_DISCOVERY_ENTRIES,
    _budget: _NativeMemoryBudget | None = None,
) -> dict[str, Any]:
    """Collect Claude Code project-memory Markdown with stable project scope.

    Claude's project directory name is a stable native key, but decoding that
    key back into a filesystem path is lossy when path components contain
    hyphens.  This adapter therefore uses ``claude-project:<native-key>`` unless
    the caller supplies an explicit ``project_scope_map`` entry.
    """

    budget = _budget or _new_budget(
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_discovery_entries=max_discovery_entries,
    )
    root = (
        _absolute_path(projects_root)
        if projects_root is not None
        else _configured_external_tool_path("claudeCode", "projectsRoot")
    )
    diagnostics: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    projects_boundary = _resolved_directory(
        root,
        diagnostics=diagnostics,
        tool="claude-code",
        label="claude-projects",
    )

    if projects_boundary is not None:
        for project_dir in _iter_directory_entries(
            root,
            budget=budget,
            diagnostics=diagnostics,
            producer_tool="claude-code",
            error_reason="project-list-error",
        ):
            try:
                project_lstat = project_dir.lstat()
                project_real = project_dir.resolve(strict=True)
                project_stat = project_real.stat()
            except (OSError, RuntimeError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "claude-code",
                        project_dir,
                        "skipped",
                        f"unresolvable-project:{exc.__class__.__name__}",
                    )
                )
                continue
            if not stat.S_ISDIR(project_stat.st_mode):
                continue
            if not _is_within(project_real, projects_boundary):
                diagnostics.append(
                    _diagnostic(
                        "claude-code",
                        project_dir,
                        "skipped",
                        "project-symlink-target-outside-root",
                    )
                )
                continue
            if stat.S_ISLNK(project_lstat.st_mode):
                diagnostics.append(
                    _diagnostic(
                        "claude-code",
                        project_dir,
                        "skipped",
                        "project-symlink-rejected-for-reliable-scope",
                    )
                )
                continue

            scope_key, scope_evidence = _claude_scope(
                project_dir,
                project_scope_map=project_scope_map,
            )
            memory_root = project_dir / "memory"
            try:
                memory_boundary = memory_root.resolve(strict=True)
                memory_stat = memory_boundary.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, RuntimeError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "claude-code",
                        memory_root,
                        "skipped",
                        f"unresolvable-memory-root:{exc.__class__.__name__}",
                    )
                )
                continue
            if not stat.S_ISDIR(memory_stat.st_mode):
                continue
            if not _is_within(memory_boundary, project_real):
                diagnostics.append(
                    _diagnostic(
                        "claude-code",
                        memory_root,
                        "skipped",
                        "memory-symlink-target-outside-project",
                    )
                )
                continue

            for item in _markdown_candidates(
                memory_root,
                boundary=memory_boundary,
                root=memory_root,
                producer_tool="claude-code",
                native_kind="project-memory",
                scope_type="project",
                scope_key=scope_key,
                scope_evidence=scope_evidence,
                diagnostics=diagnostics,
                budget=budget,
                recursive=True,
            ):
                if item["path"].name == "MEMORY.md":
                    item["native_kind"] = "project-memory-index"
                item["project_slug"] = project_dir.name
                candidates.append(item)

    if include_instructions:
        claude_home = root.parent
        home_boundary = _resolved_directory(
            claude_home,
            diagnostics=diagnostics,
            tool="claude-code",
            label="claude-home",
        )
        if home_boundary is not None:
            instructions_file = claude_home / "CLAUDE.md"
            if instructions_file.exists() or instructions_file.is_symlink():
                _append_candidate(
                    candidates,
                    _candidate(
                        instructions_file,
                        boundary=home_boundary,
                        root=claude_home,
                        producer_tool="claude-code",
                        native_kind="instructions",
                        scope_type="runtime",
                        scope_key="claude-code:global",
                        scope_evidence="claude-global-instructions",
                    ),
                    budget=budget,
                    diagnostics=diagnostics,
                )

    return _collect_candidates(
        candidates,
        diagnostics=diagnostics,
        include_instructions=include_instructions,
        adapter="claude-native-memory",
        budget=budget,
    )


def collect_native_memory_sources(
    *,
    codex_home: Path | str | None = None,
    claude_projects_root: Path | str | None = None,
    claude_project_scope_map: Mapping[str, Path | str] | None = None,
    include_instructions: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_discovery_entries: int = DEFAULT_MAX_DISCOVERY_ENTRIES,
) -> dict[str, Any]:
    """Collect all supported native-memory sources into one stable manifest."""

    budget = _new_budget(
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        max_discovery_entries=max_discovery_entries,
    )
    codex = collect_codex_native_memory(
        codex_home,
        include_instructions=include_instructions,
        _budget=budget,
    )
    claude = collect_claude_native_memory(
        claude_projects_root,
        project_scope_map=claude_project_scope_map,
        include_instructions=include_instructions,
        _budget=budget,
    )
    documents = sorted(
        [*codex["documents"], *claude["documents"]],
        key=lambda item: (
            str(item.get("producerTool") or ""),
            str(item.get("scopeKey") or ""),
            str(item.get("sourcePath") or ""),
            str(item.get("id") or ""),
        ),
    )
    sources = sorted(
        [*codex["sources"], *claude["sources"]],
        key=lambda item: (
            str(item.get("producerTool") or ""),
            str(item.get("scopeKey") or ""),
            str(item.get("path") or ""),
        ),
    )
    return _result(
        documents=documents,
        sources=sources,
        diagnostics=[*codex["diagnostics"], *claude["diagnostics"]],
        include_instructions=include_instructions,
        adapter="native-memory-composite",
        budget=budget,
    )


# Compatibility aliases for callers that use source-oriented naming.
collect_codex_native_memory_sources = collect_codex_native_memory
collect_claude_native_memory_sources = collect_claude_native_memory
discover_native_memory_sources = collect_native_memory_sources


def _collect_candidates(
    candidates: list[dict[str, Any]],
    *,
    diagnostics: list[dict[str, Any]],
    include_instructions: bool,
    adapter: str,
    budget: _NativeMemoryBudget,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, str]] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (
            str(item["producer_tool"]),
            str(item["scope_key"]),
            str(item["path"]),
        ),
    ):
        path = candidate["path"]
        identity = (str(candidate["producer_tool"]), str(path.absolute()))
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        if _path_has_forbidden_part(path, root=candidate["root"]):
            diagnostics.append(
                _diagnostic(
                    candidate["producer_tool"],
                    path,
                    "skipped",
                    "forbidden-path-component",
                )
            )
            continue
        if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
            continue
        remaining_total_bytes = budget.max_total_bytes - budget.bytes_read
        if remaining_total_bytes <= 0:
            _record_truncation(
                diagnostics,
                candidate["producer_tool"],
                path,
                "max-total-bytes",
                budget.max_total_bytes,
            )
            break
        read_limit = min(budget.max_file_bytes, remaining_total_bytes)
        try:
            raw, opened_stat, symlink = _secure_read(
                path,
                boundary=candidate["boundary"],
                maximum_bytes=read_limit,
            )
            budget.bytes_read += len(raw)
            text = raw.decode("utf-8-sig")
        except NativeMemorySourceError as exc:
            if exc.code == "file-too-large" and read_limit < budget.max_file_bytes:
                _record_truncation(
                    diagnostics,
                    candidate["producer_tool"],
                    path,
                    "max-total-bytes",
                    budget.max_total_bytes,
                )
                continue
            diagnostics.append(
                _diagnostic(
                    candidate["producer_tool"],
                    path,
                    "skipped",
                    f"{exc.code}:{exc}",
                )
            )
            continue
        except UnicodeError as exc:
            diagnostics.append(
                _diagnostic(
                    candidate["producer_tool"],
                    path,
                    "skipped",
                    f"invalid-utf8:{exc.__class__.__name__}",
                )
            )
            continue
        text = text.strip()
        if not text:
            diagnostics.append(
                _diagnostic(candidate["producer_tool"], path, "skipped", "empty-memory-file")
            )
            continue

        header = _header_metadata(text)
        scope_type = str(candidate["scope_type"])
        scope_key = str(candidate["scope_key"])
        scope_evidence = str(candidate["scope_evidence"])
        native_kind = str(candidate["native_kind"])
        source_set = (
            INSTRUCTIONS_SOURCE_SET
            if native_kind == "instructions"
            else SOURCE_SET
        )
        if (
            candidate["producer_tool"] == "codex"
            and native_kind == "rollout-summary"
        ):
            explicit_cwd = _explicit_cwd(header, text)
            if explicit_cwd:
                scope_type = "project"
                scope_key = explicit_cwd
                scope_evidence = "codex-rollout-cwd"

        content_hash = hashlib.sha256(raw).hexdigest()
        relative_path = _relative_path(path, candidate["root"])
        source_id = _stable_source_id(
            source_set=source_set,
            producer_tool=candidate["producer_tool"],
            scope_key=scope_key,
            relative_path=relative_path,
        )
        fingerprint = hashlib.sha256(
            f"{source_id}|{content_hash}|{PARSER_VERSION}".encode("utf-8")
        ).hexdigest()
        updated_at = datetime.fromtimestamp(opened_stat.st_mtime).astimezone().isoformat()
        date_value = _memory_date(header, opened_stat)
        derived = _derived_from_actanara(header)
        lineage_family = _lineage_family(
            producer_tool=candidate["producer_tool"],
            native_kind=native_kind,
            scope_key=scope_key,
            source_id=source_id,
            header=header,
        )
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        title = _document_title(text, fallback=path.stem)
        dedupe_key = hashlib.sha256(
            f"{source_set}|{candidate['producer_tool']}|{scope_key}|{text_hash}".encode(
                "utf-8"
            )
        ).hexdigest()
        provenance = {
            "authority": (
                "Agent Runtime-managed native memory; useful recall evidence that "
                "must be validated against current authoritative state."
            ),
            "producerTool": candidate["producer_tool"],
            "nativeKind": native_kind,
            "scopeType": scope_type,
            "scopeKey": scope_key,
            "scopeReliable": True,
            "scopeEvidence": scope_evidence,
            "projectSlug": candidate.get("project_slug"),
            "relativePath": relative_path,
            "contentHash": content_hash,
            "mtimeNs": opened_stat.st_mtime_ns,
            "parserVersion": PARSER_VERSION,
            "lineageFamily": lineage_family,
            "requiresValidation": True,
            "derivedFromActanara": derived,
            "symlink": symlink,
        }
        chunk_governance = _chunk_governance(
            source_set=source_set,
            dedupe_key=dedupe_key,
            derived_from_actanara=derived,
        )
        document_id = hashlib.sha256(
            f"{source_id}|{text_hash}|1".encode("utf-8")
        ).hexdigest()
        documents.append(
            {
                "id": document_id,
                "text": text,
                "textPreview": text[:500],
                "textHash": text_hash,
                "title": title,
                "layer": "agent-native-memory",
                "date": date_value,
                "agent": candidate["producer_tool"],
                "producerTool": candidate["producer_tool"],
                "project": scope_key if scope_type == "project" else None,
                "sourceSet": source_set,
                "sourceId": source_id,
                "sourcePath": str(path.absolute()),
                "sourceType": f"{candidate['producer_tool']}-native-memory-markdown",
                "lineNumber": 1,
                "dedupeKey": dedupe_key,
                "privacyClass": "local-private",
                "provenance": provenance,
                "governance": chunk_governance,
                "tags": sorted(
                    {
                        "memory",
                        "native-memory",
                        str(candidate["producer_tool"]),
                        native_kind,
                    }
                ),
                "workType": "memory",
                "scopeType": scope_type,
                "scopeKey": scope_key,
                "scopeReliable": True,
                "scopeEvidence": scope_evidence,
                "lineageFamily": lineage_family,
                "requiresValidation": True,
                "derivedFromActanara": derived,
                "nativeKind": native_kind,
                "metadata": {
                    "sourceSet": source_set,
                    "producerTool": candidate["producer_tool"],
                    "nativeKind": native_kind,
                    "scopeType": scope_type,
                    "scopeKey": scope_key,
                    "scopeReliable": True,
                    "scopeEvidence": scope_evidence,
                    "lineageFamily": lineage_family,
                    "requiresValidation": True,
                    "derivedFromActanara": derived,
                },
            }
        )
        sources.append(
            {
                "sourceSet": source_set,
                "sourceType": f"{candidate['producer_tool']}-native-memory-markdown",
                "sourceId": source_id,
                "sourceLogicalPath": (
                    f"{candidate['producer_tool']}:{scope_key}:{relative_path}"
                ),
                "path": str(path.absolute()),
                "rootPath": str(candidate["root"].absolute()),
                "relativePath": relative_path,
                "exists": True,
                "regularFile": True,
                "symlink": symlink,
                "byteSize": opened_stat.st_size,
                "mtimeNs": opened_stat.st_mtime_ns,
                "updatedAt": updated_at,
                "modifiedTime": updated_at,
                "contentHash": content_hash,
                "parserVersion": PARSER_VERSION,
                "parserStatus": "parsed",
                "fingerprint": fingerprint,
                "chunkCount": 1,
                "privacyClass": "local-private",
                "retentionPolicy": "agent-runtime-controlled",
                "governance": _source_governance(source_set),
                "producerTool": candidate["producer_tool"],
                "scopeType": scope_type,
                "scopeKey": scope_key,
                "lineageFamily": lineage_family,
                "requiresValidation": True,
                "derivedFromActanara": derived,
                "nativeKind": native_kind,
            }
        )

    return _result(
        documents=documents,
        sources=sources,
        diagnostics=diagnostics,
        include_instructions=include_instructions,
        adapter=adapter,
        budget=budget,
    )


def _result(
    *,
    documents: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    include_instructions: bool,
    adapter: str,
    budget: _NativeMemoryBudget,
) -> dict[str, Any]:
    ordered_documents = sorted(
        documents,
        key=lambda item: (
            str(item.get("producerTool") or ""),
            str(item.get("scopeKey") or ""),
            str(item.get("sourcePath") or ""),
            str(item.get("id") or ""),
        ),
    )
    ordered_sources = sorted(
        sources,
        key=lambda item: (
            str(item.get("producerTool") or ""),
            str(item.get("scopeKey") or ""),
            str(item.get("path") or ""),
        ),
    )
    manifest_hash = hashlib.sha256(
        "\n".join(
            f"{item.get('sourceId')}:{item.get('fingerprint')}"
            for item in ordered_sources
        ).encode("utf-8")
    ).hexdigest()
    truncated = any(item.get("status") == "truncated" for item in diagnostics)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sourceSet": SOURCE_SET,
        "adapter": adapter,
        "truncated": truncated,
        "limits": budget.limits(),
        "usage": budget.usage(),
        "documents": ordered_documents,
        "sources": ordered_sources,
        "manifest": {
            "schemaVersion": SCHEMA_VERSION,
            "sourceSet": SOURCE_SET,
            "adapter": adapter,
            "sourceCount": len(ordered_sources),
            "documentCount": len(ordered_documents),
            "instructionsIncluded": bool(include_instructions),
            "truncated": truncated,
            "limits": budget.limits(),
            "usage": budget.usage(),
            "manifestHash": manifest_hash,
            "sources": ordered_sources,
        },
        "diagnostics": sorted(
            diagnostics,
            key=lambda item: (
                str(item.get("producerTool") or ""),
                str(item.get("path") or ""),
                str(item.get("reason") or ""),
            ),
        ),
    }


def _candidate(
    path: Path,
    *,
    boundary: Path,
    root: Path,
    producer_tool: str,
    native_kind: str,
    scope_type: str,
    scope_key: str,
    scope_evidence: str,
) -> dict[str, Any]:
    return {
        "path": path.absolute(),
        "boundary": boundary,
        "root": root.absolute(),
        "producer_tool": producer_tool,
        "native_kind": native_kind,
        "scope_type": scope_type,
        "scope_key": scope_key,
        "scope_evidence": scope_evidence,
    }


def _new_budget(
    *,
    max_file_bytes: int,
    max_files: int,
    max_total_bytes: int,
    max_discovery_entries: int,
) -> _NativeMemoryBudget:
    limits = {
        "max_file_bytes": max_file_bytes,
        "max_files": max_files,
        "max_total_bytes": max_total_bytes,
        "max_discovery_entries": max_discovery_entries,
    }
    for name, value in limits.items():
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    return _NativeMemoryBudget(**limits)


def _record_truncation(
    diagnostics: list[dict[str, Any]],
    producer_tool: str,
    path: Path,
    limit_name: str,
    limit: int,
) -> None:
    item = _diagnostic(
        producer_tool,
        path,
        "truncated",
        f"{limit_name}-reached:limit={limit}",
    )
    if item not in diagnostics:
        diagnostics.append(item)


def _consume_discovery_entry(
    budget: _NativeMemoryBudget,
    *,
    diagnostics: list[dict[str, Any]],
    producer_tool: str,
    path: Path,
) -> bool:
    if budget.discovery_entries >= budget.max_discovery_entries:
        _record_truncation(
            diagnostics,
            producer_tool,
            path,
            "max-discovery-entries",
            budget.max_discovery_entries,
        )
        return False
    budget.discovery_entries += 1
    return True


def _append_candidate(
    candidates: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    budget: _NativeMemoryBudget,
    diagnostics: list[dict[str, Any]],
    discovery_counted: bool = False,
) -> bool:
    path = candidate["path"]
    producer_tool = str(candidate["producer_tool"])
    if not discovery_counted and not _consume_discovery_entry(
        budget,
        diagnostics=diagnostics,
        producer_tool=producer_tool,
        path=path,
    ):
        return False
    if budget.files_considered >= budget.max_files:
        _record_truncation(
            diagnostics,
            producer_tool,
            path,
            "max-files",
            budget.max_files,
        )
        return False
    if budget.bytes_read >= budget.max_total_bytes:
        _record_truncation(
            diagnostics,
            producer_tool,
            path,
            "max-total-bytes",
            budget.max_total_bytes,
        )
        return False
    budget.files_considered += 1
    candidates.append(candidate)
    return True


def _iter_directory_entries(
    directory: Path,
    *,
    budget: _NativeMemoryBudget,
    diagnostics: list[dict[str, Any]],
    producer_tool: str,
    error_reason: str,
) -> Iterator[Path]:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if not _consume_discovery_entry(
                    budget,
                    diagnostics=diagnostics,
                    producer_tool=producer_tool,
                    path=path,
                ):
                    return
                yield path
    except OSError as exc:
        diagnostics.append(
            _diagnostic(
                producer_tool,
                directory,
                "error",
                f"{error_reason}:{exc.__class__.__name__}",
            )
        )


def _markdown_candidates(
    directory: Path,
    *,
    boundary: Path,
    root: Path,
    producer_tool: str,
    native_kind: str,
    scope_type: str,
    scope_key: str,
    scope_evidence: str,
    diagnostics: list[dict[str, Any]],
    budget: _NativeMemoryBudget,
    recursive: bool,
) -> list[dict[str, Any]]:
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink():
        diagnostics.append(
            _diagnostic(
                producer_tool,
                directory,
                "skipped",
                "memory-directory-symlink-rejected",
            )
        )
        return []
    try:
        directory_real = directory.resolve(strict=True)
        directory_stat = directory_real.stat()
    except (OSError, RuntimeError) as exc:
        diagnostics.append(
            _diagnostic(
                producer_tool,
                directory,
                "skipped",
                f"unresolvable-memory-directory:{exc.__class__.__name__}",
            )
        )
        return []
    if not stat.S_ISDIR(directory_stat.st_mode):
        return []
    if not _is_within(directory_real, boundary):
        diagnostics.append(
            _diagnostic(
                producer_tool,
                directory,
                "skipped",
                "memory-directory-symlink-target-outside-root",
            )
        )
        return []
    candidates: list[dict[str, Any]] = []
    pending_directories = [directory]
    stop_scanning = False
    while pending_directories and not stop_scanning:
        current = pending_directories.pop()
        if budget.files_considered >= budget.max_files:
            _record_truncation(
                diagnostics,
                producer_tool,
                current,
                "max-files",
                budget.max_files,
            )
            break
        if budget.discovery_entries >= budget.max_discovery_entries:
            _record_truncation(
                diagnostics,
                producer_tool,
                current,
                "max-discovery-entries",
                budget.max_discovery_entries,
            )
            break
        for path in _iter_directory_entries(
            current,
            budget=budget,
            diagnostics=diagnostics,
            producer_tool=producer_tool,
            error_reason="memory-list-error",
        ):
            if _path_has_forbidden_part(path, root=directory):
                continue
            try:
                path_stat = path.lstat()
            except OSError as exc:
                diagnostics.append(
                    _diagnostic(
                        producer_tool,
                        path,
                        "skipped",
                        f"unresolvable-memory-entry:{exc.__class__.__name__}",
                    )
                )
                continue
            if stat.S_ISLNK(path_stat.st_mode):
                try:
                    if path.is_dir():
                        diagnostics.append(
                            _diagnostic(
                                producer_tool,
                                path,
                                "skipped",
                                "memory-directory-symlink-rejected",
                            )
                        )
                        continue
                except OSError:
                    pass
            elif stat.S_ISDIR(path_stat.st_mode):
                if recursive:
                    pending_directories.append(path)
                continue
            if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
                continue
            if not _append_candidate(
                candidates,
                _candidate(
                    path,
                    boundary=directory_real,
                    root=root,
                    producer_tool=producer_tool,
                    native_kind=native_kind,
                    scope_type=scope_type,
                    scope_key=scope_key,
                    scope_evidence=scope_evidence,
                ),
                budget=budget,
                diagnostics=diagnostics,
                discovery_counted=True,
            ):
                stop_scanning = True
                break
    return candidates


def _secure_read(
    path: Path,
    *,
    boundary: Path,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result, bool]:
    try:
        lexical_stat = path.lstat()
        resolved_before = path.resolve(strict=True)
        boundary_real = boundary.resolve(strict=True)
        target_stat = resolved_before.stat()
    except (OSError, RuntimeError) as exc:
        raise NativeMemorySourceError(
            "unresolvable-path",
            f"{exc.__class__.__name__}",
        ) from exc
    if not _is_within(resolved_before, boundary_real):
        raise NativeMemorySourceError(
            "symlink-target-outside-root",
            "memory path resolves outside its native memory root",
        )
    if _path_has_forbidden_part(resolved_before, root=boundary_real):
        raise NativeMemorySourceError(
            "forbidden-target-path",
            "memory path resolves into a forbidden native-runtime subtree",
        )
    if not stat.S_ISREG(target_stat.st_mode):
        raise NativeMemorySourceError(
            "not-a-regular-file",
            "native memory source is not a regular file",
        )
    if target_stat.st_size > maximum_bytes:
        raise NativeMemorySourceError(
            "file-too-large",
            f"native memory source exceeds max_file_bytes={maximum_bytes}",
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved_before, flags)
    except OSError as exc:
        raise NativeMemorySourceError(
            "secure-open-failed",
            f"{exc.__class__.__name__}",
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise NativeMemorySourceError(
                "not-a-regular-file",
                "native memory source changed file type while opening",
            )
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            raise NativeMemorySourceError(
                "file-identity-changed",
                "native memory source changed after discovery",
            )
        try:
            resolved_after = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise NativeMemorySourceError(
                "path-changed-after-open",
                f"{exc.__class__.__name__}",
            ) from exc
        if resolved_after != resolved_before or not _is_within(
            resolved_after, boundary_real
        ):
            raise NativeMemorySourceError(
                "path-escaped-after-open",
                "native memory path changed while opening",
            )
        pieces: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            piece = os.read(descriptor, min(1024 * 1024, remaining))
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        raw = b"".join(pieces)
        if len(raw) > maximum_bytes:
            raise NativeMemorySourceError(
                "file-grew-too-large",
                "native memory source exceeded its size limit while reading",
            )
        return raw, opened_stat, stat.S_ISLNK(lexical_stat.st_mode)
    finally:
        os.close(descriptor)


def _resolved_directory(
    path: Path,
    *,
    diagnostics: list[dict[str, Any]],
    tool: str,
    label: str,
) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        path_stat = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        diagnostics.append(_diagnostic(tool, path, "missing", f"{label}-not-found"))
        return None
    except (OSError, RuntimeError) as exc:
        diagnostics.append(
            _diagnostic(
                tool,
                path,
                "error",
                f"{label}-unresolvable:{exc.__class__.__name__}",
            )
        )
        return None
    if not stat.S_ISDIR(path_stat.st_mode):
        diagnostics.append(_diagnostic(tool, path, "skipped", f"{label}-not-directory"))
        return None
    return resolved


def _claude_scope(
    project_dir: Path,
    *,
    project_scope_map: Mapping[str, Path | str] | None,
) -> tuple[str, str]:
    mapping = project_scope_map or {}
    mapped = mapping.get(project_dir.name)
    if mapped is None:
        mapped = mapping.get(str(project_dir.absolute()))
    if mapped is not None and str(mapped).strip():
        raw_value = str(mapped).strip()
        value = (
            str(Path(raw_value).expanduser().absolute())
            if isinstance(mapped, Path) or raw_value.startswith(("/", "~/"))
            else raw_value
        )
        return value, "explicit-project-scope-map"
    return f"claude-project:{project_dir.name}", "claude-native-project-directory-key"


def _header_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    lines = text.splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for index, line in enumerate(lines[:120]):
        stripped = line.strip()
        if index > 0 and in_frontmatter and stripped == "---":
            break
        if not in_frontmatter and stripped.startswith("#"):
            break
        match = _HEADER_FIELD_RE.match(line)
        if match:
            metadata.setdefault(
                match.group("key").replace("-", "_").casefold(),
                match.group("value").strip().strip("\"'"),
            )
    return metadata


def _explicit_cwd(header: dict[str, str], text: str) -> str | None:
    candidate = str(header.get("cwd") or "").strip()
    if not candidate:
        match = _CWD_RE.search("\n".join(text.splitlines()[:80]))
        candidate = match.group("cwd") if match else ""
    if not candidate or not Path(candidate).is_absolute():
        return None
    return str(Path(candidate).expanduser().absolute())


def _memory_date(header: dict[str, str], opened_stat: os.stat_result) -> str:
    for key in ("updated_at", "updatedat", "date", "created_at", "createdat"):
        value = str(header.get(key) or "").strip()
        match = re.match(r"^\d{4}-\d{2}-\d{2}", value)
        if match:
            return match.group(0)
    return datetime.fromtimestamp(opened_stat.st_mtime).astimezone().date().isoformat()


def _derived_from_actanara(header: dict[str, str]) -> bool:
    for key in (
        "derivedfromactanara",
        "derived_from_actanara",
        "actanara_derived",
        "x_actanara_derived",
    ):
        if key in header:
            return str(header[key]).strip().casefold() in _TRUE_VALUES
    return False


def _lineage_family(
    *,
    producer_tool: str,
    native_kind: str,
    scope_key: str,
    source_id: str,
    header: dict[str, str],
) -> str:
    if producer_tool == "codex":
        thread_id = str(header.get("thread_id") or header.get("threadid") or "").strip()
        if thread_id:
            return f"codex-thread:{thread_id}"
    if producer_tool == "claude-code":
        session_id = str(
            header.get("originsessionid")
            or header.get("origin_session_id")
            or ""
        ).strip()
        if session_id:
            return f"claude-session:{session_id}"
    # Without an explicit session/thread lineage, independent native files are
    # separate evidence. Grouping every file in a project would silently drop
    # all but one result during retrieval deduplication.
    return f"{producer_tool}-memory:{native_kind}:{scope_key}:{source_id}"


def _document_title(text: str, *, fallback: str) -> str:
    header = _header_metadata(text)
    if str(header.get("name") or "").strip():
        return str(header["name"]).strip()
    for line in text.splitlines()[:120]:
        match = _HEADING_RE.match(line)
        if match:
            return match.group("title").strip()
    return fallback.replace("_", " ").replace("-", " ").strip()


def _stable_source_id(
    *,
    source_set: str,
    producer_tool: str,
    scope_key: str,
    relative_path: str,
) -> str:
    return hashlib.sha256(
        f"{source_set}|{producer_tool}|{scope_key}|{relative_path}".encode("utf-8")
    ).hexdigest()[:24]


def _source_governance(source_set: str) -> dict[str, Any]:
    instructions = source_set == INSTRUCTIONS_SOURCE_SET
    return {
        "version": 1,
        "sourceSet": source_set,
        "authorityRank": 74 if instructions else 68,
        "lifecycle": "agent-native-instructions" if instructions else "agent-native-memory",
        "retention": "agent-runtime-controlled",
        "canonicalEligible": False,
        "retrievalWeight": 0.98 if instructions else 0.96,
        "requiresValidation": True,
    }


def _chunk_governance(
    *,
    source_set: str,
    dedupe_key: str,
    derived_from_actanara: bool,
) -> dict[str, Any]:
    warnings = ["requires-validation"]
    if derived_from_actanara:
        warnings.append("derived-from-actanara")
    return {
        **_source_governance(source_set),
        "provenanceScore": 1.0,
        "duplicateGroupKey": f"{source_set}:{dedupe_key[:16]}",
        "canonicalCandidate": False,
        "supersessionScope": None,
        "warnings": warnings,
        "derivedFromActanara": derived_from_actanara,
    }


def _diagnostic(
    producer_tool: str,
    path: Path,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "sourceSet": SOURCE_SET,
        "producerTool": producer_tool,
        "path": str(path.absolute()),
        "status": status,
        "reason": reason,
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return path.name


def _path_has_forbidden_part(path: Path, *, root: Path) -> bool:
    try:
        parts = path.absolute().relative_to(root.absolute()).parts
    except ValueError:
        parts = path.parts
    return any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in parts)


def _is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
        return True
    except ValueError:
        return path == boundary


def _absolute_path(value: Path | str) -> Path:
    return Path(value).expanduser().absolute()


def _configured_external_tool_path(tool: str, key: str) -> Path:
    """Resolve a native adapter default through the shared tool catalog."""

    from .settings import external_tool_path

    return external_tool_path(tool, key)


__all__ = [
    "DEFAULT_MAX_DISCOVERY_ENTRIES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "INSTRUCTIONS_SOURCE_SET",
    "NativeMemorySourceError",
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_SET",
    "collect_claude_native_memory",
    "collect_claude_native_memory_sources",
    "collect_codex_native_memory",
    "collect_codex_native_memory_sources",
    "collect_native_memory_sources",
    "discover_native_memory_sources",
]
