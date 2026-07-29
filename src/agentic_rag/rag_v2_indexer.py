"""RAG v2 candidate index builder.

This module writes only to the v2 candidate store under
``$ACTANARA_HOME/reserved/rag/v2``. It never rewrites, compacts, deletes or replaces
the legacy production index.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .rag_retriever import infer_tags, infer_work_type
from .rag_settings import RagSettings, effective_indexing_source_sets, resolve_rag_settings
from .rag_v2_store import SCHEMA_VERSION, initialize_shadow_build, _read_json, _root_manifest_for_candidate_update
from .rag_memory_governance import governance_for_chunk, governance_for_source
from .rag_profile import profile_hash, settings_embedding_profile, source_profile_hash

RETIRED_SOURCE_SETS = {"legacy-diary-daily"}

try:
    from data_foundation.diary_markdown import parse_diary_markdown
    from data_foundation.diary_paths import diary_report_paths, diary_report_type_for_filename, iter_diary_markdown_files
    from data_foundation.memory_corpus import CorpusCollector, collect_memory_corpus, lessons_read_paths
    from data_foundation.nova_task import _extract_nova_task_payload
    from data_foundation.tasks import _parse_report_updates, parse_task_board_markdown
    from data_foundation.time import business_date_for, parse_timestamp
except ImportError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from data_foundation.diary_markdown import parse_diary_markdown  # type: ignore
    from data_foundation.diary_paths import diary_report_paths, diary_report_type_for_filename, iter_diary_markdown_files  # type: ignore
    from data_foundation.memory_corpus import CorpusCollector, collect_memory_corpus, lessons_read_paths  # type: ignore
    from data_foundation.nova_task import _extract_nova_task_payload  # type: ignore
    from data_foundation.tasks import _parse_report_updates, parse_task_board_markdown  # type: ignore
    from data_foundation.time import business_date_for, parse_timestamp  # type: ignore


EmbeddingFn = Callable[[list[str]], list[list[float]]]


def build_v2_candidate_index(
    settings: RagSettings | None = None,
    *,
    requested_by: str = "operator",
    embedding_fn: EmbeddingFn | None = None,
    source_sets: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a v2 candidate index without changing active or legacy state."""
    resolved = settings or resolve_rag_settings()
    if not resolved.indexing_enabled:
        raise ValueError("RAG indexing is disabled")
    selected_source_sets = tuple(source_sets) if source_sets is not None else effective_indexing_source_sets(resolved)
    _validate_source_sets(selected_source_sets)
    _validate_external_source_selection(resolved, selected_source_sets)
    build = initialize_shadow_build(
        resolved,
        requested_by=requested_by,
        reason="v2-candidate-index",
    )
    candidate_dir = Path(build["run"]["candidatePath"])
    index_path = candidate_dir / "index.jsonl"
    chunks_path = candidate_dir / "chunks.jsonl"
    embeddings_path = candidate_dir / "embeddings.jsonl"
    sources_path = candidate_dir / "sources.jsonl"
    report_path = candidate_dir / "build-report.json"
    chunks, source_records = collect_candidate_chunks(resolved, selected_source_sets)
    if embedding_fn is None:
        raise ValueError("embedding_fn is required; production indexing must inject the nova-RAG server /encode client")
    embed = embedding_fn
    reusable_embeddings = _load_active_embedding_cache(resolved)
    embedding_count = 0
    generated_embedding_count = 0
    reused_embedding_count = 0
    dimension_mismatch_count = 0
    checksum = hashlib.sha256()

    with (
        index_path.open("w", encoding="utf-8") as index_handle,
        chunks_path.open("w", encoding="utf-8") as chunks_handle,
        embeddings_path.open("w", encoding="utf-8") as embeddings_handle,
    ):
        for offset in range(0, len(chunks), resolved.embedding_batch_size):
            batch = chunks[offset : offset + resolved.embedding_batch_size]
            embeddings_by_position: dict[int, tuple[list[float], bool]] = {}
            missing_positions: list[int] = []
            missing_texts: list[str] = []
            for position, chunk in enumerate(batch):
                reusable = _reusable_embedding(chunk, reusable_embeddings, resolved)
                if reusable is not None:
                    embeddings_by_position[position] = (reusable, True)
                    continue
                missing_positions.append(position)
                missing_texts.append(str(chunk["text"]))
            generated_embeddings = embed(missing_texts) if missing_texts else []
            for generated_position, position in enumerate(missing_positions):
                embedding = generated_embeddings[generated_position] if generated_position < len(generated_embeddings) else []
                embeddings_by_position[position] = (embedding, False)
            for position, chunk in enumerate(batch):
                chunks_handle.write(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n")
                embedding, reused = embeddings_by_position.get(position, ([], False))
                if len(embedding) != resolved.embedding_dimension:
                    dimension_mismatch_count += 1
                    continue
                embedding_payload = {
                    "chunkId": chunk["id"],
                    "embedding": embedding,
                    "dimension": len(embedding),
                    "model": resolved.embedding_model,
                    "provider": resolved.embedding_provider,
                }
                embeddings_handle.write(json.dumps(embedding_payload, ensure_ascii=False, sort_keys=True) + "\n")
                payload = {**chunk, "embedding": embedding}
                line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                checksum.update(line.encode("utf-8"))
                checksum.update(b"\n")
                index_handle.write(line + "\n")
                embedding_count += 1
                if reused:
                    reused_embedding_count += 1
                else:
                    generated_embedding_count += 1

    with sources_path.open("w", encoding="utf-8") as handle:
        for source in source_records:
            handle.write(json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n")

    now = _now_iso()
    byte_size = index_path.stat().st_size if index_path.exists() else 0
    checksum_value = checksum.hexdigest() if embedding_count else None
    blocking_external_source_count = sum(
        1
        for source in source_records
        if source.get("sourceSet") == "external-content"
        and source.get("parserStatus") in {"error", "missing", "skipped", "unsupported"}
    )
    ready = bool(chunks) and embedding_count == len(chunks) and blocking_external_source_count == 0
    status = "ready" if ready else "partial"
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": build["run"]["runId"],
        "status": status,
        "sourceProfile": _source_profile(resolved, selected_source_sets),
        "sourceProfileHash": source_profile_hash(_source_profile(resolved, selected_source_sets)),
        "sourceSets": list(selected_source_sets),
        "documentCount": len(source_records),
        "chunkCount": len(chunks),
        "embeddingCount": embedding_count,
        "generatedEmbeddingCount": generated_embedding_count,
        "reusedEmbeddingCount": reused_embedding_count,
        "dimensionMismatchCount": dimension_mismatch_count,
        "skippedCount": len(chunks) - embedding_count,
        "blockingExternalSourceCount": blocking_external_source_count,
        "incremental": {
            "mode": "active-embedding-reuse",
            "activeCacheEntries": len(reusable_embeddings),
            "reuseRequires": ["chunkId", "dedupeKey", "textHash", "model", "dimension"],
        },
        "candidatePath": str(candidate_dir),
        "files": {
            "index": str(index_path),
            "chunks": str(chunks_path),
            "embeddings": str(embeddings_path),
            "sources": str(sources_path),
        },
    }
    _write_json_atomic(report_path, report)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "indexVersion": build["run"]["runId"],
        "status": status,
        "createdAt": build["run"]["createdAt"],
        "updatedAt": now,
        "completedAt": now,
        "model": resolved.embedding_model,
        "dimension": resolved.embedding_dimension,
        "languageProfile": resolved.language_profile,
        "embeddingProvider": resolved.embedding_provider,
        "embeddingProviderId": resolved.embedding_provider_id,
        "embeddingProfile": settings_embedding_profile(resolved),
        "embeddingProfileHash": profile_hash(settings_embedding_profile(resolved)),
        "sourceProfile": _source_profile(resolved, selected_source_sets),
        "sourceProfileHash": source_profile_hash(_source_profile(resolved, selected_source_sets)),
        "sourceSets": list(selected_source_sets),
        "documentCount": len(source_records),
        "chunkCount": len(chunks),
        "embeddingCount": embedding_count,
        "generatedEmbeddingCount": generated_embedding_count,
        "reusedEmbeddingCount": reused_embedding_count,
        "dimensionMismatchCount": dimension_mismatch_count,
        "skippedCount": len(chunks) - embedding_count,
        "blockingExternalSourceCount": blocking_external_source_count,
        "incremental": {
            "mode": "active-embedding-reuse",
            "activeCacheEntries": len(reusable_embeddings),
            "reuseRequires": ["chunkId", "dedupeKey", "textHash", "model", "dimension"],
        },
        "byteSize": byte_size,
        "checksum": checksum_value,
        "activeIndexPath": None,
        "candidatePath": str(candidate_dir),
        "candidateIndexPath": str(index_path),
        "chunksPath": str(chunks_path),
        "embeddingsPath": str(embeddings_path),
        "sourcesPath": str(sources_path),
        "buildReportPath": str(report_path),
        "lastBuildRunId": build["run"]["runId"],
        "lastError": None,
        "activePromotionAllowed": ready,
        "notes": "Candidate index only; active v2 and legacy production index were not mutated.",
    }
    _write_json_atomic(candidate_dir / "manifest.json", manifest)
    run = {
        **build["run"],
        "status": status,
        "phase": "v2-candidate-index",
        "updatedAt": now,
        "completedAt": now,
        "model": resolved.embedding_model,
        "dimension": resolved.embedding_dimension,
        "languageProfile": resolved.language_profile,
        "embeddingProvider": resolved.embedding_provider,
        "embeddingProviderId": resolved.embedding_provider_id,
        "embeddingProfile": settings_embedding_profile(resolved),
        "embeddingProfileHash": profile_hash(settings_embedding_profile(resolved)),
        "sourceProfile": _source_profile(resolved, selected_source_sets),
        "sourceProfileHash": source_profile_hash(_source_profile(resolved, selected_source_sets)),
        "sourceSets": list(selected_source_sets),
        "documentCount": len(source_records),
        "chunkCount": len(chunks),
        "embeddingCount": embedding_count,
        "generatedEmbeddingCount": generated_embedding_count,
        "reusedEmbeddingCount": reused_embedding_count,
        "dimensionMismatchCount": dimension_mismatch_count,
        "skippedCount": len(chunks) - embedding_count,
        "blockingExternalSourceCount": blocking_external_source_count,
        "incremental": {
            "mode": "active-embedding-reuse",
            "activeCacheEntries": len(reusable_embeddings),
            "reuseRequires": ["chunkId", "dedupeKey", "textHash", "model", "dimension"],
        },
        "candidateIndexPath": str(index_path),
        "chunksPath": str(chunks_path),
        "embeddingsPath": str(embeddings_path),
        "sourcesPath": str(sources_path),
        "buildReportPath": str(report_path),
        "checksum": checksum_value,
        "activePromotionAllowed": ready,
    }
    _append_jsonl(resolved.v2_store_path / "build-runs.jsonl", run)
    root_manifest_path = resolved.v2_store_path / "manifest.json"
    root_status = "candidate-ready" if ready else "candidate-partial"
    _write_json_atomic(
        root_manifest_path,
        _root_manifest_for_candidate_update(
            resolved,
            existing_root_manifest=_read_json(root_manifest_path),
            candidate_manifest={
                **manifest,
                "manifestPath": str(candidate_dir / "manifest.json"),
            },
            root_status=root_status,
            now=now,
        ),
    )
    return {
        **build,
        "status": status,
        "run": run,
        "candidateManifest": str(candidate_dir / "manifest.json"),
        "candidateIndex": str(index_path),
        "chunksPath": str(chunks_path),
        "embeddingsPath": str(embeddings_path),
        "sourcesPath": str(sources_path),
        "buildReportPath": str(report_path),
        "manifest": manifest,
    }


