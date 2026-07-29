"""Local, rebuildable lexical memory search.

This module is deliberately independent from embedding providers and model
services.  It mirrors governed memory chunks into a disposable SQLite sidecar
and exposes the same evidence-oriented response shape used by external-agent
memory search.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import RuntimePaths, load_paths
from .settings import (
    native_memory_policy_digest,
    native_memory_policy_profile,
    resolve_memory_search_settings,
)


LOCAL_MEMORY_SCHEMA_VERSION = 2
DEFAULT_INDEX_MAX_AGE_SECONDS = 300
MAX_QUERY_CHARACTERS = 1000
MAX_CANDIDATES_PER_LANE = 80
RRF_K = 60.0


class _UnsafeLocalMemoryIndexError(ValueError):
    """Raised when a managed sidecar path could escape its private directory."""


def local_memory_index_path(paths: RuntimePaths | None = None) -> Path:
    selected = paths or load_paths()
    return selected.state_dir / "cache" / "memory-search.sqlite3"


def collect_local_memory_corpus(
    paths: RuntimePaths | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect governed chunks without invoking embeddings or an LLM."""
    selected = paths or load_paths()
    diagnostics: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    try:
        from .memory_corpus import collect_runtime_memory_corpus
    except (ImportError, AttributeError):
        collect_runtime_memory_corpus = None  # type: ignore[assignment]

    if collect_runtime_memory_corpus is not None:
        chunks, sources = collect_runtime_memory_corpus(selected)
    else:
        from agentic_rag.rag_settings import resolve_rag_settings
        from agentic_rag.rag_v2_indexer import collect_candidate_chunks

        rag_settings = resolve_rag_settings(selected)
        chunks, sources = collect_candidate_chunks(
            rag_settings,
            rag_settings.indexing_source_sets,
        )

    memory_settings = resolve_memory_search_settings(selected)
    native = memory_settings.get("nativeMemory") if isinstance(memory_settings.get("nativeMemory"), dict) else {}
    tools = native.get("tools") if isinstance(native.get("tools"), dict) else {}
    if (
        native.get("enabled") is True
        and native.get("allowInRag") is not True
        and any(value is True for value in tools.values())
    ):
        try:
            from .native_memory_sources import (
                collect_claude_native_memory,
                collect_codex_native_memory,
            )

            native_results: list[dict[str, Any]] = []
            if tools.get("codex") is True:
                native_results.append(
                    collect_codex_native_memory(
                        _external_tool_home(selected, "codex"),
                        include_instructions=native.get("includeInstructions") is True,
                    )
                )
            if tools.get("claudeCode") is True:
                native_results.append(
                    collect_claude_native_memory(
                        _external_tool_path(selected, "claudeCode", "projectsRoot"),
                        include_instructions=native.get("includeInstructions") is True,
                    )
                )
            for native_result in native_results:
                native_documents = native_result.get("documents")
                native_sources = native_result.get("sources")
                native_diagnostics = native_result.get("diagnostics")
                if isinstance(native_documents, list):
                    chunks.extend(item for item in native_documents if isinstance(item, dict))
                if isinstance(native_sources, list):
                    sources.extend(item for item in native_sources if isinstance(item, dict))
                if isinstance(native_diagnostics, list):
                    diagnostics.extend(item for item in native_diagnostics if isinstance(item, dict))
        except (ImportError, OSError, ValueError) as exc:
            diagnostics.append(
                {
                    "source": "agent-native-memory",
                    "status": "unavailable",
                    "reason": str(exc),
                }
            )

    deduped: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        chunk_id = str(chunk.get("id") or "").strip()
        if chunk_id:
            deduped.setdefault(chunk_id, chunk)
    return list(deduped.values()), sources, diagnostics


