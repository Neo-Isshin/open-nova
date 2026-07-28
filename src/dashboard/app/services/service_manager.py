"""Platform-neutral Dashboard service management for launchd and systemd user."""

from __future__ import annotations

import platform
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from data_foundation.paths import RuntimePaths, load_paths
from data_foundation.settings import (
    read_settings,
    write_service_manager_settings,
)
from data_foundation.systemd_user import (
    SystemdUserError,
    UserUnit,
    control_user_units,
    dashboard_unit,
    default_user_unit_dir,
    enqueue_user_unit_action,
    finalize_user_unit_transaction,
    inspect_user_units,
    install_user_units,
    rag_unit,
    rollback_user_unit_transaction,
    transient_user_action_unit_name,
    uninstall_user_units,
)

from . import launcher


SERVICE_KINDS = {"dashboard", "rag"}
SERVICE_ACTIONS = {"install", "uninstall", "start", "stop", "restart"}
_CONFIRMATIONS = {
    ("dashboard", "install"): "INSTALL ACTANARA DASHBOARD SERVICE",
    ("dashboard", "uninstall"): "UNINSTALL ACTANARA DASHBOARD SERVICE",
    ("dashboard", "start"): "START ACTANARA DASHBOARD SERVICE",
    ("dashboard", "stop"): "STOP ACTANARA DASHBOARD SERVICE",
    ("dashboard", "restart"): "RESTART ACTANARA DASHBOARD SERVICE",
    ("rag", "install"): "INSTALL ACTANARA RAG SERVICE",
    ("rag", "uninstall"): "UNINSTALL ACTANARA RAG SERVICE",
    ("rag", "start"): "START ACTANARA RAG SERVICE",
    ("rag", "stop"): "STOP ACTANARA RAG SERVICE",
    ("rag", "restart"): "RESTART ACTANARA RAG SERVICE",
}


class ServiceManagerError(RuntimeError):
    pass


class _PendingRequestChanged(ServiceManagerError):
    pass