def collect_candidate_chunks(
    settings: RagSettings,
    source_sets: list[str] | tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _validate_source_sets(source_sets)
    return collect_memory_corpus(
        settings,
        source_sets,
        (
            CorpusCollector(("filtered-dialogue-daily",), _collect_filtered_dialogue_daily),
            CorpusCollector(("lessons",), _collect_lessons),
            CorpusCollector(("foundation-usage-rollups",), _collect_foundation_usage_rollups),
            CorpusCollector(("foundation-dashboard-snapshots",), _collect_foundation_dashboard_snapshots),
            CorpusCollector(("diary-markdown-sections", "diary-markdown"), _collect_diary_markdown_sections),
            CorpusCollector(("diary-markdown-embedded-json",), _collect_diary_markdown_embedded_json),
            CorpusCollector(("technical-report-task-events", "technical-reports"), _collect_technical_report_task_events),
            CorpusCollector(
                ("nova-task-work-graph-events",),
                lambda selected: _collect_nova_task_work_graph_events(
                    selected,
                    source_set="nova-task-work-graph-events",
                ),
            ),
            CorpusCollector(
                ("nova-task-reconciliation-events",),
                lambda selected: _collect_nova_task_work_graph_events(
                    selected,
                    source_set="nova-task-reconciliation-events",
                ),
            ),
            CorpusCollector(("task-board-snapshot",), _collect_task_board_snapshot),
            CorpusCollector(("foundation-period-projections",), _collect_foundation_period_projections),
            CorpusCollector(("external-content",), _collect_external_content),
            CorpusCollector(
                ("agent-native-memory", "agent-native-instructions"),
                _collect_agent_native_memory,
                filter_to_selected_source_sets=True,
            ),
        ),
        retired_source_sets=RETIRED_SOURCE_SETS,
    )


def _collect_agent_native_memory(
    settings: RagSettings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from data_foundation.native_memory_sources import (
        collect_claude_native_memory,
        collect_codex_native_memory,
    )
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.settings import (
        external_tool_path,
        resolve_memory_search_settings,
    )

    paths = runtime_paths_for_home(settings.runtime_home)
    memory_settings = resolve_memory_search_settings(paths)
    native = (
        memory_settings.get("nativeMemory")
        if isinstance(memory_settings.get("nativeMemory"), dict)
        else {}
    )
    if (
        native.get("enabled") is not True
        or native.get("allowInRag") is not True
    ):
        return [], []
    tools = native.get("tools") if isinstance(native.get("tools"), dict) else {}
    include_instructions = native.get("includeInstructions") is True
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if tools.get("codex") is True:
        result = collect_codex_native_memory(
            external_tool_path("codex", "home", paths),
            include_instructions=include_instructions,
        )
        chunks.extend(result["documents"])
        sources.extend(result["sources"])
    if tools.get("claudeCode") is True:
        result = collect_claude_native_memory(
            external_tool_path("claudeCode", "projectsRoot", paths),
            include_instructions=include_instructions,
        )
        chunks.extend(result["documents"])
        sources.extend(result["sources"])
    return chunks, sources


def _collect_external_content(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .rag_external_sources import collect_external_source_chunks

    return collect_external_source_chunks(settings)


def _collect_filtered_dialogue_daily(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    daily_root = _diary_source_root(settings) / "__diary_daily"
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not daily_root.exists():
        return chunks, sources
    files = sorted(daily_root.glob("*/_filtered/*/*.jsonl"))
    source_set = "filtered-dialogue-daily"
    for path in files:
        count_before = len(chunks)
        date_value = _business_date_from_filtered_path(path)
        agent = path.parent.name
        for line_number, line in enumerate(_iter_lines(path), 1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(payload.get("content") or "").strip()
            if not text:
                continue
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=text,
                    layer="dialogue",
                    date=date_value,
                    agent=agent,
                    source_path=path,
                    line_number=line_number,
                    source_type="filtered-dialogue-jsonl",
                    provenance={
                        "role": payload.get("role"),
                        "time": payload.get("time"),
                        "authority": "Filtered dialogue is the cleaned conversation source for RAG recall.",
                    },
                )
            )
        sources.append(_source_record(source_set, path, len(chunks) - count_before, source_type="filtered-dialogue-jsonl"))
    return chunks, sources


def _collect_lessons(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from data_foundation.paths import runtime_paths_for_home

    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    runtime_paths = runtime_paths_for_home(settings.runtime_home)
    paths = lessons_read_paths(
        canonical_path=_lessons_path(settings),
        diary_source_root=_diary_source_root(settings),
        legacy_diary_root=runtime_paths.legacy_diary_root,
    )
    for lessons_path in paths:
        if not lessons_path.exists():
            continue
        count_before = len(chunks)
        for line_number, line in enumerate(_iter_lines(lessons_path), 1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            identity = str(payload.get("id") or _text_hash(text))
            if identity in seen:
                continue
            seen.add(identity)
            chunk = _chunk_payload(
                source_set="lessons",
                text=text,
                layer="lesson",
                date=payload.get("date"),
                agent=payload.get("agent"),
                source_path=lessons_path,
                line_number=line_number,
                stable_id=payload.get("id"),
            )
            chunks.append(chunk)
        sources.append(_source_record("lessons", lessons_path, len(chunks) - count_before, source_type="jsonl"))
    return chunks, sources


def _collect_foundation_usage_rollups(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db_path = _foundation_db_path(settings)
    source_set = "foundation-usage-rollups"
    chunks: list[dict[str, Any]] = []
    if not db_path.exists():
        return chunks, [_source_record(source_set, db_path, 0, source_type="sqlite")]
    for table, sql in (
        (
            "daily_tool_usage",
            """
            SELECT business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
            FROM daily_tool_usage
            ORDER BY business_date, tool_key
            """,
        ),
        (
            "daily_model_usage",
            """
            SELECT business_date, model_key, tool_key, tokens, messages, sessions, source_run_id
            FROM daily_model_usage
            ORDER BY business_date, model_key, tool_key
            """,
        ),
        (
            "daily_project_usage",
            """
            SELECT business_date, project_id_or_bucket, tool_key, tokens, messages, active_sessions, evidence_confidence, source_run_id
            FROM daily_project_usage
            ORDER BY business_date, project_id_or_bucket, tool_key
            """,
        ),
    ):
        for ordinal, row in enumerate(_sqlite_query(db_path, sql), 1):
            row_dict = dict(row)
            text = _foundation_row_text(table, row_dict)
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=text,
                    layer="usage",
                    date=row_dict.get("business_date"),
                    agent=row_dict.get("tool_key"),
                    project=row_dict.get("project_id_or_bucket"),
                    source_path=db_path,
                    line_number=ordinal,
                    stable_id=f"{source_set}:{table}:{_stable_row_hash(row_dict)}",
                    source_type="foundation-sqlite",
                    provenance={
                        "table": table,
                        "row": row_dict,
                        "authority": "Foundation SQLite rollups are normalized usage facts.",
                    },
                )
            )
    return chunks, [_source_record(source_set, db_path, len(chunks), source_type="foundation-sqlite")]


def _collect_foundation_dashboard_snapshots(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db_path = _foundation_db_path(settings)
    source_set = "foundation-dashboard-snapshots"
    chunks: list[dict[str, Any]] = []
    if not db_path.exists():
        return chunks, [_source_record(source_set, db_path, 0, source_type="sqlite")]
    sql = """
        SELECT snapshot_key, projection_type, payload_json, generated_at, source_run_id, status
        FROM dashboard_snapshots
        WHERE status = 'ready'
        ORDER BY snapshot_key
    """
    for ordinal, row in enumerate(_sqlite_query(db_path, sql), 1):
        payload = _json_loads(row["payload_json"])
        text = (
            f"Dashboard snapshot {row['snapshot_key']} ({row['projection_type']}) generated {row['generated_at']}. "
            f"Payload summary: {_json_summary(payload)}"
        )
        chunks.append(
            _chunk_payload(
                source_set=source_set,
                text=text,
                layer="snapshot",
                date=_date_from_snapshot_key(str(row["snapshot_key"])) or str(row["generated_at"])[:10],
                agent=None,
                source_path=db_path,
                line_number=ordinal,
                stable_id=f"{source_set}:{row['snapshot_key']}",
                source_type="foundation-sqlite",
                provenance={
                    "table": "dashboard_snapshots",
                    "snapshotKey": row["snapshot_key"],
                    "projectionType": row["projection_type"],
                    "sourceRunId": row["source_run_id"],
                    "authority": "Foundation dashboard snapshots are materialized read models.",
                },
            )
        )
    return chunks, [_source_record(source_set, db_path, len(chunks), source_type="foundation-sqlite")]


def _collect_diary_markdown_sections(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = _diary_source_root(settings)
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not root.exists():
        return chunks, sources
    for path in iter_diary_markdown_files(root):
        report_type = _report_type_for(path, settings=settings)
        if report_type not in {"narrative", "learning", "technical"}:
            continue
        source_set = "diary-markdown-sections"
        count_before = len(chunks)
        content = _read_text(path)
        if not content:
            sources.append(_source_record(source_set, path, 0, source_type="markdown"))
            continue
        parsed = parse_diary_markdown(content)
        business_date = _business_date_from_path(path) or _business_date_from_embedded(parsed.embedded_json)
        if parsed.title:
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=parsed.title,
                    layer=report_type,
                    date=business_date,
                    agent=None,
                    source_path=path,
                    line_number=1,
                    source_type="markdown",
                    provenance={
                        "reportType": report_type,
                        "field": "title",
                        "authority": "Generated Diary Markdown is read-only RAG source material.",
                    },
                )
            )
        for section in parsed.sections:
            text = _section_text(section.heading, section.body_markdown)
            if not text.strip():
                continue
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=text,
                    layer=report_type,
                    date=business_date,
                    agent=None,
                    source_path=path,
                    line_number=section.ordinal + 1,
                    source_type="markdown",
                    provenance={
                        "reportType": report_type,
                        "heading": section.heading,
                        "headingPath": list(section.heading_path),
                        "headingLevel": section.heading_level,
                        "sectionOrdinal": section.ordinal,
                        "authority": "Generated Diary Markdown is read-only RAG source material.",
                    },
                )
            )
        sources.append(_source_record(source_set, path, len(chunks) - count_before, source_type="markdown"))
    return chunks, sources


def _collect_diary_markdown_embedded_json(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = _diary_source_root(settings)
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not root.exists():
        return chunks, sources
    source_set = "diary-markdown-embedded-json"
    for path in iter_diary_markdown_files(root):
        report_type = _report_type_for(path, settings=settings)
        if report_type == "unknown":
            continue
        count_before = len(chunks)
        content = _read_text(path)
        parsed = parse_diary_markdown(content) if content else None
        business_date = _business_date_from_path(path) or _business_date_from_embedded(parsed.embedded_json if parsed else None)
        if parsed and parsed.embedded_json:
            top_level_keys = sorted(parsed.embedded_json.keys())
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text="Embedded JSON top-level keys: " + ", ".join(top_level_keys),
                    layer=report_type,
                    date=business_date,
                    agent=None,
                    source_path=path,
                    line_number=0,
                    source_type="embedded-json",
                    provenance={
                        "reportType": report_type,
                        "field": "embedded_json",
                        "topLevelKeys": top_level_keys,
                        "readOnly": True,
                        "authority": "Embedded JSON is parsed read-only; top-level keys are not changed.",
                    },
                )
            )
        sources.append(_source_record(source_set, path, len(chunks) - count_before, source_type="embedded-json"))
    return chunks, sources


def _collect_technical_report_task_events(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = _diary_source_root(settings)
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not root.exists():
        return chunks, sources
    source_set = "technical-report-task-events"
    technical_paths: list[Path] = []
    for day_dir in sorted({path.parent for path in iter_diary_markdown_files(root)}):
        business_date = _business_date_from_path(day_dir)
        if not business_date:
            continue
        technical_paths.extend(diary_report_paths(root, business_date, "technical", language_profile=settings.language_profile))
    for path in sorted(set(technical_paths)):
        count_before = len(chunks)
        content = _read_text(path)
        report_date, updates = _parse_report_updates(content)
        for ordinal, update in enumerate(updates, 1):
            text = " | ".join(
                str(value)
                for value in (
                    update.get("task_id"),
                    update.get("title"),
                    update.get("status"),
                    update.get("progress_delta"),
                )
                if value is not None and str(value) != ""
            )
            if not text:
                continue
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=text,
                    layer="task",
                    date=report_date or _business_date_from_path(path),
                    agent=None,
                    project=update.get("project_id"),
                    source_path=path,
                    line_number=ordinal,
                    source_type="technical-report-task-event",
                    provenance={
                        "authority": "Technical report task events are historical observations, not current task authority.",
                        "reportDate": report_date,
                        "eventOrdinal": ordinal,
                        "taskId": update.get("task_id"),
                        "status": update.get("status"),
                        "progressDelta": update.get("progress_delta"),
                    },
                )
            )
        sources.append(_source_record(source_set, path, len(chunks) - count_before, source_type="technical-report-task-event"))
    return chunks, sources


