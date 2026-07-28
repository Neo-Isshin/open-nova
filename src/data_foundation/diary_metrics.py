"""Readiness reporting for the guarded daily diary metrics reader."""

from __future__ import annotations

import json
import hashlib
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .aggregate import DIARY_TOOL_KEYS, daily_diary_usage_metrics
from .paths import RuntimePaths
from .snapshots import read_diary_memory_snapshot, read_diary_tasks_snapshot

DIARY_TABLE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read",
    "total_tokens",
    "api_calls",
    "messages_count",
    "active_sessions",
    "sessions_total",
)
SESSION_COUNT_FIELDS = {"active_sessions", "sessions_total"}
CODEX_CACHE_INPUT_FIELDS = {"input_tokens", "total_tokens"}
TABLE_METRICS_APPROVAL_LOG = "diary-metrics-table-approvals.jsonl"


def _legacy_diary_metrics(business_date: date) -> dict:
    diary_generator_root = Path(__file__).resolve().parents[1]
    if str(diary_generator_root) not in sys.path:
        sys.path.insert(0, str(diary_generator_root))
    from diary_generator import narrative_pass

    return narrative_pass._calculate_stats_legacy(business_date.isoformat())


def _legacy_diary_memory_stats() -> dict:
    diary_generator_root = Path(__file__).resolve().parents[1]
    if str(diary_generator_root) not in sys.path:
        sys.path.insert(0, str(diary_generator_root))
    from diary_generator import narrative_pass

    return narrative_pass._get_memory_stats_legacy()


def _legacy_diary_tasks() -> dict:
    diary_generator_root = Path(__file__).resolve().parents[1]
    if str(diary_generator_root) not in sys.path:
        sys.path.insert(0, str(diary_generator_root))
    from diary_generator import narrative_pass

    return narrative_pass._get_task_board_snapshot_legacy()


def _table_differences(legacy: dict, foundation: dict) -> dict[str, dict[str, int]]:
    differences = {}
    for tool_key in (*DIARY_TOOL_KEYS, "total"):
        changed = {
            field: int((foundation.get(tool_key) or {}).get(field, 0))
            - int((legacy.get(tool_key) or {}).get(field, 0))
            for field in DIARY_TABLE_FIELDS
            if int((foundation.get(tool_key) or {}).get(field, 0))
            != int((legacy.get(tool_key) or {}).get(field, 0))
        }
        if changed:
            differences[tool_key] = changed
    return differences


def _stable_json_digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_table_metrics_approval(paths: RuntimePaths, business_date: date, differences: dict) -> dict | None:
    if not differences:
        return None
    approval_path = paths.state_dir / "migration" / TABLE_METRICS_APPROVAL_LOG
    expected_digest = _stable_json_digest(differences)
    try:
        lines = approval_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("businessDate") != business_date.isoformat():
            continue
        if record.get("surface") != "diary-metrics":
            continue
        if record.get("approvalType") != "table-metrics-mismatch":
            continue
        if record.get("differencesDigest") != expected_digest:
            continue
        return record
    return None


def write_diary_metrics_table_mismatch_approval(
    paths: RuntimePaths,
    business_date: date,
    *,
    operator: str = "operator",
    note: str = "",
) -> dict:
    """Record an auditable approval for the current diary table metrics mismatch."""
    report_path = paths.state_dir / "migration" / f"diary-metrics-readiness-{business_date.isoformat()}.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"missing diary metrics readiness report: {report_path}") from exc
    differences = ((report.get("tableMetrics") or {}).get("differences") or {})
    if not differences:
        raise ValueError("current diary metrics readiness report has no table metrics differences")
    if (report.get("canEnable") or {}).get("diaryMetricsSourceFoundation"):
        raise ValueError("current diary metrics readiness report is already enabled")
    record = {
        "recordedAt": datetime.now().astimezone().isoformat(),
        "operator": operator or "operator",
        "surface": "diary-metrics",
        "approvalType": "table-metrics-mismatch",
        "businessDate": business_date.isoformat(),
        "differencesDigest": _stable_json_digest(differences),
        "differences": differences,
        "note": note,
        "readinessReportPath": str(report_path),
    }
    approval_path = paths.state_dir / "migration" / TABLE_METRICS_APPROVAL_LOG
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    with approval_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    record["approvalPath"] = str(approval_path)
    return record