def bounded_scan_local_memory_corpus(
    query: str,
    *,
    filters: dict[str, Any],
    paths: RuntimePaths,
    max_files: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Query a deliberately small, Runtime-owned file subset under hard I/O caps.

    This emergency path is used only when the disposable SQLite sidecar cannot
    be read or rebuilt. It never calls the regular corpus collectors because
    those collectors are allowed to materialize the complete corpus.
    """
    file_limit = max(1, int(max_files))
    byte_limit = max(1, int(max_bytes))
    match_limit = file_limit
    scanned_files = 0
    scanned_bytes = 0
    truncated = False
    chunks: list[dict[str, Any]] = []
    normalized_query = _normalized_text(query)
    terms = [_normalized_text(term) for term in _query_terms(query)]

    candidates = _bounded_scan_paths(paths)
    for path in candidates:
        if scanned_files >= file_limit or scanned_bytes >= byte_limit:
            truncated = True
            break
        try:
            file_lstat = path.lstat()
            if stat.S_ISLNK(file_lstat.st_mode) or not stat.S_ISREG(file_lstat.st_mode):
                continue
            remaining = byte_limit - scanned_bytes
            read_size = min(int(file_lstat.st_size), remaining)
            with path.open("rb") as handle:
                raw = handle.read(read_size)
        except (FileNotFoundError, OSError):
            continue
        scanned_files += 1
        scanned_bytes += len(raw)
        if int(file_lstat.st_size) > read_size:
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        for line_number, record_text, payload in _bounded_file_records(path, text):
            normalized_text = _normalized_text(record_text)
            if normalized_query not in normalized_text and not any(
                term and term in normalized_text for term in terms
            ):
                continue
            chunk = _bounded_scan_chunk(
                paths,
                path,
                line_number=line_number,
                text=record_text,
                payload=payload,
                file_stat=file_lstat,
            )
            if not _chunk_matches_filters(chunk, filters):
                continue
            chunks.append(chunk)
            if len(chunks) >= match_limit:
                truncated = True
                break
        if len(chunks) >= match_limit:
            break

    return chunks, [
        {
            "source": "bounded-runtime-file-scan",
            "status": "completed",
            "scannedFiles": scanned_files,
            "scannedBytes": scanned_bytes,
            "matchedDocuments": len(chunks),
            "maxFiles": file_limit,
            "maxBytes": byte_limit,
            "truncated": truncated,
            "scope": "runtime-owned-markdown-json-jsonl",
        }
    ]


def sync_local_memory_index(
    paths: RuntimePaths | None = None,
    *,
    chunks: Iterable[dict[str, Any]] | None = None,
    sources: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Incrementally synchronize the lexical sidecar by logical source."""
    selected = paths or load_paths()
    settings = resolve_memory_search_settings(selected)
    if settings.get("enabled") is not True or (settings.get("local") or {}).get("enabled") is not True:
        return {
            "schemaVersion": LOCAL_MEMORY_SCHEMA_VERSION,
            "status": "disabled",
            "available": False,
            "reason": "local-memory-search-disabled",
        }

    diagnostics: list[dict[str, Any]] = []
    if chunks is None or sources is None:
        collected_chunks, collected_sources, collected_diagnostics = collect_local_memory_corpus(selected)
        chunk_list = collected_chunks
        source_list = collected_sources
        diagnostics.extend(collected_diagnostics)
    else:
        chunk_list = [dict(item) for item in chunks if isinstance(item, dict)]
        source_list = [dict(item) for item in sources if isinstance(item, dict)]

    index_path = local_memory_index_path(selected)
    _prepare_private_index_location(index_path)
    started = time.monotonic()
    connection = _open_index(index_path)
    try:
        capabilities = _ensure_schema(connection)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for chunk in chunk_list:
            source_id = str(chunk.get("sourceId") or "").strip()
            if not source_id:
                source_id = _fallback_source_id(chunk)
                chunk["sourceId"] = source_id
            grouped[source_id].append(_normalized_chunk(chunk))

        current_sources = _source_manifest(source_list, grouped)
        existing = {
            str(row["source_id"]): str(row["fingerprint"] or "")
            for row in connection.execute("SELECT source_id, fingerprint FROM memory_sources")
        }
        changed = [
            source_id
            for source_id, record in current_sources.items()
            if existing.get(source_id) != str(record.get("fingerprint") or "")
        ]
        deleted = sorted(set(existing) - set(current_sources))

        connection.execute("BEGIN IMMEDIATE")
        try:
            for source_id in deleted:
                _delete_source(connection, source_id, capabilities)
            for source_id in changed:
                _delete_source(connection, source_id, capabilities)
                record = current_sources[source_id]
                connection.execute(
                    """
                    INSERT INTO memory_sources(
                        source_id, source_set, source_type, path, fingerprint,
                        updated_at, indexed_at, chunk_count, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        source_set=excluded.source_set,
                        source_type=excluded.source_type,
                        path=excluded.path,
                        fingerprint=excluded.fingerprint,
                        updated_at=excluded.updated_at,
                        indexed_at=excluded.indexed_at,
                        chunk_count=excluded.chunk_count,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        source_id,
                        record.get("sourceSet"),
                        record.get("sourceType"),
                        record.get("path"),
                        record.get("fingerprint"),
                        record.get("updatedAt"),
                        _now_iso(),
                        len(grouped.get(source_id, [])),
                        _json(record),
                    ),
                )
                for chunk in grouped.get(source_id, []):
                    _insert_chunk(connection, chunk, capabilities)
            _set_metadata(connection, "schemaVersion", str(LOCAL_MEMORY_SCHEMA_VERSION))
            _set_metadata(connection, "lastSyncAt", _now_iso())
            _set_metadata(connection, "capabilities", _json(capabilities))
            _set_metadata(connection, "diagnostics", _json(diagnostics))
            _set_metadata(
                connection,
                "nativeMemoryPolicyDigest",
                native_memory_policy_digest(selected),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        status = _status_from_connection(connection, index_path, capabilities=capabilities)
        return {
            **status,
            "status": "ready",
            "available": True,
            "changedSources": len(changed),
            "deletedSources": len(deleted),
            "unchangedSources": max(0, len(current_sources) - len(changed)),
            "collectedChunks": len(chunk_list),
            "diagnostics": diagnostics,
            "durationMs": round((time.monotonic() - started) * 1000, 3),
        }
    finally:
        connection.close()
        _ensure_private_file(index_path)


def ensure_local_memory_index(
    paths: RuntimePaths | None = None,
    *,
    max_age_seconds: int = DEFAULT_INDEX_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    selected = paths or load_paths()
    status = local_memory_status(selected)
    native_policy_changed = (
        status.get("ready") is True
        and status.get("nativeMemoryPolicyDigest")
        != native_memory_policy_digest(selected)
    )
    if (
        status.get("ready") is True
        and not native_policy_changed
        and status.get("ageSeconds") is not None
        and float(status["ageSeconds"]) <= max(0, max_age_seconds)
    ):
        return status
    try:
        if status.get("status") in {"corrupt", "schema-mismatch"}:
            return rebuild_local_memory_index(selected)
        return sync_local_memory_index(selected)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        stale = local_memory_status(selected)
        return {
            **stale,
            "status": (
                "stale"
                if stale.get("ready")
                else "unsafe"
                if stale.get("status") == "unsafe"
                else "unavailable"
            ),
            "available": bool(stale.get("ready")),
            "reason": f"local-memory-sync-failed:{exc.__class__.__name__}",
            "error": str(exc),
        }


def rebuild_local_memory_index(
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    """Rebuild the disposable sidecar, restoring the prior index on failure."""
    selected = paths or load_paths()
    index_path = local_memory_index_path(selected)
    _prepare_private_index_location(index_path)
    backup_path = index_path.with_name(f"{index_path.name}.rebuild-backup")
    sqlite_sidecars = tuple(
        index_path.with_name(index_path.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    )
    _assert_safe_managed_file(backup_path)
    for sidecar in sqlite_sidecars:
        _assert_safe_managed_file(sidecar)
    try:
        _unlink_managed_file(backup_path)
        if _assert_safe_managed_file(index_path):
            index_path.replace(backup_path)
        for sidecar in sqlite_sidecars:
            _unlink_managed_file(sidecar)
        result = sync_local_memory_index(selected)
        if result.get("ready") is not True:
            raise RuntimeError(str(result.get("reason") or "local-memory-rebuild-failed"))
    except Exception:
        try:
            _unlink_managed_file(index_path)
            if _assert_safe_managed_file(backup_path):
                backup_path.replace(index_path)
        finally:
            _ensure_private_file(index_path)
        raise
    _unlink_managed_file(backup_path)
    return {
        **result,
        "rebuilt": True,
    }


def local_memory_status(paths: RuntimePaths | None = None) -> dict[str, Any]:
    selected = paths or load_paths()
    index_path = local_memory_index_path(selected)
    try:
        index_exists = _assert_safe_index_location(index_path)
    except (OSError, ValueError) as exc:
        return {
            "schemaVersion": LOCAL_MEMORY_SCHEMA_VERSION,
            "backend": {
                "kind": "local-fts",
                "semantic": False,
                "degraded": True,
                "indexPath": str(index_path),
            },
            "exists": False,
            "ready": False,
            "available": False,
            "documentCount": 0,
            "sourceCount": 0,
            "indexedAt": None,
            "ageSeconds": None,
            "capabilities": {
                "lexical": True,
                "semantic": False,
                "metadataFilters": True,
                "citations": True,
                "fts5": False,
                "unicode61": False,
                "trigram": False,
                "exactScan": True,
            },
            "status": "unsafe",
            "reason": "local-memory-index-unsafe",
            "error": str(exc),
        }
    base = {
        "schemaVersion": LOCAL_MEMORY_SCHEMA_VERSION,
        "backend": {
            "kind": "local-fts",
            "semantic": False,
            "degraded": True,
            "indexPath": str(index_path),
        },
        "exists": index_exists,
        "ready": False,
        "available": False,
        "documentCount": 0,
        "sourceCount": 0,
        "indexedAt": None,
        "ageSeconds": None,
        "capabilities": {
            "lexical": True,
            "semantic": False,
            "metadataFilters": True,
            "citations": True,
            "fts5": False,
            "unicode61": False,
            "trigram": False,
            "exactScan": True,
        },
    }
    if not index_exists:
        return {**base, "status": "missing"}
    try:
        _assert_safe_index_location(index_path)
        connection = sqlite3.connect(
            index_path.absolute().as_uri() + "?mode=ro",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
            if not row or int(row[0]) != LOCAL_MEMORY_SCHEMA_VERSION:
                return {**base, "status": "schema-mismatch"}
            capabilities = _metadata_json(connection, "capabilities") or base["capabilities"]
            status = _status_from_connection(connection, index_path, capabilities=capabilities)
            return {**status, "status": "ready", "ready": True, "available": True}
        finally:
            connection.close()
    except (OSError, ValueError) as exc:
        return {
            **base,
            "status": "unsafe",
            "ready": False,
            "available": False,
            "reason": "local-memory-index-unsafe",
            "error": str(exc),
        }
    except sqlite3.Error as exc:
        return {
            **base,
            "status": "corrupt",
            "reason": f"local-memory-index-unreadable:{exc.__class__.__name__}",
            "error": str(exc),
        }


def search_local_memory(
    query: str,
    *,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
    caller: str | None = None,
    paths: RuntimePaths | None = None,
    ensure_fresh: bool = True,
) -> dict[str, Any]:
    selected = paths or load_paths()
    normalized_query = _normalize_query(query)
    if not normalized_query:
        raise ValueError("query is required")
    bounded_top_k = max(1, min(int(top_k or 5), 20))
    settings = resolve_memory_search_settings(selected)
    if settings.get("enabled") is not True or (settings.get("local") or {}).get("enabled") is not True:
        return _unavailable_response(
            normalized_query,
            bounded_top_k,
            reason="local-memory-search-disabled",
        )

    freshness = ensure_local_memory_index(selected) if ensure_fresh else local_memory_status(selected)
    native_policy = native_memory_policy_profile(selected)
    current_native_policy_digest = native_memory_policy_digest(
        profile=native_policy
    )
    native_policy_stale = (
        str(freshness.get("nativeMemoryPolicyDigest") or "")
        != current_native_policy_digest
    )
    if native_policy_stale:
        # A stale sidecar can still contain rows authorized by the prior
        # native-memory root or consent state. Exclude all native rows until a
        # successful sync records the current complete policy identity.
        native_policy = {
            **native_policy,
            "enabled": False,
        }
    index_path = local_memory_index_path(selected)
    if not freshness.get("ready") and not freshness.get("available"):
        return _bounded_corpus_fallback(
            normalized_query,
            top_k=bounded_top_k,
            filters=filters or {},
            caller=caller,
            paths=selected,
            sync_failure=freshness,
        )

    started = time.monotonic()
    try:
        _assert_safe_index_location(index_path)
        connection = sqlite3.connect(
            index_path.absolute().as_uri() + "?mode=ro",
            uri=True,
            timeout=3.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            capabilities = _metadata_json(connection, "capabilities") or {}
            candidates = _search_connection(
                connection,
                normalized_query,
                filters=filters or {},
                capabilities=capabilities,
                native_policy=native_policy,
            )
        finally:
            connection.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _bounded_corpus_fallback(
            normalized_query,
            top_k=bounded_top_k,
            filters=filters or {},
            caller=caller,
            paths=selected,
            sync_failure={
                **freshness,
                "reason": f"local-memory-query-failed:{exc.__class__.__name__}",
                "error": str(exc),
            },
        )

    ranked = _rank_candidates(candidates, normalized_query, caller=caller)
    selected_rows = _dedupe_ranked(ranked, limit=bounded_top_k)
    results = _result_rows(selected_rows)
    return _response(
        normalized_query,
        bounded_top_k,
        results,
        backend={
            "kind": "local-fts",
            "semantic": False,
            "degraded": True,
            "fallbackFrom": None,
            "indexedAt": freshness.get("indexedAt"),
            "stale": freshness.get("status") == "stale",
            "nativePolicyStale": native_policy_stale,
        },
        capabilities={
            "lexical": True,
            "semantic": False,
            "metadataFilters": True,
            "citations": True,
            "fts5": bool((freshness.get("capabilities") or {}).get("fts5")),
            "unicode61": bool((freshness.get("capabilities") or {}).get("unicode61")),
            "trigram": bool((freshness.get("capabilities") or {}).get("trigram")),
            "exactScan": True,
        },
        filters=filters or {},
        duration_ms=(time.monotonic() - started) * 1000,
        diagnostics=freshness.get("diagnostics") if isinstance(freshness.get("diagnostics"), list) else [],
    )


def _search_connection(
    connection: sqlite3.Connection,
    query: str,
    *,
    filters: dict[str, Any],
    capabilities: dict[str, Any],
    native_policy: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    clauses, parameters = _filter_sql(filters, table_alias="d")
    native_clauses, native_parameters = _native_policy_sql(
        native_policy,
        table_alias="d",
    )
    clauses.extend(native_clauses)
    parameters.extend(native_parameters)
    where_suffix = f" AND {' AND '.join(clauses)}" if clauses else ""
    normalized = _normalized_text(query)

    exact_sql = (
        "SELECT d.*, 0.0 AS lane_score FROM memory_documents d "
        "WHERE instr(d.text_normalized, ?) > 0"
        + where_suffix
        + " ORDER BY CASE WHEN d.text_normalized = ? THEN 0 ELSE 1 END, d.business_date DESC "
        "LIMIT ?"
    )
    exact_params: list[Any] = [normalized, *parameters, normalized, MAX_CANDIDATES_PER_LANE]
    for rank, row in enumerate(connection.execute(exact_sql, exact_params), 1):
        _candidate(candidates, row, lane="exact", rank=rank)

    if capabilities.get("unicode61"):
        fts_query = _unicode_match_query(query)
        if fts_query:
            sql = (
                "SELECT d.*, bm25(memory_fts_unicode, 0.0, 5.0, 1.0, 2.0) AS lane_score "
                "FROM memory_fts_unicode JOIN memory_documents d "
                "ON d.rowid = memory_fts_unicode.rowid "
                "WHERE memory_fts_unicode MATCH ?"
                + where_suffix
                + " ORDER BY lane_score LIMIT ?"
            )
            try:
                rows = connection.execute(sql, [fts_query, *parameters, MAX_CANDIDATES_PER_LANE])
                for rank, row in enumerate(rows, 1):
                    _candidate(candidates, row, lane="unicode", rank=rank)
            except sqlite3.OperationalError:
                pass

    if capabilities.get("trigram"):
        trigram_query = _trigram_match_query(query)
        if trigram_query:
            sql = (
                "SELECT d.*, bm25(memory_fts_trigram, 0.0, 5.0, 1.0, 2.0) AS lane_score "
                "FROM memory_fts_trigram JOIN memory_documents d "
                "ON d.rowid = memory_fts_trigram.rowid "
                "WHERE memory_fts_trigram MATCH ?"
                + where_suffix
                + " ORDER BY lane_score LIMIT ?"
            )
            try:
                rows = connection.execute(sql, [trigram_query, *parameters, MAX_CANDIDATES_PER_LANE])
                for rank, row in enumerate(rows, 1):
                    _candidate(candidates, row, lane="trigram", rank=rank)
            except sqlite3.OperationalError:
                pass
    return candidates


def _candidate(
    candidates: dict[str, dict[str, Any]],
    row: sqlite3.Row,
    *,
    lane: str,
    rank: int,
) -> None:
    document_id = str(row["document_id"])
    candidate = candidates.setdefault(
        document_id,
        {
            "row": dict(row),
            "lanes": {},
        },
    )
    candidate["lanes"][lane] = {
        "rank": rank,
        "score": row["lane_score"],
    }


def _rank_candidates(
    candidates: dict[str, dict[str, Any]],
    query: str,
    *,
    caller: str | None,
) -> list[dict[str, Any]]:
    query_normalized = _normalized_text(query)
    query_terms = _query_terms(query)
    ranked: list[dict[str, Any]] = []
    for candidate in candidates.values():
        row = candidate["row"]
        lanes = candidate["lanes"]
        score = 0.0
        lane_weights = {"exact": 2.4, "unicode": 1.0, "trigram": 1.2}
        for lane, detail in lanes.items():
            score += lane_weights.get(lane, 1.0) / (RRF_K + float(detail["rank"]))
        text_normalized = str(row.get("text_normalized") or "")
        exact_phrase = query_normalized in text_normalized
        covered = [term for term in query_terms if _normalized_text(term) in text_normalized]
        coverage = len(covered) / max(1, len(query_terms))
        if exact_phrase:
            score += 0.12
        score += coverage * 0.045
        authority = max(0.0, min(float(row.get("authority_rank") or 50.0), 100.0))
        score += authority / 100.0 * 0.025
        lifecycle = str(row.get("lifecycle") or "")
        if lifecycle in {"current-state", "canonical"}:
            score *= 1.06
        elif lifecycle == "episodic":
            score *= 0.97
        provenance = _json_object(row.get("provenance_json"))
        if provenance.get("derivedFromActanara") is True:
            score *= 0.55
        if row.get("source_set") in {
            "agent-native-memory",
            "agent-native-instructions",
        } and _same_tool(row.get("agent"), caller):
            score *= 0.68
        ranked.append(
            {
                **candidate,
                "rawScore": score,
                "coveredTerms": covered,
                "coverage": coverage,
                "exactPhrase": exact_phrase,
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["rawScore"]),
            str(item["row"].get("business_date") or ""),
            str(item["row"].get("document_id") or ""),
        )
    )
    maximum = max((float(item["rawScore"]) for item in ranked), default=1.0)
    for item in ranked:
        item["score"] = round(float(item["rawScore"]) / maximum, 6) if maximum > 0 else 0.0
    return ranked


def _dedupe_ranked(ranked: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_lineage: set[str] = set()
    seen_text: set[str] = set()
    for candidate in ranked:
        row = candidate["row"]
        lineage = str(row.get("lineage_family") or row.get("dedupe_key") or "")
        text_hash = str(row.get("text_hash") or "")
        if lineage and lineage in seen_lineage:
            continue
        if text_hash and text_hash in seen_text:
            continue
        selected.append(candidate)
        if lineage:
            seen_lineage.add(lineage)
        if text_hash:
            seen_text.add(text_hash)
        if len(selected) >= limit:
            break
    return selected


def _result_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(rows, 1):
        row = candidate["row"]
        payload = _json_object(row.get("payload_json"))
        governance = _json_object(row.get("governance_json"))
        provenance = _json_object(row.get("provenance_json"))
        citation_id = f"local-{index}-{str(row.get('document_id') or '')[:12]}"
        result = {
            **payload,
            "id": row.get("document_id"),
            "resultId": row.get("document_id"),
            "text": row.get("text"),
            "textPreview": str(row.get("text") or "")[:500],
            "date": row.get("business_date"),
            "agent": row.get("agent"),
            "project": row.get("project"),
            "role": row.get("role"),
            "sourceSet": row.get("source_set"),
            "sourceId": row.get("source_id"),
            "sourcePath": row.get("source_path"),
            "sourceType": row.get("source_type"),
            "lineNumber": row.get("line_number"),
            "textHash": row.get("text_hash"),
            "dedupeKey": row.get("dedupe_key"),
            "lineageFamily": row.get("lineage_family"),
            "scopeType": row.get("scope_type"),
            "scopeKey": row.get("scope_key"),
            "governance": governance,
            "provenance": provenance,
            "score": candidate["score"],
            "scoreComponents": {
                "retrievalMode": "lexical",
                "lanes": candidate["lanes"],
                "exactPhrase": candidate["exactPhrase"],
                "termCoverage": round(candidate["coverage"], 4),
                "authorityRank": governance.get("authorityRank", row.get("authority_rank")),
                "semanticSimilarity": None,
            },
            "citationId": citation_id,
        }
        results.append(result)
    return results


def _response(
    query: str,
    top_k: int,
    results: list[dict[str, Any]],
    *,
    backend: dict[str, Any],
    capabilities: dict[str, Any],
    filters: dict[str, Any],
    duration_ms: float,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    terms = _query_terms(query)
    joined = _normalized_text(" ".join(str(item.get("text") or "") for item in results))
    covered = [term for term in terms if _normalized_text(term) in joined]
    missing = [term for term in terms if term not in covered]
    coverage = len(covered) / max(1, len(terms))
    status = "strong" if results and coverage >= 0.75 else "weak" if results else "insufficient"
    citations = [
        {
            "citationId": item["citationId"],
            "resultId": item["resultId"],
            "excerpt": item.get("textPreview"),
            "sourceSet": item.get("sourceSet"),
            "sourcePath": item.get("sourcePath"),
            "lineNumber": item.get("lineNumber"),
            "score": item.get("score"),
            "provenance": item.get("provenance"),
        }
        for item in results
    ]
    return {
        "schemaVersion": 2,
        "available": True,
        "reason": None,
        "query": query,
        "topK": top_k,
        "results": results,
        "backend": backend,
        "retrievalMode": "lexical",
        "capabilities": capabilities,
        "queryPlan": {
            "schemaVersion": 2,
            "query": query,
            "topK": top_k,
            "stages": ["exact", "unicode61", "trigram", "rrf", "governance", "dedupe"],
            "subQueries": [query],
            "explicitFilters": {key: value for key, value in filters.items() if value not in (None, "", [])},
            "status": "ready",
        },
        "citationPack": citations,
        "eventAggregation": {
            "schemaVersion": 2,
            "status": "not-computed" if results else "no-events",
            "eventCount": 0,
            "events": [],
            "timeline": [],
            "mostSevereEvent": None,
            "resolutionCitations": [],
            "reason": "lexical-backend-does-not-aggregate-events",
        },
        "answerSynthesis": {
            "status": "ready" if results else "no-results",
            "method": "extractive",
            "summary": str(results[0].get("textPreview") or "") if results else "",
            "citationIds": [item["citationId"] for item in results[:3]],
            "reason": "local-lexical-extractive-only",
        },
        "quality": {
            "schemaVersion": 1,
            "status": status,
            "needsMoreEvidence": status != "strong",
            "resultCount": len(results),
            "keyTerms": terms,
            "coveredTerms": covered,
            "missingTerms": missing,
            "coverage": round(coverage, 4),
            "flags": {
                "semanticRecallUnavailable": True,
                "exactTermDependent": True,
                "localLexicalFallback": True,
            },
            "recommendations": (
                []
                if status == "strong"
                else ["retry-with-rare-exact-terms", "install-or-enable-nova-rag-for-semantic-recall"]
            ),
        },
        "retrievalController": {
            "schemaVersion": 1,
            "mode": "single-pass-lexical",
            "serverSide": False,
            "executionPolicy": "exact-plus-local-fts",
            "passesRun": ["exact", "unicode61", "trigram", "rrf"],
            "passes": [],
            "qualityStatus": status,
            "needsMoreEvidence": status != "strong",
        },
        "agentic": {
            "schemaVersion": 2,
            "evidenceFieldsStable": True,
            "serverSidePlanning": False,
            "serverSideMultiPass": False,
            "serverSideQualityGate": False,
            "serverSideEventAggregation": False,
            "llmGenerated": False,
        },
        "diagnostics": diagnostics,
        "timing": {"durationMs": round(duration_ms, 3)},
    }


def _bounded_corpus_fallback(
    query: str,
    *,
    top_k: int,
    filters: dict[str, Any],
    caller: str | None,
    paths: RuntimePaths,
    sync_failure: dict[str, Any],
) -> dict[str, Any]:
    settings = resolve_memory_search_settings(paths)
    local = settings.get("local") if isinstance(settings.get("local"), dict) else {}
    max_documents = int(local.get("maxScanFiles") or 2000)
    max_bytes = int(local.get("maxScanBytes") or 64 * 1024 * 1024)
    started = time.monotonic()
    try:
        chunks, diagnostics = bounded_scan_local_memory_corpus(
            query,
            filters=filters,
            paths=paths,
            max_files=max_documents,
            max_bytes=max_bytes,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _unavailable_response(
            query,
            top_k,
            reason=f"local-memory-fallback-failed:{exc.__class__.__name__}",
            error=str(exc),
            fallback_from=sync_failure.get("reason"),
        )
    normalized = _normalized_text(query)
    terms = _query_terms(query)
    candidates: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        normalized_text = _normalized_text(text)
        covered = [term for term in terms if _normalized_text(term) in normalized_text]
        if normalized not in normalized_text and not covered:
            continue
        chunk_id = str(chunk.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest())
        governance = chunk.get("governance") if isinstance(chunk.get("governance"), dict) else {}
        candidates[chunk_id] = {
            "row": {
                "document_id": chunk_id,
                "text": text,
                "text_normalized": normalized_text,
                "business_date": chunk.get("date"),
                "agent": chunk.get("agent"),
                "project": chunk.get("project"),
                "role": chunk.get("role") or (chunk.get("provenance") or {}).get("role"),
                "source_set": chunk.get("sourceSet"),
                "source_id": chunk.get("sourceId"),
                "source_path": chunk.get("sourcePath"),
                "source_type": chunk.get("sourceType"),
                "line_number": chunk.get("lineNumber"),
                "text_hash": chunk.get("textHash"),
                "dedupe_key": chunk.get("dedupeKey"),
                "lineage_family": chunk.get("lineageFamily"),
                "scope_type": chunk.get("scopeType"),
                "scope_key": chunk.get("scopeKey"),
                "authority_rank": governance.get("authorityRank", 50),
                "lifecycle": governance.get("lifecycle", "unknown"),
                "payload_json": _json(chunk),
                "governance_json": _json(governance),
                "provenance_json": _json(chunk.get("provenance") or {}),
            },
            "lanes": {"bounded-scan": {"rank": len(candidates) + 1, "score": 0.0}},
        }
    ranked = _rank_candidates(candidates, query, caller=caller)
    results = _result_rows(_dedupe_ranked(ranked, limit=top_k))
    return _response(
        query,
        top_k,
        results,
        backend={
            "kind": "bounded-scan",
            "semantic": False,
            "degraded": True,
            "fallbackFrom": sync_failure.get("reason") or "local-fts-unavailable",
            "indexedAt": None,
            "stale": True,
        },
        capabilities={
            "lexical": True,
            "semantic": False,
            "metadataFilters": True,
            "citations": True,
            "fts5": False,
            "unicode61": False,
            "trigram": False,
            "exactScan": True,
        },
        filters=filters,
        duration_ms=(time.monotonic() - started) * 1000,
        diagnostics=diagnostics,
    )


def _unavailable_response(
    query: str,
    top_k: int,
    *,
    reason: str,
    error: str | None = None,
    fallback_from: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "available": False,
        "reason": reason,
        "error": error,
        "query": query,
        "topK": top_k,
        "results": [],
        "backend": {
            "kind": "unavailable",
            "semantic": False,
            "degraded": True,
            "fallbackFrom": fallback_from,
        },
        "retrievalMode": "unavailable",
        "capabilities": {
            "lexical": False,
            "semantic": False,
            "metadataFilters": False,
            "citations": False,
        },
        "citationPack": [],
    }


def _open_index(index_path: Path) -> sqlite3.Connection:
    _assert_safe_index_location(index_path)
    connection = sqlite3.connect(index_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, LOCAL_MEMORY_SCHEMA_VERSION}:
        _drop_schema(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_sources (
            source_id TEXT PRIMARY KEY,
            source_set TEXT NOT NULL,
            source_type TEXT,
            path TEXT,
            fingerprint TEXT,
            updated_at TEXT,
            indexed_at TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_documents (
            rowid INTEGER PRIMARY KEY,
            document_id TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL,
            source_set TEXT NOT NULL,
            source_type TEXT,
            title TEXT,
            text TEXT NOT NULL,
            text_normalized TEXT NOT NULL,
            business_date TEXT,
            agent TEXT,
            project TEXT,
            role TEXT,
            work_type TEXT,
            tags_search TEXT NOT NULL,
            lifecycle TEXT,
            authority_rank REAL NOT NULL,
            source_path TEXT,
            line_number INTEGER,
            text_hash TEXT,
            dedupe_key TEXT,
            lineage_family TEXT,
            scope_type TEXT,
            scope_key TEXT,
            provenance_json TEXT NOT NULL,
            governance_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(source_id) REFERENCES memory_sources(source_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS memory_documents_source_idx
            ON memory_documents(source_id);
        CREATE INDEX IF NOT EXISTS memory_documents_filter_idx
            ON memory_documents(source_set, business_date, project, role, agent, lifecycle, work_type);
        CREATE INDEX IF NOT EXISTS memory_documents_lineage_idx
            ON memory_documents(lineage_family, text_hash);
        """
    )
    capabilities = {
        "lexical": True,
        "semantic": False,
        "metadataFilters": True,
        "citations": True,
        "fts5": False,
        "unicode61": False,
        "trigram": False,
        "exactScan": True,
    }
    if not _table_exists(connection, "memory_fts_unicode"):
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE memory_fts_unicode USING fts5(
                    document_id UNINDEXED,
                    title,
                    text,
                    project,
                    tokenize="unicode61 remove_diacritics 2 tokenchars '-_'"
                )
                """
            )
        except sqlite3.OperationalError:
            pass
    capabilities["unicode61"] = _table_exists(connection, "memory_fts_unicode")
    if not _table_exists(connection, "memory_fts_trigram"):
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE memory_fts_trigram USING fts5(
                    document_id UNINDEXED,
                    title,
                    text,
                    project,
                    tokenize="trigram"
                )
                """
            )
        except sqlite3.OperationalError:
            pass
    capabilities["trigram"] = _table_exists(connection, "memory_fts_trigram")
    capabilities["fts5"] = bool(capabilities["unicode61"] or capabilities["trigram"])
    connection.execute(f"PRAGMA user_version={LOCAL_MEMORY_SCHEMA_VERSION}")
    connection.commit()
    return capabilities