def _collect_nova_task_work_graph_events(
    settings: RagSettings,
    *,
    source_set: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    directory = _nova_task_work_graph_dir(settings, source_set=source_set)
    chunks: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if not directory.exists():
        return chunks, sources
    for path in sorted(directory.glob("*.md")):
        count_before = len(chunks)
        content = _read_text(path)
        if not _nova_task_artifact_applied(content):
            sources.append(
                {
                    **_source_record(source_set, path, 0, source_type="nova-task-work-graph"),
                    "skippedReason": "not-applied",
                }
            )
            continue
        payload = _extract_nova_task_payload(content)
        if not isinstance(payload, dict):
            sources.append(_source_record(source_set, path, 0, source_type="nova-task-work-graph"))
            continue
        business_date = str(payload.get("date") or _business_date_from_reconciliation_path(path) or "")
        for ordinal, item in enumerate(_reconciliation_payload_items(payload), 1):
            text = item["text"]
            if not text:
                continue
            chunks.append(
                _chunk_payload(
                    source_set=source_set,
                    text=text,
                    layer="task",
                    date=business_date or None,
                    agent=None,
                    project=item.get("project"),
                    source_path=path,
                    line_number=ordinal,
                    stable_id=f"{path.name}:{item['kind']}:{ordinal}:{item.get('stable') or ''}",
                    source_type="nova-task-work-graph",
                    provenance={
                        "authority": "Nova-Task work-graph artifact; LLM classification with deterministic graph/ledger/planning validation.",
                        "sourceSet": source_set,
                        "recordType": item["kind"],
                        "businessDate": business_date or None,
                        "targetNodeId": item.get("targetNodeId"),
                        "candidateId": item.get("candidateId"),
                        "action": item.get("action"),
                        "confidence": item.get("confidence"),
                    },
                )
            )
        sources.append(_source_record(source_set, path, len(chunks) - count_before, source_type="nova-task-work-graph"))
    return chunks, sources


def _nova_task_work_graph_dir(settings: RagSettings, *, source_set: str) -> Path:
    task_board = _task_board_path(settings)
    try:
        home = task_board.parents[2]
    except IndexError:
        home = Path(settings.v2_store_path).expanduser().absolute()
        for parent in home.parents:
            if parent.name == "reserved":
                home = parent.parent
                break
    if source_set == "nova-task-reconciliation-events":
        return home / "state" / "nova-task" / "candidate-reconciliation"
    return home / "state" / "nova-task" / "work-graph"


def _business_date_from_reconciliation_path(path: Path) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})-", path.name)
    return match.group(1) if match else None