def _approved_codex_cache_input_normalization(legacy: dict, foundation: dict, differences: dict[str, dict[str, int]]) -> bool:
    """Return true when differences only reflect Codex cached-input double counting in legacy metrics."""
    if not differences:
        return False
    codex_cache_read = int(foundation["codex"].get("cache_read", 0) or 0)
    legacy_codex_cache_read = int(legacy["codex"].get("cache_read", 0) or 0)
    if codex_cache_read <= 0 or codex_cache_read != legacy_codex_cache_read:
        return False
    for tool_key, changed in differences.items():
        token_changes = {
            field: delta
            for field, delta in changed.items()
            if field not in SESSION_COUNT_FIELDS
        }
        if not token_changes:
            continue
        if tool_key not in {"codex", "total"}:
            return False
        if set(token_changes) - CODEX_CACHE_INPUT_FIELDS:
            return False
        if int(foundation[tool_key].get("cache_read", 0) or 0) != int(legacy[tool_key].get("cache_read", 0) or 0):
            return False
        for field in CODEX_CACHE_INPUT_FIELDS:
            if field in token_changes and int(token_changes[field]) != -codex_cache_read:
                return False
    return True


def diary_metrics_readiness(
    paths: RuntimePaths,
    business_date: date,
    *,
    legacy_builder: Callable[[date], dict] | None = None,
    approve_model_usage_normalization: bool = False,
    approve_session_count_normalization: bool = False,
) -> dict:
    """Check whether enabling the diary metrics flag is approved and ready."""
    try:
        foundation = daily_diary_usage_metrics(paths, business_date)
    except Exception as error:
        return {
            "businessDate": business_date.isoformat(),
            "status": "unavailable",
            "foundationReady": False,
            "error": str(error),
            "canEnable": {"diaryMetricsSourceFoundation": False},
            "preservedSources": {"rag": "v2", "memory": "separately_guarded", "tasks": "legacy"},
        }
    if foundation is None:
        return {
            "businessDate": business_date.isoformat(),
            "status": "missing",
            "foundationReady": False,
            "canEnable": {"diaryMetricsSourceFoundation": False},
            "preservedSources": {"rag": "v2", "memory": "separately_guarded", "tasks": "legacy"},
        }
    legacy = (legacy_builder or _legacy_diary_metrics)(business_date)
    table_differences = _table_differences(legacy, foundation)
    legacy_models = legacy.get("model_usage_list", [])
    foundation_models = foundation.get("model_usage_list", [])
    model_usage_changes = legacy_models != foundation_models
    model_usage_approved = model_usage_changes and approve_model_usage_normalization
    non_session_differences = {
        tool_key: {
            field: delta
            for field, delta in changed.items()
            if field not in SESSION_COUNT_FIELDS
        }
        for tool_key, changed in table_differences.items()
    }
    non_session_differences = {tool_key: changed for tool_key, changed in non_session_differences.items() if changed}
    session_count_changes = bool(table_differences) and not non_session_differences
    codex_cache_input_approved = (
        bool(non_session_differences)
        and approve_model_usage_normalization
        and _approved_codex_cache_input_normalization(legacy, foundation, table_differences)
    )
    if codex_cache_input_approved:
        non_session_differences = {}
        session_count_changes = any(
            field in SESSION_COUNT_FIELDS
            for changed in table_differences.values()
            for field in changed
        )
    session_count_approved = session_count_changes and approve_session_count_normalization
    operator_table_approval = _read_table_metrics_approval(paths, business_date, table_differences)
    table_matched = not table_differences
    table_approved = table_matched or session_count_approved or (
        codex_cache_input_approved and (not session_count_changes or session_count_approved)
    ) or bool(operator_table_approval)
    status = "ready"
    if table_differences and not table_approved:
        status = "table_metrics_mismatch"
    elif model_usage_changes and not model_usage_approved:
        status = "model_usage_change_requires_approval"
    elif codex_cache_input_approved and session_count_approved and model_usage_approved:
        status = "ready_with_approved_metric_normalizations"
    elif codex_cache_input_approved and session_count_approved:
        status = "ready_with_approved_metric_normalizations"
    elif codex_cache_input_approved and model_usage_approved:
        status = "ready_with_approved_metric_normalizations"
    elif codex_cache_input_approved:
        status = "ready_with_approved_token_semantics_change"
    elif operator_table_approval:
        status = "ready_with_operator_approved_table_metrics_change"
    elif session_count_approved and model_usage_approved:
        status = "ready_with_approved_metric_normalizations"
    elif session_count_approved:
        status = "ready_with_approved_session_count_change"
    elif model_usage_approved:
        status = "ready_with_approved_model_usage_change"
    return {
        "businessDate": business_date.isoformat(),
        "generatedAt": datetime.now().astimezone().isoformat(),
        "status": status,
        "foundationReady": True,
        "tableMetrics": {
            "matched": table_matched,
            "fields": list(DIARY_TABLE_FIELDS),
            "differences": table_differences,
            "approvedSessionCountNormalization": session_count_approved,
            "approvedCodexCacheInputNormalization": codex_cache_input_approved,
            "operatorApprovedTableMetricsMismatch": bool(operator_table_approval),
            "operatorApproval": operator_table_approval,
            "requiresApproval": (session_count_changes and not session_count_approved)
            or (bool(non_session_differences) and not codex_cache_input_approved and not operator_table_approval),
        },
        "modelUsage": {
            "matched": not model_usage_changes,
            "legacy": legacy_models,
            "foundation": foundation_models,
            "approvedNormalization": model_usage_approved,
            "requiresApproval": model_usage_changes and not model_usage_approved,
        },
        "canEnable": {
            "tokenTableFoundation": table_approved,
            "diaryMetricsSourceFoundation": table_approved and (
                not model_usage_changes or model_usage_approved
            ),
        },
        "preservedSources": {"rag": "v2", "memory": "separately_guarded", "tasks": "legacy"},
        "notes": [
            "The diary Markdown/embedded JSON format is unchanged by this readiness check.",
            "The DIARY_METRICS_SOURCE flag covers both table metrics and modelUsage values.",
            "An approved modelUsage normalization remains visible in the report as a data correction.",
            "Approved session-count normalization may use Foundation session identity when token/message/API totals match.",
        ],
    }


