"""Shared, embedding-free memory corpus contracts.

The corpus layer deliberately owns only source collection orchestration and
stable record normalization.  It does not create an index, start nova-RAG, or
call an embedding provider.  RAG and lightweight lexical retrieval can
therefore consume the same source records without duplicating source policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


MemoryChunk = dict[str, Any]
MemorySource = dict[str, Any]
CollectorResult = tuple[list[MemoryChunk], list[MemorySource]]
SourceCollector = Callable[[Any], CollectorResult]


@dataclass(frozen=True)
class CorpusCollector:
    """Bind one collector to one or more compatible source-set names."""

    source_sets: tuple[str, ...]
    collect: SourceCollector
    filter_to_selected_source_sets: bool = False

    def selected_by(self, selected_source_sets: set[str]) -> bool:
        return bool(selected_source_sets.intersection(self.source_sets))


def collect_memory_corpus(
    settings: Any,
    source_sets: Sequence[str],
    collectors: Sequence[CorpusCollector],
    *,
    retired_source_sets: Iterable[str] = (),
) -> CollectorResult:
    """Collect and normalize a corpus without any embedding/index side effects.

    Collector order is significant and is preserved.  Chunk ids are deduped
    using first-wins semantics, matching the historic RAG v2 collector.
    """

    selected = tuple(str(item).strip() for item in source_sets if str(item).strip())
    retired = sorted(set(selected).intersection(str(item).strip() for item in retired_source_sets))
    if retired:
        raise ValueError(f"retired memory sourceSets are not allowed: {', '.join(retired)}")

    selected_set = set(selected)
    chunks: list[MemoryChunk] = []
    sources: list[MemorySource] = []
    for spec in collectors:
        if not spec.selected_by(selected_set):
            continue
        collected_chunks, collected_sources = spec.collect(settings)
        if spec.filter_to_selected_source_sets:
            collected_chunks = [
                item
                for item in collected_chunks
                if str(item.get("sourceSet") or "") in selected_set
            ]
            collected_sources = [
                item
                for item in collected_sources
                if str(item.get("sourceSet") or "") in selected_set
            ]
        chunks.extend(normalize_memory_chunk(item) for item in collected_chunks)
        sources.extend(normalize_memory_source(item) for item in collected_sources)

    deduped: dict[str, MemoryChunk] = {}
    for chunk in chunks:
        deduped.setdefault(str(chunk["id"]), chunk)
    return list(deduped.values()), sources


def normalize_memory_chunk(value: Mapping[str, Any]) -> MemoryChunk:
    """Return a stable corpus chunk while retaining source-specific metadata."""

    chunk = dict(value)
    source_set = str(chunk.get("sourceSet") or "").strip()
    text = str(chunk.get("text") or "")
    chunk_id = str(chunk.get("id") or "").strip()
    if not source_set:
        raise ValueError("memory chunk sourceSet is required")
    if not chunk_id:
        raise ValueError("memory chunk id is required")
    if not text.strip():
        raise ValueError(f"memory chunk text is required: {chunk_id}")

    chunk["id"] = chunk_id
    chunk["text"] = text
    chunk["sourceSet"] = source_set
    chunk.setdefault("textPreview", text[:500])
    chunk.setdefault("textHash", hashlib.sha256(text.encode("utf-8")).hexdigest())
    chunk.setdefault("privacyClass", "local-private")
    chunk.setdefault("provenance", {})
    chunk.setdefault("governance", {})
    chunk.setdefault(
        "dedupeKey",
        hashlib.sha256(
            f"{source_set}|{chunk.get('sourcePath') or ''}|{chunk.get('lineNumber') or 0}|{text[:160]}".encode(
                "utf-8"
            )
        ).hexdigest(),
    )
    return chunk


def normalize_memory_source(value: Mapping[str, Any]) -> MemorySource:
    """Return a stable source record while retaining parser-specific fields."""

    source = dict(value)
    source_set = str(source.get("sourceSet") or "").strip()
    if not source_set:
        raise ValueError("memory source sourceSet is required")
    source["sourceSet"] = source_set
    source.setdefault("privacyClass", "local-private")
    source.setdefault("retentionPolicy", "operator-controlled")
    source.setdefault("chunkCount", 0)
    return source


def canonical_lessons_path(paths: Any) -> Path:
    """Return the single authoritative Lessons write path for a Runtime."""

    return Path(paths.home).expanduser().absolute() / "artifacts" / "learning" / "lessons.jsonl"


def lessons_read_paths(
    *,
    canonical_path: Path,
    diary_source_root: Path | None = None,
    legacy_diary_root: Path | None = None,
) -> tuple[Path, ...]:
    """Return canonical and legacy Lessons inputs in authority order.

    Legacy paths stay readable for upgrades, but new writes always target the
    canonical ``artifacts/learning`` path.
    """

    candidates = [Path(canonical_path).expanduser().absolute()]
    if diary_source_root is not None:
        candidates.append(Path(diary_source_root).expanduser().absolute() / "lessons.jsonl")
    if legacy_diary_root is not None:
        candidates.append(Path(legacy_diary_root).expanduser().absolute() / "lessons.jsonl")
    return tuple(dict.fromkeys(candidates))


def merge_lessons_into_canonical(paths: Any, lessons: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Migrate legacy Lessons and append new unique records atomically.

    The legacy file is intentionally left untouched.  Existing canonical bytes
    are preserved, including malformed lines; valid legacy records absent from
    the canonical file are copied forward once.
    """

    canonical = canonical_lessons_path(paths)
    read_paths = lessons_read_paths(
        canonical_path=canonical,
        diary_source_root=getattr(paths, "diary_dir", None),
        legacy_diary_root=getattr(paths, "legacy_diary_root", None),
    )
    canonical_text = _read_text(canonical)
    seen = _lesson_keys(canonical_text.splitlines())
    appended: list[str] = []
    migrated = 0

    for legacy_path in read_paths[1:]:
        for payload in _iter_json_objects(legacy_path):
            key = _lesson_key(payload)
            if key in seen:
                continue
            appended.append(json.dumps(payload, ensure_ascii=False))
            seen.add(key)
            migrated += 1

    added = 0
    for item in lessons:
        payload = dict(item)
        key = _lesson_key(payload)
        if key in seen:
            continue
        appended.append(json.dumps(payload, ensure_ascii=False))
        seen.add(key)
        added += 1

    if appended:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        prefix = canonical_text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = prefix + "\n".join(appended) + "\n"
        temporary = canonical.with_suffix(canonical.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(canonical)

    return {
        "path": canonical,
        "added": added,
        "migrated": migrated,
        "unchanged": not appended,
    }


def collect_runtime_memory_corpus(
    paths: Any | None = None,
    source_sets: Sequence[str] | None = None,
) -> CollectorResult:
    """Collect the configured Runtime corpus without requiring nova-RAG.

    Imports are intentionally lazy to avoid a package cycle while the existing
    source adapters remain in ``rag_v2_indexer``.  This function only resolves
    settings and reads source material; it never checks the RAG enabled flag,
    creates a candidate index, or calls an embedding endpoint.
    """

    from agentic_rag.rag_settings import (
        effective_indexing_source_sets,
        resolve_rag_settings,
    )
    from agentic_rag.rag_v2_indexer import collect_candidate_chunks

    resolved = resolve_rag_settings(paths) if paths is not None else resolve_rag_settings()
    selected = tuple(source_sets) if source_sets is not None else effective_indexing_source_sets(resolved)
    return collect_candidate_chunks(resolved, selected)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def _iter_json_objects(path: Path):
    for line in _read_text(path).splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _lesson_keys(lines: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            result.add(_lesson_key(payload))
    return result


def _lesson_key(payload: Mapping[str, Any]) -> str:
    stable_id = str(payload.get("id") or "").strip()
    if stable_id:
        return f"id:{stable_id}"
    serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