def _nova_task_artifact_applied(content: str) -> bool:
    header = str(content or "").split("```", 1)[0].lower()
    return re.search(r"(?m)^\s*-\s*applied:\s*true\s*$", header) is not None


def _reconciliation_payload_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def evidence_text(item: dict[str, Any]) -> str:
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return ""
        return " evidence=" + "; ".join(str(value) for value in evidence[:4] if value is not None)

    for item in _payload_list(payload, "matched_tasks"):
        task_id = str(item.get("task_id") or "")
        summary = str(item.get("summary") or "")
        event_type = str(item.get("event_type") or "progress")
        text = " | ".join(value for value in (task_id, event_type, summary) if value) + evidence_text(item)
        records.append(
            {
                "kind": "matched_task",
                "text": text,
                "targetNodeId": task_id or None,
                "confidence": item.get("confidence"),
                "stable": task_id,
            }
        )
    for key, kind in (("candidate_parent_tasks", "candidate_parent"), ("candidate_subtasks", "candidate_subtask")):
        for item in _payload_list(payload, key):
            title = str(item.get("proposed_title") or "")
            parent = str(item.get("proposed_parent_task_id") or "")
            level = str(item.get("proposed_level") or "")
            reason = str(item.get("reason") or "")
            text = " | ".join(value for value in (kind, parent, f"L{level}" if level else "", title, reason) if value)
            text += evidence_text(item)
            records.append(
                {
                    "kind": kind,
                    "text": text,
                    "targetNodeId": parent or None,
                    "confidence": item.get("confidence"),
                    "stable": title,
                }
            )
    for item in _payload_list(payload, "status_signals"):
        task_id = str(item.get("task_id") or "")
        target_status = str(item.get("target_status") or "")
        reason = str(item.get("status_reason") or "")
        text = " | ".join(value for value in ("status_signal", task_id, target_status, reason) if value)
        text += evidence_text(item)
        records.append(
            {
                "kind": "status_signal",
                "text": text,
                "targetNodeId": task_id or None,
                "confidence": item.get("confidence"),
                "stable": task_id,
            }
        )
    for item in _payload_list(payload, "candidate_actions"):
        candidate_id = str(item.get("candidate_id") or "")
        action = str(item.get("action") or "")
        target = str(item.get("target_node_id") or "")
        reason = str(item.get("reason") or "")
        text = " | ".join(value for value in ("candidate_action", candidate_id, action, target, reason) if value)
        text += evidence_text(item)
        records.append(
            {
                "kind": "candidate_action",
                "text": text,
                "targetNodeId": target or None,
                "candidateId": candidate_id or None,
                "action": action or None,
                "confidence": item.get("confidence"),
                "stable": candidate_id,
            }
        )
    for item in _payload_list(payload, "unresolved"):
        summary = str(item.get("summary") or "")
        reason = str(item.get("reason") or "")
        text = " | ".join(value for value in ("unresolved", reason, summary) if value)
        text += evidence_text(item)
        records.append({"kind": "unresolved", "text": text, "confidence": item.get("confidence"), "stable": summary})
    return records