def _drop_schema(connection: sqlite3.Connection) -> None:
    for table in (
        "memory_fts_unicode",
        "memory_fts_trigram",
        "memory_documents",
        "memory_sources",
        "memory_metadata",
    ):
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')
    connection.execute("PRAGMA user_version=0")
    connection.commit()


def _insert_chunk(
    connection: sqlite3.Connection,
    chunk: dict[str, Any],
    capabilities: dict[str, Any],
) -> None:
    governance = chunk.get("governance") if isinstance(chunk.get("governance"), dict) else {}
    provenance = chunk.get("provenance") if isinstance(chunk.get("provenance"), dict) else {}
    title = str(chunk.get("title") or _title_from_text(chunk.get("text")) or "")
    role = chunk.get("role") or provenance.get("role")
    cursor = connection.execute(
        """
        INSERT INTO memory_documents(
            document_id, source_id, source_set, source_type, title, text,
            text_normalized, business_date, agent, project, role, work_type,
            tags_search, lifecycle,
            authority_rank, source_path, line_number, text_hash, dedupe_key,
            lineage_family, scope_type, scope_key, provenance_json,
            governance_json, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.get("id"),
            chunk.get("sourceId"),
            chunk.get("sourceSet"),
            chunk.get("sourceType"),
            title,
            chunk.get("text"),
            _normalized_text(chunk.get("text")),
            chunk.get("date"),
            chunk.get("agent") or chunk.get("producerTool"),
            chunk.get("project"),
            role,
            _chunk_work_type(chunk),
            _tags_search_value(_chunk_tag_values(chunk)),
            governance.get("lifecycle") or chunk.get("lifecycle") or "unknown",
            float(governance.get("authorityRank") or chunk.get("authorityRank") or 50),
            chunk.get("sourcePath"),
            _optional_int(chunk.get("lineNumber")),
            chunk.get("textHash"),
            chunk.get("dedupeKey"),
            chunk.get("lineageFamily") or governance.get("duplicateGroupKey"),
            chunk.get("scopeType"),
            chunk.get("scopeKey"),
            _json(provenance),
            _json(governance),
            _json(chunk),
        ),
    )
    rowid = int(cursor.lastrowid)
    fts_values = (rowid, chunk.get("id"), title, chunk.get("text"), chunk.get("project"))
    if capabilities.get("unicode61"):
        connection.execute(
            "INSERT INTO memory_fts_unicode(rowid, document_id, title, text, project) VALUES (?, ?, ?, ?, ?)",
            fts_values,
        )
    if capabilities.get("trigram"):
        connection.execute(
            "INSERT INTO memory_fts_trigram(rowid, document_id, title, text, project) VALUES (?, ?, ?, ?, ?)",
            fts_values,
        )


def _delete_source(
    connection: sqlite3.Connection,
    source_id: str,
    capabilities: dict[str, Any],
) -> None:
    rowids = [
        int(row[0])
        for row in connection.execute(
            "SELECT rowid FROM memory_documents WHERE source_id = ?",
            (source_id,),
        )
    ]
    for rowid in rowids:
        if capabilities.get("unicode61"):
            connection.execute("DELETE FROM memory_fts_unicode WHERE rowid = ?", (rowid,))
        if capabilities.get("trigram"):
            connection.execute("DELETE FROM memory_fts_trigram WHERE rowid = ?", (rowid,))
    connection.execute("DELETE FROM memory_documents WHERE source_id = ?", (source_id,))
    connection.execute("DELETE FROM memory_sources WHERE source_id = ?", (source_id,))


def _source_manifest(
    sources: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_id = str(source.get("sourceId") or "").strip()
        if not source_id:
            continue
        result[source_id] = dict(source)
    for source_id, chunks in grouped.items():
        record = result.setdefault(
            source_id,
            {
                "sourceId": source_id,
                "sourceSet": chunks[0].get("sourceSet") or "unknown",
                "sourceType": chunks[0].get("sourceType") or "memory-chunks",
                "path": chunks[0].get("sourcePath"),
                "updatedAt": None,
                "chunkCount": len(chunks),
            },
        )
        # The shared collectors have already read and normalized each chunk, so
        # include their complete stable payload in the sidecar fingerprint.
        # This catches same-size/same-mtime edits and metadata-only changes.
        digest_input = "|".join(
            sorted(
                hashlib.sha256(_json(chunk).encode("utf-8")).hexdigest()
                for chunk in chunks
            )
        )
        record["fingerprint"] = hashlib.sha256(
            (
                f"{source_id}|{record.get('fingerprint') or ''}|"
                f"{digest_input}"
            ).encode("utf-8")
        ).hexdigest()
    return result


def _normalized_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(chunk)
    text = str(normalized.get("text") or "").strip()
    normalized["text"] = text
    normalized.setdefault("textPreview", text[:500])
    normalized.setdefault("textHash", hashlib.sha256(text.encode("utf-8")).hexdigest())
    normalized.setdefault(
        "dedupeKey",
        hashlib.sha256(
            f"{normalized.get('sourceSet')}|{normalized.get('sourceId')}|{normalized.get('id')}|{text[:160]}".encode(
                "utf-8"
            )
        ).hexdigest(),
    )
    normalized.setdefault("privacyClass", "local-private")
    normalized.setdefault("provenance", {})
    normalized.setdefault("governance", {})
    return normalized


def _fallback_source_id(chunk: dict[str, Any]) -> str:
    value = "|".join(
        (
            str(chunk.get("sourceSet") or "unknown"),
            str(chunk.get("sourcePath") or ""),
            str(chunk.get("sourceType") or ""),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_scan_paths(paths: RuntimePaths) -> Iterable[Path]:
    from .memory_corpus import canonical_lessons_path, lessons_read_paths

    seen: set[Path] = set()
    explicit = [
        *lessons_read_paths(
            canonical_path=canonical_lessons_path(paths),
            diary_source_root=paths.diary_dir,
            legacy_diary_root=paths.legacy_diary_root,
        ),
        paths.task_board_path,
    ]
    for candidate in explicit:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if resolved in seen or candidate.suffix.casefold() not in {
            ".md",
            ".markdown",
            ".txt",
            ".json",
            ".jsonl",
        }:
            continue
        seen.add(resolved)
        yield candidate

    roots = [
        paths.diary_dir,
        paths.reports_dir,
        paths.task_intelligence_dir,
        *([paths.legacy_diary_root] if paths.legacy_diary_root else []),
    ]
    for root in roots:
        try:
            boundary = root.resolve(strict=True)
            if not boundary.is_dir():
                continue
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not (directory_path / name).is_symlink()
            )
            for name in sorted(file_names):
                candidate = directory_path / name
                if candidate.suffix.casefold() not in {
                    ".md",
                    ".markdown",
                    ".txt",
                    ".json",
                    ".jsonl",
                }:
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(boundary)
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    continue
                if resolved in seen or candidate.is_symlink():
                    continue
                seen.add(resolved)
                yield candidate


def _bounded_file_records(
    path: Path,
    text: str,
) -> Iterable[tuple[int, str, dict[str, Any]]]:
    suffix = path.suffix.casefold()
    if suffix == ".jsonl":
        for line_number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            record_text = _bounded_payload_text(parsed)
            if record_text:
                yield line_number, record_text, parsed
        return
    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return
        records = parsed if isinstance(parsed, list) else [parsed]
        for line_number, item in enumerate(records, 1):
            if not isinstance(item, dict):
                continue
            record_text = _bounded_payload_text(item)
            if record_text:
                yield line_number, record_text, item
        return
    stripped = text.strip()
    if stripped:
        yield 1, stripped, {}


def _bounded_payload_text(payload: dict[str, Any]) -> str:
    for key in (
        "content",
        "text",
        "summary",
        "description",
        "lesson",
        "message",
        "title",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bounded_scan_chunk(
    paths: RuntimePaths,
    path: Path,
    *,
    line_number: int,
    text: str,
    payload: dict[str, Any],
    file_stat: os.stat_result,
) -> dict[str, Any]:
    source_set = _bounded_scan_source_set(paths, path)
    source_id = hashlib.sha256(
        f"{source_set}|{path.absolute()}".encode("utf-8")
    ).hexdigest()[:24]
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    document_id = hashlib.sha256(
        f"{source_id}|{line_number}|{text_hash}".encode("utf-8")
    ).hexdigest()
    date_value = str(
        payload.get("date")
        or payload.get("businessDate")
        or payload.get("business_date")
        or ""
    ).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", date_value):
        path_date = re.search(r"\d{4}-\d{2}-\d{2}", str(path))
        date_value = (
            path_date.group(0)
            if path_date
            else datetime.fromtimestamp(file_stat.st_mtime).astimezone().date().isoformat()
        )
    agent = payload.get("agent")
    if not agent and source_set == "filtered-dialogue-daily":
        agent = path.parent.name
    chunk = {
        "id": document_id,
        "text": text,
        "textPreview": text[:500],
        "textHash": text_hash,
        "title": str(payload.get("title") or _title_from_text(text)),
        "date": date_value,
        "agent": agent,
        "project": payload.get("project"),
        "role": payload.get("role"),
        "sourceSet": source_set,
        "sourceId": source_id,
        "sourcePath": str(path.absolute()),
        "sourceType": f"bounded-scan-{path.suffix.casefold().lstrip('.')}",
        "lineNumber": line_number,
        "dedupeKey": hashlib.sha256(
            f"{source_set}|{text_hash}".encode("utf-8")
        ).hexdigest(),
        "lineageFamily": f"bounded:{source_id}:{line_number}",
        "privacyClass": "local-private",
        "provenance": {
            "authority": "Emergency bounded scan of an Actanara Runtime-owned source file.",
            "boundedScan": True,
        },
        "tags": payload.get("tags") if isinstance(payload.get("tags"), list) else [],
        "workType": payload.get("workType") or payload.get("work_type"),
    }
    try:
        from agentic_rag.rag_memory_governance import governance_for_chunk

        chunk["governance"] = governance_for_chunk(chunk)
    except (ImportError, TypeError, ValueError):
        chunk["governance"] = {
            "authorityRank": 50,
            "lifecycle": "unknown",
            "canonicalEligible": False,
        }
    return chunk


def _bounded_scan_source_set(paths: RuntimePaths, path: Path) -> str:
    if path.name == "lessons.jsonl":
        return "lessons"
    if "_filtered" in path.parts:
        return "filtered-dialogue-daily"
    try:
        if path.resolve() == paths.task_board_path.resolve():
            return "task-board-snapshot"
        path.resolve().relative_to(paths.reports_dir.resolve())
        return "technical-report-task-events"
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        pass
    try:
        path.resolve().relative_to(paths.task_intelligence_dir.resolve())
        return "nova-task-work-graph-events"
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return "diary-markdown-sections"


def _filter_sql(filters: dict[str, Any], *, table_alias: str) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    prefix = f"{table_alias}."
    date_range = filters.get("dateRange") if isinstance(filters.get("dateRange"), dict) else {}
    date = str(filters.get("date") or "").strip()
    date_from = str(
        filters.get("dateFrom")
        or filters.get("date_from")
        or date_range.get("from")
        or ""
    ).strip()
    date_to = str(
        filters.get("dateTo")
        or filters.get("date_to")
        or date_range.get("to")
        or ""
    ).strip()
    project = str(filters.get("project") or "").strip()
    role = str(filters.get("role") or "").strip()
    source_sets = _filter_values(filters, "sourceSets", "source_sets")
    lifecycles = _filter_values(filters, "lifecycle", "lifecycles")
    work_types = _filter_values(filters, "workType", "workTypes", "work_type")
    agents = _filter_values(filters, "agent", "agents")
    tags = _filter_values(filters, "tags", "tag")
    if date:
        clauses.append(f"{prefix}business_date = ?")
        parameters.append(date)
    if date_from:
        clauses.append(f"{prefix}business_date >= ?")
        parameters.append(date_from)
    if date_to:
        clauses.append(f"{prefix}business_date <= ?")
        parameters.append(date_to)
    if project:
        clauses.append(f"{prefix}project = ? COLLATE NOCASE")
        parameters.append(project)
    if role:
        clauses.append(f"({prefix}role = ? COLLATE NOCASE OR {prefix}agent = ? COLLATE NOCASE)")
        parameters.extend((role, role))
    if source_sets:
        placeholders = ", ".join("?" for _ in source_sets)
        clauses.append(f"{prefix}source_set IN ({placeholders})")
        parameters.extend(source_sets)
    if lifecycles:
        placeholders = ", ".join("?" for _ in lifecycles)
        clauses.append(f"{prefix}lifecycle IN ({placeholders})")
        parameters.extend(lifecycles)
    if work_types:
        placeholders = ", ".join("?" for _ in work_types)
        clauses.append(f"{prefix}work_type IN ({placeholders})")
        parameters.extend(work_types)
    if agents:
        placeholders = ", ".join("?" for _ in agents)
        clauses.append(f"{prefix}agent IN ({placeholders})")
        parameters.extend(agents)
    if tags:
        tag_clauses = []
        for tag in tags:
            tag_clauses.append(f"instr({prefix}tags_search, ?) > 0")
            parameters.append(_tag_search_token(tag))
        clauses.append("(" + " OR ".join(tag_clauses) + ")")
    return clauses, parameters


def _native_policy_sql(
    native: dict[str, Any],
    *,
    table_alias: str,
) -> tuple[list[str], list[Any]]:
    """Apply current opt-in policy even when a stale sidecar still has rows."""
    prefix = f"{table_alias}."
    tools = native.get("tools") if isinstance(native.get("tools"), dict) else {}
    if native.get("enabled") is not True:
        return [
            f"{prefix}source_set NOT IN (?, ?)"
        ], ["agent-native-memory", "agent-native-instructions"]

    policy_paths = native.get("paths") if isinstance(native.get("paths"), dict) else {}
    allowed_branches: list[str] = []
    allowed_parameters: list[Any] = []
    if tools.get("codex") is True:
        codex_home = str(policy_paths.get("codexHome") or "").rstrip("/")
        if codex_home:
            allowed_branches.append(
                (
                    f"({prefix}agent = ? AND "
                    f"({prefix}source_path = ? OR "
                    f"instr({prefix}source_path, ? || '/') = 1))"
                )
            )
            allowed_parameters.extend(("codex", codex_home, codex_home))
    if tools.get("claudeCode") is True:
        projects_root = str(policy_paths.get("claudeProjectsRoot") or "").rstrip("/")
        if projects_root:
            claude_instructions = str(Path(projects_root).parent / "CLAUDE.md")
            allowed_branches.append(
                (
                    f"({prefix}agent = ? AND ("
                    f"({prefix}source_set = ? AND "
                    f"({prefix}source_path = ? OR "
                    f"instr({prefix}source_path, ? || '/') = 1)) "
                    f"OR ({prefix}source_set = ? AND {prefix}source_path = ?)))"
                )
            )
            allowed_parameters.extend(
                (
                    "claude-code",
                    "agent-native-memory",
                    projects_root,
                    projects_root,
                    "agent-native-instructions",
                    claude_instructions,
                )
            )
    if not allowed_branches:
        return [
            f"{prefix}source_set NOT IN (?, ?)"
        ], ["agent-native-memory", "agent-native-instructions"]

    clauses = [
        (
            f"({prefix}source_set NOT IN (?, ?) "
            f"OR ({' OR '.join(allowed_branches)}))"
        )
    ]
    parameters: list[Any] = [
        "agent-native-memory",
        "agent-native-instructions",
        *allowed_parameters,
    ]
    if native.get("includeInstructions") is not True:
        clauses.append(f"{prefix}source_set != ?")
        parameters.append("agent-native-instructions")
    return clauses, parameters


def _chunk_allowed_by_native_policy(
    chunk: dict[str, Any],
    native: dict[str, Any],
) -> bool:
    source_set = str(chunk.get("sourceSet") or "")
    if source_set not in {"agent-native-memory", "agent-native-instructions"}:
        return True
    if native.get("enabled") is not True:
        return False
    if source_set == "agent-native-instructions" and native.get("includeInstructions") is not True:
        return False
    tools = native.get("tools") if isinstance(native.get("tools"), dict) else {}
    producer = str(chunk.get("agent") or chunk.get("producerTool") or "").casefold()
    if producer == "codex":
        return tools.get("codex") is True
    if producer in {"claude-code", "claudecode", "claude code"}:
        return tools.get("claudeCode") is True
    return False


def _chunk_matches_filters(chunk: dict[str, Any], filters: dict[str, Any]) -> bool:
    date_range = filters.get("dateRange") if isinstance(filters.get("dateRange"), dict) else {}
    date = str(chunk.get("date") or "")
    if filters.get("date") and date != str(filters["date"]):
        return False
    date_from = str(
        filters.get("dateFrom")
        or filters.get("date_from")
        or date_range.get("from")
        or ""
    )
    date_to = str(
        filters.get("dateTo")
        or filters.get("date_to")
        or date_range.get("to")
        or ""
    )
    if date_from and date < date_from:
        return False
    if date_to and date > date_to:
        return False
    if filters.get("project") and str(chunk.get("project") or "").casefold() != str(filters["project"]).casefold():
        return False
    if filters.get("role"):
        role = str(chunk.get("role") or (chunk.get("provenance") or {}).get("role") or chunk.get("agent") or "")
        if role.casefold() != str(filters["role"]).casefold():
            return False
    source_sets = _filter_values(filters, "sourceSets", "source_sets")
    if source_sets and str(chunk.get("sourceSet") or "") not in set(source_sets):
        return False
    governance = chunk.get("governance") if isinstance(chunk.get("governance"), dict) else {}
    lifecycles = _filter_values(filters, "lifecycle", "lifecycles")
    if lifecycles and str(governance.get("lifecycle") or chunk.get("lifecycle") or "unknown") not in set(lifecycles):
        return False
    work_types = _filter_values(filters, "workType", "workTypes", "work_type")
    if work_types and _chunk_work_type(chunk) not in set(work_types):
        return False
    agents = _filter_values(filters, "agent", "agents")
    if agents and str(chunk.get("agent") or chunk.get("producerTool") or "") not in set(agents):
        return False
    tags = _filter_values(filters, "tags", "tag")
    if tags and set(_chunk_tag_values(chunk)).isdisjoint(tags):
        return False
    return True


def _filter_values(filters: dict[str, Any], *keys: str) -> list[str]:
    value: Any = None
    for key in keys:
        if key in filters and filters.get(key) not in (None, "", []):
            value = filters.get(key)
            break
    if value is None:
        return []
    candidates = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in candidates:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _chunk_tag_values(chunk: dict[str, Any]) -> list[str]:
    configured = chunk.get("tags")
    if isinstance(configured, (list, tuple, set)):
        values = [str(item).strip() for item in configured if str(item or "").strip()]
    else:
        try:
            from agentic_rag.rag_retriever import infer_tags

            values = [str(item).strip() for item in infer_tags(chunk) if str(item or "").strip()]
        except (ImportError, TypeError, ValueError):
            values = []
    return list(dict.fromkeys(values))


def _chunk_work_type(chunk: dict[str, Any]) -> str:
    configured = str(chunk.get("workType") or chunk.get("work_type") or "").strip()
    if configured:
        return configured
    tags = _chunk_tag_values(chunk)
    try:
        from agentic_rag.rag_retriever import infer_work_type

        return str(infer_work_type(chunk, set(tags)) or "other")
    except (ImportError, TypeError, ValueError):
        return "other"


def _tag_search_token(tag: str) -> str:
    return f"\x1f{_normalized_text(tag)}\x1f"


def _tags_search_value(tags: Iterable[str]) -> str:
    normalized = [_normalized_text(tag) for tag in tags if str(tag or "").strip()]
    return "\x1f" + "\x1f".join(dict.fromkeys(normalized)) + "\x1f"


def _unicode_match_query(query: str) -> str:
    terms = _query_terms(query)
    return " OR ".join(_fts_phrase(term) for term in terms[:12] if term)


def _trigram_match_query(query: str) -> str:
    candidates = [query.strip(), *_query_terms(query)]
    selected: list[str] = []
    for value in candidates:
        normalized = _normalized_text(value)
        if len(normalized) < 3 or normalized in selected:
            continue
        selected.append(normalized)
    return " OR ".join(_fts_phrase(value) for value in selected[:8])


def _fts_phrase(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[\w./:@+-]+", unicodedata.normalize("NFKC", query), flags=re.UNICODE)
    deduped: list[str] = []
    for term in terms:
        normalized = term.strip()
        if normalized and normalized.casefold() not in {item.casefold() for item in deduped}:
            deduped.append(normalized)
    return deduped or [query.strip()]


def _normalize_query(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()[:MAX_QUERY_CHARACTERS]


def _normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _title_from_text(value: Any) -> str:
    line = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
    return line.lstrip("# ").strip()[:200]


def _status_from_connection(
    connection: sqlite3.Connection,
    index_path: Path,
    *,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    indexed_at = _metadata_value(connection, "lastSyncAt")
    age_seconds = None
    if indexed_at:
        try:
            parsed = datetime.fromisoformat(indexed_at)
            age_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            age_seconds = None
    document_count = int(connection.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0])
    source_count = int(connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0])
    return {
        "schemaVersion": LOCAL_MEMORY_SCHEMA_VERSION,
        "ready": True,
        "available": True,
        "exists": True,
        "indexPath": str(index_path),
        "documentCount": document_count,
        "sourceCount": source_count,
        "indexedAt": indexed_at,
        "ageSeconds": round(age_seconds, 3) if age_seconds is not None else None,
        "capabilities": capabilities,
        "nativeMemoryPolicyDigest": _metadata_value(
            connection,
            "nativeMemoryPolicyDigest",
        ),
        "backend": {
            "kind": "local-fts",
            "semantic": False,
            "degraded": True,
            "indexPath": str(index_path),
            "indexedAt": indexed_at,
            "stale": age_seconds is None or age_seconds > DEFAULT_INDEX_MAX_AGE_SECONDS,
        },
        "diagnostics": _metadata_json(connection, "diagnostics") or [],
    }


def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO memory_metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def _metadata_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM memory_metadata WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def _metadata_json(connection: sqlite3.Connection, key: str) -> Any:
    raw = _metadata_value(connection, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _external_tool_home(paths: RuntimePaths, tool: str) -> Path:
    return _external_tool_path(paths, tool, "home")


def _external_tool_path(paths: RuntimePaths, tool: str, key: str) -> Path:
    from .settings import external_tool_path

    return external_tool_path(tool, key, paths)


def _same_tool(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    aliases = {
        "claudecode": "claudecode",
        "claude-code": "claudecode",
        "claude code": "claudecode",
        "codex": "codex",
    }
    left_key = aliases.get(str(left).strip().casefold(), str(left).strip().casefold().replace("-", ""))
    right_key = aliases.get(str(right).strip().casefold(), str(right).strip().casefold().replace("-", ""))
    return left_key == right_key


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _managed_index_paths(index_path: Path) -> tuple[Path, ...]:
    return (
        index_path,
        index_path.with_name(f"{index_path.name}.rebuild-backup"),
        index_path.with_name(index_path.name + "-wal"),
        index_path.with_name(index_path.name + "-shm"),
        index_path.with_name(index_path.name + "-journal"),
    )


def _assert_safe_directory(path: Path, *, label: str) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(path_stat.st_mode):
        raise _UnsafeLocalMemoryIndexError(f"{label} must not be a symbolic link: {path}")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise _UnsafeLocalMemoryIndexError(f"{label} must be a directory: {path}")
    return True


def _assert_safe_managed_file(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(path_stat.st_mode):
        raise _UnsafeLocalMemoryIndexError(
            f"managed memory sidecar path must not be a symbolic link: {path}"
        )
    if not stat.S_ISREG(path_stat.st_mode):
        raise _UnsafeLocalMemoryIndexError(
            f"managed memory sidecar path must be a regular file: {path}"
        )
    return True


def _assert_safe_index_location(index_path: Path) -> bool:
    state_dir = index_path.parent.parent
    if not _assert_safe_directory(state_dir, label="Runtime state directory"):
        return False
    if not _assert_safe_directory(index_path.parent, label="memory sidecar cache directory"):
        return False
    index_exists = False
    for managed_path in _managed_index_paths(index_path):
        exists = _assert_safe_managed_file(managed_path)
        if managed_path == index_path:
            index_exists = exists
    return index_exists


def _prepare_private_index_location(index_path: Path) -> None:
    state_dir = index_path.parent.parent
    if not _assert_safe_directory(state_dir, label="Runtime state directory"):
        state_dir.mkdir(parents=True, exist_ok=False)
        _assert_safe_directory(state_dir, label="Runtime state directory")
    if not _assert_safe_directory(index_path.parent, label="memory sidecar cache directory"):
        index_path.parent.mkdir(exist_ok=False)
        _assert_safe_directory(index_path.parent, label="memory sidecar cache directory")
    _assert_safe_index_location(index_path)
    try:
        os.chmod(index_path.parent, 0o700)
    except OSError:
        pass


def _unlink_managed_file(path: Path) -> None:
    if _assert_safe_managed_file(path):
        path.unlink()


def _ensure_private_file(path: Path) -> None:
    if not _assert_safe_managed_file(path):
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
