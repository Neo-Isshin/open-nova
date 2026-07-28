"""Dashboard service for Actanara runtime settings."""

from __future__ import annotations

import os
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))

from data_foundation.settings import (
    MASKED_SECRET,
    build_agent_schedule_prompt,
    llm_provider_chain_readiness_error,
    llm_provider_readiness_error,
    normalize_rag_settings_update,
    read_llm_provider,
    read_settings,
    resolve_llm_provider_chain,
    runtime_authority_contract,
    write_operator_settings,
    write_operator_settings_bundle,
    write_settings,
)
from data_foundation.llm_provider_catalog import (
    llm_provider_catalog,
    normalize_llm_provider_chain_update,
    normalize_llm_provider_update,
)
from data_foundation.llm_provider_test import check_llm_provider_availability
from data_foundation.secret_store import default_secret_backend, read_secret
from data_foundation.external_tool_catalog import add_external_tool_instance, rediscover_external_tools, supported_external_tool_catalog
from data_foundation.db import connect
from data_foundation.diary_reconcile import (
    plan_diary_projection_rebuild,
    rebuild_diary_projections,
    recent_diary_projection_rebuild_jobs,
)
from data_foundation.diary_paths import iter_diary_markdown_files
from data_foundation.sqlite_cache_rebuild import (
    SQLITE_CACHE_REBUILD_CONFIRMATION,
    plan_sqlite_cache_rebuild,
    rebuild_sqlite_cache,
)
from data_foundation.workspace_attribution import (
    add_workspace_attribution_rule,
    materialize_workspace_attribution_catalog,
    read_workspace_attribution_rules,
    validate_workspace_path,
    workspace_attribution_catalog_path,
    workspace_attribution_rules_path,
)
from data_foundation.paths import (
    LegacyImportResult,
    PathValidation,
    RuntimePaths,
    initialize_home,
    load_paths,
    select_home,
    validate_home,
)
from agentic_rag.rag_settings import is_rag_product_enabled, rag_product_disabled_reason, resolve_rag_settings
from agentic_rag.rag_external_sources import plan_external_sources
from agentic_rag.rag_status import read_rag_status
from agentic_rag.rag_server_lifecycle import read_server_process_state, start_rag_server, stop_rag_server
from agentic_rag.rag_v2_sync import sync_v2_production_index
from agentic_rag.rag_v2_coverage import read_v2_coverage
from agentic_rag.rag_v2_eval import run_rag_eval
from agentic_rag.rag_v2_promote import promote_v2_candidate, required_v2_promotion_confirmation
from agentic_rag.rag_v2_rollback import rollback_v2_manifest, required_v2_manifest_rollback_confirmation
from agentic_rag.rag_profile import profiles_match, settings_embedding_profile
from .external_rag_skill_registration import (
    list_rag_skill_registration_jobs,
    plan_rag_skill_registration,
    queue_rag_skill_registration,
)


_DIARY_RELATIVE_DATE_RE = re.compile(r"(?:^|/)diary-(\d{4}-\d{2}-\d{2})/")
RAG_SERVER_START_CONFIRMATION = "START ACTANARA RAG SERVER"
RAG_SERVER_STOP_CONFIRMATION = "STOP ACTANARA RAG SERVER"
RUNTIME_PATH_SELECT_CONFIRMATION = "SELECT ACTANARA RUNTIME PATH"
DIARY_PROJECTION_REBUILD_CONFIRMATION = "REBUILD ACTANARA DIARY PROJECTIONS"
RAG_SERVER_SEARCH_BUDGET_SECONDS = 60.0
RAG_FACADE_TRANSPORT_GRACE_SECONDS = 5.0


def _date_range(values: list[str]) -> dict | None:
    selected: list[str] = []
    for value in values:
        match = _DIARY_RELATIVE_DATE_RE.search(str(value))
        if match:
            selected.append(match.group(1))
    if not selected:
        return None
    selected = sorted(set(selected))
    return {"startDate": selected[0], "endDate": selected[-1]}


def get_settings() -> dict:
    paths = load_paths()
    settings = read_settings(paths)
    settings["llmProvider"] = read_llm_provider(paths)
    settings["agentSchedulePrompt"] = build_agent_schedule_prompt(settings)
    settings["authority"] = runtime_authority_contract(paths)
    settings["runtimePath"] = current_runtime_path()
    settings["ragStatus"] = get_rag_status(probe_server=False)
    return settings


def workspace_attribution_status() -> dict:
    paths = load_paths()
    catalog = materialize_workspace_attribution_catalog(paths)
    rules = read_workspace_attribution_rules(paths)
    try:
        from . import ai_assets

        assets = ai_assets.get_ai_assets_cached()
        qa = assets.get("workspaceAttributionQa") or {}
        workspace_usage = (assets.get("workspaceUsage") or [])[:20]
    except Exception:
        qa = {}
        workspace_usage = []
    return {
        "catalog": catalog,
        "rules": rules,
        "paths": {
            "catalog": str(workspace_attribution_catalog_path(paths)),
            "rules": str(workspace_attribution_rules_path(paths)),
        },
        "qa": qa,
        "workspaceUsage": workspace_usage,
    }