def write_diary_metrics_readiness_report(
    paths: RuntimePaths,
    business_date: date,
    *,
    approve_model_usage_normalization: bool = False,
    approve_session_count_normalization: bool = False,
) -> dict:
    report = diary_metrics_readiness(
        paths,
        business_date,
        approve_model_usage_normalization=approve_model_usage_normalization,
        approve_session_count_normalization=approve_session_count_normalization,
    )
    output = paths.state_dir / "migration" / f"diary-metrics-readiness-{business_date.isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["outputPath"] = str(output)
    return report


def diary_memory_readiness(
    paths: RuntimePaths,
    business_date: date,
    *,
    legacy_builder: Callable[[], dict] | None = None,
) -> dict:
    """Check whether the materialized diary memoryStats payload matches legacy."""
    try:
        snapshot = read_diary_memory_snapshot(paths, business_date)
    except Exception as error:
        return {
            "businessDate": business_date.isoformat(),
            "status": "unavailable",
            "foundationReady": False,
            "error": str(error),
            "canEnable": {"diaryMemorySourceFoundation": False},
            "preservedSources": {"rag": "v2", "tasks": "legacy"},
        }
    if snapshot is None:
        return {
            "businessDate": business_date.isoformat(),
            "status": "missing",
            "foundationReady": False,
            "canEnable": {"diaryMemorySourceFoundation": False},
            "preservedSources": {"rag": "v2", "tasks": "legacy"},
        }
    legacy = (legacy_builder or _legacy_diary_memory_stats)()
    foundation = snapshot["payload"]
    matched = foundation == legacy
    return {
        "businessDate": business_date.isoformat(),
        "generatedAt": datetime.now().astimezone().isoformat(),
        "status": "ready" if matched else "memory_stats_mismatch",
        "foundationReady": True,
        "projectionType": snapshot["projectionType"],
        "snapshotGeneratedAt": snapshot["generatedAt"],
        "sourceRunId": snapshot["sourceRunId"],
        "memoryStats": {
            "matched": matched,
            "legacy": legacy,
            "foundation": foundation,
        },
        "canEnable": {"diaryMemorySourceFoundation": matched},
        "preservedSources": {"rag": "v2", "tasks": "legacy"},
        "notes": [
            "The diary embedded JSON memoryStats field shape is unchanged by this reader.",
            "The memory snapshot is built without reading deferred RAG data.",
        ],
    }


