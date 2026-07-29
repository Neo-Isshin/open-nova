"""Read-only RAG subsystem status helpers."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .rag_active_source import resolve_active_rag_index
from .rag_server_lifecycle import read_server_process_state
from .rag_settings import RagSettings, effective_indexing_source_sets, rag_product_disabled_reason, resolve_rag_settings
from .rag_profile import manifest_embedding_profile, profiles_match, profile_with_hash, settings_embedding_profile
from data_foundation.network import RAG_SERVER_NON_LOOPBACK_ISSUE_CODE, host_for_url, is_loopback_host

try:
    from data_foundation.secret_store import read_secret
except ImportError:  # pragma: no cover - direct script fallback
    read_secret = None  # type: ignore


def read_rag_status(
    *,
    settings: RagSettings | None = None,
    count_legacy_entries: bool = True,
    inspect_legacy_sample: bool = False,
    include_legacy_metadata: bool = False,
    probe_server: bool = False,
    server_timeout_seconds: float = 0.5,
) -> dict[str, Any]:
    """Return read-only status without mutating RAG storage."""
    resolved = settings or resolve_rag_settings()
    legacy = _legacy_index_status(
        resolved.legacy_index_path,
        configured_dimension=resolved.embedding_dimension,
        count_entries=count_legacy_entries,
        inspect_sample=inspect_legacy_sample,
        include_metadata=include_legacy_metadata,
    )
    v2 = _v2_store_status(resolved.v2_store_path, configured_dimension=resolved.embedding_dimension)
    profile = _profile_status(resolved, v2)
    source_profile = _source_profile_status(resolved, v2)
    active_index = resolve_active_rag_index(resolved)
    network_boundary = _network_boundary_status(resolved)
    server = _server_status(resolved, probe=probe_server, timeout_seconds=server_timeout_seconds)
    _annotate_server_search_readiness(server, active_index.to_dict(), profile["configured"])
    lifecycle = read_server_process_state(
        resolved,
        probe_health=False,
        timeout_seconds=server_timeout_seconds,
    )
    active_source = _active_source(resolved.mode, legacy, v2, active_index.to_dict())
    disabled_reason = rag_product_disabled_reason(resolved)
    ready = resolved.enabled and active_source["ready"]
    query_embedding = _query_embedding_status(resolved)
    search_available = (
        ready
        and server["searchReady"]
        and query_embedding["configured"]
        and not profile["mismatch"]
        and not source_profile["mismatch"]
        and network_boundary["status"] == "ready"
    )
    return {
        "enabled": resolved.enabled,
        "mode": resolved.mode,
        "productEnabled": disabled_reason is None,
        "disabledReason": disabled_reason,
        "activeSource": active_source["source"],
        "activeIndex": active_index.to_dict(),
        "ready": ready,
        "searchAvailable": search_available,
        "settings": resolved.to_dict(),
        "profile": profile,
        "sourceProfile": source_profile,
        "provider": _provider_schema(resolved),
        "queryEmbedding": query_embedding,
        "legacy": legacy,
        "v2": v2,
        "serving": _serving_schema(resolved, server, lifecycle),
        "server": server,
        "networkBoundary": network_boundary,
        "lifecycle": lifecycle,
        "freshness": {
            "status": _freshness_status(
                resolved,
                active_source,
                server,
                profile,
                source_profile,
                query_embedding,
                network_boundary,
            ),
            "checkedAt": datetime.now().astimezone().isoformat(),
        },
    }


def _legacy_index_status(
    path: Path,
    *,
    configured_dimension: int,
    count_entries: bool,
    inspect_sample: bool,
    include_metadata: bool,
) -> dict[str, Any]:
    if not include_metadata:
        return {
            "source": "legacy",
            "indexPath": str(path),
            "exists": None,
            "ready": False,
            "entries": None,
            "sizeMB": None,
            "updatedAt": None,
            "embeddingDimension": None,
            "configuredDimension": configured_dimension,
            "dimensionMismatch": False,
            "metadataRead": False,
            "reason": "legacy-rag-retired",
        }
    exists = path.exists()
    stat = path.stat() if exists else None
    embedding_dimension = _first_embedding_dimension(path) if exists and inspect_sample else None
    dimension_mismatch = (
        embedding_dimension is not None and embedding_dimension != configured_dimension
    )
    return {
        "source": "legacy",
        "indexPath": str(path),
        "exists": exists,
        "ready": exists,
        "entries": _count_lines(path) if exists and count_entries else None,
        "sizeMB": round(stat.st_size / (1024 * 1024), 2) if stat else 0,
        "updatedAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat() if stat else None,
        "embeddingDimension": embedding_dimension,
        "configuredDimension": configured_dimension,
        "dimensionMismatch": dimension_mismatch,
        "metadataRead": True,
    }


def _v2_store_status(path: Path, *, configured_dimension: int) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    manifest = _read_json(manifest_path)
    status = str(manifest.get("status") or "missing")
    manifest_dimension = _optional_int(manifest.get("dimension"))
    active_index_path = _v2_index_path_from_manifest(manifest)
    candidate_index_path = _v2_candidate_index_path_from_manifest(manifest)
    active_ready = manifest_path.exists() and status == "active" and bool(active_index_path and active_index_path.exists())
    candidate_ready = manifest_path.exists() and status in {"ready", "candidate-ready", "active"} and bool(
        (candidate_index_path and candidate_index_path.exists()) or active_ready
    )
    return {
        "source": "v2",
        "storePath": str(path),
        "manifestPath": str(manifest_path),
        "exists": path.exists(),
        "manifestExists": manifest_path.exists(),
        "ready": active_ready,
        "activeReady": active_ready,
        "candidateReady": candidate_ready,
        "status": status,
        "activeIndexPath": str(active_index_path) if active_index_path else None,
        "activeIndexExists": bool(active_index_path and active_index_path.exists()),
        "candidateIndexPath": str(candidate_index_path) if candidate_index_path else None,
        "candidateIndexExists": bool(candidate_index_path and candidate_index_path.exists()),
        "model": manifest.get("model"),
        "embeddingProvider": manifest.get("embeddingProvider"),
        "embeddingProviderId": manifest.get("embeddingProviderId"),
        "embeddingProfile": manifest.get("embeddingProfile") if isinstance(manifest.get("embeddingProfile"), dict) else manifest_embedding_profile(manifest),
        "embeddingProfileHash": manifest.get("embeddingProfileHash"),
        "sourceProfile": manifest.get("sourceProfile") if isinstance(manifest.get("sourceProfile"), dict) else None,
        "sourceProfileHash": manifest.get("sourceProfileHash"),
        "dimension": manifest_dimension,
        "configuredDimension": configured_dimension,
        "dimensionMismatch": (
            manifest_dimension is not None and manifest_dimension != configured_dimension
        ),
        "chunkCount": manifest.get("chunkCount"),
        "documentCount": manifest.get("documentCount"),
        "updatedAt": manifest.get("updatedAt"),
        "lastBuildRunId": manifest.get("lastBuildRunId"),
        "latestBuildRun": _latest_build_run(path),
        "lastError": manifest.get("lastError"),
    }


def _profile_status(settings: RagSettings, v2: dict[str, Any]) -> dict[str, Any]:
    configured = profile_with_hash(settings_embedding_profile(settings))
    active_profile = v2.get("embeddingProfile") if isinstance(v2.get("embeddingProfile"), dict) else None
    active = profile_with_hash(active_profile) if active_profile and v2.get("ready") else None
    mismatch = bool(active and not profiles_match(configured, active))
    return {
        "configured": configured,
        "active": active,
        "locked": bool(active),
        "migrationRequired": mismatch,
        "mismatch": mismatch,
        "reason": "embedding-profile-mismatch" if mismatch else None,
    }


def _source_profile_status(settings: RagSettings, v2: dict[str, Any]) -> dict[str, Any]:
    configured = _configured_source_profile(settings)
    active = v2.get("sourceProfile") if isinstance(v2.get("sourceProfile"), dict) and v2.get("ready") else None
    mismatch = bool(active and not _source_profiles_match(configured, active))
    return {
        "configured": configured,
        "active": active,
        "locked": bool(active),
        "migrationRequired": mismatch,
        "mismatch": mismatch,
        "reason": "source-profile-mismatch" if mismatch else None,
    }


def _configured_source_profile(settings: RagSettings) -> dict[str, Any]:
    diary_root = Path(settings.diary_source_root).expanduser().absolute()
    source_sets = effective_indexing_source_sets(settings)
    result = {
        "schemaVersion": 1,
        "diarySourceRoot": str(diary_root),
        "filteredDialogueRoot": str(diary_root / "__diary_daily"),
        "lessonsPath": str(Path(settings.lessons_path).expanduser().absolute()),
        "taskBoardPath": str(Path(settings.task_board_path).expanduser().absolute()),
        "foundationDbPath": str(Path(settings.foundation_db_path).expanduser().absolute()),
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


def _source_profiles_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("diarySourceRoot", "filteredDialogueRoot", "lessonsPath", "taskBoardPath", "foundationDbPath")
    if any(str(left.get(key) or "") != str(right.get(key) or "") for key in keys):
        return False
    if sorted(str(item) for item in left.get("sourceSets", [])) != sorted(str(item) for item in right.get("sourceSets", [])):
        return False
    return (
        left.get("externalSources") == right.get("externalSources")
        and left.get("nativeMemory") == right.get("nativeMemory")
    )


def _server_status(settings: RagSettings, *, probe: bool, timeout_seconds: float) -> dict[str, Any]:
    url = f"http://{host_for_url(settings.server_host)}:{settings.server_port}{settings.server_health_path}"
    result: dict[str, Any] = {
        "enabled": settings.server_enabled,
        "url": url,
        "healthy": False,
        "probed": probe,
        "statusCode": None,
        "error": None,
        "payload": None,
        "indexLoaded": False,
        "indexPath": None,
        "indexMatchesActive": False,
        "embeddingProfile": None,
        "embeddingProfileHash": None,
        "profileMatchesSettings": False,
        "profileStale": False,
        "searchReady": False,
    }
    if not is_loopback_host(settings.server_host):
        result["error"] = RAG_SERVER_NON_LOOPBACK_ISSUE_CODE
        result["networkBoundary"] = "blocked"
        return result
    if not probe:
        result["healthy"] = False
        result["error"] = "not-probed"
        return result
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            result["statusCode"] = response.status
            result["healthy"] = 200 <= response.status < 300
            try:
                payload = json.loads(response.read().decode("utf-8"))
                result["payload"] = payload if isinstance(payload, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                result["payload"] = None
    except (OSError, urllib.error.URLError) as exc:
        result["error"] = exc.__class__.__name__
    return result


def _annotate_server_search_readiness(
    server: dict[str, Any],
    active_index: dict[str, Any],
    configured_profile: dict[str, Any],
) -> None:
    payload = server.get("payload") if isinstance(server.get("payload"), dict) else {}
    index_path = payload.get("indexPath")
    active_path = active_index.get("indexPath")
    index_loaded = bool(payload.get("indexLoaded"))
    index_matches = bool(index_path and active_path and str(index_path) == str(active_path))
    server_profile = payload.get("embeddingProfile") if isinstance(payload.get("embeddingProfile"), dict) else {
        "mode": payload.get("provider"),
        "providerId": payload.get("providerId") or payload.get("provider"),
        "model": payload.get("model"),
        "dimension": payload.get("dimension"),
    }
    profile_matches = profiles_match(configured_profile, server_profile)
    server["indexLoaded"] = index_loaded
    server["indexPath"] = index_path
    server["indexMatchesActive"] = index_matches
    server["embeddingProfile"] = server_profile
    server["embeddingProfileHash"] = payload.get("embeddingProfileHash")
    server["profileMatchesSettings"] = profile_matches
    server["profileStale"] = bool(server.get("healthy") and not profile_matches)
    server["searchReady"] = bool(server.get("healthy") and index_loaded and index_matches and profile_matches)


def _active_source(mode: str, legacy: dict[str, Any], v2: dict[str, Any], active_index: dict[str, Any]) -> dict[str, Any]:
    if mode == "disabled":
        return {"source": "disabled", "ready": False}
    if mode == "v2":
        return {"source": "v2", "ready": bool(v2["ready"] and active_index["ready"])}
    if mode in {"legacy", "v2-shadow"}:
        return {"source": "retired", "ready": False}
    return {"source": "retired", "ready": False}


def _freshness_status(
    settings: RagSettings,
    active_source: dict[str, Any],
    server: dict[str, Any],
    profile: dict[str, Any],
    source_profile: dict[str, Any],
    query_embedding: dict[str, Any],
    network_boundary: dict[str, Any],
) -> str:
    if not settings.enabled or settings.mode == "disabled":
        return "disabled"
    if profile.get("mismatch"):
        return "embedding-profile-mismatch"
    if source_profile.get("mismatch"):
        return "source-profile-mismatch"
    if network_boundary.get("status") == "blocked":
        return RAG_SERVER_NON_LOOPBACK_ISSUE_CODE
    if not active_source["ready"]:
        return "missing"
    if not query_embedding.get("configured"):
        return query_embedding.get("failureMode") or "query-embedding-not-configured"
    if settings.server_enabled and not server["healthy"]:
        return "server-unhealthy"
    if settings.server_enabled and server.get("profileStale"):
        return "server-profile-stale"
    if settings.server_enabled and not server.get("searchReady"):
        return "server-index-not-ready"
    return "ready"


def _provider_schema(settings: RagSettings) -> dict[str, Any]:
    """Return one stable local/cloud embedding provider contract."""
    cloud_api_key_configured = _cloud_api_key_configured(settings)
    secret_backend = str((settings.embedding_secret_ref or {}).get("backend") or "")
    requires_api_key_env = not settings.embedding_secret_ref or secret_backend == "process-env"
    active = {
        "mode": settings.embedding_provider,
        "providerId": settings.embedding_provider_id,
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "endpoint": settings.embedding_endpoint if settings.embedding_provider == "cloud" else "",
        "endpointConfigured": bool(settings.embedding_endpoint) if settings.embedding_provider == "cloud" else True,
        "requiresServer": settings.embedding_provider == "local",
        "requiresApiKeyEnv": settings.embedding_provider == "cloud" and requires_api_key_env,
        "apiKeyConfigured": cloud_api_key_configured if settings.embedding_provider == "cloud" else False,
    }
    return {
        "schemaVersion": 1,
        "mode": settings.embedding_provider,
        "providerId": settings.embedding_provider_id,
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "batchSize": settings.embedding_batch_size,
        "active": active,
        "local": {
            "enabled": settings.embedding_provider == "local",
            "providerId": "local",
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "endpoint": "",
            "endpointConfigured": True,
            "requiresServer": True,
            "requiresApiKeyEnv": False,
            "apiKeyConfigured": False,
            "device": settings.embedding_device,
            "serverEnabled": settings.server_enabled,
            "serverUrl": f"http://{host_for_url(settings.server_host)}:{settings.server_port}{settings.server_health_path}",
        },
        "cloud": {
            "enabled": settings.embedding_provider == "cloud",
            "providerId": settings.embedding_provider_id if settings.embedding_provider == "cloud" else "",
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "endpointConfigured": bool(settings.embedding_endpoint),
            "endpoint": settings.embedding_endpoint,
            "requiresServer": False,
            "requiresApiKeyEnv": requires_api_key_env,
            "apiKeyConfigured": cloud_api_key_configured,
            "apiKeyEnv": settings.embedding_api_key_env,
            "hasSecretRef": bool(settings.embedding_secret_ref),
            "secretRef": settings.embedding_secret_ref or {},
            "secretMigrationRequired": settings.embedding_secret_migration_required,
            "storesSecretValue": False,
        },
    }


def _query_embedding_status(settings: RagSettings) -> dict[str, Any]:
    if settings.embedding_provider == "cloud":
        endpoint_configured = bool(settings.embedding_endpoint)
        api_key_configured = _cloud_api_key_configured(settings)
        return {
            "provider": "cloud",
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "configured": endpoint_configured and api_key_configured,
            "endpointConfigured": endpoint_configured,
            "apiKeyConfigured": api_key_configured,
            "requiresLocalRuntime": False,
            "failureMode": None if endpoint_configured and api_key_configured else "cloud-embedding-not-configured",
        }
    return {
        "provider": "local",
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "configured": True,
        "endpointConfigured": True,
        "apiKeyConfigured": False,
        "requiresLocalRuntime": True,
        "failureMode": None,
    }


def _cloud_api_key_configured(settings: RagSettings) -> bool:
    if (
        settings.embedding_secret_ref
        and not settings.embedding_secret_migration_required
        and read_secret is not None
    ):
        try:
            if read_secret(
                settings.embedding_secret_ref,
                **(
                    {"runtime_home": settings.runtime_home}
                    if settings.embedding_secret_ref.get("backend") == "runtime-file"
                    else {}
                ),
            ):
                return True
        except Exception:
            pass
        if settings.embedding_secret_ref.get("backend") == "process-env":
            account_env = str(settings.embedding_secret_ref.get("account") or "").strip()
            if account_env and os.environ.get(account_env):
                return True
    return bool(os.environ.get(settings.embedding_api_key_env))


def _serving_schema(settings: RagSettings, server: dict[str, Any], lifecycle: dict[str, Any]) -> dict[str, Any]:
    product_enabled = rag_product_disabled_reason(settings) is None
    return {
        "schemaVersion": 1,
        "role": "rag-search-server",
        "productEnabled": product_enabled,
        "enabled": bool(settings.server_enabled),
        "requiresSearchServer": product_enabled,
        "queryEmbeddingProvider": settings.embedding_provider,
        "requiresLocalEmbeddingRuntime": settings.embedding_provider == "local",
        "localEmbeddingRuntimePolicy": "load-on-serving-process" if settings.embedding_provider == "local" else "not-required",
        "embeddingModel": settings.embedding_model,
        "embeddingDimension": settings.embedding_dimension,
        "url": server.get("url"),
        "healthy": bool(server.get("healthy")),
        "running": bool(lifecycle.get("running")),
        "status": lifecycle.get("status"),
        "networkBoundary": _network_boundary_status(settings),
        "internalAuthorization": lifecycle.get("internalAuthorization"),
    }


def _network_boundary_status(settings: RagSettings) -> dict[str, Any]:
    loopback = is_loopback_host(settings.server_host)
    return {
        "schemaVersion": 1,
        "policy": "loopback-only",
        "host": settings.server_host,
        "loopback": loopback,
        "status": "ready" if loopback else "blocked",
        "issueCode": None if loopback else RAG_SERVER_NON_LOOPBACK_ISSUE_CODE,
        "encodeExposure": "internal-token-and-loopback" if loopback else "blocked-non-loopback",
    }


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for _ in handle:
            count += 1
    return count


def _first_embedding_dimension(path: Path) -> int | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                embedding = payload.get("embedding")
                if isinstance(embedding, list):
                    return len(embedding)
                return None
    except OSError:
        return None
    return None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _v2_index_path_from_manifest(manifest: dict) -> Path | None:
    value = manifest.get("activeIndexPath")
    if value:
        path = Path(str(value)).expanduser()
        if path.suffix == ".jsonl":
            return path
        nested = path / "index.jsonl"
        if nested.exists():
            return nested
    return None


def _v2_candidate_index_path_from_manifest(manifest: dict) -> Path | None:
    for key in ("candidateIndexPath", "activeIndexPath"):
        value = manifest.get(key)
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if path.suffix == ".jsonl":
            return path
        nested = path / "index.jsonl"
        if nested.exists():
            return nested
    return None


def _latest_build_run(store_path: Path) -> dict | None:
    latest = None
    try:
        with (store_path / "build-runs.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    latest = value
    except OSError:
        return None
    return latest


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
