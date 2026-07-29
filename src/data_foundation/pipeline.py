"""Application service boundary for the existing daily production pipeline."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import os
import json
import re
import subprocess
import sys
import fcntl
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

import config

from .diary_metrics import (
    write_diary_memory_readiness_report,
    write_diary_metrics_readiness_report,
    write_diary_tasks_readiness_report,
)
from .aggregate import daily_diary_usage_metrics, refresh_daily_usage
from .diary_paths import diary_report_paths, diary_technical_report_path
from .db import migrate
from .ingest import run_shadow_ingestion
from .jobs import begin_ingestion_run, finish_ingestion_run
from .nova_task import export_task_board_markdown, reconcile_workspace_project_anchors
from .nova_task_work_graph_reconciliation import run_work_graph_reconciliation
from .paths import RuntimePaths, initialize_home, load_paths
from .pipeline_execution import PipelineExecutionBoundary, PipelineExecutionContext
from .pipeline_llm_attribution import PIPELINE_RUN_ID_ENV, PIPELINE_STAGE_ID_ENV
from .pipeline_runs import (
    append_pipeline_step,
    classify_pipeline_failure,
    create_pipeline_run,
    finish_pipeline_run_if_status,
    pipeline_run_by_id,
    sanitize_pipeline_error_summary,
)
from .refresh import run_pipeline_blank_day_materialization, run_pipeline_daily_materialization
from .settings import (
    is_nova_task_enabled,
    llm_provider_readiness_error,
    resolve_memory_search_settings,
    resolve_pipeline_settings,
    resolve_runtime_source,
    runtime_environment_overrides,
)
from .snapshots import materialize_diary_memory_snapshot, materialize_diary_tasks_snapshot
from .time import resolve_timezone
from .workspace_attribution import materialize_workspace_attribution_catalog

try:
    from agentic_rag.rag_settings import is_rag_product_enabled, rag_product_disabled_reason
    from agentic_rag.rag_settings import resolve_rag_settings as resolve_rag_runtime_settings
    from agentic_rag.rag_v2_sync import plan_v2_production_sync
except ImportError:  # pragma: no cover - direct script fallback
    is_rag_product_enabled = None  # type: ignore
    rag_product_disabled_reason = None  # type: ignore
    resolve_rag_runtime_settings = None  # type: ignore
    plan_v2_production_sync = None  # type: ignore

SRC_DIR = config.WORKSPACE_DIR / "src"


@dataclass(frozen=True)
class PipelineStep:
    name: str
    script: Path
    args: tuple[str, ...] = ()
    stage_id: str | None = None


@dataclass(frozen=True)
class PipelineRunResult:
    business_date: str
    succeeded_steps: int
    total_steps: int
    success: bool
    failed_step: str | None = None


@dataclass(frozen=True)
class StepExecutionResult:
    success: bool
    reason: str | None = None
    stdout: str = ""


_STEP_DISPLAY_NAMES = {
    "unified_source_collector.py": "Collect activity",
    "narrative_pass.py": "Generate diary · Daily story",
    "technical_pass.py": "Generate diary · Technical notes",
    "learning_pass.py": "Generate diary · Lessons learned",
    "rag_v2_sync.py": "Update search memory",
}


def _print_pipeline_status(marker: str, label: str, detail: str | None = None) -> None:
    message = f"{marker} {label}"
    if detail:
        message += f" — {' '.join(str(detail).split())}"
    print(message)


def _pipeline_step_display_name(step: PipelineStep) -> str:
    return _STEP_DISPLAY_NAMES.get(step.script.name, _pipeline_stage_display_name(step.name))


def _pipeline_stage_display_name(name: str) -> str:
    normalized = str(name or "").casefold()
    if "rag" in normalized or "search memory" in normalized:
        return "Update search memory"
    if "nova-task" in normalized or "task" in normalized:
        return "Refresh tasks"
    if "source" in normalized or "collect" in normalized:
        return "Collect activity"
    if any(token in normalized for token in ("narrative", "technical", "learning")):
        return "Generate diary"
    if any(token in normalized for token in ("foundation", "materialization", "inputs")):
        return "Prepare diary"
    if "artifact" in normalized or "terminal" in normalized:
        return "Finish diary"
    if "pipeline" in normalized:
        return "Daily diary"
    return "Daily diary"


def _subprocess_failure_detail(output: str) -> str:
    normalized = str(output or "").casefold()
    if any(token in normalized for token in ("usage limit", "rate limit", "quota", "http 403")):
        return "The model usage limit was reached."
    if any(token in normalized for token in ("unauthorized", "invalid api key", "http 401")):
        return "Model credentials were rejected."
    if any(token in normalized for token in ("connection refused", "network is unreachable", "timed out")):
        return "A required service could not be reached."
    return "The step could not finish."


def _exception_failure_detail(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "Permission was denied."
    if isinstance(exc, FileNotFoundError):
        return "A required file is missing."
    if isinstance(exc, TimeoutError):
        return "The step timed out."
    return "An unexpected error occurred."


ZH_PRODUCTION_STEPS = (
    PipelineStep(
        "0. 全域 AI 资产智慧收集 (Unified-Collect)",
        SRC_DIR / "ai_assets_center" / "unified_source_collector.py",
        ("{date}",),
    ),
    PipelineStep(
        "2. 叙事轨道生成 (Narrative Pass)",
        SRC_DIR / "diary_generator" / "narrative_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "4. 技术轨道生成 (Technical Pass)",
        SRC_DIR / "diary_generator" / "technical_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "7. 经验教训学习 (Learning Pass)",
        SRC_DIR / "diary_generator" / "learning_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "8. nova-RAG 索引同步 (Active Sync)",
        SRC_DIR / "agentic_rag" / "rag_v2_sync.py",
        (),
    ),
)
EN_PRODUCTION_STEPS = (
    PipelineStep(
        "0. Unified AI asset collection",
        SRC_DIR / "ai_assets_center" / "unified_source_collector.py",
        ("{date}",),
    ),
    PipelineStep(
        "2. Narrative pass (English)",
        SRC_DIR / "diary_generator" / "en" / "narrative_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "4. Technical pass (English)",
        SRC_DIR / "diary_generator" / "en" / "technical_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "7. Learning pass (English)",
        SRC_DIR / "diary_generator" / "en" / "learning_pass.py",
        ("{date}",),
    ),
    PipelineStep(
        "8. nova-RAG index sync",
        SRC_DIR / "agentic_rag" / "rag_v2_sync.py",
        (),
    ),
)
PRODUCTION_STEPS = ZH_PRODUCTION_STEPS

Runner = Callable[..., subprocess.CompletedProcess[str]]
PreMaterializer = Callable[..., bool]
NovaTaskMaterializer = Callable[..., bool]
PostMaterializer = Callable[..., bool]


def default_business_date(now: datetime | None = None, paths: RuntimePaths | None = None) -> str:
    """Default no-argument runs to the previous calendar day in the configured timezone."""
    tz = resolve_timezone(paths)
    reference = now or datetime.now(tz)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=tz)
    else:
        reference = reference.astimezone(tz)
    return (reference.date() - timedelta(days=1)).isoformat()


def _date_value(business_date: date | str | None, paths: RuntimePaths | None = None) -> str:
    if isinstance(business_date, date):
        return business_date.isoformat()
    return business_date if business_date is not None else default_business_date(paths=paths)


def _normalize_command_date(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if re.fullmatch(r"\d{6}", stripped):
        stripped = f"20{stripped[:2]}-{stripped[2:4]}-{stripped[4:6]}"
    try:
        return date.fromisoformat(stripped).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD or YYMMDD") from exc


def _acquire_daily_pipeline_lock(paths: RuntimePaths, date_str: str):
    lock_dir = paths.state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"daily-pipeline-{date_str}.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\ndate={date_str}\nstartedAt={datetime.now().astimezone().isoformat()}\n")
    handle.flush()
    return handle


def _release_daily_pipeline_lock(handle) -> None:
    lock_path = Path(handle.name)
    owner_marker = f"pid={os.getpid()}\n"
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    try:
        if lock_path.read_text(encoding="utf-8").startswith(owner_marker):
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _pipeline_failure_log_path(paths: RuntimePaths) -> Path:
    return paths.state_dir / "logs" / "pipeline-failures.jsonl"


def record_pipeline_failure(paths: RuntimePaths, *, business_date: str, failed_step: str, reason: str | None = None) -> dict:
    payload = {
        "businessDate": business_date,
        "failedStep": failed_step,
        "reason": sanitize_pipeline_error_summary(reason or failed_step),
        "createdAt": datetime.now().astimezone().isoformat(),
    }
    path = _pipeline_failure_log_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def _pipeline_run_kind(trigger: str) -> str:
    if trigger.startswith("history-backfill"):
        return "history_backfill"
    if trigger.startswith("manual"):
        return "manual"
    if trigger in {"scheduler", "launchd", "daily"}:
        return "daily"
    return str(trigger or "daily").replace("-", "_")


def _pipeline_requested_by(trigger: str) -> str:
    if trigger.startswith("history-backfill"):
        return "dashboard"
    if trigger.startswith("manual"):
        return "cli"
    if trigger in {"scheduler", "launchd", "daily"}:
        return "scheduler"
    return str(trigger or "scheduler")


@contextmanager
def _pipeline_llm_environment(pipeline_run_id: int, stage_id: str):
    """Expose parent-process LLM attribution without leaking it after the stage."""

    previous_run_id = os.environ.get(PIPELINE_RUN_ID_ENV)
    previous_stage_id = os.environ.get(PIPELINE_STAGE_ID_ENV)
    os.environ[PIPELINE_RUN_ID_ENV] = str(int(pipeline_run_id))
    os.environ[PIPELINE_STAGE_ID_ENV] = str(stage_id)
    try:
        yield
    finally:
        if previous_run_id is None:
            os.environ.pop(PIPELINE_RUN_ID_ENV, None)
        else:
            os.environ[PIPELINE_RUN_ID_ENV] = previous_run_id
        if previous_stage_id is None:
            os.environ.pop(PIPELINE_STAGE_ID_ENV, None)
        else:
            os.environ[PIPELINE_STAGE_ID_ENV] = previous_stage_id


def _pipeline_artifact_paths(paths: RuntimePaths, date_str: str, *, language_profile: str = "zh") -> dict:
    narrative = diary_report_paths(paths.diary_dir, date_str, "narrative", language_profile=language_profile)
    technical = diary_report_paths(paths.diary_dir, date_str, "technical", language_profile=language_profile)
    learning = diary_report_paths(paths.diary_dir, date_str, "learning", language_profile=language_profile)
    return {
        "narrative": [str(path) for path in narrative],
        "technical": [str(path) for path in technical],
        "learning": [str(path) for path in learning],
    }


_PRODUCTION_STAGE_IDS = {
    "unified_source_collector.py": "source-collection",
    "narrative_pass.py": "narrative",
    "technical_pass.py": "technical",
    "learning_pass.py": "learning",
    "rag_v2_sync.py": "rag-sync",
}


def _pipeline_step_id(step: PipelineStep) -> str:
    if step.stage_id:
        return str(step.stage_id)
    production_id = _PRODUCTION_STAGE_IDS.get(step.script.name)
    if production_id:
        return production_id
    normalized = re.sub(r"[^a-z0-9]+", "-", str(step.name or step.script.stem).casefold()).strip("-")
    return f"step-{normalized or 'unnamed'}"


def _pipeline_step_contract(step: PipelineStep) -> dict[str, Any]:
    encoded_args = json.dumps(list(step.args), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "stageId": _pipeline_step_id(step),
        "scriptName": step.script.name,
        "scriptSha256": _stable_file_sha256(step.script),
        "argsCount": len(step.args),
        "argsSha256": hashlib.sha256(encoded_args).hexdigest(),
    }


def _stable_file_sha256(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return None
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
    except OSError:
        return None
    before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_signature != after_signature or len(content) != after.st_size:
        return None
    return hashlib.sha256(content).hexdigest()


def _call_materializer(
    materializer: Callable[..., bool],
    date_str: str,
    paths: RuntimePaths,
    execution_context: PipelineExecutionContext,
) -> bool:
    """Call new context-aware materializers without breaking legacy two-arg callables."""
    try:
        signature = inspect.signature(materializer)
    except (TypeError, ValueError):
        return bool(materializer(date_str, paths))
    parameter = signature.parameters.get("execution_context")
    if parameter is not None:
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            return bool(materializer(date_str, paths, execution_context))
        return bool(materializer(date_str, paths, execution_context=execution_context))
    return bool(materializer(date_str, paths))


_NON_REUSABLE_PIPELINE_STAGES = {
    "final-rag-readiness",
    "pipeline-boundary",
}

_REPORT_TYPES_BY_STAGE = {
    "narrative": {"narrative"},
    "blank-narrative": {"narrative"},
    "technical": {"technical"},
    "learning": {"learning"},
}


def _pipeline_artifact_proof_map(
    paths: RuntimePaths,
    date_str: str,
    *,
    language_profile: str,
) -> dict[str, dict[str, Any]]:
    """Return stable hashes only for current, regular diary artifacts."""
    root = paths.diary_dir.resolve()
    proofs: dict[str, dict[str, Any]] = {}
    for report_type in ("narrative", "technical", "learning"):
        for path in diary_report_paths(root, date_str, report_type, language_profile=language_profile):
            try:
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                before = resolved.stat()
                content = resolved.read_bytes()
                after = resolved.stat()
            except (FileNotFoundError, OSError, ValueError):
                continue
            before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if before_signature != after_signature or len(content) != after.st_size:
                continue
            proofs[str(resolved)] = {
                "path": str(resolved),
                "sha256": hashlib.sha256(content).hexdigest(),
                "byteSize": int(after.st_size),
                "reportType": report_type,
            }
    return proofs


def _stage_artifact_proofs(
    stage_id: str,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    report_types = _REPORT_TYPES_BY_STAGE.get(stage_id)
    if report_types:
        selected = [proof for proof in after.values() if proof.get("reportType") in report_types]
    else:
        selected = [proof for path, proof in after.items() if before.get(path) != proof]
    return sorted(selected, key=lambda item: str(item.get("path") or ""))


def _proofs_are_current(
    proofs: object,
    current: dict[str, dict[str, Any]],
) -> bool:
    if not isinstance(proofs, list) or not proofs:
        return False
    for proof in proofs:
        if not isinstance(proof, dict):
            return False
        path = str(proof.get("path") or "")
        observed = current.get(path)
        if observed is None:
            return False
        if any(observed.get(key) != proof.get(key) for key in ("sha256", "byteSize", "reportType")):
            return False
    return True


def _retry_committed_stage_ids(
    parent_run: dict | None,
    *,
    paths: RuntimePaths,
    business_date: str,
    language_profile: str,
    nova_task_enabled: bool,
    step_manifest: list[str],
    step_contract: list[dict[str, Any]],
    pipeline_contract_hash: str | None,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    if parent_run is None:
        return "none", {}
    metadata = parent_run.get("metadata") if isinstance(parent_run.get("metadata"), dict) else {}
    if (
        metadata.get("stageContractVersion") != 2
        or str(metadata.get("languageProfile") or "") != language_profile
        or metadata.get("novaTaskEnabled") is not nova_task_enabled
        or metadata.get("stepManifest") != step_manifest
        or metadata.get("stepContract") != step_contract
        or not pipeline_contract_hash
        or metadata.get("pipelineContractHash") != pipeline_contract_hash
        or any(not item.get("scriptSha256") for item in step_contract)
    ):
        return "legacy-full", {}
    current_proofs = _pipeline_artifact_proof_map(
        paths,
        business_date,
        language_profile=language_profile,
    )
    outcomes: dict[str, tuple[str, bool, object]] = {}
    for item in parent_run.get("steps") or []:
        if not isinstance(item, dict):
            continue
        stage_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        stage_id = str(stage_metadata.get("stageId") or "")
        if not stage_id or stage_id in _NON_REUSABLE_PIPELINE_STAGES:
            continue
        outcomes[stage_id] = (
            str(item.get("status") or ""),
            stage_metadata.get("committed") is True,
            stage_metadata.get("artifactProofs"),
        )
    committed = {
        stage_id: list(proofs)
        for stage_id, (status, is_committed, proofs) in outcomes.items()
        if is_committed
        and status in {"completed", "skipped"}
        and _proofs_are_current(proofs, current_proofs)
    }
    return "native", committed


def latest_pipeline_failure(paths: RuntimePaths) -> dict | None:
    path = _pipeline_failure_log_path(paths)
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    for line in reversed(lines[-100:]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["reason"] = sanitize_pipeline_error_summary(payload.get("reason"))
            return payload
    return None


def _run_step(
    step: PipelineStep,
    date_str: str,
    runner: Runner,
    paths: RuntimePaths | None = None,
    *,
    timeout_override: float | None = None,
    pipeline_run_id: int | None = None,
    stage_id: str | None = None,
) -> StepExecutionResult:
    display_name = _pipeline_step_display_name(step)
    if not step.script.exists():
        _print_pipeline_status("[X]", display_name, "A required component is missing.")
        return StepExecutionResult(False, "script-not-found")
    if _is_blank_day_passthrough_step(step):
        llm_reason = _llm_provider_blocking_reason(paths)
        if llm_reason:
            _print_pipeline_status("[X]", display_name, "Check model settings and credentials.")
            return StepExecutionResult(False, llm_reason)
    args = [argument.replace("{date}", date_str) for argument in step.args]
    command = [sys.executable, str(step.script), *args]
    timeout = max(0.000001, float(timeout_override)) if timeout_override is not None else _step_timeout_seconds(step, paths)
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR)
        env.update(runtime_environment_overrides(paths))
        env.update(_pipeline_language_environment(paths))
        if pipeline_run_id is not None and stage_id:
            env[PIPELINE_RUN_ID_ENV] = str(int(pipeline_run_id))
            env[PIPELINE_STAGE_ID_ENV] = str(stage_id)
        result = runner(
            command,
            capture_output=True,
            text=True,
            env=env,
            cwd=step.script.parent,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timeout_text = _format_timeout_seconds(timeout)
        _print_pipeline_status("[X]", display_name, f"Timed out after {timeout_text}s.")
        return StepExecutionResult(False, f"timeout after {timeout_text}s")
    except Exception as exc:
        _print_pipeline_status("[X]", display_name, _exception_failure_detail(exc))
        return StepExecutionResult(False, str(exc))
    if result.returncode != 0:
        rag_reason = _rag_sync_failure_reason(step, result.stdout or "")
        detail = (
            "Search memory could not be updated."
            if rag_reason
            else _subprocess_failure_detail(result.stderr or result.stdout or "")
        )
        _print_pipeline_status("[X]", display_name, detail)
        return StepExecutionResult(False, rag_reason or f"exit {result.returncode}", result.stderr or result.stdout or "")
    rag_reason = _rag_sync_failure_reason(step, result.stdout or "")
    if rag_reason:
        _print_pipeline_status("[X]", display_name, "Search memory could not be updated.")
        return StepExecutionResult(False, rag_reason, result.stdout or "")
    _print_pipeline_status("[OK]", display_name)
    return StepExecutionResult(True, stdout=result.stdout or "")


def _pipeline_language_environment(paths: RuntimePaths | None = None) -> dict[str, str]:
    pipeline = resolve_pipeline_settings(paths)
    return {
        "ACTANARA_PIPELINE_LANGUAGE_PROFILE": str(pipeline.get("languageProfile") or "zh"),
        "ACTANARA_DIARY_SCHEMA_VERSION": str(pipeline.get("diarySchemaVersion") or "diary-v1-zh"),
        "ACTANARA_PROMPT_PAYLOAD_PROFILE": str(pipeline.get("promptPayloadProfile") or "zh-CN"),
        "ACTANARA_DISPLAY_LOCALE": str(pipeline.get("displayLocale") or "zh-CN"),
        "NOVA_RAG_LANGUAGE_PROFILE": str(pipeline.get("ragLanguageProfile") or "zh"),
        "LLM_THINKING_MODE": str(pipeline.get("thinkingMode") or "off"),
    }


def production_steps_for_language(language_profile: str | None = None) -> tuple[PipelineStep, ...]:
    return EN_PRODUCTION_STEPS if str(language_profile or "").strip() == "en" else ZH_PRODUCTION_STEPS


def _rag_sync_failure_reason(step: PipelineStep, stdout: str) -> str | None:
    if not _is_rag_sync_step(step):
        return None
    payload = _extract_json_object(stdout)
    if not payload:
        return None
    status = str(payload.get("status") or "")
    if status in {"promoted", "candidate-ready"}:
        return None
    if status == "skipped":
        return f"nova-RAG index sync skipped: {payload.get('reason') or 'unknown reason'}"
    if status:
        return f"nova-RAG index sync {status}: {payload.get('reason') or 'see step output'}"
    return None


def _extract_json_object(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _step_timeout_seconds(step: PipelineStep, paths: RuntimePaths | None = None) -> int:
    pipeline = resolve_pipeline_settings(paths)
    default_timeout = int(pipeline.get("stepTimeoutSeconds") or 1800)
    overrides = pipeline.get("stepTimeouts") if isinstance(pipeline.get("stepTimeouts"), dict) else {}
    for key in (step.script.name, step.name):
        try:
            value = int(overrides.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return _positive_timeout(default_timeout)


def _pipeline_total_timeout_seconds(paths: RuntimePaths | None = None) -> int:
    pipeline = resolve_pipeline_settings(paths)
    return _positive_timeout(pipeline.get("totalWatchdogSeconds") or 7200)


def _positive_timeout(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _format_timeout_seconds(value: float | int) -> str:
    parsed = float(value)
    if parsed.is_integer():
        return str(int(parsed))
    return f"{parsed:.3f}".rstrip("0").rstrip(".")


def _diary_foundation_enabled(paths: RuntimePaths | None = None) -> bool:
    return (
        resolve_runtime_source("DIARY_METRICS_SOURCE", paths) == "foundation"
        or resolve_runtime_source("DIARY_MEMORY_SOURCE", paths) == "foundation"
        or resolve_runtime_source("DIARY_TASKS_SOURCE", paths) == "foundation"
    )


def _is_narrative_step(step: PipelineStep) -> bool:
    return step.script.name == "narrative_pass.py"


def _is_collect_step(step: PipelineStep) -> bool:
    return step.script.name == "unified_source_collector.py"


def _is_source_collection_step(step: PipelineStep) -> bool:
    return step.script.name == "unified_source_collector.py"


def _is_technical_step(step: PipelineStep) -> bool:
    return step.script.name == "technical_pass.py"


def _is_rag_sync_step(step: PipelineStep) -> bool:
    return step.script.name == "rag_v2_sync.py"


def _is_blank_day_passthrough_step(step: PipelineStep) -> bool:
    return _is_narrative_step(step) or _is_technical_step(step) or step.script.name == "learning_pass.py"


def _llm_provider_blocking_reason(paths: RuntimePaths | None = None) -> str | None:
    return llm_provider_readiness_error(paths, require_cross_process_secret=True)


def _is_blank_day_after_collect(date_str: str, paths: RuntimePaths) -> bool:
    return _non_cron_filtered_entry_count(date_str, paths) == 0


def _non_cron_filtered_entry_count(date_str: str, paths: RuntimePaths) -> int:
    base_dir = paths.diary_dir / "__diary_daily" / date_str / "_filtered"
    if not base_dir.exists():
        return 0
    count = 0
    for source_dir in sorted(path for path in base_dir.iterdir() if path.is_dir()):
        if source_dir.name == "cron":
            continue
        for jsonl_path in sorted(source_dir.glob("*.jsonl")):
            try:
                with jsonl_path.open("r", encoding="utf-8") as handle:
                    count += sum(1 for line in handle if line.strip())
            except OSError:
                continue
    return count


def materialize_blank_day_narrative(date_str: str, paths: RuntimePaths | None = None) -> bool:
    selected = paths or load_paths()
    pipeline = resolve_pipeline_settings(selected)
    language_profile = str(pipeline.get("languageProfile") or "zh").lower()
    try:
        if language_profile == "en":
            from diary_generator.en import narrative_pass

            narrative_pass.write_blank_day_report(date_str, selected.diary_dir)
        else:
            from diary_generator import narrative_pass

            narrative_pass.write_blank_day_report(date_str, selected.diary_dir)
        return True
    except Exception:
        _print_pipeline_status("[X]", "Generate diary", "The no-activity diary could not be created.")
        return False


def prepare_blank_day_foundation_inputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Create the minimum Foundation metric marker needed for a no-activity diary."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    run_id = None
    try:
        migrate(selected)
        run_id = begin_ingestion_run(
            selected,
            trigger_type="pipeline-blank-day-inputs",
            business_date=target,
            adapter_versions={
                "projection": "blank-day-foundation-inputs-v1",
                "businessDate": target.isoformat(),
            },
            status="running",
        )
        refresh_daily_usage(selected, target, run_id)
        finish_ingestion_run(selected, run_id, status="completed")
        return True
    except Exception as exc:
        if run_id is not None:
            finish_ingestion_run(selected, run_id, status="failed", error_summary=str(exc))
        _print_pipeline_status("[X]", "Prepare diary", "The no-activity diary could not be prepared.")
        return False


def _skip_final_rag_reason(paths: RuntimePaths | None = None) -> str | None:
    pipeline = resolve_pipeline_settings(paths)
    if _truthy(pipeline.get("skipFinalRag")):
        return f"{pipeline.get('skipFinalRagEnv') or 'ACTANARA_PIPELINE_SKIP_FINAL_RAG'} override is set"
    if is_rag_product_enabled is not None:
        try:
            if not is_rag_product_enabled(paths=paths):
                if rag_product_disabled_reason is not None:
                    return rag_product_disabled_reason(paths=paths) or "nova-RAG subsystem is disabled by settings."
                return "nova-RAG subsystem is disabled by settings."
        except Exception:
            _print_pipeline_status("[!]", "Update search memory", "Readiness could not be checked; continuing.")
    return None


def _final_rag_readiness_blocking_reason(paths: RuntimePaths | None = None) -> str | None:
    if resolve_rag_runtime_settings is None or plan_v2_production_sync is None:
        return None
    try:
        settings = resolve_rag_runtime_settings(paths)
        plan = plan_v2_production_sync(settings, action="daily-pipeline-final-rag-sync", requested_by="daily-pipeline")
    except Exception as exc:
        return f"nova-RAG final sync readiness preflight failed: {exc}"
    if plan.get("canExecute"):
        return None
    blockers = plan.get("blockers") if isinstance(plan.get("blockers"), list) else []
    if blockers:
        return "; ".join(
            str((blocker or {}).get("message") or (blocker or {}).get("code") or "unknown blocker")
            for blocker in blockers
            if isinstance(blocker, dict)
        ) or "nova-RAG final sync is not ready"
    return str(plan.get("reason") or "nova-RAG final sync is not ready")


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "skip"}


def _technical_report_path(date_str: str, paths: RuntimePaths | None = None) -> Path:
    selected = paths or load_paths()
    pipeline = resolve_pipeline_settings(selected)
    language_profile = str(pipeline.get("languageProfile") or "zh")
    root = selected.diary_dir
    existing = diary_report_paths(Path(root), date_str, "technical", language_profile=language_profile)
    return existing[0] if existing else diary_technical_report_path(Path(root), date_str, language_profile=language_profile)


def materialize_nova_task_outputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Run Nova-Task work-graph reconciliation after technical pass and export projection."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    try:
        report_path = _technical_report_path(date_str, selected)
        if not report_path.exists():
            _print_pipeline_status("[!]", "Refresh tasks", "Diary details are not ready.")
            return False
        reconcile_workspace_project_anchors(selected)
        result = run_work_graph_reconciliation(
            selected,
            business_date=target,
            apply=True,
            auto_confirm_non_l1=True,
            technical_report_path=report_path,
        )
        if getattr(result, "response_malformed", False):
            _print_pipeline_status("[!]", "Refresh tasks", "Generated task updates could not be read.")
            return False
        export_task_board_markdown(selected)
        return True
    except Exception:
        _print_pipeline_status("[!]", "Refresh tasks", "Task updates could not be refreshed.")
        return False


def prepare_diary_foundation_inputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Materialize and gate selected diary Foundation readers before narrative assembly."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    try:
        result = run_shadow_ingestion(
            selected,
            target,
            trigger="pipeline-diary-pre-materialization",
            observe_assets=False,
        )
        if result.errors:
            _print_pipeline_status("[X]", "Prepare diary", "Activity data could not be prepared.")
            return False
        materialize_workspace_attribution_catalog(selected)
        if resolve_runtime_source("DIARY_MEMORY_SOURCE", selected) == "foundation":
            materialize_diary_memory_snapshot(selected, target, result.run_id)
            memory = write_diary_memory_readiness_report(selected, target)
            if not memory["canEnable"]["diaryMemorySourceFoundation"]:
                _print_pipeline_status("[X]", "Prepare diary", "Activity history is not ready.")
                return False
        if resolve_runtime_source("DIARY_METRICS_SOURCE", selected) == "foundation":
            metrics = write_diary_metrics_readiness_report(
                selected,
                target,
                approve_model_usage_normalization=True,
                approve_session_count_normalization=True,
            )
            if not metrics["canEnable"]["diaryMetricsSourceFoundation"]:
                if _is_soft_diary_metrics_readiness_failure(metrics):
                    _print_pipeline_status("[!]", "Prepare diary", "Some activity totals may be incomplete.")
                else:
                    _print_pipeline_status("[X]", "Prepare diary", "Activity totals are not ready.")
                    return False
        if resolve_runtime_source("DIARY_TASKS_SOURCE", selected) == "foundation":
            materialize_diary_tasks_snapshot(
                selected,
                target,
                result.run_id,
            )
            tasks = write_diary_tasks_readiness_report(
                selected,
                target,
                approve_checkbox_normalization=True,
            )
            if not tasks["canEnable"]["diaryTasksSourceFoundation"]:
                _print_pipeline_status("[X]", "Prepare diary", "Task data is not ready.")
                return False
        return True
    except Exception:
        _print_pipeline_status("[X]", "Prepare diary", "Diary data could not be prepared.")
        return False


def prepare_existing_diary_foundation_inputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Gate already-materialized Foundation diary inputs without re-reading source facts."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    try:
        migrate(selected)
        metrics_ready = daily_diary_usage_metrics(selected, target)
        if metrics_ready is None:
            _print_pipeline_status("[X]", "Prepare diary", "Saved activity totals are missing.")
            return False
        materialize_workspace_attribution_catalog(selected)
        if resolve_runtime_source("DIARY_MEMORY_SOURCE", selected) == "foundation":
            memory = write_diary_memory_readiness_report(selected, target)
            if not memory["canEnable"]["diaryMemorySourceFoundation"]:
                _print_pipeline_status("[X]", "Prepare diary", "Saved activity history is not ready.")
                return False
        if resolve_runtime_source("DIARY_METRICS_SOURCE", selected) == "foundation":
            metrics = write_diary_metrics_readiness_report(
                selected,
                target,
                approve_model_usage_normalization=True,
                approve_session_count_normalization=True,
            )
            if not metrics["canEnable"]["diaryMetricsSourceFoundation"]:
                if _is_soft_diary_metrics_readiness_failure(metrics):
                    _print_pipeline_status("[!]", "Prepare diary", "Some saved activity totals may be incomplete.")
                else:
                    _print_pipeline_status("[X]", "Prepare diary", "Saved activity totals are not ready.")
                    return False
        if resolve_runtime_source("DIARY_TASKS_SOURCE", selected) == "foundation":
            tasks = write_diary_tasks_readiness_report(
                selected,
                target,
                approve_checkbox_normalization=True,
            )
            if not tasks["canEnable"]["diaryTasksSourceFoundation"]:
                _print_pipeline_status("[X]", "Prepare diary", "Saved task data is not ready.")
                return False
        return True
    except Exception:
        _print_pipeline_status("[X]", "Prepare diary", "Saved diary data could not be prepared.")
        return False


def _is_soft_diary_metrics_readiness_failure(report: dict) -> bool:
    """Allow diary generation to continue when only metric parity drift is blocking."""
    return str(report.get("status") or "") == "table_metrics_mismatch"


def materialize_pipeline_foundation_outputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Materialize Dashboard/Foundation read models after generated Markdown exists."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    try:
        run_pipeline_daily_materialization(selected, target)
        return True
    except Exception:
        _print_pipeline_status("[X]", "Prepare diary", "Diary views could not be refreshed.")
        return False


def materialize_blank_day_pipeline_outputs(date_str: str, paths: RuntimePaths | None = None) -> bool:
    """Materialize only daily read models for no-activity days."""
    target = date.fromisoformat(date_str)
    selected = paths or load_paths()
    try:
        run_pipeline_blank_day_materialization(selected, target)
        return True
    except Exception:
        _print_pipeline_status("[X]", "Prepare diary", "Diary views could not be refreshed.")
        return False


def sync_local_memory_after_pipeline(paths: RuntimePaths | None = None) -> dict[str, Any]:
    """Refresh lexical recall after materialization without making it a pipeline gate."""
    selected = paths or load_paths()
    settings = resolve_memory_search_settings(selected)
    local = settings.get("local") if isinstance(settings.get("local"), dict) else {}
    if (
        settings.get("enabled") is not True
        or local.get("enabled") is not True
        or local.get("syncAfterPipeline") is not True
    ):
        return {
            "status": "skipped",
            "available": False,
            "reason": "local-memory-pipeline-sync-disabled",
        }
    try:
        from .local_memory_search import sync_local_memory_index

        return sync_local_memory_index(selected)
    except Exception as exc:
        return {
            "status": "degraded",
            "available": False,
            "reason": f"local-memory-pipeline-sync-failed:{exc.__class__.__name__}",
            "error": str(exc),
        }


def run_daily_pipeline(
    business_date: date | str | None = None,
    *,
    paths: RuntimePaths | None = None,
    trigger: str = "manual",
    steps: Sequence[PipelineStep] = PRODUCTION_STEPS,
    runner: Runner = subprocess.run,
    pre_materializer: PreMaterializer = prepare_diary_foundation_inputs,
    nova_task_materializer: NovaTaskMaterializer = materialize_nova_task_outputs,
    post_materializer: PostMaterializer = materialize_pipeline_foundation_outputs,
    reuse_foundation_inputs: bool = False,
    retry_of_run_id: int | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> PipelineRunResult:
    """Execute production steps and keep daily Foundation projections current."""
    if trigger in {"history-backfill-frozen", "manual-regeneration-frozen"}:
        reuse_foundation_inputs = True
    selected = paths
    if selected is None:
        selected = load_paths()
    target_date = _date_value(business_date, selected)
    pipeline_settings = resolve_pipeline_settings(selected)
    active_steps = (
        production_steps_for_language(str(pipeline_settings.get("languageProfile") or "zh"))
        if steps is PRODUCTION_STEPS
        else tuple(steps)
    )
    language_profile = str(pipeline_settings.get("languageProfile") or "zh")
    nova_task_enabled = is_nova_task_enabled(selected)
    step_manifest = [_pipeline_step_id(step) for step in active_steps]
    if any(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", stage_id) is None for stage_id in step_manifest):
        raise ValueError("pipeline stage IDs must use 1-64 lowercase letters, digits, or hyphens")
    if len(step_manifest) != len(set(step_manifest)):
        raise ValueError("pipeline stage IDs must be unique")
    step_contract = [_pipeline_step_contract(step) for step in active_steps]
    pipeline_contract_hash = _stable_file_sha256(Path(__file__))
    total_timeout = _pipeline_total_timeout_seconds(selected)
    parent_run = pipeline_run_by_id(selected, retry_of_run_id) if retry_of_run_id is not None else None
    if retry_of_run_id is not None:
        if parent_run is None:
            raise ValueError("retry pipeline run not found")
        if parent_run.get("businessDate") != target_date:
            raise ValueError("retry pipeline run business date does not match")
        if parent_run.get("status") not in {"failed", "partial"}:
            raise ValueError("retry pipeline run must be failed or partial")
    retry_mode, committed_retry_stages = _retry_committed_stage_ids(
        parent_run,
        paths=selected,
        business_date=target_date,
        language_profile=language_profile,
        nova_task_enabled=nova_task_enabled,
        step_manifest=step_manifest,
        step_contract=step_contract,
        pipeline_contract_hash=pipeline_contract_hash,
    )
    print(f"Actanara · Daily diary · {target_date}")
    language_block_reason = _pipeline_language_blocking_reason(pipeline_settings)
    if language_block_reason:
        _print_pipeline_status("[X]", "Generate diary", "English output is not enabled.")
        _print_pipeline_status("[X]", "Daily diary stopped", "Check language settings.")
        record_pipeline_failure(
            selected,
            business_date=target_date,
            failed_step="Pipeline Language Profile",
            reason=language_block_reason,
        )
        return PipelineRunResult(target_date, 0, len(active_steps), False, "Pipeline Language Profile")
    lock_handle = _acquire_daily_pipeline_lock(selected, target_date)
    if lock_handle is None:
        _print_pipeline_status("[X]", "Daily diary", "A run for this date is already in progress.")
        record_pipeline_failure(selected, business_date=target_date, failed_step="Pipeline Run Lock")
        return PipelineRunResult(target_date, 0, len(active_steps), False, "Pipeline Run Lock")
    ledger_run_id: int | None = None
    execution_context: PipelineExecutionContext | None = None
    succeeded = 0
    blank_day_fast_path = False
    source_collection_completed = False
    try:
        ledger_run_id = create_pipeline_run(
            selected,
            business_date=target_date,
            run_kind=_pipeline_run_kind(trigger),
            requested_by=_pipeline_requested_by(trigger),
            retry_of_run_id=retry_of_run_id,
            metadata={
                "trigger": trigger,
                "reuseFoundationInputs": reuse_foundation_inputs,
                "stageContractVersion": 2,
                "languageProfile": language_profile,
                "novaTaskEnabled": nova_task_enabled,
                "stepManifest": step_manifest,
                "stepContract": step_contract,
                "pipelineContractHash": pipeline_contract_hash,
                "retryMode": retry_mode,
            },
        )
        execution_context = PipelineExecutionContext.start(
            total_timeout,
            monotonic_clock=monotonic_clock,
            cancellation_requested=cancellation_requested,
        )

        def artifact_paths() -> dict[str, Any]:
            return _pipeline_artifact_paths(
                selected,
                target_date,
                language_profile=language_profile,
            )

        def artifact_proof_map() -> dict[str, dict[str, Any]]:
            return _pipeline_artifact_proof_map(
                selected,
                target_date,
                language_profile=language_profile,
            )

        reused_stage_proofs: dict[str, list[dict[str, Any]]] = {}

        def append_stage(
            *,
            name: str,
            stage_id: str,
            status: str,
            committed: bool,
            reason: str | None = None,
            metadata: dict[str, Any] | None = None,
            artifact_proofs: list[dict[str, Any]] | None = None,
            started_at: str | None = None,
            completed_at: str | None = None,
            duration_seconds: float | None = None,
        ) -> None:
            observed_completed_at = completed_at or datetime.now().astimezone().isoformat()
            observed_started_at = started_at or observed_completed_at
            observed_duration = 0.0 if duration_seconds is None else max(0.0, float(duration_seconds))
            proofs = list(artifact_proofs or [])
            durable_commit = bool(committed and proofs)
            stage_metadata: dict[str, Any] = dict(metadata or {})
            stage_metadata.update({
                "stageId": stage_id,
                "committed": durable_commit,
                "artifactProofs": proofs,
            })
            if proofs:
                stage_metadata["artifactPaths"] = [proof["path"] for proof in proofs]
            append_pipeline_step(
                selected,
                ledger_run_id,
                name=name,
                status=status,
                reason=reason,
                metadata=stage_metadata,
                started_at=observed_started_at,
                completed_at=observed_completed_at,
                duration_seconds=observed_duration,
            )

        def reuse_stage(stage_id: str, name: str) -> bool:
            if stage_id not in committed_retry_stages:
                return False
            proofs = committed_retry_stages[stage_id]
            if not _proofs_are_current(proofs, artifact_proof_map()):
                return False
            reused_stage_proofs[stage_id] = proofs
            reason = f"reusing committed stage from pipeline run {retry_of_run_id}"
            _print_pipeline_status("[-]", _pipeline_stage_display_name(name), "Reused from the previous attempt.")
            append_stage(
                name=name,
                stage_id=stage_id,
                status="skipped",
                committed=True,
                reason=reason,
                metadata={"reusedFromRunId": retry_of_run_id},
                artifact_proofs=proofs,
            )
            return True

        def fail_run(
            *,
            failed_step: str,
            failure_class: str,
            reason: str | None = None,
            stage_id: str,
            append_outcome: bool = True,
            stage_metadata: dict[str, Any] | None = None,
        ) -> PipelineRunResult:
            failure_reason = reason or failed_step
            record_pipeline_failure(
                selected,
                business_date=target_date,
                failed_step=failed_step,
                reason=failure_reason,
            )
            if append_outcome:
                append_stage(
                    name=failed_step,
                    stage_id=stage_id,
                    status="failed",
                    committed=False,
                    reason=failure_reason,
                    metadata=stage_metadata,
                )
            finish_pipeline_run_if_status(
                selected,
                ledger_run_id,
                expected_statuses={"running"},
                status="failed",
                failure_class=failure_class,
                error_summary=failure_reason,
                artifact_paths=artifact_paths(),
                metadata={
                    "blankDayFastPath": blank_day_fast_path,
                    "durationSeconds": execution_context.elapsed_seconds(),
                },
            )
            display_name = _pipeline_stage_display_name(failed_step)
            _print_pipeline_status("[X]", "Daily diary stopped", f"{display_name} could not finish.")
            return PipelineRunResult(target_date, succeeded, len(active_steps), False, failed_step)

        execution_context.checkpoint("pipeline-start")
        for step in active_steps:
            stage_id = _pipeline_step_id(step)
            execution_context.checkpoint(f"{stage_id}:before")
            if reuse_foundation_inputs and _is_source_collection_step(step):
                _print_pipeline_status("[-]", "Collect activity", "Using saved activity.")
                append_stage(
                    name=step.name,
                    stage_id=stage_id,
                    status="skipped",
                    committed=True,
                    reason="reusing frozen Foundation inputs",
                )
                execution_context.checkpoint(f"{stage_id}:after")
                source_collection_completed = True
                succeeded += 1
                continue
            if _is_narrative_step(step) and _diary_foundation_enabled(selected):
                foundation_preparer = prepare_existing_diary_foundation_inputs if reuse_foundation_inputs else pre_materializer
                blank_day_fast_path = (source_collection_completed or reuse_foundation_inputs) and _is_blank_day_after_collect(target_date, selected)
                input_stage_id = "blank-inputs" if blank_day_fast_path else "foundation-inputs"
                input_name = "Blank Day Foundation Inputs" if blank_day_fast_path else "Foundation Diary Inputs"
                execution_context.checkpoint(f"{input_stage_id}:before")
                inputs_reused = reuse_stage(input_stage_id, input_name)
                if inputs_reused:
                    inputs_ready = True
                else:
                    input_materializer = prepare_blank_day_foundation_inputs if blank_day_fast_path else foundation_preparer
                    input_artifacts_before = artifact_proof_map()
                    input_started_at = datetime.now().astimezone().isoformat()
                    input_started_monotonic = monotonic_clock()
                    inputs_ready = _call_materializer(input_materializer, target_date, selected, execution_context)
                    input_completed_at = datetime.now().astimezone().isoformat()
                    input_duration = max(0.0, float(monotonic_clock()) - float(input_started_monotonic))
                    input_artifacts_after = artifact_proof_map()
                    append_stage(
                        name=input_name,
                        stage_id=input_stage_id,
                        status="completed" if inputs_ready else "failed",
                        committed=inputs_ready,
                        artifact_proofs=_stage_artifact_proofs(
                            input_stage_id,
                            input_artifacts_before,
                            input_artifacts_after,
                        ),
                        started_at=input_started_at,
                        completed_at=input_completed_at,
                        duration_seconds=input_duration,
                    )
                execution_context.checkpoint(f"{input_stage_id}:after")
                if not inputs_ready:
                    return fail_run(
                        failed_step="Foundation Diary Inputs",
                        failure_class="data_missing",
                        stage_id=input_stage_id,
                        append_outcome=False,
                    )
                if not inputs_reused:
                    detail = "No activity found." if blank_day_fast_path else None
                    _print_pipeline_status("[OK]", "Prepare diary", detail)
                if blank_day_fast_path:
                    execution_context.checkpoint("blank-narrative:before")
                    blank_narrative_reused = reuse_stage("blank-narrative", "Blank Day Narrative")
                    if blank_narrative_reused:
                        blank_narrative_ready = True
                    else:
                        blank_narrative_artifacts_before = artifact_proof_map()
                        blank_narrative_started_at = datetime.now().astimezone().isoformat()
                        blank_narrative_started_monotonic = monotonic_clock()
                        blank_narrative_ready = _call_materializer(
                            materialize_blank_day_narrative,
                            target_date,
                            selected,
                            execution_context,
                        )
                        blank_narrative_completed_at = datetime.now().astimezone().isoformat()
                        blank_narrative_duration = max(
                            0.0,
                            float(monotonic_clock()) - float(blank_narrative_started_monotonic),
                        )
                        blank_narrative_artifacts_after = artifact_proof_map()
                        append_stage(
                            name="Blank Day Narrative",
                            stage_id="blank-narrative",
                            status="completed" if blank_narrative_ready else "failed",
                            committed=blank_narrative_ready,
                            artifact_proofs=_stage_artifact_proofs(
                                "blank-narrative",
                                blank_narrative_artifacts_before,
                                blank_narrative_artifacts_after,
                            ),
                            started_at=blank_narrative_started_at,
                            completed_at=blank_narrative_completed_at,
                            duration_seconds=blank_narrative_duration,
                        )
                    execution_context.checkpoint("blank-narrative:after")
                    if not blank_narrative_ready:
                        return fail_run(
                            failed_step="Blank Day Narrative",
                            failure_class="blank_day_policy",
                            stage_id="blank-narrative",
                            append_outcome=False,
                        )
                    if not blank_narrative_reused:
                        _print_pipeline_status("[OK]", "Generate diary", "No activity to summarize.")
                    _print_pipeline_status("[-]", "Refresh tasks", "No activity to update.")
            if blank_day_fast_path and _is_blank_day_passthrough_step(step):
                append_stage(
                    name=step.name,
                    stage_id=stage_id,
                    status="skipped",
                    committed=True,
                    reason="blank day fast path",
                )
                execution_context.checkpoint(f"{stage_id}:after")
                succeeded += 1
                continue
            if _is_rag_sync_step(step):
                post_stage_id = "blank-foundation-materialization" if blank_day_fast_path else "foundation-materialization"
                post_name = "Blank-Day Foundation Materialization" if blank_day_fast_path else "Pipeline Foundation Materialization"
                execution_context.checkpoint(f"{post_stage_id}:before")
                if reuse_stage(post_stage_id, post_name):
                    post_ready = True
                else:
                    selected_post_materializer = materialize_blank_day_pipeline_outputs if blank_day_fast_path else post_materializer
                    post_artifacts_before = artifact_proof_map()
                    post_started_at = datetime.now().astimezone().isoformat()
                    post_started_monotonic = monotonic_clock()
                    post_ready = _call_materializer(selected_post_materializer, target_date, selected, execution_context)
                    post_completed_at = datetime.now().astimezone().isoformat()
                    post_duration = max(0.0, float(monotonic_clock()) - float(post_started_monotonic))
                    post_artifacts_after = artifact_proof_map()
                    append_stage(
                        name=post_name,
                        stage_id=post_stage_id,
                        status="completed" if post_ready else "failed",
                        committed=post_ready,
                        artifact_proofs=_stage_artifact_proofs(
                            post_stage_id,
                            post_artifacts_before,
                            post_artifacts_after,
                        ),
                        started_at=post_started_at,
                        completed_at=post_completed_at,
                        duration_seconds=post_duration,
                    )
                execution_context.checkpoint(f"{post_stage_id}:after")
                if not post_ready:
                    return fail_run(
                        failed_step="Pipeline Foundation Materialization",
                        failure_class="internal_error",
                        stage_id=post_stage_id,
                        append_outcome=False,
                    )
                local_memory_sync = sync_local_memory_after_pipeline(selected)
                if local_memory_sync.get("status") == "degraded":
                    _print_pipeline_status(
                        "[!]",
                        "Update local memory",
                        "Continuing with bounded lexical fallback.",
                    )
                if blank_day_fast_path:
                    _print_pipeline_status("[-]", "Update search memory", "No new activity to index.")
                    append_stage(
                        name=step.name,
                        stage_id=stage_id,
                        status="skipped",
                        committed=True,
                        reason="blank day fast path",
                    )
                    execution_context.checkpoint(f"{stage_id}:after")
                    succeeded += 1
                    continue
                skip_reason = _skip_final_rag_reason(selected)
                execution_context.checkpoint("rag-skip-readiness:after")
                if skip_reason:
                    skip_detail = "Not enabled." if "disabled" in skip_reason.casefold() else "Skipped by settings."
                    _print_pipeline_status("[-]", "Update search memory", skip_detail)
                    append_stage(
                        name=step.name,
                        stage_id=stage_id,
                        status="skipped",
                        committed=True,
                        reason=skip_reason,
                    )
                    execution_context.checkpoint(f"{stage_id}:after")
                    succeeded += 1
                    continue
                execution_context.checkpoint("final-rag-readiness:before")
                if reuse_stage("final-rag-readiness", "Final RAG Sync Readiness"):
                    readiness_reason = None
                else:
                    readiness_reason = _final_rag_readiness_blocking_reason(selected)
                    append_stage(
                        name="Final RAG Sync Readiness",
                        stage_id="final-rag-readiness",
                        status="failed" if readiness_reason else "completed",
                        committed=False,
                        reason=readiness_reason,
                    )
                execution_context.checkpoint("final-rag-readiness:after")
                if readiness_reason:
                    _print_pipeline_status("[X]", "Update search memory", "Search memory is not ready.")
                    return fail_run(
                        failed_step="Final RAG Sync Readiness",
                        failure_class=classify_pipeline_failure(readiness_reason),
                        reason=readiness_reason,
                        stage_id="final-rag-readiness",
                        append_outcome=False,
                    )
            if reuse_stage(stage_id, step.name):
                step_result = StepExecutionResult(True)
            else:
                step_artifacts_before = artifact_proof_map()
                step_started_at = datetime.now().astimezone().isoformat()
                step_started_monotonic = monotonic_clock()
                step_timeout = execution_context.bounded_timeout(
                    float(_step_timeout_seconds(step, selected)),
                    checkpoint=f"{stage_id}:subprocess-start",
                )
                step_result = _run_step(
                    step,
                    target_date,
                    runner,
                    selected,
                    timeout_override=step_timeout,
                    pipeline_run_id=ledger_run_id,
                    stage_id=stage_id,
                )
                step_completed_at = datetime.now().astimezone().isoformat()
                step_duration = max(0.0, float(monotonic_clock()) - float(step_started_monotonic))
                step_artifacts_after = artifact_proof_map()
                append_stage(
                    name=step.name,
                    stage_id=stage_id,
                    status="completed" if step_result.success else "failed",
                    committed=step_result.success,
                    reason=step_result.reason,
                    artifact_proofs=_stage_artifact_proofs(
                        stage_id,
                        step_artifacts_before,
                        step_artifacts_after,
                    ),
                    started_at=step_started_at,
                    completed_at=step_completed_at,
                    duration_seconds=step_duration,
                )
                execution_context.checkpoint(f"{stage_id}:after")
                if not step_result.success:
                    failure_text = " ".join(part for part in (step_result.reason, step_result.stdout) if part)
                    return fail_run(
                        failed_step=step.name,
                        failure_class=classify_pipeline_failure(failure_text),
                        reason=step_result.reason or step.name,
                        stage_id=stage_id,
                        append_outcome=False,
                    )
            succeeded += 1
            if _is_collect_step(step):
                source_collection_completed = True
            if _is_technical_step(step):
                execution_context.checkpoint("nova-task:before")
                nova_task_reused = reuse_stage("nova-task", "Nova-Task Work Graph")
                if nova_task_reused:
                    nova_task_ready = True
                elif not nova_task_enabled:
                    _print_pipeline_status("[-]", "Refresh tasks", "Not enabled.")
                    append_stage(
                        name="Nova-Task Work Graph",
                        stage_id="nova-task",
                        status="skipped",
                        committed=True,
                        reason="Nova-Task subsystem is disabled by settings",
                    )
                    nova_task_ready = True
                else:
                    nova_task_artifacts_before = artifact_proof_map()
                    nova_task_started_at = datetime.now().astimezone().isoformat()
                    nova_task_started_monotonic = monotonic_clock()
                    with _pipeline_llm_environment(ledger_run_id, "nova-task"):
                        nova_task_ready = _call_materializer(
                            nova_task_materializer,
                            target_date,
                            selected,
                            execution_context,
                        )
                    nova_task_completed_at = datetime.now().astimezone().isoformat()
                    nova_task_duration = max(
                        0.0,
                        float(monotonic_clock()) - float(nova_task_started_monotonic),
                    )
                    nova_task_artifacts_after = artifact_proof_map()
                    append_stage(
                        name="Nova-Task Work Graph",
                        stage_id="nova-task",
                        status="completed" if nova_task_ready else "failed",
                        committed=nova_task_ready,
                        metadata={"nonFatal": not nova_task_ready},
                        artifact_proofs=_stage_artifact_proofs(
                            "nova-task",
                            nova_task_artifacts_before,
                            nova_task_artifacts_after,
                        ),
                        started_at=nova_task_started_at,
                        completed_at=nova_task_completed_at,
                        duration_seconds=nova_task_duration,
                    )
                execution_context.checkpoint("nova-task:after")
                if nova_task_ready and nova_task_enabled and not nova_task_reused:
                    _print_pipeline_status("[OK]", "Refresh tasks")
                elif not nova_task_ready:
                    _print_pipeline_status("[!]", "Refresh tasks", "Continuing without task updates.")
        completed_artifacts = artifact_paths()
        current_artifact_proofs = artifact_proof_map()
        invalid_reused_stages = sorted(
            stage_id
            for stage_id, proofs in reused_stage_proofs.items()
            if not _proofs_are_current(proofs, current_artifact_proofs)
        )
        if invalid_reused_stages:
            execution_context.checkpoint("artifact-validation")
            return fail_run(
                failed_step="Pipeline Artifact Validation",
                failure_class="data_missing",
                reason="reused committed artifact changed before terminal commit",
                stage_id="artifact-validation",
                stage_metadata={"invalidReusedStageIds": invalid_reused_stages},
            )
        terminal_remaining = execution_context.remaining_at_checkpoint("pipeline-terminal-commit")
        duration = execution_context.total_timeout_seconds - terminal_remaining
        finalized = finish_pipeline_run_if_status(
            selected,
            ledger_run_id,
            expected_statuses={"running"},
            status="skipped" if blank_day_fast_path else "completed",
            artifact_paths=completed_artifacts,
            metadata={"blankDayFastPath": blank_day_fast_path, "durationSeconds": duration},
        )
        if not finalized:
            terminal = pipeline_run_by_id(selected, ledger_run_id)
            if terminal is None or terminal.get("status") not in {"completed", "skipped"}:
                _print_pipeline_status("[X]", "Daily diary stopped", "The final status could not be saved.")
                return PipelineRunResult(
                    target_date,
                    succeeded,
                    len(active_steps),
                    False,
                    "Pipeline Terminal State",
                )
        _print_pipeline_status("[OK]", "Daily diary complete", f"{duration:.1f}s")
        return PipelineRunResult(target_date, succeeded, len(active_steps), True)
    except PipelineExecutionBoundary as boundary:
        if ledger_run_id is None or execution_context is None:
            raise
        failed_step = "Daily Pipeline Cancellation" if boundary.failure_class == "cancelled" else "Daily Pipeline Timeout"
        boundary_detail = "The run was cancelled." if boundary.failure_class == "cancelled" else "The run timed out."
        _print_pipeline_status("[X]", "Daily diary", boundary_detail)
        return fail_run(
            failed_step=failed_step,
            failure_class=boundary.failure_class,
            reason=boundary.reason,
            stage_id="pipeline-boundary",
            stage_metadata={"checkpoint": boundary.checkpoint},
        )
    finally:
        _release_daily_pipeline_lock(lock_handle)


def _pipeline_language_blocking_reason(pipeline: dict[str, object] | RuntimePaths | None = None) -> str | None:
    if isinstance(pipeline, dict):
        settings = pipeline
    else:
        settings = resolve_pipeline_settings(pipeline)
    language_profile = str(settings.get("languageProfile") or "zh")
    if language_profile != "en":
        return None
    if _truthy(settings.get("englishEnabled")):
        return None
    return (
        "Pipeline language profile en is selected, but pipeline.englishEnabled is false; "
        "English diary pass execution requires an explicit runtime gate."
    )


def command_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Actanara daily production pipeline.")
    parser.add_argument("date", nargs="?", type=_normalize_command_date, help="Business date, YYYY-MM-DD or YYMMDD.")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    result = run_daily_pipeline(args.date)
    return 0 if result.success else 1