def write_diary_memory_readiness_report(paths: RuntimePaths, business_date: date) -> dict:
    report = diary_memory_readiness(paths, business_date)
    output = paths.state_dir / "migration" / f"diary-memory-readiness-{business_date.isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["outputPath"] = str(output)
    return report


def diary_tasks_readiness(
    paths: RuntimePaths,
    business_date: date,
    *,
    legacy_builder: Callable[[], dict] | None = None,
    approve_checkbox_normalization: bool = False,
) -> dict:
    """Gate diary task counts sourced from Nova-Task SQLite authority."""
    del approve_checkbox_normalization
    try:
        snapshot = read_diary_tasks_snapshot(paths, business_date)
    except Exception as error:
        return {
            "businessDate": business_date.isoformat(),
            "status": "unavailable",
            "foundationReady": False,
            "error": str(error),
            "canEnable": {"diaryTasksSourceFoundation": False},
            "preservedSources": {"taskAuthority": "Nova-Task v2 SQLite", "taskBoard": "projection", "rag": "v2"},
        }
    if snapshot is None:
        return {
            "businessDate": business_date.isoformat(),
            "status": "missing",
            "foundationReady": False,
            "canEnable": {"diaryTasksSourceFoundation": False},
            "preservedSources": {"taskAuthority": "Nova-Task v2 SQLite", "taskBoard": "projection", "rag": "v2"},
        }
    foundation = snapshot["payload"]
    comparison = None
    if legacy_builder is not None:
        legacy = legacy_builder()
        comparison = {
            "legacy": legacy,
            "changed": foundation != legacy,
        }
    return {
        "businessDate": business_date.isoformat(),
        "generatedAt": datetime.now().astimezone().isoformat(),
        "status": "ready",
        "foundationReady": True,
        "projectionType": snapshot["projectionType"],
        "snapshotGeneratedAt": snapshot["generatedAt"],
        "sourceRunId": snapshot["sourceRunId"],
        "tasks": {
            "foundation": foundation,
            "comparison": comparison,
            "requiresApproval": False,
        },
        "canEnable": {"diaryTasksSourceFoundation": True},
        "preservedSources": {"taskAuthority": "Nova-Task v2 SQLite", "taskBoard": "projection", "rag": "v2"},
        "notes": [
            "Nova-Task v2 SQLite is the task authority.",
            "TASK_BOARD.md is a projection / compatibility display, not the normal task source.",
        ],
    }


def write_diary_tasks_readiness_report(
    paths: RuntimePaths,
    business_date: date,
    *,
    approve_checkbox_normalization: bool = False,
) -> dict:
    report = diary_tasks_readiness(
        paths,
        business_date,
        approve_checkbox_normalization=approve_checkbox_normalization,
    )
    output = paths.state_dir / "migration" / f"diary-tasks-readiness-{business_date.isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["outputPath"] = str(output)
    return report