def _payload_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _collect_task_board_snapshot(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    board_path = _task_board_path(settings)
    if not board_path.exists():
        return [], []
    content = _read_text(board_path)
    parsed = parse_task_board_markdown(content)
    if not parsed.get("projects") and not parsed.get("items"):
        parsed = _parse_nova_task_projection_board(content)
    chunks: list[dict[str, Any]] = []
    for project in parsed["projects"]:
        chunks.append(
            _chunk_payload(
                source_set="task-board-snapshot",
                text=f"{project['section']} / {project['project']}",
                layer="task",
                date=None,
                agent=None,
                project=project["project"],
                source_path=board_path,
                line_number=project["projectOrdinal"] + 1,
                source_type="task-board-markdown",
                provenance={
                    "authority": "Nova-Task v2 SQLite authority; TASK_BOARD.md projection",
                    "recordType": "project",
                    "section": project["section"],
                    "projectOrdinal": project["projectOrdinal"],
                },
            )
        )
    for item in parsed["items"]:
        chunks.append(
            _chunk_payload(
                source_set="task-board-snapshot",
                text=item["content"],
                layer="task",
                date=None,
                agent=item.get("agent") or None,
                project=item.get("project"),
                source_path=board_path,
                line_number=item["sourceLine"],
                stable_id=item["itemKey"],
                source_type="task-board-markdown",
                provenance={
                    "authority": "Nova-Task v2 SQLite authority; TASK_BOARD.md projection",
                    "recordType": "task-item",
                    "section": item["section"],
                    "project": item["project"],
                    "done": item["done"],
                    "identifiedTaskId": item.get("identifiedTaskId"),
                    "rawLine": item["rawLine"],
                },
            )
        )
    return chunks, [_source_record("task-board-snapshot", board_path, len(chunks), source_type="task-board-markdown")]


def _parse_nova_task_projection_board(content: str) -> dict[str, Any]:
    """Parse current Nova-Task v2 projection boards without legacy project headings."""
    projects: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    current_section = "未知"
    current_project: dict[str, Any] | None = None
    task_re = re.compile(r"^(?P<indent>\s*)-\s*\[(?P<checked>[^\]]*)\]\s+\*\*\[(?P<id>NT-[^\]]+)\]\*\*\s+(?P<title>.+)$")

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.rstrip()
        section_match = re.match(r"^##\s+(.+)$", line)
        if section_match:
            current_section = re.sub(r"\s*\(.*?\)$", "", section_match.group(1).strip())
            current_project = None
            continue
        task_match = task_re.match(line)
        if not task_match:
            continue
        indent = len(task_match.group("indent") or "")
        task_id = task_match.group("id").strip()
        checked = task_match.group("checked").strip().lower()
        raw_title = task_match.group("title").strip()
        title = _strip_task_projection_suffix(raw_title)
        if indent == 0 or current_project is None:
            current_project = {
                "projectOrdinal": len(projects),
                "section": current_section,
                "project": title,
            }
            projects.append(current_project)
        item_key = f"nova-task-v2:{task_id}"
        items.append(
            {
                "itemKey": item_key,
                "projectOrdinal": current_project["projectOrdinal"],
                "itemOrdinal": sum(1 for item in items if item["projectOrdinal"] == current_project["projectOrdinal"]),
                "section": current_project["section"],
                "project": current_project["project"],
                "done": checked == "x",
                "content": f"[{task_id}] {raw_title}",
                "agent": "",
                "identifiedTaskId": task_id,
                "sourceLine": line_number,
                "rawLine": line,
            }
        )
    return {
        "projects": projects,
        "items": items,
        "counts": {
            "projects": len(projects),
            "items": len(items),
            "Completed": sum(1 for item in items if item["done"]),
            "InProgress": sum(1 for item in items if not item["done"]),
        },
    }


def _strip_task_projection_suffix(value: str) -> str:
    return re.sub(r"\s+\([^)]*\)\s*$", "", str(value or "")).strip()


def _collect_foundation_period_projections(settings: RagSettings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db_path = _foundation_db_path(settings)
    source_set = "foundation-period-projections"
    chunks: list[dict[str, Any]] = []
    if not db_path.exists():
        return chunks, [_source_record(source_set, db_path, 0, source_type="sqlite")]
    sql = """
        SELECT report_key, period_type, start_date, end_date, projection_type, metrics_json,
               generated_at, source_run_id, status
        FROM period_reports
        WHERE status = 'ready'
        ORDER BY start_date, end_date, projection_type
    """
    for ordinal, row in enumerate(_sqlite_query(db_path, sql), 1):
        metrics = _json_loads(row["metrics_json"])
        text = (
            f"{row['period_type']} projection {row['projection_type']} "
            f"for {row['start_date']} to {row['end_date']}. "
            f"Metrics summary: {_json_summary(metrics)}"
        )
        chunks.append(
            _chunk_payload(
                source_set=source_set,
                text=text,
                layer="period",
                date=row["end_date"],
                agent=None,
                source_path=db_path,
                line_number=ordinal,
                stable_id=f"{source_set}:{row['report_key']}",
                source_type="foundation-sqlite",
                provenance={
                    "table": "period_reports",
                    "reportKey": row["report_key"],
                    "periodType": row["period_type"],
                    "startDate": row["start_date"],
                    "endDate": row["end_date"],
                    "projectionType": row["projection_type"],
                    "sourceRunId": row["source_run_id"],
                    "authority": "Foundation period projections are materialized report read models.",
                },
            )
        )
    return chunks, [_source_record(source_set, db_path, len(chunks), source_type="foundation-sqlite")]


def _source_profile(settings: RagSettings, source_sets: list[str] | tuple[str, ...]) -> dict[str, Any]:
    _validate_source_sets(source_sets)
    diary_root = _diary_source_root(settings)
    result = {
        "schemaVersion": 1,
        "diarySourceRoot": str(diary_root),
        "filteredDialogueRoot": str(diary_root / "__diary_daily"),
        "lessonsPath": str(_lessons_path(settings)),
        "taskBoardPath": str(_task_board_path(settings)),
        "foundationDbPath": str(_foundation_db_path(settings)),
        "sourceSets": list(source_sets),
    }
    if "external-content" in source_sets:
        result["externalSources"] = settings.external_sources.to_dict()
    if {"agent-native-memory", "agent-native-instructions"}.intersection(source_sets):
        from data_foundation.paths import runtime_paths_for_home
        from data_foundation.settings import native_memory_policy_profile

        result["nativeMemory"] = native_memory_policy_profile(
            runtime_paths_for_home(settings.runtime_home)
        )
    return result


def _diary_source_root(settings: RagSettings) -> Path:
    return Path(settings.diary_source_root).expanduser().absolute()


def _validate_source_sets(source_sets: list[str] | tuple[str, ...]) -> None:
    retired = sorted(RETIRED_SOURCE_SETS & {str(item).strip() for item in source_sets})
    if retired:
        raise ValueError(f"retired RAG sourceSets are not allowed in production v2 indexing: {', '.join(retired)}")


def _validate_external_source_selection(settings: RagSettings, source_sets: list[str] | tuple[str, ...]) -> None:
    if "external-content" not in source_sets:
        return
    if not settings.external_sources.enabled:
        raise ValueError("external-content sourceSet requires rag.indexing.externalSources.enabled=true")
    if not settings.external_sources.paths:
        raise ValueError("external-content sourceSet requires at least one configured absolute path")


def _lessons_path(settings: RagSettings) -> Path:
    return Path(settings.lessons_path).expanduser().absolute()


def _task_board_path(settings: RagSettings) -> Path:
    return Path(settings.task_board_path).expanduser().absolute()


def _embed_chunks(
    chunks: list[dict[str, Any]],
    settings: RagSettings,
    embedding_fn: EmbeddingFn | None,
) -> list[list[float]]:
    if not chunks:
        return []
    if embedding_fn is None:
        raise ValueError("embedding_fn is required; direct embedding fallback has been retired")
    embed = embedding_fn
    embeddings: list[list[float]] = []
    for offset in range(0, len(chunks), settings.embedding_batch_size):
        batch = chunks[offset : offset + settings.embedding_batch_size]
        embeddings.extend(embed([str(chunk["text"]) for chunk in batch]))
    return embeddings


def _load_active_embedding_cache(settings: RagSettings) -> dict[str, dict[str, Any]]:
    manifest = _read_json(settings.v2_store_path / "manifest.json")
    if manifest.get("status") != "active":
        return {}
    if str(manifest.get("model") or "") != settings.embedding_model:
        return {}
    if int(manifest.get("dimension") or 0) != settings.embedding_dimension:
        return {}
    index_path = _index_path_from_manifest(manifest)
    if not index_path or not index_path.exists():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    for line in _iter_lines(index_path):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        embedding = payload.get("embedding")
        chunk_id = str(payload.get("id") or "")
        if not chunk_id or not isinstance(embedding, list) or len(embedding) != settings.embedding_dimension:
            continue
        cache[chunk_id] = {
            "embedding": embedding,
            "dedupeKey": payload.get("dedupeKey"),
            "textHash": payload.get("textHash") or _text_hash(payload.get("text")),
        }
    return cache


def _index_path_from_manifest(manifest: dict[str, Any]) -> Path | None:
    value = manifest.get("activeIndexPath")
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.suffix == ".jsonl":
        return path
    nested = path / "index.jsonl"
    return nested if nested.exists() else path


def _reusable_embedding(
    chunk: dict[str, Any],
    cache: dict[str, dict[str, Any]],
    settings: RagSettings,
) -> list[float] | None:
    cached = cache.get(str(chunk.get("id") or ""))
    if not cached:
        return None
    embedding = cached.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != settings.embedding_dimension:
        return None
    if cached.get("dedupeKey") != chunk.get("dedupeKey"):
        return None
    if cached.get("textHash") != chunk.get("textHash"):
        return None
    return embedding


def _message_content_and_timestamp(payload: dict[str, Any]) -> tuple[Any, Any]:
    timestamp = payload.get("timestamp")
    if payload.get("type") == "message":
        message = payload.get("message") or {}
        return message.get("content"), message.get("timestamp") or timestamp
    return payload.get("content"), timestamp


def _clean_content(content: Any) -> tuple[str, str]:
    if not content:
        return "", ""
    if isinstance(content, list):
        content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    text = str(content)
    tech_indicators = [r"Traceback", r"Exception:", r"Error\s*:", r"failed\s*:", r"Exit code", r"\[toolResult\]"]
    is_tech = any(re.search(pattern, text, re.I) for pattern in tech_indicators)
    cleaned = re.sub(r"Conversation info.*?(\n\n|\[USER\]|$)", "", text, flags=re.DOTALL).strip()
    if len(cleaned) < 20 and not is_tech:
        return "", ""
    return cleaned, "technical" if is_tech else "narrative"


def _chunk_payload(
    *,
    source_set: str,
    text: str,
    layer: str,
    date: Any,
    agent: Any,
    source_path: Path,
    source_identity: str | None = None,
    line_number: int,
    stable_id: Any = None,
    project: Any = None,
    source_type: str = "jsonl",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_source_identity = source_identity or _logical_source_path(source_path)
    source_id = _source_id(source_set, selected_source_identity)
    chunk_id = str(stable_id or _chunk_id(source_set, selected_source_identity, line_number, text))
    chunk = {
        "id": chunk_id,
        "text": text,
        "layer": layer,
        "date": date,
        "agent": agent,
        "project": project,
        "sourceSet": source_set,
        "sourceId": source_id,
        "sourcePath": str(source_path),
        "sourceType": source_type,
        "lineNumber": line_number,
        "textPreview": text[:500],
        "textHash": _text_hash(text),
        "privacyClass": "local-private",
        "provenance": provenance or {},
        "dedupeKey": hashlib.sha256(f"{source_set}|{selected_source_identity}|{line_number}|{text[:160]}".encode("utf-8")).hexdigest(),
    }
    tags = infer_tags(chunk)
    chunk["tags"] = tags
    chunk["workType"] = infer_work_type(chunk, tags)
    chunk["governance"] = governance_for_chunk(chunk)
    return chunk


def _source_record(source_set: str, path: Path, chunk_count: int, *, source_type: str) -> dict[str, Any]:
    stat = path.stat() if path.exists() else None
    fingerprint = _source_fingerprint(source_set, path, stat)
    return {
        "sourceSet": source_set,
        "sourceType": source_type,
        "sourceId": _source_id(source_set, _logical_source_path(path)),
        "sourceLogicalPath": _logical_source_path(path),
        "path": str(path),
        "exists": path.exists(),
        "byteSize": stat.st_size if stat else 0,
        "updatedAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat() if stat else None,
        "fingerprint": fingerprint,
        "chunkCount": chunk_count,
        "privacyClass": "local-private",
        "retentionPolicy": "operator-controlled",
        "governance": governance_for_source(source_set),
    }


def _text_hash(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _source_fingerprint(source_set: str, path: Path, stat: Any) -> str | None:
    if stat is None:
        return None
    return hashlib.sha256(
        f"{source_set}|{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()


def _logical_source_path(path: Path) -> str:
    resolved = Path(path).expanduser()
    parts = list(resolved.parts)
    for marker in ("__diary_daily",):
        if marker in parts:
            return "/".join(parts[parts.index(marker) :])
    for index, part in enumerate(parts):
        if re.match(r"diary-\d{4}$", part):
            return "/".join(parts[index:])
        if re.match(r"diary-\d{4}-\d{2}-\d{2}$", part):
            return "/".join(parts[index:])
    for index in range(0, len(parts) - 2):
        if re.match(r"\d{4}$", parts[index]) and re.match(r"\d{2}$", parts[index + 1]) and re.match(r"\d{2}$", parts[index + 2]):
            return "/".join(parts[index:])
    if resolved.name in {"lessons.jsonl", "TASK_BOARD.md", "actanara_data.sqlite3"}:
        return resolved.name
    return resolved.name or str(resolved)


def _report_type_for(path: Path, *, settings: RagSettings | None = None) -> str:
    language_profile = settings.language_profile if settings is not None else "mixed"
    return diary_report_type_for_filename(path.name, language_profile=language_profile)


def _section_text(heading: str, body: str) -> str:
    body = str(body or "").strip()
    return f"{heading}\n{body}" if body else heading


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _business_date_from_path(path: Path) -> str | None:
    parts = list(path.parts)
    for index in range(0, len(parts) - 2):
        if re.match(r"diary-\d{4}$", parts[index]) and re.match(r"diary-\d{4}-\d{2}$", parts[index + 1]) and re.match(r"\d{2}-\d{2}$", parts[index + 2]):
            year = parts[index].removeprefix("diary-")
            month = parts[index + 1].rsplit("-", 1)[-1]
            day = parts[index + 2].split("-", 1)[-1]
            return f"{year}-{month}-{day}"
    for part in parts:
        match = re.match(r"diary-(\d{4}-\d{2}-\d{2})$", part)
        if match:
            return match.group(1)
    for index in range(0, len(parts) - 2):
        candidate = "/".join(parts[index : index + 3])
        if re.match(r"\d{4}/\d{2}/\d{2}$", candidate):
            return candidate.replace("/", "-")
    return None


def _business_date_from_filtered_path(path: Path) -> str | None:
    parts = list(path.parts)
    try:
        index = parts.index("__diary_daily")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    candidate = parts[index + 1]
    return candidate if re.match(r"\d{4}-\d{2}-\d{2}$", candidate) else None


def _business_date_from_embedded(value: dict | None) -> str | None:
    if not isinstance(value, dict):
        return None
    date_value = value.get("date")
    return str(date_value)[:10] if date_value else None


def _source_id(source_set: str, path: str) -> str:
    return hashlib.sha256(f"{source_set}|{path}".encode("utf-8")).hexdigest()[:24]


def _chunk_id(source_set: str, path: str, line_number: int, text: str) -> str:
    return hashlib.sha256(f"{source_set}|{path}|{line_number}|{text[:160]}".encode("utf-8")).hexdigest()


def _iter_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            yield from handle
    except OSError:
        return


def _hkt_date(iso_text: str) -> str:
    try:
        parsed = parse_timestamp(iso_text)
        if parsed is None:
            return "Unknown"
        return business_date_for(parsed).isoformat()
    except (TypeError, ValueError):
        return "Unknown"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _foundation_db_path(settings: RagSettings) -> Path:
    return Path(settings.foundation_db_path).expanduser().absolute()


def _sqlite_query(db_path: Path, sql: str) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(sql))
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def _foundation_row_text(table: str, row: dict[str, Any]) -> str:
    if table == "daily_tool_usage":
        return (
            f"{row.get('business_date')} {row.get('tool_key')} usage: "
            f"{row.get('tokens')} tokens, {row.get('messages')} messages, "
            f"{row.get('sessions')} sessions, {row.get('api_calls')} API calls."
        )
    if table == "daily_model_usage":
        return (
            f"{row.get('business_date')} model {row.get('model_key')} via {row.get('tool_key')}: "
            f"{row.get('tokens')} tokens, {row.get('messages')} messages, {row.get('sessions')} sessions."
        )
    return (
        f"{row.get('business_date')} project {row.get('project_id_or_bucket')} via {row.get('tool_key')}: "
        f"{row.get('tokens')} tokens, {row.get('messages')} messages, "
        f"{row.get('active_sessions')} active sessions, confidence {row.get('evidence_confidence')}."
    )


def _stable_row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _json_loads(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def _json_summary(value: Any, *, max_items: int = 12, max_chars: int = 1200) -> str:
    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys())[:max_items]:
            child = value[key]
            if isinstance(child, dict):
                parts.append(f"{key}: object({len(child)})")
            elif isinstance(child, list):
                parts.append(f"{key}: list({len(child)})")
            else:
                parts.append(f"{key}: {str(child)[:120]}")
        return "; ".join(parts)[:max_chars]
    if isinstance(value, list):
        return f"list({len(value)}) " + json.dumps(value[:3], ensure_ascii=False)[:max_chars]
    return str(value)[:max_chars]


def _date_from_snapshot_key(value: str) -> str | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else None