class PlatformServiceManager:
    """Stable service interface whose backend is selected from the host platform."""

    def __init__(
        self,
        *,
        paths: RuntimePaths | None = None,
        systemctl_runner=None,
        systemd_run_runner=None,
        launchctl_runner=None,
        unit_dir: Path | None = None,
    ) -> None:
        self.paths = paths or load_paths()
        self.systemctl_runner = systemctl_runner or subprocess.run
        self.systemd_run_runner = systemd_run_runner or subprocess.run
        self.launchctl_runner = launchctl_runner
        self.unit_dir = unit_dir

    @property
    def provider(self) -> str:
        system = platform.system()
        if system == "Darwin":
            return "launchd-user"
        if system == "Linux":
            return "systemd-user"
        return "unsupported"

    def preview(self, kind: str, *, probe_runtime: bool = True) -> dict[str, Any]:
        selected = _kind(kind)
        if self.provider == "launchd-user":
            preview = (
                launcher.preview_dashboard_launch_agent(
                    probe_runtime=probe_runtime,
                    launchctl_runner=self.launchctl_runner,
                )
                if selected == "dashboard"
                else launcher.preview_rag_launch_agent(
                    probe_runtime=probe_runtime,
                    launchctl_runner=self.launchctl_runner,
                )
            )
            return {**preview, "serviceManager": self.provider, "supported": True}
        if self.provider == "systemd-user":
            return self._systemd_preview(selected, probe_runtime=probe_runtime)
        return {
            "kind": selected,
            "provider": self.provider,
            "serviceManager": self.provider,
            "supported": False,
            "error": "user service management is unsupported on this platform",
        }

    def status(self, kind: str) -> dict[str, Any]:
        return self.preview(kind, probe_runtime=True)

    def install(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "install", payload)

    def update(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "install", payload)

    def uninstall(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "uninstall", payload)

    def start(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "start", payload)

    def stop(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "stop", payload)

    def restart(self, kind: str, payload: dict | None = None) -> dict[str, Any]:
        return self._apply(kind, "restart", payload)

    def enqueue(self, kind: str, action: str, payload: dict | None = None) -> dict[str, Any]:
        request = payload if isinstance(payload, dict) else {}
        if self.provider != "systemd-user" or request.get("dryRun") is True:
            return self._enqueue_guarded(kind, action, payload)
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(self.paths.home, blocking=False):
                require_runtime_mutation_owner(self.paths.home, owner_id=None)
                return self._enqueue_guarded(kind, action, payload)
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise ServiceManagerError(str(exc)) from exc

    def _enqueue_guarded(self, kind: str, action: str, payload: dict | None = None) -> dict[str, Any]:
        """Accept a Linux action into an independent transient systemd user job."""

        selected = _kind(kind)
        if action not in {"install", "uninstall", "stop", "restart"}:
            raise ValueError("asynchronous service action must be install, uninstall, stop, or restart")
        if self.provider != "systemd-user":
            raise ServiceManagerError("asynchronous service actions require systemd user")
        request = payload if isinstance(payload, dict) else {}
        if request.get("dryRun") is True:
            return self._apply(selected, action, request)
        required = _confirmation(selected, action)
        if str(request.get("confirmationText") or "") != required:
            raise ValueError(f"confirmationText must be exactly: {required}")

        request_id = uuid.uuid4().hex
        units = self._units(selected)
        expected_units = [
            (unit.name, unit.content, unit.enable_now)
            for unit in units
        ]
        saved: dict[str, Any] | None = None
        previous = self._registration_snapshot(selected, units)
        if action in {"install", "uninstall"}:
            update = self._registration_update(
                selected,
                action,
                units,
                status="queued",
                request_id=request_id,
                previous=previous,
            )
            def require_unchanged(current: dict[str, Any]) -> None:
                self._require_unit_inputs_unchanged(
                    selected,
                    current,
                    expected_units,
                )
                actual = self._registration_snapshot_from_settings(selected, current)
                metadata = actual["metadata"]
                if metadata.get("pendingRequestId") or actual != previous:
                    raise _PendingRequestChanged(
                        f"a {selected} service action is already pending or changed concurrently"
                    )

            try:
                saved = write_service_manager_settings(
                    update,
                    self.paths,
                    precommit_side_effects=lambda _context: None,
                    current_validator=require_unchanged,
                )
            except RuntimeError as exc:
                raise ServiceManagerError(str(exc)) from exc
        try:
            job = enqueue_user_unit_action(
                self.paths,
                kind=selected,
                action=action,
                request_id=request_id,
                units=units,
                unit_dir=self.unit_dir,
                runner=self.systemd_run_runner,
            )
        except (SystemdUserError, RuntimeError) as exc:
            if action in {"install", "uninstall"}:
                self._record_enqueue_failure(
                    selected,
                    action,
                    request_id=request_id,
                    previous=previous,
                    error=str(exc),
                )
            raise ServiceManagerError(str(exc)) from exc
        return {
            "accepted": True,
            "status": "queued",
            "kind": selected,
            "action": action,
            "provider": "systemd-user",
            "serviceManager": self.provider,
            "job": job,
            **(
                {"settingsTransaction": saved.get("settingsTransaction")}
                if isinstance(saved, dict)
                else {}
            ),
        }

    def _apply(self, kind: str, action: str, payload: dict | None) -> dict[str, Any]:
        request = payload if isinstance(payload, dict) else {}
        if self.provider != "systemd-user" or request.get("dryRun") is True:
            return self._apply_guarded(kind, action, payload)
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(self.paths.home, blocking=False):
                require_runtime_mutation_owner(self.paths.home, owner_id=None)
                return self._apply_guarded(kind, action, payload)
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise ServiceManagerError(str(exc)) from exc

    def _apply_guarded(self, kind: str, action: str, payload: dict | None) -> dict[str, Any]:
        selected = _kind(kind)
        if action not in SERVICE_ACTIONS:
            raise ValueError("unknown service-manager action")
        request = payload if isinstance(payload, dict) else {}
        if self.provider == "launchd-user":
            if action == "install":
                result = (
                    launcher.install_dashboard_launch_agent(request)
                    if selected == "dashboard"
                    else launcher.install_rag_launch_agent(request)
                )
            elif action == "uninstall":
                result = (
                    launcher.uninstall_dashboard_launch_agent(request)
                    if selected == "dashboard"
                    else launcher.uninstall_rag_launch_agent(request)
                )
            else:
                result = launcher.control_launch_agent(selected, action, request)
            return {**result, "serviceManager": self.provider, "provider": "launchd"}
        if self.provider != "systemd-user":
            raise ServiceManagerError("user service management is unsupported on this platform")
        if request.get("dryRun") is True:
            preview = self._systemd_preview(selected, probe_runtime=False)
            return {
                **preview,
                "dryRun": True,
                "action": action,
                "confirmationTextRequired": _confirmation(selected, action),
            }
        required = _confirmation(selected, action)
        if str(request.get("confirmationText") or "") != required:
            raise ValueError(f"confirmationText must be exactly: {required}")
        if action in {"start", "stop", "restart"}:
            try:
                result = control_user_units(
                    self.paths,
                    self._units(selected),
                    action,
                    unit_dir=self.unit_dir,
                    runner=self.systemctl_runner,
                )
            except SystemdUserError as exc:
                raise ServiceManagerError(str(exc)) from exc
            return {**result, "kind": selected, "serviceManager": self.provider}
        return self._systemd_registration_handoff(selected, action)

    def _units(self, kind: str) -> list[UserUnit]:
        settings = read_settings(self.paths, redact_secrets=False, persist_defaults=False)
        return self._units_from_settings(kind, settings)

    def _units_from_settings(
        self,
        kind: str,
        settings: dict[str, Any],
    ) -> list[UserUnit]:
        if kind == "dashboard":
            dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
            return [dashboard_unit(self.paths, dashboard)]
        rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
        server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
        return [rag_unit(self.paths, server)]

    def _configured_registration(self, kind: str) -> dict[str, Any]:
        settings = read_settings(self.paths, redact_secrets=True, persist_defaults=False)
        if kind == "dashboard":
            dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
            value = dashboard.get("systemdUser")
        else:
            rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
            server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
            value = server.get("systemdUser")
        return value if isinstance(value, dict) else {}

    def _systemd_preview(self, kind: str, *, probe_runtime: bool) -> dict[str, Any]:
        units = self._units(kind)
        if probe_runtime:
            inspection = inspect_user_units(
                units,
                unit_dir=self.unit_dir,
                runner=self.systemctl_runner,
            )
        else:
            root = self.unit_dir or default_user_unit_dir()
            inspection = {
                "supported": True,
                "unitDirectory": str(root) if root else None,
                "units": [
                    {
                        "name": unit.name,
                        "path": str(root / unit.name),
                        "enableNow": unit.enable_now,
                        "exists": None,
                        "managed": None,
                        "aligned": None,
                        "enabled": None,
                        "active": None,
                    }
                    for unit in units
                ],
                "definitionsPresent": None,
                "definitionsManaged": None,
                "definitionsAligned": None,
                "actualEnabled": None,
                "actualActive": None,
                "actualRegistered": None,
            }
        configured = self._configured_registration(kind)
        actual_enabled = inspection.get("actualEnabled")
        actual_active = inspection.get("actualActive")
        configured_registered = bool(configured.get("registered"))
        registered = configured_registered if actual_enabled is None else bool(actual_enabled)
        if actual_enabled is True and actual_active is True:
            runtime_status = "running"
        elif actual_enabled is True:
            runtime_status = "stopped"
        elif actual_enabled is False:
            runtime_status = "disabled"
        else:
            runtime_status = "not-probed"
        jobs = []
        for record in inspection.get("units") or []:
            jobs.append(
                {
                    "kind": f"{kind}-service",
                    "unitName": record.get("name"),
                    "unitPath": record.get("path"),
                    "runtimeStatus": {
                        "provider": "systemd-user",
                        "status": runtime_status,
                        "systemdEnabled": record.get("enabled"),
                        "systemdActive": record.get("active"),
                        "definitionExists": record.get("exists"),
                        "definitionManaged": record.get("managed"),
                        "definitionAligned": record.get("aligned"),
                    },
                }
            )
        return {
            "kind": kind,
            "action": "install",
            "provider": "systemd-user",
            "serviceManager": self.provider,
            "supported": bool(inspection.get("supported", True)),
            "dryRun": True,
            "confirmationTextRequired": _confirmation(kind, "install"),
            "installConfirmationTextRequired": _confirmation(kind, "install"),
            "uninstallConfirmationTextRequired": _confirmation(kind, "uninstall"),
            "startConfirmationTextRequired": _confirmation(kind, "start"),
            "stopConfirmationTextRequired": _confirmation(kind, "stop"),
            "restartConfirmationTextRequired": _confirmation(kind, "restart"),
            "registered": registered,
            "configuredRegistered": configured_registered,
            "actualRegistered": actual_enabled,
            "actualRunning": actual_active,
            "registrationSource": "systemd-probe" if actual_enabled is not None else "settings",
            "registrationMismatch": (
                configured_registered != bool(actual_enabled)
                if actual_enabled is not None
                else False
            ),
            "definitionsPresent": inspection.get("definitionsPresent"),
            "definitionsManaged": inspection.get("definitionsManaged"),
            "definitionsAligned": inspection.get("definitionsAligned"),
            "runtimeProbe": {
                "enabled": probe_runtime,
                "status": runtime_status,
                "expectedJobs": len(units),
                "loadedJobs": sum(
                    1 for item in inspection.get("units") or [] if item.get("active") is True
                ),
                "definitionJobs": sum(
                    1 for item in inspection.get("units") or [] if item.get("exists") is True
                ),
            },
            "jobs": jobs,
            "mutationPolicy": {
                "writesUserUnits": False,
                "callsSystemctlUser": False,
                "callsSudo": False,
                "changesLinger": False,
                "settingsMutated": False,
            },
        }

    def _registration_snapshot(
        self,
        kind: str,
        units: list[UserUnit],
    ) -> dict[str, Any]:
        settings = read_settings(self.paths, redact_secrets=False, persist_defaults=False)
        return self._registration_snapshot_from_settings(kind, settings)

    def _registration_snapshot_from_settings(
        self,
        kind: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        if kind == "dashboard":
            dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
            server = dashboard.get("server") if isinstance(dashboard.get("server"), dict) else {}
            registration = dashboard.get("systemdUser")
        else:
            rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
            server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
            registration = server.get("systemdUser")
        metadata = registration if isinstance(registration, dict) else {}
        configured_enabled = server.get("enabled")
        enabled = (
            configured_enabled
            if type(configured_enabled) is bool
            else bool(metadata.get("registered"))
        )
        configured_units = metadata.get("units") if isinstance(metadata.get("units"), list) else []
        names = [str(name) for name in configured_units if isinstance(name, str)]
        return {
            "enabled": enabled,
            "registered": bool(metadata.get("registered")),
            "metadata": dict(metadata),
            "units": names,
        }

    def _registration_update(
        self,
        kind: str,
        action: str,
        units: list[UserUnit],
        *,
        status: str,
        request_id: str | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        registered = action == "install"
        now = datetime.now().astimezone().isoformat()
        metadata = {
            "registered": registered,
            "provider": "systemd-user",
            "registrationManagedBy": "dashboard-service-manager",
            "registeredAt" if registered else "unregisteredAt": now,
            # Retain the selected names after uninstall so the detached helper
            # and later audits address the exact definitions that were removed.
            "units": [unit.name for unit in units],
            "lastAction": action,
            "lastActionStatus": status,
            "lastError": None,
            "lastErrorAt": None,
            "pendingAction": action if status == "queued" else None,
            "pendingRequestId": request_id if status == "queued" else None,
            "pendingJobUnit": (
                transient_user_action_unit_name(kind, action, request_id)
                if status == "queued" and request_id is not None
                else None
            ),
            "pendingPreviousState": (
                {
                    "serverEnabled": bool(previous["enabled"]),
                    "registered": bool(previous["registered"]),
                    "units": list(previous["units"]),
                }
                if status == "queued" and previous is not None
                else None
            ),
        }
        if kind == "dashboard":
            return {
                "dashboard": {
                    "server": {"enabled": registered},
                    "systemdUser": metadata,
                }
            }
        return {
            "rag": {
                "server": {
                    "enabled": registered,
                    "systemdUser": metadata,
                }
            }
        }

    def _record_enqueue_failure(
        self,
        kind: str,
        action: str,
        *,
        request_id: str,
        previous: dict[str, Any],
        error: str,
    ) -> None:
        current = self._configured_registration(kind)
        if (
            current.get("pendingRequestId") != request_id
            or current.get("pendingAction") != action
        ):
            return
        metadata = {
            **previous["metadata"],
            "registered": bool(previous["registered"]),
            "provider": "systemd-user",
            "units": previous["units"],
            "lastAction": action,
            "lastActionStatus": "failed",
            "lastError": error,
            "lastErrorAt": datetime.now().astimezone().isoformat(),
            "pendingAction": None,
            "pendingRequestId": None,
            "pendingJobUnit": None,
            "pendingPreviousState": None,
        }
        update = (
            {
                "dashboard": {
                    "server": {"enabled": bool(previous["enabled"])},
                    "systemdUser": metadata,
                }
            }
            if kind == "dashboard"
            else {
                "rag": {
                    "server": {
                        "enabled": bool(previous["enabled"]),
                        "systemdUser": metadata,
                    }
                }
            }
        )
        try:
            write_service_manager_settings(
                update,
                self.paths,
                precommit_side_effects=lambda _context: None,
                current_validator=lambda current: self._require_pending_request(
                    current,
                    kind=kind,
                    action=action,
                    request_id=request_id,
                ),
            )
        except _PendingRequestChanged:
            return

    def _require_pending_request(
        self,
        settings: dict[str, Any],
        *,
        kind: str,
        action: str,
        request_id: str,
    ) -> None:
        current = self._registration_snapshot_from_settings(kind, settings)["metadata"]
        if (
            current.get("pendingRequestId") != request_id
            or current.get("pendingAction") != action
        ):
            raise _PendingRequestChanged("queued service request changed concurrently")

    def _restore_registration_snapshot(
        self,
        kind: str,
        action: str,
        previous: dict[str, Any],
        error: str,
    ) -> None:
        metadata = {
            **previous["metadata"],
            "registered": bool(previous["registered"]),
            "provider": "systemd-user",
            "units": list(previous["units"]),
            "lastAction": action,
            "lastActionStatus": "failed",
            "lastError": error,
            "lastErrorAt": datetime.now().astimezone().isoformat(),
            "pendingAction": None,
            "pendingRequestId": None,
            "pendingJobUnit": None,
            "pendingPreviousState": None,
        }
        update = (
            {
                "dashboard": {
                    "server": {"enabled": bool(previous["enabled"])},
                    "systemdUser": metadata,
                }
            }
            if kind == "dashboard"
            else {
                "rag": {
                    "server": {
                        "enabled": bool(previous["enabled"]),
                        "systemdUser": metadata,
                    }
                }
            }
        )
        write_service_manager_settings(
            update,
            self.paths,
            precommit_side_effects=lambda _context: None,
        )

    def _systemd_registration_handoff(self, kind: str, action: str) -> dict[str, Any]:
        units = self._units(kind)
        expected_units = [(unit.name, unit.content, unit.enable_now) for unit in units]
        registered = action == "install"
        update = self._registration_update(kind, action, units, status="success")
        holder: dict[str, Any] = {}

        def precommit(context: dict[str, str]):
            try:
                result = (
                    install_user_units(
                        self.paths,
                        units,
                        unit_dir=self.unit_dir,
                        runner=self.systemctl_runner,
                        defer_commit=True,
                        transaction_context=context,
                    )
                    if registered
                    else uninstall_user_units(
                        self.paths,
                        units,
                        unit_dir=self.unit_dir,
                        runner=self.systemctl_runner,
                        defer_commit=True,
                        transaction_context=context,
                    )
                )
            except SystemdUserError as exc:
                raise ServiceManagerError(str(exc)) from exc
            holder["result"] = result

            def cleanup() -> None:
                rollback_user_unit_transaction(
                    self.paths,
                    str(result["transactionId"]),
                    runner=self.systemctl_runner,
                )

            return cleanup

        def postcommit(_context: dict[str, str]) -> None:
            result = holder.get("result")
            if not isinstance(result, dict):
                raise ServiceManagerError(
                    "systemd service handoff did not create a transaction"
                )
            finalize_user_unit_transaction(
                self.paths,
                str(result["transactionId"]),
                runner=self.systemctl_runner,
            )

        try:
            saved = write_service_manager_settings(
                update,
                self.paths,
                precommit_side_effects=precommit,
                postcommit_side_effects=postcommit,
                current_validator=lambda current: self._require_unit_inputs_unchanged(
                    kind,
                    current,
                    expected_units,
                ),
            )
        except SystemdUserError as exc:
            raise ServiceManagerError(
                "systemd service registration is applied but finalization is pending: "
                f"{exc}"
            ) from exc
        result = holder.get("result")
        if not isinstance(result, dict):
            raise ServiceManagerError("systemd service handoff did not create a transaction")
        return {
            **result,
            "kind": kind,
            "action": action,
            "status": "registered" if registered else "unregistered",
            "jobs": [
                {"kind": f"{kind}-service", "unitName": unit.name}
                for unit in units
            ],
            "serviceManager": self.provider,
            "settingsTransaction": saved.get("settingsTransaction"),
        }

    def _require_unit_inputs_unchanged(
        self,
        kind: str,
        settings: dict[str, Any],
        expected_units: list[tuple[str, str, bool]],
    ) -> None:
        actual = [
            (unit.name, unit.content, unit.enable_now)
            for unit in self._units_from_settings(kind, settings)
        ]
        if actual != expected_units:
            raise _PendingRequestChanged(
                f"{kind} service Settings changed while the unit handoff was prepared"
            )


def _kind(value: str) -> str:
    selected = str(value or "").strip().lower()
    if selected not in SERVICE_KINDS:
        raise ValueError("service kind must be one of: dashboard, rag")
    return selected


def _confirmation(kind: str, action: str) -> str:
    try:
        return _CONFIRMATIONS[(_kind(kind), action)]
    except KeyError as exc:
        raise ValueError("unknown service-manager action") from exc


def preview_service(kind: str, *, probe_runtime: bool = True, **kwargs) -> dict[str, Any]:
    return PlatformServiceManager(**kwargs).preview(kind, probe_runtime=probe_runtime)


def install_service(kind: str, payload: dict | None = None, **kwargs) -> dict[str, Any]:
    return PlatformServiceManager(**kwargs).install(kind, payload)


def uninstall_service(kind: str, payload: dict | None = None, **kwargs) -> dict[str, Any]:
    return PlatformServiceManager(**kwargs).uninstall(kind, payload)


def service_action_requires_async(
    kind: str,
    action: str,
    payload: dict | None = None,
) -> bool:
    selected = _kind(kind)
    request = payload if isinstance(payload, dict) else {}
    return (
        platform.system() == "Linux"
        and selected == "dashboard"
        and action in {"install", "uninstall", "stop", "restart"}
        and request.get("dryRun") is not True
    )


def enqueue_service_action(
    kind: str,
    action: str,
    payload: dict | None = None,
    **kwargs,
) -> dict[str, Any]:
    return PlatformServiceManager(**kwargs).enqueue(kind, action, payload)


def control_service(kind: str, action: str, payload: dict | None = None, **kwargs) -> dict[str, Any]:
    manager = PlatformServiceManager(**kwargs)
    return getattr(manager, action)(_kind(kind), payload)