def workspace_attribution_rule_preview(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return add_workspace_attribution_rule(payload, load_paths(), dry_run=True)


def workspace_attribution_rule_add(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    result = add_workspace_attribution_rule(payload, load_paths(), dry_run=False)
    result["status"] = workspace_attribution_status()
    return result


def workspace_attribution_path_validate(path: str) -> dict:
    return validate_workspace_path(path)


def update_settings(payload: dict) -> dict:
    paths = load_paths()
    settings = write_operator_settings(payload if isinstance(payload, dict) else {}, paths)
    settings["llmProvider"] = read_llm_provider(paths)
    settings["agentSchedulePrompt"] = build_agent_schedule_prompt(settings)
    settings["authority"] = runtime_authority_contract(paths)
    settings["ragStatus"] = get_rag_status(probe_server=False)
    return settings


def update_settings_bundle(payload: dict) -> dict:
    paths = load_paths()
    update = payload if isinstance(payload, dict) else {}
    _validate_rag_profile_write(update)
    if "llmProvider" in update:
        _validate_llm_provider_update_pipeline_secret(paths, update.get("llmProvider"))
    verifier = None
    if "llmProvider" in update:
        verifier = lambda: _raise_if_llm_provider_not_pipeline_ready(paths)
    settings = write_operator_settings_bundle(
        update,
        paths,
        readiness_verifier=verifier if "llmProvider" in update else None,
    )
    settings["llmProvider"] = read_llm_provider(paths)
    if "llmProvider" in update:
        _raise_if_llm_provider_not_pipeline_ready(paths)
    settings["agentSchedulePrompt"] = build_agent_schedule_prompt(settings)
    settings["authority"] = runtime_authority_contract(paths)
    settings["runtimePath"] = current_runtime_path()
    settings["ragStatus"] = get_rag_status(probe_server=False)
    return settings


def diary_path_consistency() -> dict:
    paths = load_paths()
    diary_root = paths.diary_dir
    disk_files = iter_diary_markdown_files(diary_root) if diary_root.exists() else []
    disk_relative = {path.relative_to(diary_root).as_posix() for path in disk_files}
    db_rows: list[dict] = []
    if paths.db_path.exists():
        try:
            with connect(paths, read_only=True) as connection:
                db_rows = [
                    {
                        "documentKey": row["document_key"],
                        "businessDate": row["business_date"],
                        "reportType": row["report_type"],
                        "relativePath": row["relative_path"],
                        "status": row["status"],
                    }
                    for row in connection.execute(
                        """
                        SELECT document_key, business_date, report_type, relative_path, status
                        FROM diary_markdown_documents
                        WHERE status = 'ready'
                        ORDER BY business_date DESC, report_type
                        """
                    )
                ]
        except sqlite3.OperationalError as exc:
            return {
                "status": "unknown",
                "reason": str(exc),
                "diaryRoot": str(diary_root),
                "database": str(paths.db_path),
                "diskMarkdownFiles": len(disk_files),
                "readyRows": 0,
                "matchedRows": 0,
                "missingDiskFiles": [],
                "extraDiskFiles": sorted(disk_relative)[:50],
                "requiresProjectionRefresh": True,
            }
    db_relative = {row["relativePath"] for row in db_rows}
    missing = sorted(db_relative - disk_relative)
    extra = sorted(disk_relative - db_relative)
    matched = sorted(db_relative & disk_relative)
    mismatch = missing + extra
    return {
        "status": "ok" if not missing and not extra else "mismatch",
        "diaryRoot": str(diary_root),
        "database": str(paths.db_path),
        "diskMarkdownFiles": len(disk_files),
        "readyRows": len(db_rows),
        "matchedRows": len(matched),
        "diskDateRange": _date_range(sorted(disk_relative)),
        "databaseDateRange": _date_range(sorted(db_relative)),
        "mismatchDateRange": _date_range(sorted(mismatch)),
        "missingDiskFiles": missing[:50],
        "extraDiskFiles": extra[:50],
        "truncated": len(missing) > 50 or len(extra) > 50,
        "requiresProjectionRefresh": bool(missing or extra),
    }


def _parse_date_field(payload: dict, key: str) -> date:
    raw = payload.get(key)
    if not raw:
        raise ValueError(f"{key} is required")
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"{key} must be YYYY-MM-DD") from exc


def rebuild_diary_path_projection(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    start_date = _parse_date_field(payload, "startDate")
    end_date = _parse_date_field(payload, "endDate")
    if end_date < start_date:
        raise ValueError("endDate must be on or after startDate")
    paths = load_paths()
    if payload.get("dryRun", True) is False:
        if str(payload.get("confirmationText") or "") != DIARY_PROJECTION_REBUILD_CONFIRMATION:
            raise ValueError(f"confirmationText must be exactly: {DIARY_PROJECTION_REBUILD_CONFIRMATION}")
        return rebuild_diary_projections(
            paths,
            start_date,
            end_date,
            include_usage=payload.get("includeUsage", True) is not False,
        )
    plan = plan_diary_projection_rebuild(paths, start_date, end_date)
    plan["confirmationTextRequired"] = DIARY_PROJECTION_REBUILD_CONFIRMATION
    return plan


def diary_path_rebuild_jobs(limit: int = 20) -> dict:
    return {
        "jobs": recent_diary_projection_rebuild_jobs(load_paths(), limit=limit),
    }


def sqlite_cache_rebuild(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    start_date = date.fromisoformat(str(payload["startDate"])) if payload.get("startDate") else None
    end_date = date.fromisoformat(str(payload["endDate"])) if payload.get("endDate") else None
    paths = load_paths()
    if payload.get("dryRun", True) is False:
        return rebuild_sqlite_cache(
            paths,
            confirmation_text=str(payload.get("confirmationText") or ""),
            start_date=start_date,
            end_date=end_date,
        )
    plan = plan_sqlite_cache_rebuild(paths, start_date=start_date, end_date=end_date)
    plan["confirmationTextRequired"] = SQLITE_CACHE_REBUILD_CONFIRMATION
    return plan


def get_rag_settings() -> dict:
    settings = read_settings()
    return {
        "rag": settings.get("rag", {}),
        "status": get_rag_status(probe_server=False),
    }


def update_rag_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    update = payload.get("rag") if isinstance(payload.get("rag"), dict) else payload
    update = normalize_rag_settings_update(update)
    _validate_rag_profile_update(update)
    paths = load_paths()
    settings = write_operator_settings_bundle({"rag": update}, paths)
    return {
        "rag": settings.get("rag", {}),
        "status": get_rag_status(probe_server=False),
    }


def plan_rag_external_sources(payload: dict | None = None) -> dict:
    """Preview external-source discovery/parsing without writing settings or indexes."""

    request = payload if isinstance(payload, dict) else {}
    update = request.get("rag") if isinstance(request.get("rag"), dict) else request
    normalized = normalize_rag_settings_update(update)
    current_settings = read_settings()
    current_rag = current_settings.get("rag") if isinstance(current_settings.get("rag"), dict) else {}
    merged_rag = _deep_merge_dict(current_rag, normalized)
    candidate = resolve_rag_settings(settings={**current_settings, "rag": merged_rag})
    return plan_external_sources(candidate)


def get_rag_status(*, probe_server: bool = True) -> dict:
    return read_rag_status(
        settings=resolve_rag_settings(),
        count_legacy_entries=False,
        inspect_legacy_sample=False,
        include_legacy_metadata=False,
        probe_server=probe_server,
    )


def _validate_rag_profile_write(payload: dict) -> None:
    if "rag" not in payload:
        return
    update = payload.get("rag")
    if isinstance(update, dict) and isinstance(update.get("rag"), dict):
        update = update["rag"]
    _validate_rag_profile_update(update if isinstance(update, dict) else {})


def _validate_rag_profile_update(update: dict) -> None:
    if "languageProfile" in update:
        raise ValueError("rag.languageProfile is immutable after install; choose the language profile during installer/runtime bootstrap.")
    enabled = update.get("enabled")
    mode = str(update.get("mode") or "").strip()
    if enabled is False or mode == "disabled":
        return
    current = resolve_rag_settings()
    status = read_rag_status(settings=current, count_legacy_entries=False, probe_server=False)
    active_profile = ((status.get("profile") or {}).get("active") or {})
    if not active_profile:
        return
    current_settings = read_settings(load_paths())
    merged_rag = _deep_merge_dict(current_settings.get("rag") if isinstance(current_settings.get("rag"), dict) else {}, update)
    candidate = resolve_rag_settings(settings={**current_settings, "rag": merged_rag})
    if not profiles_match(settings_embedding_profile(candidate), active_profile):
        raise ValueError("RAG embedding profile is locked by the active index; use RAG profile migration instead.")


def _deep_merge_dict(base: dict, update: dict) -> dict:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def rag_operator_action(action: str, payload: dict | None = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    if action not in {"server-start", "server-stop", "index-run"}:
        raise ValueError("unknown RAG action")
    settings = resolve_rag_settings()
    if action in {"server-start", "index-run"}:
        if not is_rag_product_enabled(settings):
            return {
                "accepted": False,
                "status": "rag-disabled",
                "action": action,
                "reason": rag_product_disabled_reason(settings) or "nova-RAG subsystem is disabled by settings.",
                "ragStatus": get_rag_status(probe_server=False),
            }
    if action in {"server-start", "server-stop"}:
        required = RAG_SERVER_START_CONFIRMATION if action == "server-start" else RAG_SERVER_STOP_CONFIRMATION
        if payload.get("dryRun") is True:
            return {
                "accepted": True,
                "dryRun": True,
                "action": action,
                "confirmationTextRequired": required,
                "ragStatus": get_rag_status(probe_server=False),
            }
        if str(payload.get("confirmationText") or "") != required:
            raise ValueError(f"confirmationText must be exactly: {required}")
    if action == "index-run":
        result = sync_v2_production_index(settings, requested_by="dashboard", promote=False)
        return {
            **result,
            "action": action,
            "reason": "Built a v2 candidate index only; no promotion and no legacy index mutation.",
            "ragStatus": get_rag_status(probe_server=False),
        }
    if action == "server-start":
        result = _running_rag_server_result(settings)
        launch_agent = None
        if result is None:
            result = start_rag_server(settings, requested_by="dashboard")
    else:
        launch_agent = None
        result = stop_rag_server(settings, requested_by="dashboard")
    return {
        **result,
        "action": action,
        "launchAgent": launch_agent,
        "ragStatus": get_rag_status(probe_server=False),
    }


def _running_rag_server_result(settings) -> dict | None:
    existing = read_server_process_state(settings, probe_health=True, timeout_seconds=1.0)
    if existing.get("health") and existing["health"].get("healthy"):
        return {
            "accepted": True,
            "status": "already-running",
            "reason": "nova-RAG search server health endpoint is already healthy.",
            "lifecycle": existing,
        }
    if existing.get("running"):
        return {
            "accepted": True,
            "status": "running-unhealthy",
            "reason": "nova-RAG search server process is running but health is not ready yet.",
            "lifecycle": existing,
        }
    return None


def rag_stats() -> dict:
    status = get_rag_status(probe_server=True)
    if not status.get("server", {}).get("healthy"):
        return {
            "available": False,
            "reason": status.get("freshness", {}).get("status") or "server-unavailable",
            "ragStatus": status,
            "api": {
                "readOnly": True,
                "mutationAllowed": False,
                "endpoints": {
                    "health": "GET /health",
                    "stats": "GET /stats",
                    "search": "POST /search",
                },
            },
        }
    stats_url = str(status["server"]["url"]).replace(str(status["settings"]["server_health_path"]), "/stats")
    with urllib.request.urlopen(stats_url, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    return {
        "available": True,
        "ragStatus": status,
        **(result if isinstance(result, dict) else {"stats": result}),
    }


def rag_coverage() -> dict:
    return read_v2_coverage(resolve_rag_settings())


def rag_eval_latest() -> dict:
    return run_rag_eval(resolve_rag_settings(), search_fn=rag_search)


def rag_external_agent_contract() -> dict:
    status = get_rag_status(probe_server=False)
    return {
        "version": 2,
        "readOnly": True,
        "mutationAllowed": False,
        "purpose": "nova-RAG is a read-only long-term memory service backed by cleaned Actanara pipeline outputs.",
        "usagePrompt": (
            "Use evidence sources in this order: (1) the current conversation, user-provided material, "
            "and local authoritative files; (2) the host Agent Runtime's built-in or connected "
            "memory/history retrieval, when available; and (3) nova-RAG only when the preceding sources "
            "do not provide enough reliable information. If the user explicitly asks to query nova-RAG, "
            "that is an exception and it may be used directly. Treat nova-RAG results as evidence rather "
            "than authority; prefer high authorityRank, high provenanceScore, and lifecycle values "
            "current-state/canonical when relevant. Do not call mutation endpoints."
        ),
        "allowedEndpoints": [
            "GET /api/rag/external/health",
            "GET /api/rag/external/stats",
            "GET /api/rag/external/contract",
            "POST /api/rag/external/search",
        ],
        "provider": status.get("provider") or {},
        "searchRequest": {
            "requiredFields": ["query"],
            "optionalFields": [
                "topK",
                "date",
                "dateRange",
                "project",
                "role",
                "tags",
                "sourceSets",
                "lifecycle",
                "workType",
                "includeFullText",
                "includeGovernance",
            ],
        },
        "searchResponse": {
            "schemaVersion": 2,
            "includes": [
                "results",
                "queryPlan",
                "citationPack",
                "eventAggregation",
                "answerSynthesis",
                "quality",
                "retrievalController",
                "agentic",
                "externalAgentContract",
                "reranker",
                "governance",
                "provenance",
            ],
        },
        "rankingGuidance": [
            "Use sourceSet and provenance to cite where memory came from.",
            "Prefer lifecycle=current-state for current task/status questions.",
            "Prefer lifecycle=canonical for lessons, decisions and durable memory.",
            "Use filtered-dialogue-daily as episodic evidence rather than final state.",
        ],
        "rejectedMutationStatus": 403,
    }


def normalize_external_rag_search_response(result: dict | None, *, query: str = "", top_k: int = 5) -> dict:
    """Guarantee the external-agent evidence schema on every search response."""
    response = dict(result) if isinstance(result, dict) else {"results": []}
    results = response.get("results") if isinstance(response.get("results"), list) else []
    available = bool(response.get("available", True))
    reason = str(response.get("reason") or "")
    response.setdefault("schemaVersion", 2)
    response.setdefault("available", available)
    response.setdefault("results", results)
    response.setdefault(
        "queryPlan",
        {
            "schemaVersion": 2,
            "query": str(response.get("query") or query or ""),
            "topK": int(response.get("topK") or top_k or 5),
            "stages": [],
            "subQueries": [str(response.get("query") or query or "")] if str(response.get("query") or query or "").strip() else [],
            "explicitFilters": {},
            "status": "unavailable" if not available else "ready",
        },
    )
    response.setdefault("citationPack", [])
    response.setdefault(
        "eventAggregation",
        {
            "schemaVersion": 2,
            "status": "unavailable" if not available else "no-events" if not results else "not-computed",
            "eventCount": 0,
            "events": [],
            "timeline": [],
            "mostSevereEvent": None,
            "resolutionCitations": [],
            "reason": reason or None,
        },
    )
    response.setdefault(
        "answerSynthesis",
        {
            "status": "unavailable" if not available else "no-results" if not results else "ready",
            "method": "extractive",
            "summary": "",
            "citationIds": [],
            "reason": reason or None,
        },
    )
    response.setdefault(
        "quality",
        {
            "schemaVersion": 1,
            "status": "insufficient" if not available else "weak" if not results else "not-computed",
            "needsMoreEvidence": True if not available or not results else None,
            "resultCount": len(results),
            "keyTerms": [],
            "coveredTerms": [],
            "missingTerms": [],
            "coverage": 0.0 if not results else None,
            "flags": {},
            "recommendations": ["retry-when-rag-available"] if not available else [],
        },
    )
    response.setdefault(
        "retrievalController",
        {
            "schemaVersion": 1,
            "mode": "bounded-deterministic-multi-pass",
            "serverSide": True,
            "executionPolicy": "not reported by backend",
            "passesRun": ["quality-gate"],
            "passes": [
                {
                    "id": "quality-gate",
                    "status": "insufficient" if not available else "weak" if not results else "not-computed",
                    "needsMoreEvidence": True if not available or not results else None,
                }
            ],
            "qualityStatus": "insufficient" if not available else "weak" if not results else "not-computed",
            "needsMoreEvidence": True if not available or not results else None,
        },
    )
    response.setdefault(
        "agentic",
        {
            "schemaVersion": 2,
            "evidenceFieldsStable": True,
            "serverSidePlanning": True,
            "serverSideMultiPass": True,
            "serverSideQualityGate": True,
            "serverSideEventAggregation": True,
            "llmGenerated": False,
        },
    )
    agentic = response.get("agentic")
    if isinstance(agentic, dict):
        agentic.setdefault("serverSideMultiPass", True)
        agentic.setdefault("serverSideQualityGate", True)
    for key in ("queryPlan", "eventAggregation", "agentic"):
        section = response.get(key)
        if isinstance(section, dict):
            section["schemaVersion"] = 2
    for key in ("quality", "retrievalController"):
        section = response.get(key)
        if isinstance(section, dict):
            section["schemaVersion"] = 1
    return response


def rag_external_skill_registration_plan(payload: dict | None = None) -> dict:
    return plan_rag_skill_registration(payload)


def rag_external_skill_registration(payload: dict | None = None) -> dict:
    return queue_rag_skill_registration(payload, requested_by="dashboard")


def rag_external_skill_registration_jobs(limit: int = 20) -> dict:
    return {"jobs": list_rag_skill_registration_jobs(limit=limit)}


def rag_v2_promote(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    run_id = str(payload.get("runId") or payload.get("candidateRunId") or "").strip()
    if not run_id:
        raise ValueError("runId is required")
    result = promote_v2_candidate(
        resolve_rag_settings(),
        run_id=run_id,
        confirm=payload.get("confirm") is True,
        confirmation_text=str(payload.get("confirmationText") or payload.get("confirmation") or ""),
        requested_by=str(payload.get("requestedBy") or "dashboard"),
        reason=str(payload.get("reason") or "dashboard operator promotion"),
    )
    result["requiredConfirmation"] = required_v2_promotion_confirmation(run_id)
    result["ragStatus"] = get_rag_status(probe_server=False)
    return result


def rag_v2_manifest_rollback(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    backup_name = str(payload.get("backupName") or payload.get("backupFileName") or "").strip()
    if not backup_name:
        raise ValueError("backupName is required")
    result = rollback_v2_manifest(
        resolve_rag_settings(),
        backup_name=backup_name,
        confirm=payload.get("confirm") is True,
        confirmation_text=str(payload.get("confirmationText") or payload.get("confirmation") or ""),
        requested_by=str(payload.get("requestedBy") or "dashboard"),
        reason=str(payload.get("reason") or "dashboard operator manifest rollback"),
    )
    result["requiredConfirmation"] = required_v2_manifest_rollback_confirmation(backup_name)
    result["ragStatus"] = get_rag_status(probe_server=False)
    return result


def rag_search(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    rag_settings = resolve_rag_settings()
    top_k = int(payload.get("topK") or payload.get("top_k") or rag_settings.retrieval_top_k)
    top_k = max(1, min(top_k, 20))
    budget_started = time.monotonic()
    server_budget_seconds = min(
        max(float(rag_settings.retrieval_latency_budget_seconds), 0.1),
        RAG_SERVER_SEARCH_BUDGET_SECONDS,
    )
    requested_server_budget_ms = _positive_budget_ms(
        payload.get("latencyBudgetMs", payload.get("latency_budget_ms"))
    )
    if requested_server_budget_ms is not None:
        server_budget_seconds = max(0.1, min(server_budget_seconds, requested_server_budget_ms / 1000.0))
    facade_budget_seconds = server_budget_seconds + RAG_FACADE_TRANSPORT_GRACE_SECONDS
    incoming_remaining_ms = _positive_budget_ms(payload.get("remainingBudgetMs"))
    if incoming_remaining_ms is not None:
        facade_budget_seconds = min(facade_budget_seconds, incoming_remaining_ms / 1000.0)
    budget_deadline = budget_started + facade_budget_seconds

    def finish(result: dict) -> dict:
        normalized = normalize_external_rag_search_response(result, query=query, top_k=top_k)
        _attach_facade_budget_telemetry(
            normalized,
            started=budget_started,
            total_seconds=facade_budget_seconds,
            server_budget_seconds=server_budget_seconds,
        )
        return normalized

    if facade_budget_seconds < 0.2:
        return finish(
            {
                "available": False,
                "reason": "rag-search-budget-exhausted",
                "error": "insufficient remaining search budget",
                "results": [],
            }
        )
    status = get_rag_status(probe_server=True)
    if not status.get("searchAvailable"):
        return finish(
            {
            "available": False,
            "reason": status.get("freshness", {}).get("status") or "unavailable",
            "ragStatus": status,
            "results": [],
            },
        )
    server = status.get("server") or {}
    search_url = str(server.get("url") or "").replace(str(status["settings"]["server_health_path"]), "/search")
    search_payload = {"query": query, "top_k": top_k}
    remaining_seconds = budget_deadline - time.monotonic()
    if remaining_seconds < 0.2:
        return finish(
            {
                "available": False,
                "reason": "rag-search-budget-exhausted",
                "error": "search budget exhausted during readiness probe",
                "ragStatus": status,
                "results": [],
            }
        )
    request_server_budget = min(server_budget_seconds, max(0.1, remaining_seconds - 0.05))
    search_payload["latency_budget_ms"] = int(request_server_budget * 1000)
    if payload.get("date"):
        search_payload["date"] = str(payload["date"])
    date_range = payload.get("dateRange") if isinstance(payload.get("dateRange"), dict) else {}
    if payload.get("dateFrom") or payload.get("date_from") or date_range.get("from"):
        search_payload["date_from"] = str(payload.get("dateFrom") or payload.get("date_from") or date_range.get("from"))
    if payload.get("dateTo") or payload.get("date_to") or date_range.get("to"):
        search_payload["date_to"] = str(payload.get("dateTo") or payload.get("date_to") or date_range.get("to"))
    if payload.get("role"):
        search_payload["role"] = str(payload["role"])
    if payload.get("project"):
        search_payload["project"] = str(payload["project"])
    tags = payload.get("tags")
    if isinstance(tags, list):
        search_payload["tags"] = [str(tag) for tag in tags if str(tag).strip()]
    elif payload.get("tag"):
        search_payload["tags"] = [str(payload["tag"])]
    for payload_key, server_key in (
        ("sourceSets", "source_sets"),
        ("source_sets", "source_sets"),
        ("lifecycle", "lifecycle"),
        ("lifecycles", "lifecycle"),
        ("workType", "work_type"),
        ("workTypes", "work_type"),
        ("work_type", "work_type"),
    ):
        value = payload.get(payload_key)
        if isinstance(value, list):
            search_payload[server_key] = [str(item) for item in value if str(item).strip()]
        elif isinstance(value, str) and value.strip():
            search_payload[server_key] = [value.strip()]
    if "includeFullText" in payload:
        search_payload["include_full_text"] = bool(payload.get("includeFullText"))
    if "includeGovernance" in payload:
        search_payload["include_governance"] = bool(payload.get("includeGovernance"))
    body = json.dumps(search_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        search_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        transport_timeout = max(0.1, budget_deadline - time.monotonic())
        with urllib.request.urlopen(request, timeout=transport_timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return finish(
            {
                "available": False,
                "reason": f"rag-server-http-{exc.code}",
                "error": exc.read().decode("utf-8", errors="replace"),
                "ragStatus": status,
                "results": [],
            },
        )
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return finish(
            {
                "available": False,
                "reason": f"rag-server-unavailable:{exc.__class__.__name__}",
                "error": str(exc),
                "ragStatus": status,
                "results": [],
            },
        )
    return finish(
        {
            "available": True,
            "schemaVersion": 2,
            "query": query,
            "topK": top_k,
            "ragStatus": status,
            **(result if isinstance(result, dict) else {"results": result}),
        },
    )


def _positive_budget_ms(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _attach_facade_budget_telemetry(
    payload: dict,
    *,
    started: float,
    total_seconds: float,
    server_budget_seconds: float,
) -> None:
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    telemetry = {
        "policy": "monotonic-total-budget",
        "totalBudgetMs": int(total_seconds * 1000),
        "serverBudgetCapMs": int(server_budget_seconds * 1000),
        "elapsedMs": elapsed_ms,
        "remainingBudgetMs": max(0, int(total_seconds * 1000 - elapsed_ms)),
    }
    payload["facadeBudget"] = telemetry
    controller = payload.setdefault("retrievalController", {})
    controller["facadeBudget"] = dict(telemetry)


def get_llm_provider() -> dict:
    return read_llm_provider()


def get_llm_provider_chain() -> dict:
    return _llm_provider_chain_status(load_paths())


def update_llm_provider_chain(payload: dict) -> dict:
    paths = load_paths()
    entries = _llm_provider_chain_entries_payload(payload)
    _validate_llm_provider_chain_update_pipeline_secrets(paths, entries)
    saved = write_operator_settings_bundle(
        {"llmProviderChain": entries},
        paths,
        readiness_verifier=lambda: _raise_if_llm_provider_chain_not_pipeline_ready(paths),
    )
    result = _llm_provider_chain_status(paths)
    result["settingsTransaction"] = saved.get("settingsTransaction")
    return result


def test_llm_provider_chain_entry(payload: dict | None = None) -> dict:
    request = payload if isinstance(payload, dict) else {}
    candidate = request.get("entry") if isinstance(request.get("entry"), dict) else request
    if not isinstance(candidate, dict) or not candidate:
        raise ValueError("entry must be a non-empty provider object")
    candidate = dict(candidate)
    paths = load_paths()
    raw_api_key = str(candidate.get("apiKey") or "")
    entry_id = str(candidate.get("entryId") or "")
    if not raw_api_key and entry_id:
        persisted = next(
            (
                entry
                for entry in resolve_llm_provider_chain(paths, False, False)
                if str(entry.get("entryId") or "") == entry_id
            ),
            None,
        )
        if (
            persisted
            and str(persisted.get("provider") or "") == str(candidate.get("provider") or "")
            and persisted.get("apiKey")
        ):
            candidate["apiKey"] = persisted["apiKey"]
    result = check_llm_provider_availability(
        paths,
        candidate=candidate,
    )
    return {
        **result,
        "entryId": str(candidate.get("entryId") or "candidate"),
        "persisted": False,
    }


def _llm_provider_chain_status(paths: RuntimePaths) -> dict:
    entries = resolve_llm_provider_chain(
        paths,
        True,
        True,
    )
    providers = []
    for entry in entries:
        public_entry = dict(entry)
        secret_ref = public_entry.pop("secretRef", None)
        source = public_entry.get("source") if isinstance(public_entry.get("source"), dict) else {}
        public_entry["hasSecretRef"] = bool(secret_ref)
        public_entry["hasSavedApiKey"] = bool(secret_ref) and source.get("apiKey") == "secret-store"
        providers.append(public_entry)
    readiness_error = llm_provider_chain_readiness_error(
        paths,
        True,
    )
    return {
        "providers": providers,
        "catalog": llm_provider_catalog(),
        "readiness": {
            "ready": readiness_error is None,
            "status": "ready" if readiness_error is None else "not-ready",
            **({"error": readiness_error} if readiness_error else {}),
        },
        "legacyPrimary": read_llm_provider(paths),
    }


def _llm_provider_chain_entries_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    entries = payload.get("providers")
    if entries is None:
        entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("providers must contain at least one provider")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("providers must contain provider objects")
    return entries


def _validate_llm_provider_chain_update_pipeline_secrets(
    paths: RuntimePaths,
    entries: list[dict],
) -> None:
    settings = read_settings(paths, redact_secrets=False)
    current_entries = settings.get("llmProviderChain")
    current_chain = current_entries if isinstance(current_entries, list) else []
    normalized = normalize_llm_provider_chain_update(entries, current_chain)
    resolved_current = resolve_llm_provider_chain(
        paths,
        False,
        True,
    )
    current_by_id = {
        str(entry.get("entryId")): entry
        for entry in resolved_current
        if isinstance(entry, dict) and entry.get("entryId")
    }
    legacy_primary = resolved_current[0] if resolved_current else {}
    secret_backend = default_secret_backend()

    for index, (raw_entry, normalized_entry) in enumerate(zip(entries, normalized, strict=True)):
        entry_id = str(normalized_entry.get("entryId") or f"provider-{index + 1}")
        provider_id = str(normalized_entry.get("provider") or "custom")
        missing = [
            field
            for field in ("endpoint", "model")
            if not str(normalized_entry.get(field) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"LLM provider chain entry {entry_id} is not ready: missing {', '.join(missing)}."
            )

        raw_api_key = str(raw_entry.get("apiKey") or "")
        if raw_api_key and raw_api_key != MASKED_SECRET:
            if secret_backend in {"memory", "process-env", ""}:
                raise ValueError(
                    f"LLM provider chain entry {entry_id} is not ready for pipeline execution: "
                    f"apiKey would be stored in the {secret_backend or 'unknown'} backend, which "
                    "pipeline subprocesses cannot read. Use the runtime-file backend or the "
                    "configured apiKeyEnv."
                )
            continue

        current = current_by_id.get(entry_id)
        if (
            current is None
            and index == 0
            and provider_id == str(legacy_primary.get("provider") or "")
        ):
            current = legacy_primary
        api_key_env = str(normalized_entry.get("apiKeyEnv") or "LLM_API_KEY")
        if os.getenv(api_key_env):
            continue
        if current and provider_id == str(current.get("provider") or ""):
            readiness = current.get("readiness") if isinstance(current.get("readiness"), dict) else {}
            if readiness.get("ready"):
                continue
            error = str(readiness.get("error") or "apiKey is unavailable")
            raise ValueError(
                f"LLM provider chain entry {entry_id} is not ready for pipeline execution: {error}."
            )
        raise ValueError(
            f"LLM provider chain entry {entry_id} is not ready for pipeline execution: missing apiKey; "
            f"save a readable secret or set {api_key_env}."
        )


def _raise_if_llm_provider_chain_not_pipeline_ready(paths: RuntimePaths) -> None:
    readiness_error = llm_provider_chain_readiness_error(
        paths,
        True,
    )
    if readiness_error:
        raise ValueError(readiness_error)


def update_llm_provider(payload: dict) -> dict:
    paths = load_paths()
    _validate_llm_provider_update_pipeline_secret(paths, payload)
    saved = write_operator_settings_bundle(
        {"llmProvider": payload if isinstance(payload, dict) else {}},
        paths,
        readiness_verifier=lambda: _raise_if_llm_provider_not_pipeline_ready(paths),
    )
    provider = read_llm_provider(paths)
    provider["settingsTransaction"] = saved.get("settingsTransaction")
    _raise_if_llm_provider_not_pipeline_ready(paths)
    return provider


def _validate_llm_provider_update_pipeline_secret(paths: RuntimePaths, payload: dict | None) -> None:
    update = payload if isinstance(payload, dict) else {}
    raw_api_key = str(update.get("apiKey") or "")
    if raw_api_key and raw_api_key != MASKED_SECRET:
        backend = default_secret_backend()
        if backend == "memory":
            raise ValueError(
                "LLM provider is not ready for pipeline execution: apiKey would be stored in the process-local "
                "memory backend, which daily pipeline subprocesses cannot read. Save the Provider using the "
                "runtime-file backend or set the configured apiKeyEnv before running."
            )
        if backend in {"process-env", ""}:
            raise ValueError(
                "LLM provider is not ready for pipeline execution: apiKey would be stored in the "
                f"{backend or 'unknown'} secret backend, which daily pipeline subprocesses cannot read. "
                "Save the Provider using the runtime-file backend or set the configured apiKeyEnv before running."
            )
        return

    settings = read_settings(paths, redact_secrets=False)
    current = settings.get("llmProvider") if isinstance(settings.get("llmProvider"), dict) else {}
    normalized = normalize_llm_provider_update(update, current if isinstance(current, dict) else {})
    provider_id = str(normalized.get("provider") or normalized.get("presetProvider") or "custom").strip() or "custom"
    refs = settings.get("llmProviderSecrets") if isinstance(settings.get("llmProviderSecrets"), dict) else {}
    secret_ref = refs.get(provider_id) if isinstance(refs.get(provider_id), dict) else None
    current_provider_id = str((current or {}).get("provider") or (current or {}).get("presetProvider") or "custom").strip() or "custom"
    if not secret_ref and provider_id == current_provider_id and isinstance((current or {}).get("secretRef"), dict):
        secret_ref = current["secretRef"]
    if not secret_ref:
        return
    backend = str(secret_ref.get("backend") or "").strip()
    if backend == "memory":
        raise ValueError(
            "LLM provider is not ready for pipeline execution: apiKey is stored in the process-local "
            "memory backend, which daily pipeline subprocesses cannot read. Re-enter and save the Provider "
            "using the runtime-file backend or set the configured apiKeyEnv before running."
        )
    secret_value = (
        read_secret(secret_ref, runtime_home=paths.home)
        if backend == "runtime-file"
        else read_secret(secret_ref)
    )
    if backend and not secret_value:
        raise ValueError(
            "LLM provider is not ready for pipeline execution: apiKey is not readable from the configured "
            f"{backend} secret reference. Re-save the Provider in Dashboard or set the configured apiKeyEnv before running."
        )


def _raise_if_llm_provider_not_pipeline_ready(paths: RuntimePaths) -> None:
    readiness_error = llm_provider_readiness_error(paths, require_cross_process_secret=True)
    if readiness_error:
        raise ValueError(readiness_error)


def test_llm_provider(payload: dict | None = None) -> dict:
    return check_llm_provider_availability(candidate=payload if isinstance(payload, dict) else None)


def external_tool_catalog() -> dict:
    return supported_external_tool_catalog()


def rediscover_external_tool_paths() -> dict:
    return rediscover_external_tools()


def add_external_tool(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return add_external_tool_instance(
        str(payload.get("tool") or payload.get("toolId") or ""),
        str(payload.get("path") or payload.get("home") or ""),
        instance_id=str(payload.get("instanceId") or "").strip() or None,
    )


def browse_path(path: str | None = None) -> dict:
    candidate = Path(path or "/").expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.exists():
        candidate = candidate.parent if candidate.parent.exists() else Path("/")
    candidate = candidate.absolute()
    entries = []
    try:
        children = sorted(candidate.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError:
        children = []
    for child in children[:250]:
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "directory" if child.is_dir() else "file",
                "selectable": True,
            }
        )
    return {
        "current": str(candidate),
        "parent": str(candidate.parent) if candidate.parent != candidate else None,
        "entries": entries,
    }


def current_runtime_path() -> dict:
    selected = load_paths()
    validation = validate_home(selected.home)
    return {
        "selected": _paths_payload(selected),
        "validation": _validation_payload(validation),
        "envOverride": os.getenv("ACTANARA_HOME") is not None,
        "locationFile": os.getenv("ACTANARA_LOCATION_FILE"),
    }


def validate_runtime_path(path: str | None) -> dict:
    if not path:
        raise ValueError("path is required")
    validation = validate_home(Path(path))
    return {
        "validation": _validation_payload(validation),
        "current": str(load_paths().home),
    }


def select_runtime_path(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if str(payload.get("confirmationText") or "") != RUNTIME_PATH_SELECT_CONFIRMATION:
        raise ValueError(f"confirmationText must be exactly: {RUNTIME_PATH_SELECT_CONFIRMATION}")
    raw_path = payload.get("path")
    if not raw_path:
        raise ValueError("path is required")
    mode = payload.get("mode", "use")
    if mode not in {"use", "initialize"}:
        raise ValueError("mode must be one of use, initialize")
    candidate = Path(raw_path).expanduser().absolute()
    validation_before = validate_home(candidate)
    import_result = None
    if mode == "use":
        selected = select_home(candidate, "use")
    else:
        initialized = initialize_home(candidate)
        selected = select_home(initialized.home, "use")
    audit = _write_path_audit(
        selected,
        {
            "action": "runtime-path-select",
            "mode": mode,
            "candidate": str(candidate),
            "validationBefore": _validation_payload(validation_before),
            "selected": _paths_payload(selected),
            "importResult": _import_result_payload(import_result),
        },
    )
    return {
        "selected": _paths_payload(selected),
        "validation": _validation_payload(validate_home(selected.home)),
        "audit": audit,
        "importResult": _import_result_payload(import_result),
        "envOverride": os.getenv("ACTANARA_HOME") is not None,
    }


def _paths_payload(paths: RuntimePaths) -> dict:
    return {
        "actanaraHome": str(paths.home),
        "configDir": str(paths.config_dir),
        "database": str(paths.db_path),
        "archives": str(paths.archives_dir),
        "diary": str(paths.diary_dir),
        "reports": str(paths.reports_dir),
        "taskBoard": str(paths.task_board_path),
        "snapshots": str(paths.snapshots_dir),
        "state": str(paths.state_dir),
        "legacyDiaryRoot": str(paths.legacy_diary_root) if paths.legacy_diary_root else None,
        "legacyRagRoot": str(paths.legacy_rag_root) if paths.legacy_rag_root else None,
    }


def _validation_payload(validation: PathValidation) -> dict:
    return {
        "candidate": str(validation.candidate),
        "exists": validation.exists,
        "initialized": validation.initialized,
        "writable": validation.writable,
        "valid": validation.valid,
        "issues": list(validation.issues),
    }


def _import_result_payload(result: LegacyImportResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "copied": result.copied,
        "matched": result.matched,
        "skipped": result.skipped,
        "conflicts": list(result.conflicts),
    }


def _write_path_audit(paths: RuntimePaths, payload: dict) -> dict:
    audit_path = paths.state_dir / "migration" / "runtime-path-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recordedAt": datetime.now().astimezone().isoformat(),
        **payload,
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "path": str(audit_path),
        "recordedAt": record["recordedAt"],
        "action": payload.get("action"),
    }
