"""In-process Dashboard scheduler for Foundation snapshot refresh."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from data_foundation.paths import load_paths
from data_foundation.scheduler_preview import (
    _launch_agent_path,
    _launchd_jobs,
    _launchd_runtime_status,
    _linux_timer_jobs,
    preview_system_timer as _preview_system_timer,
)
from data_foundation.settings import (
    read_scheduler_state,
    read_settings,
    write_scheduler_handoff_settings,
    write_scheduler_state,
    write_settings,
)
from data_foundation.settings_transaction import recover_settings_transactions
from data_foundation.systemd_user import (
    SystemdUserError,
    UserUnit,
    finalize_user_unit_transaction,
    install_user_units,
    rollback_user_unit_transaction,
    scheduler_units,
    uninstall_user_units,
)
from data_foundation.time import resolve_timezone_name

from . import backups, foundation

logger = logging.getLogger("dashboard.scheduler")

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
MAX_BACKFILL_DAYS = 366
SCHEDULER_INSTALL_CONFIRMATION = "INSTALL ACTANARA SCHEDULER"
SCHEDULER_UNINSTALL_CONFIRMATION = "UNINSTALL ACTANARA SCHEDULER"


def start_scheduler_loop() -> None:
    global _task, _stop_event
    if _task and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _stop_event = asyncio.Event()
    _task = loop.create_task(_scheduler_loop())


async def stop_scheduler_loop() -> None:
    global _task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _task:
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
    _stop_event = None


def scheduler_status() -> dict:
    paths = load_paths()
    settings = read_settings(paths, redact_secrets=True)
    schedule = settings.get("schedule", {}) if isinstance(settings.get("schedule"), dict) else {}
    system_timer = preview_system_timer(paths, probe_runtime=True)
    actual_system_enabled = None
    if system_timer.get("actualRegistered") is not None:
        actual_system_enabled = bool(system_timer.get("actualRegistered"))
    effective_enabled = bool(schedule.get("enabled"))
    if schedule.get("mode", "system") == "system" and actual_system_enabled is not None:
        effective_enabled = actual_system_enabled
    return {
        "running": bool(_task and not _task.done()),
        "enabled": bool(schedule.get("enabled")),
        "effectiveEnabled": effective_enabled,
        "actualSystemEnabled": actual_system_enabled,
        "mode": schedule.get("mode", "system"),
        "timezone": resolve_timezone_name(paths, settings=settings, group="schedule"),
        "state": read_scheduler_state(paths),
        "systemTimer": system_timer,
    }


def preview_system_timer(
    paths=None,
    *,
    probe_runtime: bool = True,
    launch_agent_home: Path | None = None,
    launchctl_runner=None,
    systemctl_runner=None,
) -> dict:
    return _preview_system_timer(
        paths or load_paths(),
        launch_agent_home=launch_agent_home,
        probe_runtime=probe_runtime,
        launchctl_runner=launchctl_runner,
        systemctl_runner=systemctl_runner,
    )


def install_system_timer(
    payload: dict | None = None,
    *,
    systemctl_runner=None,
    unit_dir: Path | None = None,
) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    paths = load_paths()
    if platform.system() == "Linux" and payload.get("dryRun") is not True:
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(paths.home, blocking=False):
                require_runtime_mutation_owner(paths.home, owner_id=None)
                return _install_system_timer_guarded(
                    payload,
                    paths=paths,
                    systemctl_runner=systemctl_runner,
                    unit_dir=unit_dir,
                )
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
    return _install_system_timer_guarded(
        payload,
        paths=paths,
        systemctl_runner=systemctl_runner,
        unit_dir=unit_dir,
    )


def _install_system_timer_guarded(
    payload: dict[str, Any],
    *,
    paths,
    systemctl_runner=None,
    unit_dir: Path | None = None,
) -> dict:
    settings = read_settings(paths, redact_secrets=False)
    schedule = settings.get("schedule", {})
    timer = schedule.get("systemTimer", {}) if isinstance(schedule.get("systemTimer"), dict) else {}
    provider = str(timer.get("provider") or "launchd")
    if provider not in {"launchd", "systemd"}:
        raise ValueError("system timer provider must be launchd or systemd")
    if (provider == "launchd" and platform.system() != "Darwin") or (
        provider == "systemd" and platform.system() != "Linux"
    ):
        raise ValueError(f"{provider} is not the user timer provider for this platform")
    if payload.get("dryRun") is True:
        return {
            **preview_system_timer(paths),
            "dryRun": True,
            "confirmationTextRequired": SCHEDULER_INSTALL_CONFIRMATION,
            "action": "install",
        }
    if str(payload.get("confirmationText") or "") != SCHEDULER_INSTALL_CONFIRMATION:
        raise ValueError(f"confirmationText must be exactly: {SCHEDULER_INSTALL_CONFIRMATION}")
    if provider == "systemd":
        return _execute_systemd_scheduler_handoff(
            paths,
            schedule=schedule,
            timer=timer,
            action="install",
            systemctl_runner=systemctl_runner,
            unit_dir=unit_dir,
        )
    timezone_boundary = preview_system_timer(paths, probe_runtime=False).get("timezoneBoundary") or {}
    if timezone_boundary.get("status") == "blocked":
        raise ValueError(f"Blocked: {timezone_boundary.get('issueCode') or 'scheduler-timezone-boundary'}")
    jobs = _launchd_jobs(schedule, timer, paths)
    schedule_tz = ZoneInfo(resolve_timezone_name(paths, settings=settings, group="schedule"))
    now = datetime.now(schedule_tz)
    installed = [
        {
            "kind": job["kind"],
            "label": job["label"],
            "plistPath": str(_launch_agent_path(job["label"])),
            "time": job["time"],
        }
        for job in jobs
    ]
    handoff = _execute_scheduler_handoff(
        paths,
        action="install",
        jobs=jobs,
        schedule_update={
            "enabled": True,
            "mode": "system",
            "systemTimer": {
                "provider": "launchd",
                "label": timer.get("label", "actanara.daily"),
                "registered": True,
                "registrationManagedBy": "dashboard-handoff",
                "registeredAt": now.isoformat(),
                "jobs": installed,
                "lastAction": "install",
                "lastActionStatus": "success",
                "lastError": None,
                "lastErrorAt": None,
                "stale": False,
                "reinstallRequired": False,
            },
        },
    )
    return {"installed": installed, "backupDir": None, "handoff": handoff}


def uninstall_system_timer(
    payload: dict | None = None,
    *,
    systemctl_runner=None,
    unit_dir: Path | None = None,
) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    paths = load_paths()
    if platform.system() == "Linux" and payload.get("dryRun") is not True:
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(paths.home, blocking=False):
                require_runtime_mutation_owner(paths.home, owner_id=None)
                return _uninstall_system_timer_guarded(
                    payload,
                    paths=paths,
                    systemctl_runner=systemctl_runner,
                    unit_dir=unit_dir,
                )
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
    return _uninstall_system_timer_guarded(
        payload,
        paths=paths,
        systemctl_runner=systemctl_runner,
        unit_dir=unit_dir,
    )


def _uninstall_system_timer_guarded(
    payload: dict[str, Any],
    *,
    paths,
    systemctl_runner=None,
    unit_dir: Path | None = None,
) -> dict:
    settings = read_settings(paths, redact_secrets=False)
    schedule = settings.get("schedule", {})
    timer = schedule.get("systemTimer", {}) if isinstance(schedule.get("systemTimer"), dict) else {}
    provider = str(timer.get("provider") or "launchd")
    if provider not in {"launchd", "systemd"}:
        raise ValueError("system timer provider must be launchd or systemd")
    if (provider == "launchd" and platform.system() != "Darwin") or (
        provider == "systemd" and platform.system() != "Linux"
    ):
        raise ValueError(f"{provider} is not the user timer provider for this platform")
    if payload.get("dryRun") is True:
        return {
            **preview_system_timer(paths),
            "dryRun": True,
            "confirmationTextRequired": SCHEDULER_UNINSTALL_CONFIRMATION,
            "action": "uninstall",
        }
    if str(payload.get("confirmationText") or "") != SCHEDULER_UNINSTALL_CONFIRMATION:
        raise ValueError(f"confirmationText must be exactly: {SCHEDULER_UNINSTALL_CONFIRMATION}")
    target_mode = str(payload.get("targetMode") or "disabled")
    if target_mode not in {"agent", "disabled"}:
        raise ValueError("targetMode must be one of: agent, disabled")
    if provider == "systemd":
        return _execute_systemd_scheduler_handoff(
            paths,
            schedule=schedule,
            timer=timer,
            action="uninstall",
            target_mode=target_mode,
            systemctl_runner=systemctl_runner,
            unit_dir=unit_dir,
        )
    jobs = _launchd_jobs(schedule, timer, paths)
    schedule_tz = ZoneInfo(resolve_timezone_name(paths, settings=settings, group="schedule"))
    now = datetime.now(schedule_tz)
    removed = [
        {"kind": job["kind"], "label": job["label"], "plistPath": str(_launch_agent_path(job["label"]))}
        for job in jobs
    ]
    handoff = _execute_scheduler_handoff(
        paths,
        action="uninstall",
        jobs=jobs,
        schedule_update={
            "enabled": target_mode == "agent",
            "mode": "agent" if target_mode == "agent" else "system",
            "systemTimer": {
                "provider": timer.get("provider", "launchd"),
                "label": timer.get("label", "actanara.daily"),
                "registered": False,
                "registrationManagedBy": "dashboard-handoff",
                "unregisteredAt": now.isoformat(),
                "jobs": [],
                "lastAction": "uninstall",
                "lastActionStatus": "success",
                "lastError": None,
                "lastErrorAt": None,
                "stale": False,
                "reinstallRequired": False,
            }
        },
    )
    return {"removed": removed, "backupDir": None, "handoff": handoff}


def _execute_systemd_scheduler_handoff(
    paths,
    *,
    schedule: dict[str, Any],
    timer: dict[str, Any],
    action: str,
    target_mode: str = "disabled",
    systemctl_runner=None,
    unit_dir: Path | None = None,
) -> dict[str, Any]:
    """Commit systemd timer definitions and scheduler settings as one handoff."""

    expected_schedule = json.loads(json.dumps(schedule))
    desired_units = scheduler_units(paths, schedule, timer)
    desired_names = [unit.name for unit in desired_units]
    recorded_names = _recorded_systemd_scheduler_names(timer)
    obsolete_names = [name for name in recorded_names if name not in desired_names]
    obsolete_units = [
        UserUnit(name=name, content="", enable_now=name.endswith(".timer"))
        for name in obsolete_names
    ]
    jobs = _linux_timer_jobs(schedule, timer, paths)
    job_results = [
        {
            "kind": job["kind"],
            "unitName": job["unitName"],
            "timerName": job["timerName"],
            "time": job["time"],
        }
        for job in jobs
    ]
    now = datetime.now().astimezone().isoformat()
    registered = action == "install"
    schedule_update = {
        "enabled": True if registered else target_mode == "agent",
        "mode": "system" if registered or target_mode == "disabled" else "agent",
        "systemTimer": {
            "provider": "systemd",
            "label": str(timer.get("label") or "actanara.daily"),
            "registered": registered,
            "registrationManagedBy": "dashboard-handoff",
            "registeredAt" if registered else "unregisteredAt": now,
            "jobs": desired_names if registered else [],
            "lastAction": action,
            "lastActionStatus": "success",
            "lastError": None,
            "lastErrorAt": None,
            "stale": False,
            "reinstallRequired": False,
        },
    }
    holder: dict[str, Any] = {"results": []}
    runner = systemctl_runner or subprocess.run

    def precommit(context: dict[str, str]):
        results: list[dict[str, Any]] = holder["results"]
        try:
            if registered:
                if obsolete_units:
                    results.append(
                        uninstall_user_units(
                            paths,
                            obsolete_units,
                            unit_dir=unit_dir,
                            runner=runner,
                            defer_commit=True,
                            transaction_context=context,
                        )
                    )
                results.append(
                    install_user_units(
                        paths,
                        desired_units,
                        unit_dir=unit_dir,
                        runner=runner,
                        defer_commit=True,
                        transaction_context=context,
                        recover_transactions=not results,
                    )
                )
            else:
                removal_by_name = {unit.name: unit for unit in desired_units}
                removal_by_name.update({unit.name: unit for unit in obsolete_units})
                results.append(
                    uninstall_user_units(
                        paths,
                        list(removal_by_name.values()),
                        unit_dir=unit_dir,
                        runner=runner,
                        defer_commit=True,
                        transaction_context=context,
                    )
                )
        except Exception:
            for result in reversed(results):
                try:
                    rollback_user_unit_transaction(paths, str(result["transactionId"]), runner=runner)
                except Exception:
                    pass
            raise

        def cleanup() -> None:
            for result in reversed(results):
                rollback_user_unit_transaction(paths, str(result["transactionId"]), runner=runner)

        return cleanup

    def postcommit(_context: dict[str, str]) -> None:
        results: list[dict[str, Any]] = holder["results"]
        if not results:
            raise RuntimeError("systemd scheduler handoff did not create a transaction")
        for result in results:
            finalize_user_unit_transaction(
                paths,
                str(result["transactionId"]),
                runner=runner,
            )

    def require_schedule_unchanged(current: dict[str, Any]) -> None:
        current_schedule = current.get("schedule")
        if not isinstance(current_schedule, dict) or current_schedule != expected_schedule:
            raise RuntimeError(
                "scheduler Settings changed while the systemd handoff was prepared"
            )

    try:
        saved = write_scheduler_handoff_settings(
            schedule_update,
            paths,
            precommit_side_effects=precommit,
            postcommit_side_effects=postcommit,
            current_validator=require_schedule_unchanged,
        )
    except SystemdUserError as exc:
        raise RuntimeError(str(exc)) from exc
    results = holder["results"]
    if not results:
        raise RuntimeError("systemd scheduler handoff did not create a transaction")
    handoff = {
        "schemaVersion": 1,
        "provider": "systemd-user",
        "status": "committed",
        "action": action,
        "transactions": [str(result["transactionId"]) for result in results],
        "settingsTransaction": saved.get("settingsTransaction"),
    }
    backup_directories = [
        str(result["backupDirectory"])
        for result in results
        if result.get("backupDirectory")
    ]
    return {
        "installed" if registered else "removed": job_results,
        "backupDir": backup_directories[-1] if backup_directories else None,
        "backupDirectories": backup_directories,
        "handoff": handoff,
    }


def _recorded_systemd_scheduler_names(timer: dict[str, Any]) -> list[str]:
    suffixes = (
        ".pipeline.service",
        ".pipeline.timer",
        ".dashboard-aggregation.service",
        ".dashboard-aggregation.timer",
    )
    return sorted(
        {
            str(name)
            for name in timer.get("jobs") or []
            if isinstance(name, str) and name.endswith(suffixes)
        }
    )


def scheduler_handoff_checkpoint(phase: str, transaction_id: str) -> None:
    """No-op production checkpoint patched by crash-window tests."""


def recover_scheduler_handoffs(
    paths=None,
    *,
    plist_path_resolver=None,
    launchctl_operation=None,
    launchctl_probe=None,
    launchctl_kickstart=None,
) -> list[dict[str, Any]]:
    selected = paths or load_paths()
    controls = _handoff_controls(
        plist_path_resolver=plist_path_resolver,
        launchctl_operation=launchctl_operation,
        launchctl_probe=launchctl_probe,
        launchctl_kickstart=launchctl_kickstart,
    )
    with _scheduler_handoff_lock(selected):
        recover_settings_transactions(selected)
        return _recover_scheduler_handoffs_locked(selected, controls=controls)


def _execute_scheduler_handoff(
    paths,
    *,
    action: str,
    jobs: list[dict[str, Any]],
    schedule_update: dict[str, Any],
    plist_path_resolver=None,
    launchctl_operation=None,
    launchctl_probe=None,
    launchctl_kickstart=None,
) -> dict[str, Any]:
    if action not in {"install", "uninstall"}:
        raise ValueError("unsupported scheduler handoff action")
    for job in jobs:
        _validate_handoff_label(str(job.get("label") or ""))
    controls = _handoff_controls(
        plist_path_resolver=plist_path_resolver,
        launchctl_operation=launchctl_operation,
        launchctl_probe=launchctl_probe,
        launchctl_kickstart=launchctl_kickstart,
    )
    with _scheduler_handoff_lock(paths):
        settings_recovery = recover_settings_transactions(paths)
        recovery = _recover_scheduler_handoffs_locked(paths, controls=controls)
        blocked = next((item for item in recovery if item.get("status") == "conflict"), None)
        if blocked:
            raise ValueError("scheduler handoff recovery is blocked by a concurrent state conflict")

        holder: dict[str, Any] = {}

        def precommit(context: dict) -> Any:
            transaction = _capture_scheduler_handoff(
                paths,
                action=action,
                jobs=jobs,
                context=context,
                controls=controls,
            )
            holder["transaction"] = transaction
            try:
                _apply_scheduler_external_state(transaction, desired_registered=action == "install")
            except Exception as error:
                try:
                    _restore_scheduler_prior_state(transaction)
                    _finish_handoff_journal(transaction, status="compensated", phase="external-apply-failed")
                except Exception:
                    _finish_handoff_journal(
                        transaction,
                        status="compensation-incomplete",
                        phase="external-compensation-failed",
                    )
                raise RuntimeError(f"scheduler handoff external apply failed: {type(error).__name__}") from None

            def cleanup() -> None:
                try:
                    _restore_scheduler_prior_state(transaction)
                    _finish_handoff_journal(transaction, status="compensated", phase="settings-compensated")
                except Exception:
                    _finish_handoff_journal(
                        transaction,
                        status="compensation-incomplete",
                        phase="settings-compensation-incomplete",
                    )
                    raise

            return cleanup

        saved = write_scheduler_handoff_settings(
            schedule_update,
            paths,
            precommit_side_effects=precommit,
        )
        transaction = holder.get("transaction")
        if not isinstance(transaction, dict):
            raise RuntimeError("scheduler handoff transaction was not created")
        scheduler_handoff_checkpoint("after-settings-committed", str(transaction["id"]))
        _finish_handoff_journal(transaction, status="committed", phase="committed")
        return {
            "schemaVersion": 1,
            "id": transaction["id"],
            "status": "committed",
            "action": action,
            "jobs": [str(job.get("label")) for job in jobs],
            "settingsTransaction": saved.get("settingsTransaction"),
            "recoveredHandoffs": [item.get("id") for item in recovery],
            "recoveredSettingsTransactions": [item.get("id") for item in settings_recovery],
        }


def _handoff_controls(
    *,
    plist_path_resolver=None,
    launchctl_operation=None,
    launchctl_probe=None,
    launchctl_kickstart=None,
) -> dict[str, Any]:
    return {
        "plistPathResolver": plist_path_resolver or _launch_agent_path,
        "operation": launchctl_operation or _launchctl,
        "probe": launchctl_probe or _probe_handoff_job,
        "kickstart": launchctl_kickstart or _launchctl_kickstart,
    }


def _capture_scheduler_handoff(
    paths,
    *,
    action: str,
    jobs: list[dict[str, Any]],
    context: dict,
    controls: dict[str, Any],
) -> dict[str, Any]:
    transaction_id = str(context.get("id") or uuid.uuid4().hex)
    transaction_dir = _scheduler_handoff_root(paths) / transaction_id
    transaction_dir.mkdir(parents=True, mode=0o700)
    os.chmod(transaction_dir, 0o700)
    captured_jobs: list[dict[str, Any]] = []
    for job in jobs:
        label = _validate_handoff_label(str(job.get("label") or ""))
        plist_path = controls["plistPathResolver"](label)
        before = _read_optional_bytes(plist_path)
        prior_mode = plist_path.stat().st_mode & 0o777 if before is not None else None
        desired = plistlib.dumps(job.get("plist") or {}, fmt=plistlib.FMT_XML, sort_keys=True)
        _write_private_snapshot(transaction_dir / f"{label}.before.plist", before)
        _write_private_snapshot(transaction_dir / f"{label}.desired.plist", desired)
        expected_before = _load_plist_bytes(before) if before is not None else None
        prior = controls["probe"](label, plist_path, expected_before)
        if prior.get("loaded") is None:
            raise RuntimeError("scheduler prior launchd state is unknown")
        if prior.get("loaded") and (before is None or not prior.get("aligned")):
            raise RuntimeError("scheduler prior loaded definition cannot be restored exactly")
        captured_jobs.append(
            {
                "label": label,
                "kind": str(job.get("kind") or "unknown"),
                "plistPath": plist_path,
                "desiredPlist": job.get("plist") or {},
                "priorPlistExists": before is not None,
                "priorPlistHash": _bytes_hash(before),
                "priorMode": prior_mode,
                "desiredPlistHash": _bytes_hash(desired),
                "priorLoaded": bool(prior.get("loaded")),
                "priorRunning": bool(prior.get("running")),
            }
        )
    journal = {
        "schemaVersion": 1,
        "id": transaction_id,
        "status": "active",
        "phase": "prior-captured",
        "action": action,
        "settingsBeforeHash": context.get("settingsBeforeHash"),
        "settingsAfterHash": context.get("settingsAfterHash"),
        "jobs": [
            {
                key: item[key]
                for key in (
                    "label",
                    "kind",
                    "priorPlistExists",
                    "priorPlistHash",
                    "priorMode",
                    "desiredPlistHash",
                    "priorLoaded",
                    "priorRunning",
                )
            }
            for item in captured_jobs
        ],
    }
    transaction = {
        "id": transaction_id,
        "dir": transaction_dir,
        "paths": paths,
        "action": action,
        "jobs": captured_jobs,
        "journal": journal,
        "controls": controls,
    }
    _write_handoff_journal(transaction)
    scheduler_handoff_checkpoint("after-prior-captured", transaction_id)
    return transaction


def _apply_scheduler_external_state(transaction: dict[str, Any], *, desired_registered: bool) -> None:
    jobs = transaction["jobs"]
    operation = transaction["controls"]["operation"]
    if desired_registered:
        for job in jobs:
            job["plistPath"].parent.mkdir(parents=True, exist_ok=True)
            _write_plist(job["plistPath"], job["desiredPlist"])
        for job in jobs:
            operation("bootout", job["label"], job["plistPath"], allow_failure=True)
            operation("bootstrap", job["label"], job["plistPath"])
    else:
        for job in jobs:
            operation("bootout", job["label"], job["plistPath"], allow_failure=True)
        for job in jobs:
            try:
                job["plistPath"].unlink()
            except FileNotFoundError:
                pass
    transaction["journal"]["phase"] = "external-applied"
    _write_handoff_journal(transaction)
    scheduler_handoff_checkpoint("after-external-applied", str(transaction["id"]))
    for job in jobs:
        _wait_for_handoff_job(
            transaction,
            job,
            loaded=desired_registered,
            running=None,
            expected_plist=job["desiredPlist"] if desired_registered else None,
        )
    transaction["journal"]["phase"] = "external-verified"
    _write_handoff_journal(transaction)


def _restore_scheduler_prior_state(transaction: dict[str, Any]) -> None:
    jobs = transaction["jobs"]
    operation = transaction["controls"]["operation"]
    kickstart = transaction["controls"]["kickstart"]
    for job in jobs:
        operation("bootout", job["label"], job["plistPath"], allow_failure=True)
    for job in jobs:
        before_path = transaction["dir"] / f"{job['label']}.before.plist"
        before = before_path.read_bytes() if job["priorPlistExists"] else None
        _replace_optional_bytes(job["plistPath"], before)
        if before is not None and job.get("priorMode") is not None:
            os.chmod(job["plistPath"], int(job["priorMode"]))
    for job in jobs:
        if job["priorLoaded"]:
            operation("bootstrap", job["label"], job["plistPath"])
            if job["priorRunning"]:
                kickstart(job["label"])
    for job in jobs:
        before = (
            (transaction["dir"] / f"{job['label']}.before.plist").read_bytes()
            if job["priorPlistExists"]
            else None
        )
        _wait_for_handoff_job(
            transaction,
            job,
            loaded=job["priorLoaded"],
            running=job["priorRunning"] if job["priorLoaded"] else False,
            expected_plist=_load_plist_bytes(before) if before is not None else None,
        )


def _recover_scheduler_handoffs_locked(paths, *, controls: dict[str, Any]) -> list[dict[str, Any]]:
    root = _scheduler_handoff_root(paths)
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    settings_hash = _resource_hash(paths.config_dir / "settings.json")
    for transaction_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        journal = _read_handoff_journal(transaction_dir)
        if not journal:
            results.append({"id": transaction_dir.name, "status": "conflict", "phase": "journal-unreadable"})
            continue
        if journal.get("status") in {"committed", "compensated"}:
            continue
        transaction = _transaction_from_journal(paths, transaction_dir, journal, controls=controls)
        if settings_hash == journal.get("settingsAfterHash"):
            if _desired_handoff_state_matches(transaction):
                _finish_handoff_journal(transaction, status="committed", phase="recovered-committed")
                results.append({"id": journal.get("id"), "status": "committed", "phase": "recovered-committed"})
            else:
                _finish_handoff_journal(transaction, status="conflict", phase="desired-state-mismatch")
                results.append({"id": journal.get("id"), "status": "conflict", "phase": "desired-state-mismatch"})
        elif settings_hash == journal.get("settingsBeforeHash"):
            try:
                _restore_scheduler_prior_state(transaction)
                _finish_handoff_journal(transaction, status="compensated", phase="recovered-prior")
                results.append({"id": journal.get("id"), "status": "compensated", "phase": "recovered-prior"})
            except Exception:
                _finish_handoff_journal(transaction, status="conflict", phase="prior-restore-failed")
                results.append({"id": journal.get("id"), "status": "conflict", "phase": "prior-restore-failed"})
        else:
            _finish_handoff_journal(transaction, status="conflict", phase="settings-cas-conflict")
            results.append({"id": journal.get("id"), "status": "conflict", "phase": "settings-cas-conflict"})
    return results


def _transaction_from_journal(
    paths,
    transaction_dir: Path,
    journal: dict[str, Any],
    *,
    controls: dict[str, Any],
) -> dict[str, Any]:
    jobs = []
    for item in journal.get("jobs") or []:
        label = _validate_handoff_label(str(item.get("label") or ""))
        desired = (transaction_dir / f"{label}.desired.plist").read_bytes()
        jobs.append(
            {
                **item,
                "plistPath": controls["plistPathResolver"](label),
                "desiredPlist": _load_plist_bytes(desired),
            }
        )
    return {
        "id": str(journal.get("id") or transaction_dir.name),
        "dir": transaction_dir,
        "paths": paths,
        "action": str(journal.get("action") or "install"),
        "jobs": jobs,
        "journal": journal,
        "controls": controls,
    }


def _desired_handoff_state_matches(transaction: dict[str, Any]) -> bool:
    desired_loaded = transaction.get("action") == "install"
    for job in transaction["jobs"]:
        state = transaction["controls"]["probe"](
            job["label"],
            job["plistPath"],
            job["desiredPlist"] if desired_loaded else None,
        )
        if state.get("loaded") is not desired_loaded:
            return False
        if desired_loaded and not state.get("aligned"):
            return False
        if not desired_loaded and job["plistPath"].exists():
            return False
    return True


def _probe_handoff_job(label: str, plist_path: Path, expected_plist: dict[str, Any] | None) -> dict[str, Any]:
    status = _launchd_runtime_status(
        label,
        plist_path,
        expected_plist=expected_plist or {},
        launchctl_runner=_launchctl_command_runner,
    )
    return {
        "loaded": status.get("launchctlLoaded"),
        "running": status.get("launchctlRunning"),
        "aligned": bool(status.get("provenanceAligned")),
        "reason": status.get("reason"),
    }


def _wait_for_handoff_job(
    transaction: dict[str, Any],
    job: dict[str, Any],
    *,
    loaded: bool,
    running: bool | None,
    expected_plist: dict[str, Any] | None,
    timeout_seconds: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = transaction["controls"]["probe"](job["label"], job["plistPath"], expected_plist)
        loaded_ok = state.get("loaded") is loaded
        aligned_ok = not loaded or bool(state.get("aligned"))
        running_ok = running is None or state.get("running") is running
        plist_ok = job["plistPath"].exists() is (expected_plist is not None)
        if loaded_ok and aligned_ok and running_ok and plist_ok:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("scheduler handoff verification did not converge")
        time.sleep(0.05)


def _launchctl_kickstart(label: str) -> None:
    result = _launchctl_command_runner(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        raise RuntimeError(f"launchctl kickstart failed for {label}")


def _finish_handoff_journal(transaction: dict[str, Any], *, status: str, phase: str) -> None:
    transaction["journal"]["status"] = status
    transaction["journal"]["phase"] = phase
    _write_handoff_journal(transaction)


def _write_handoff_journal(transaction: dict[str, Any]) -> None:
    content = (json.dumps(transaction["journal"], sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_replace_bytes(transaction["dir"] / "journal.json", content)


def _read_handoff_journal(transaction_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((transaction_dir / "journal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _scheduler_handoff_root(paths) -> Path:
    return paths.state_dir / "scheduler-handoffs"


@contextmanager
def _scheduler_handoff_lock(paths):
    root = _scheduler_handoff_root(paths)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / ".lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_handoff_label(label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", label):
        raise ValueError("scheduler label must contain only safe launchd label characters")
    return label


def _load_plist_bytes(content: bytes) -> dict[str, Any]:
    payload = plistlib.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("scheduler plist snapshot is invalid")
    return payload


def _write_private_snapshot(path: Path, content: bytes | None) -> None:
    if content is not None:
        _atomic_replace_bytes(path, content)


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _bytes_hash(content: bytes | None) -> str:
    return "missing" if content is None else hashlib.sha256(content).hexdigest()


def _resource_hash(path: Path) -> str:
    return _bytes_hash(_read_optional_bytes(path))


def _replace_optional_bytes(path: Path, content: bytes | None) -> None:
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        return
    _atomic_replace_bytes(path, content)


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def queue_backfill(start: str, end: str | None = None, days: int | None = None) -> dict:
    period_start = datetime.strptime(start, "%Y-%m-%d").date()
    if end:
        period_end = datetime.strptime(end, "%Y-%m-%d").date()
        period_days = (period_end - period_start).days + 1
    else:
        period_days = days or 1
        period_end = period_start + timedelta(days=period_days - 1)
    if period_days < 1 or period_days > MAX_BACKFILL_DAYS:
        raise ValueError(f"backfill range must be 1..{MAX_BACKFILL_DAYS} days")
    run_id = foundation.queue_refresh(period_end, period_start=period_start)
    foundation.execute_refresh(run_id, period_start=period_start, period_days=period_days)
    return {
        "runId": run_id,
        "status": "completed",
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
        "days": period_days,
    }


async def _scheduler_loop() -> None:
    logger.info("Foundation settings scheduler loop started")
    while _stop_event and not _stop_event.is_set():
        try:
            await asyncio.to_thread(run_due_snapshot_refresh)
        except Exception:
            logger.exception("Foundation settings scheduler tick failed")
        try:
            await asyncio.to_thread(backups.run_due_backup)
        except Exception:
            # Backup failures remain retryable within the current cadence bucket
            # and must not prevent Foundation snapshot scheduling.
            logger.exception("Actanara data-backup scheduler tick failed")
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    logger.info("Foundation settings scheduler loop stopped")


def run_due_snapshot_refresh(now: datetime | None = None) -> dict:
    paths = load_paths()
    settings = read_settings(paths, redact_secrets=True)
    schedule = settings.get("schedule", {})
    features = settings.get("features", {})
    timezone = ZoneInfo(resolve_timezone_name(paths, settings=settings, group="schedule"))
    current = now.astimezone(timezone) if now else datetime.now(timezone)
    today_key = current.date().isoformat()

    if not _scheduler_enabled(schedule, features):
        return {"ran": False, "reason": "disabled", "scheduledHistoryBackfills": []}

    scheduled_history = foundation.execute_due_scheduled_history_backfills()
    if current.strftime("%H:%M") < str(schedule.get("dashboardAggregationTime", "04:30")):
        return {
            "ran": bool(scheduled_history),
            "reason": "before_scheduled_time",
            "scheduledHistoryBackfills": scheduled_history,
        }

    state = read_scheduler_state(paths)
    if state.get("lastDashboardAggregationDate") == today_key:
        return {
            "ran": bool(scheduled_history),
            "reason": "already_ran_today",
            "date": today_key,
            "scheduledHistoryBackfills": scheduled_history,
        }

    try:
        run_ids = _refresh_targets(schedule, current)
    except Exception as error:
        write_scheduler_state(
            {
                "lastErrorAt": datetime.now(timezone).isoformat(),
                "lastError": str(error),
            },
            paths,
        )
        raise

    write_scheduler_state(
        {
            "lastDashboardAggregationDate": today_key,
            "lastDashboardAggregationAt": datetime.now(timezone).isoformat(),
            "lastDashboardAggregationRunIds": run_ids,
            "lastError": None,
        },
        paths,
    )
    return {"ran": True, "date": today_key, "runIds": run_ids, "scheduledHistoryBackfills": scheduled_history}


def _scheduler_enabled(schedule: dict, features: dict) -> bool:
    return (
        bool(schedule.get("enabled"))
        and schedule.get("mode") == "system"
        and bool(features.get("dashboard", True))
        and bool(features.get("foundationSnapshots", True))
    )


def _refresh_targets(schedule: dict, current: datetime) -> list[int]:
    targets = schedule.get("refreshTargets", {})
    today = current.date()
    run_ids: list[int] = []
    if targets.get("currentDay", True):
        run_id = foundation.queue_refresh(today, period_start=today)
        foundation.execute_refresh(run_id, period_start=today, period_days=1)
        run_ids.append(run_id)
    if targets.get("currentWeek", True):
        week_start = today - timedelta(days=today.weekday())
        days = (today - week_start).days + 1
        run_id = foundation.queue_refresh(today, period_start=week_start)
        foundation.execute_refresh(run_id, period_start=week_start, period_days=days)
        run_ids.append(run_id)
    if targets.get("currentMonth", True):
        month_start = today.replace(day=1)
        days = (today - month_start).days + 1
        run_id = foundation.queue_refresh(today, period_start=month_start)
        foundation.execute_refresh(run_id, period_start=month_start, period_days=days)
        run_ids.append(run_id)
    return run_ids




def _write_plist(path: Path, payload: dict) -> None:
    _atomic_replace_bytes(path, plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))


def _launchctl_command_runner(command: list[str], **kwargs):
    return subprocess.run(command, **kwargs)


def _launchctl(action: str, label: str, plist_path: Path, *, allow_failure: bool = False) -> None:
    domain = f"gui/{os.getuid()}"
    if action == "bootstrap":
        command = ["launchctl", "bootstrap", domain, str(plist_path)]
    elif action == "bootout":
        command = ["launchctl", "bootout", domain, str(plist_path)]
    else:
        raise ValueError(f"unsupported launchctl action: {action}")
    result = _launchctl_command_runner(command, capture_output=True, text=True, timeout=5)
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(f"launchctl {action} failed for {label} (exit {result.returncode})")


def _record_launchctl(
    operation_results: list[dict[str, Any]],
    action: str,
    label: str,
    plist_path: Path,
    *,
    allow_failure: bool = False,
) -> None:
    try:
        _launchctl(action, label, plist_path, allow_failure=allow_failure)
    except Exception as exc:
        operation_results.append(
            {
                "id": f"launchctl-{action}:{label}",
                "status": "failed",
                "allowFailure": allow_failure,
                "plistPath": str(plist_path),
                "error": str(exc),
            }
        )
        raise
    operation_results.append(
        {
            "id": f"launchctl-{action}:{label}",
            "status": "success",
            "allowFailure": allow_failure,
            "plistPath": str(plist_path),
        }
    )


def _write_system_timer_audit(
    paths,
    timer: dict,
    now: datetime,
    *,
    registered: bool,
    action: str,
    status: str,
    jobs: list[dict],
    backup_dir: Path,
    operation_results: list[dict[str, Any]],
    error: str | None,
) -> None:
    write_settings(
        {
            "schedule": {
                "systemTimer": {
                    "provider": timer.get("provider", "launchd"),
                    "label": timer.get("label", "actanara.daily"),
                    "registered": registered,
                    "registrationManagedBy": "dashboard",
                    "jobs": jobs if registered else [],
                    "partialJobs": jobs,
                    "backupDir": str(backup_dir) if backup_dir.exists() else None,
                    "lastAction": action,
                    "lastActionStatus": status,
                    "lastError": error,
                    "lastErrorAt": now.isoformat() if error else None,
                    "operationResults": operation_results,
                    "rollbackHint": "Use uninstall to unload partial jobs, or restore plist files from backupDir if needed.",
                }
            }
        },
        paths,
    )
