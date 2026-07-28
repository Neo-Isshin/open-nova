"""Linux systemd user-unit rendering, registration, and read-only probes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .paths import RuntimePaths
from .paths import runtime_paths_for_home
from .runtime_mutation import (
    RuntimeMutationBusy,
    RuntimeMutationUnsafe,
    current_runtime_mutation_guard_fd,
    durable_runtime_mutation_owner,
    require_runtime_mutation_owner,
    runtime_mutation_guard,
)


UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,126}$")
MANAGED_UNIT_HEADER = "# Managed by Actanara. Do not edit by hand."
Runner = Callable[..., subprocess.CompletedProcess[str]]
SYSTEMD_STATE_SETTLE_ATTEMPTS = 10
SYSTEMD_STATE_STABLE_SAMPLES = 3
SYSTEMD_STATE_SETTLE_INTERVAL_SECONDS = 0.1


def _systemd_transaction_process_identity(pid: int) -> str | None:
    if pid <= 1:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        if close > 0 and len(fields) > 19:
            return f"proc-start-ticks:{fields[19]}"
    except (OSError, UnicodeDecodeError):
        pass
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    return f"ps-lstart:{value}" if result.returncode == 0 and value else None


def _same_systemd_transaction_owner(pid: int, identity: object) -> bool:
    return (
        isinstance(identity, str)
        and bool(identity)
        and _systemd_transaction_process_identity(pid) == identity
    )


class SystemdUserError(RuntimeError):
    pass


class SystemdUserCompensationError(SystemdUserError):
    """A unit mutation failed and its prior state could not be restored."""


class _QueuedActionStale(SystemdUserError):
    pass


class _QueuedActionRetryable(SystemdUserError):
    pass


class _QueuedHelperReloadRequired(SystemdUserError):
    pass


@dataclass(frozen=True)
class UserUnit:
    name: str
    content: str
    enable_now: bool = True


def systemd_transaction_checkpoint(phase: str, transaction_id: str) -> None:
    """No-op checkpoint patched by interruption-window tests."""


def _quote(value: str) -> str:
    text = str(value)
    if any(character in text for character in "\0\r\n"):
        raise SystemdUserError("systemd unit value contains a control character")
    return '"' + text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path_value(value: Path) -> str:
    text = str(value)
    if not value.is_absolute():
        raise SystemdUserError("systemd working directory must be absolute")
    if any(character in text for character in "\0\r\n") or text.endswith("\\"):
        raise SystemdUserError("systemd working directory contains an unsafe character")
    # WorkingDirectory= is a scalar path directive, not an ExecStart-style
    # argument list. Quoting the value makes the quotes part of the path on
    # systemd 257, so preserve the scalar and escape only specifier markers.
    return text.replace("%", "%%")


def _unit_name(value: str, suffix: str) -> str:
    base = str(value or "actanara").strip()
    if not UNIT_NAME_RE.fullmatch(base):
        raise SystemdUserError("systemd unit label is unsafe")
    name = f"{base}.{suffix}"
    if not UNIT_NAME_RE.fullmatch(name):
        raise SystemdUserError("systemd unit name is unsafe")
    return name


def _configured_service_unit_name(settings: dict, default: str) -> str:
    registration = settings.get("systemdUser") if isinstance(settings.get("systemdUser"), dict) else {}
    configured = registration.get("units") if isinstance(registration.get("units"), list) else []
    if not configured:
        return default
    if (
        len(configured) != 1
        or not isinstance(configured[0], str)
        or not configured[0].endswith(".service")
        or not UNIT_NAME_RE.fullmatch(configured[0])
    ):
        raise SystemdUserError("configured systemd service unit name is unsafe")
    return configured[0]


def _environment_lines(environment: dict[str, str]) -> list[str]:
    return [f"Environment={_quote(f'{key}={value}')}" for key, value in sorted(environment.items())]


def _service_unit(
    *,
    description: str,
    command: Iterable[str],
    working_directory: Path,
    environment: dict[str, str],
    restart: bool,
    service_directives: Iterable[str] = (),
) -> str:
    command_line = " ".join(_quote(item) for item in command)
    lines = [
        MANAGED_UNIT_HEADER,
        "[Unit]",
        f"Description={description}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple" if restart else "Type=oneshot",
        f"WorkingDirectory={_path_value(working_directory)}",
        *_environment_lines(environment),
        f"ExecStart={command_line}",
        *service_directives,
    ]
    if restart:
        lines.extend(("Restart=on-failure", "RestartSec=10"))
    lines.extend(("", "[Install]", "WantedBy=default.target", ""))
    return "\n".join(lines)


def _timer_unit(*, description: str, service_name: str, time_of_day: str, timezone: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(time_of_day)):
        raise SystemdUserError("systemd timer time must use HH:MM")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*", str(timezone)):
        raise SystemdUserError("systemd timer timezone is unsafe")
    return "\n".join(
        (
            MANAGED_UNIT_HEADER,
            "[Unit]",
            f"Description={description}",
            "",
            "[Timer]",
            f"OnCalendar=*-*-* {time_of_day}:00 {timezone}",
            "Persistent=true",
            f"Unit={service_name}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        )
    )


def scheduler_units(paths: RuntimePaths, schedule: dict, timer: dict) -> list[UserUnit]:
    source = paths.home / "app" / "source"
    python = paths.home / ".venv" / "bin" / "python"
    label = str(timer.get("label") or "actanara.daily")
    timezone = str(schedule.get("timezone") or "UTC")
    environment = {
        "ACTANARA_HOME": str(paths.home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source), str(source / "src"), str(source / "src" / "dashboard"))),
    }
    jobs = (
        (
            "pipeline",
            "Actanara daily pipeline",
            str(schedule.get("dailyPipelineTime") or "04:00"),
            source / "advanced" / "pipeline" / "run_daily_pipeline.py",
        ),
        (
            "dashboard-aggregation",
            "Actanara Dashboard aggregation",
            str(schedule.get("dashboardAggregationTime") or "04:30"),
            source / "advanced" / "pipeline" / "run_dashboard_foundation_refresh.py",
        ),
    )
    units: list[UserUnit] = []
    for suffix, description, time_of_day, script in jobs:
        service_name = _unit_name(label, f"{suffix}.service")
        timer_name = _unit_name(label, f"{suffix}.timer")
        units.append(
            UserUnit(
                name=service_name,
                content=_service_unit(
                    description=description,
                    command=(str(python), str(script)),
                    working_directory=source,
                    environment=environment,
                    restart=False,
                ),
                enable_now=False,
            )
        )
        units.append(
            UserUnit(
                name=timer_name,
                content=_timer_unit(
                    description=f"{description} timer",
                    service_name=service_name,
                    time_of_day=time_of_day,
                    timezone=timezone,
                ),
            )
        )
    return units


def dashboard_unit(paths: RuntimePaths, dashboard: dict) -> UserUnit:
    source = paths.home / "app" / "source"
    python = paths.home / ".venv" / "bin" / "python"
    host = str(dashboard.get("host") or "127.0.0.1")
    port = int(dashboard.get("port") or 3036)
    environment = {
        "ACTANARA_HOME": str(paths.home),
        "ACTANARA_DATA_FOUNDATION_ENABLED": "true",
        "DASHBOARD_READ_SOURCE": "foundation",
        "DIARY_MEMORY_SOURCE": "foundation",
        "DIARY_METRICS_SOURCE": "foundation",
        "DIARY_TASKS_SOURCE": "foundation",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source), str(source / "src"), str(source / "src" / "dashboard"))),
        "REPORT_READ_SOURCE": "foundation",
    }
    return UserUnit(
        name=_configured_service_unit_name(dashboard, "actanara-dashboard.service"),
        content=_service_unit(
            description="Actanara Dashboard",
            command=(
                str(python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                str(source / "src" / "dashboard"),
                "--host",
                host,
                "--port",
                str(port),
            ),
            working_directory=source,
            environment=environment,
            restart=True,
        ),
    )


def rag_unit(paths: RuntimePaths, server: dict | None = None) -> UserUnit:
    source = paths.home / "app" / "source"
    python = paths.home / ".venv" / "bin" / "python"
    environment = {
        "ACTANARA_HOME": str(paths.home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source), str(source / "src"))),
    }
    return UserUnit(
        name=_configured_service_unit_name(server or {}, "actanara-rag-server.service"),
        content=_service_unit(
            description="Actanara nova-RAG server",
            command=(
                str(python),
                str(source / "advanced" / "dashboard" / "rag_server_launch_agent.py"),
                "run",
                "--project-root",
                str(source),
                "--actanara-home",
                str(paths.home),
            ),
            working_directory=source,
            environment=environment,
            restart=True,
            service_directives=(
                "KillMode=control-group",
                "TimeoutStopSec=10s",
                "SendSIGKILL=yes",
            ),
        ),
    )


def default_user_unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"


def _systemctl_binary() -> str:
    return os.environ.get("ACTANARA_INSTALL_SYSTEMCTL") or shutil.which("systemctl") or ""


def _systemd_run_binary() -> str:
    return os.environ.get("ACTANARA_INSTALL_SYSTEMD_RUN") or shutil.which("systemd-run") or ""


def transient_user_action_unit_name(kind: str, action: str, request_id: str) -> str:
    if kind not in {"dashboard", "rag"}:
        raise SystemdUserError("transient systemd action has an unsupported service kind")
    if action not in {"install", "uninstall", "start", "stop", "restart"}:
        raise SystemdUserError("transient systemd action is unsupported")
    if not re.fullmatch(r"[0-9a-f]{32}", str(request_id or "")):
        raise SystemdUserError("transient systemd action request id is invalid")
    return f"actanara-{kind}-service-{action}-{request_id[:12]}.service"


def user_unit_set_sha256(units: Iterable[UserUnit]) -> str:
    selected = _validated_units(units)
    payload = [
        {
            "name": unit.name,
            "content": unit.content,
            "enableNow": unit.enable_now,
        }
        for unit in selected
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run_systemctl(
    arguments: Iterable[str],
    *,
    runner: Runner = subprocess.run,
    allow_status: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    binary = _systemctl_binary()
    if not binary:
        raise SystemdUserError("systemctl is unavailable")
    command = [binary, "--user", *arguments]
    child_options: dict[str, Any] = {}
    if runner is subprocess.run and sys.platform == "linux":
        guard_fd = current_runtime_mutation_guard_fd()
        if guard_fd is not None:
            # Keep the Runtime flock alive in systemctl if the Python owner is
            # killed; recovery cannot race the in-flight external mutation.
            child_options["pass_fds"] = (guard_fd,)
        parent_pid = os.getpid()

        def configure_parent_death_signal() -> None:  # pragma: no cover - Debian only
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
                os._exit(126)
            if os.getppid() != parent_pid:
                os._exit(126)

        child_options["preexec_fn"] = configure_parent_death_signal
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        **child_options,
    )
    allowed = allow_status if allow_status is not None else {0}
    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        if len(detail) > 500:
            detail = detail[:497] + "..."
        suffix = f": {detail}" if detail else ""
        raise SystemdUserError(
            f"systemctl --user failed with status {result.returncode}{suffix}"
        )
    return result


def probe_user_units(units: Iterable[UserUnit], *, runner: Runner = subprocess.run) -> dict:
    names = [unit.name for unit in units if unit.enable_now]
    if not names:
        return {"status": "not-requested", "actualRegistered": False, "units": []}
    if platform.system() != "Linux" or not _systemctl_binary():
        return {"status": "unsupported", "actualRegistered": None, "units": names}
    records = []
    for name in names:
        enabled = _run_systemctl(("is-enabled", name), runner=runner, allow_status={0, 1, 3, 4})
        active = _run_systemctl(("is-active", name), runner=runner, allow_status={0, 1, 3, 4})
        records.append(
            {
                "name": name,
                "enabled": enabled.returncode == 0,
                "active": active.returncode == 0,
            }
        )
    return {
        "status": "registered" if all(item["enabled"] and item["active"] for item in records) else "not-registered",
        "actualRegistered": all(item["enabled"] and item["active"] for item in records),
        "units": records,
    }


def _wait_for_registered_user_units(
    units: Iterable[UserUnit],
    *,
    runner: Runner,
) -> dict[str, Any]:
    """Require enabled units to remain active across a bounded settle window."""

    selected_units = tuple(units)
    stable_samples = 0
    probe: dict[str, Any] = {}
    for attempt in range(SYSTEMD_STATE_SETTLE_ATTEMPTS):
        probe = probe_user_units(selected_units, runner=runner)
        if probe.get("actualRegistered") is True:
            stable_samples += 1
            if stable_samples >= SYSTEMD_STATE_STABLE_SAMPLES:
                return probe
        else:
            stable_samples = 0
        if attempt + 1 < SYSTEMD_STATE_SETTLE_ATTEMPTS:
            time.sleep(SYSTEMD_STATE_SETTLE_INTERVAL_SECONDS)
    return probe


def inspect_user_units(
    units: Iterable[UserUnit],
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Inspect runtime state and persistent definition alignment without writes."""

    selected_units = _validated_units(units)
    root = unit_dir or default_user_unit_dir()
    runtime_supported = platform.system() == "Linux" and bool(_systemctl_binary())
    records: list[dict[str, Any]] = []
    for unit in selected_units:
        target = root / unit.name
        file_state = _unit_file_state(target, expected=unit.content)
        enabled: bool | None = None
        active: bool | None = None
        if runtime_supported:
            state = _snapshot_unit_states((unit.name,), runner=runner)[unit.name]
            enabled = state["enabled"]
            active = state["active"]
        records.append(
            {
                "name": unit.name,
                "path": str(target),
                "enableNow": unit.enable_now,
                "enabled": enabled,
                "active": active,
                **file_state,
            }
        )
    managed = all(item["managed"] is True for item in records)
    definitions_aligned = all(item["aligned"] is True for item in records)
    enabled_records = [item for item in records if item["enableNow"]]
    actual_enabled = (
        all(item["enabled"] is True for item in enabled_records)
        if runtime_supported and enabled_records
        else None
    )
    actual_active = (
        all(item["active"] is True for item in enabled_records)
        if runtime_supported and enabled_records
        else None
    )
    return {
        "provider": "systemd-user",
        "supported": runtime_supported,
        "unitDirectory": str(root),
        "units": records,
        "definitionsPresent": all(item["exists"] is True for item in records),
        "definitionsManaged": managed,
        "definitionsAligned": definitions_aligned,
        "actualEnabled": actual_enabled,
        "actualActive": actual_active,
        "actualRegistered": (
            actual_enabled and actual_active
            if actual_enabled is not None and actual_active is not None
            else None
        ),
    }


def _control_user_units_locked(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    action: str,
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Start, stop, or restart installed Actanara-managed user units."""

    _require_linux()
    if action not in {"start", "stop", "restart"}:
        raise SystemdUserError("systemd user-unit action must be start, stop, or restart")
    selected_units = _validated_units(units)
    root = unit_dir or default_user_unit_dir()
    names = [unit.name for unit in selected_units if unit.enable_now]
    if not names:
        raise SystemdUserError("systemd user-unit action has no runnable units")
    recovery = recover_user_unit_transactions(
        paths,
        runner=runner,
        _runtime_guard_held=True,
    )
    blocked = next(
        (
            item
            for item in recovery
            if item.get("status") in {"active", "conflict"}
        ),
        None,
    )
    if blocked:
        raise SystemdUserError("systemd transaction recovery is blocked by an active transaction or state conflict")
    for unit in selected_units:
        target = root / unit.name
        state = _unit_file_state(target, expected=unit.content)
        if not state["exists"]:
            raise SystemdUserError(f"systemd unit is not installed: {unit.name}")
        if not state["managed"]:
            raise SystemdUserError(f"refusing to control an unmanaged systemd unit: {unit.name}")
        if action in {"start", "restart"} and not state["aligned"]:
            raise SystemdUserError(
                f"systemd unit definition must be reconciled before {action}: {unit.name}"
            )
    _run_systemctl((action, *names), runner=runner)
    expected_active = action != "stop"
    states = _wait_for_active_unit_states(
        names,
        expected_active=expected_active,
        runner=runner,
    )
    if any(state["active"] is not expected_active for state in states.values()):
        raise SystemdUserError(f"systemd user units did not reach the requested {action} state")
    return {
        "status": "stopped" if action == "stop" else "running",
        "action": action,
        "provider": "systemd-user",
        "units": names,
        "states": [{"name": name, **states[name]} for name in names],
        "recoveredTransactions": [item.get("id") for item in recovery],
        "linger": linger_status(runner=runner),
    }


def control_user_units(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    action: str,
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=None)
            return _control_user_units_locked(
                paths,
                units,
                action,
                unit_dir=unit_dir,
                runner=runner,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def restart_dashboard_systemd_service(
    paths: RuntimePaths,
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Restart the configured Dashboard unit without crossing into launchd."""

    from .settings import read_settings

    settings = read_settings(paths, redact_secrets=False, persist_defaults=False)
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    return control_user_units(
        paths,
        [dashboard_unit(paths, dashboard)],
        "restart",
        unit_dir=unit_dir,
        runner=runner,
    )


def enqueue_user_unit_action(
    paths: RuntimePaths,
    *,
    kind: str,
    action: str,
    request_id: str,
    units: Iterable[UserUnit],
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Submit service control to a transient user unit outside the caller cgroup."""

    _require_linux()
    selected_kind = str(kind or "").strip().lower()
    selected_action = str(action or "").strip().lower()
    job_unit = transient_user_action_unit_name(selected_kind, selected_action, request_id)
    systemd_run = _systemd_run_binary()
    systemctl = _systemctl_binary()
    if not systemd_run:
        raise SystemdUserError("systemd-run is unavailable")
    if not systemctl:
        raise SystemdUserError("systemctl is unavailable")

    source = paths.home / "app" / "source"
    try:
        source_generation = source.resolve(strict=True)
    except OSError as exc:
        if runner is subprocess.run:
            raise SystemdUserError(
                "current Runtime source generation is unavailable"
            ) from exc
        # Isolated unit tests use a synthetic Runtime and an injected runner.
        source_generation = Path(__file__).resolve().parents[2]
    if not source_generation.is_dir():
        raise SystemdUserError("current Runtime source generation is unsafe")
    expected_unit_sha256 = user_unit_set_sha256(units)
    python = paths.home / ".venv" / "bin" / "python"
    root = unit_dir or default_user_unit_dir()
    job_base = job_unit.removesuffix(".service")
    python_path = os.pathsep.join((str(source), str(source / "src"), str(source / "src" / "dashboard")))
    command = [
        systemd_run,
        "--user",
        "--no-block",
        "--collect",
        f"--unit={job_base}",
        "--on-active=1s",
        "--timer-property=AccuracySec=1s",
        "--property=Restart=on-failure",
        "--property=RestartSec=5s",
        "--property=RestartPreventExitStatus=1",
        "--property=StartLimitIntervalSec=0",
        f"--working-directory={source}",
        f"--setenv=PYTHONPATH={python_path}",
        f"--setenv=ACTANARA_INSTALL_SYSTEMCTL={systemctl}",
        str(python),
        "-m",
        "data_foundation.systemd_user",
        "service-action",
        "--runtime-home",
        str(paths.home),
        "--kind",
        selected_kind,
        "--action",
        selected_action,
        "--request-id",
        request_id,
        "--source-generation",
        str(source_generation),
        "--expected-unit-sha256",
        expected_unit_sha256,
        "--unit-dir",
        str(root),
    ]
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemdUserError("systemd-run --user could not submit the helper job") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        if len(detail) > 500:
            detail = detail[:497] + "..."
        suffix = f": {detail}" if detail else ""
        raise SystemdUserError(
            f"systemd-run --user failed with status {result.returncode}{suffix}"
        )
    return {
        "provider": "systemd-user",
        "unitName": job_unit,
        "timerName": f"{job_base}.timer",
        "requestId": request_id,
        "sourceGeneration": str(source_generation),
        "expectedUnitSha256": expected_unit_sha256,
        "command": command,
    }


def _snapshot_unit_states(names: Iterable[str], *, runner: Runner) -> dict[str, dict[str, bool]]:
    states: dict[str, dict[str, bool]] = {}
    for name in names:
        enabled = _run_systemctl(("is-enabled", name), runner=runner, allow_status={0, 1, 3, 4})
        active = _run_systemctl(("is-active", name), runner=runner, allow_status={0, 1, 3, 4})
        states[name] = {"enabled": enabled.returncode == 0, "active": active.returncode == 0}
    return states


def _snapshot_transaction_unit_states(
    names: Iterable[str],
    *,
    runner: Runner,
    normalize_failed: bool = False,
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name in names:
        enabled = _run_systemctl(
            ("is-enabled", name),
            runner=runner,
            allow_status={0, 1, 3, 4},
        )
        active = _run_systemctl(
            ("is-active", name),
            runner=runner,
            allow_status={0, 1, 3, 4},
        )
        enabled_token = (enabled.stdout or "").strip().splitlines()[:1]
        active_token = (active.stdout or "").strip().splitlines()[:1]
        enable_state = enabled_token[0].lower() if enabled_token else ""
        active_state = active_token[0].lower() if active_token else ""
        if enable_state in {"", "yes", "no"}:
            enable_state = "enabled" if enabled.returncode == 0 else "disabled"
        elif enable_state == "not-found":
            enable_state = "disabled"
        if active_state in {"", "yes", "no"}:
            active_state = "active" if active.returncode == 0 else "inactive"
        elif active_state == "not-found":
            active_state = "inactive"
        normalized_failed = active_state == "failed" and normalize_failed
        if normalized_failed:
            active_state = "inactive"
        if enable_state not in {"enabled", "disabled"} or active_state not in {
            "active",
            "inactive",
        }:
            raise SystemdUserError(
                f"systemd unit has a non-restorable prior state: {name} "
                f"({enable_state or 'unknown'}/{active_state or 'unknown'})"
            )
        states[name] = {
            "enabled": enable_state == "enabled",
            "active": active_state == "active",
            "enableState": enable_state,
            "activeState": active_state,
            "normalizedFailed": normalized_failed,
        }
    return states


def _wait_for_active_unit_states(
    names: Iterable[str],
    *,
    expected_active: bool,
    runner: Runner,
) -> dict[str, dict[str, bool]]:
    selected_names = tuple(names)
    stable_samples = 0
    states: dict[str, dict[str, bool]] = {}
    for attempt in range(SYSTEMD_STATE_SETTLE_ATTEMPTS):
        states = _snapshot_unit_states(selected_names, runner=runner)
        if all(state["active"] is expected_active for state in states.values()):
            stable_samples += 1
            if stable_samples >= SYSTEMD_STATE_STABLE_SAMPLES:
                return states
        else:
            stable_samples = 0
        if attempt + 1 < SYSTEMD_STATE_SETTLE_ATTEMPTS:
            time.sleep(SYSTEMD_STATE_SETTLE_INTERVAL_SECONDS)
    return states


def _restore_unit_states(
    states: dict[str, dict[str, bool]],
    *,
    runner: Runner,
    transaction_id: str | None = None,
) -> None:
    names = list(states)
    if names:
        try:
            _run_systemctl(("disable", "--now", *names), runner=runner, allow_status={0, 1, 3, 4, 5})
        except Exception:
            pass
        if transaction_id is not None:
            systemd_transaction_checkpoint(
                "after-compensation-disable",
                transaction_id,
            )
    if names:
        try:
            _run_systemctl(("reset-failed", *names), runner=runner, allow_status={0, 1, 3, 4, 5})
        except Exception:
            pass
        if transaction_id is not None:
            systemd_transaction_checkpoint(
                "after-compensation-reset-failed",
                transaction_id,
            )
    for name, state in states.items():
        try:
            if state["enabled"] and state["active"]:
                _run_systemctl(("enable", "--now", name), runner=runner)
            elif state["enabled"]:
                _run_systemctl(("enable", name), runner=runner)
            elif state["active"]:
                _run_systemctl(("start", name), runner=runner)
        except Exception:
            pass
        if transaction_id is not None:
            systemd_transaction_checkpoint(
                f"after-compensation-state:{name}",
                transaction_id,
            )


def _require_linux() -> None:
    if platform.system() != "Linux" and os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        raise SystemdUserError("systemd user units are only supported on Linux")


def _validated_units(units: Iterable[UserUnit]) -> list[UserUnit]:
    selected = list(units)
    if not selected or len({unit.name for unit in selected}) != len(selected):
        raise SystemdUserError("systemd unit set is empty or duplicated")
    if any(not UNIT_NAME_RE.fullmatch(unit.name) for unit in selected):
        raise SystemdUserError("systemd unit name is unsafe")
    return selected


def _unit_file_state(target: Path, *, expected: str | None = None) -> dict[str, Any]:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SystemdUserError(f"systemd unit target is unsafe: {target.name}")
    try:
        content = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"exists": False, "managed": False, "aligned": False, "definitionHash": None}
    except (OSError, UnicodeError) as exc:
        raise SystemdUserError(f"systemd unit target is unreadable: {target.name}") from exc
    managed = content.splitlines()[0] == MANAGED_UNIT_HEADER if content.splitlines() else False
    return {
        "exists": True,
        "managed": managed,
        "aligned": content == expected if expected is not None else None,
        "definitionHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _bytes_hash(content: bytes | None) -> str:
    return "missing" if content is None else hashlib.sha256(content).hexdigest()


def _resource_hash(path: Path) -> str:
    return _bytes_hash(_read_optional_bytes(path))


def _systemd_transaction_root(paths: RuntimePaths) -> Path:
    return paths.state_dir / "systemd-transactions"


@contextmanager
def _systemd_transaction_lock(paths: RuntimePaths):
    root = _systemd_transaction_root(paths)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with (root / ".lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_systemd_journal(transaction_dir: Path, journal: dict[str, Any]) -> None:
    _atomic_write_bytes(
        transaction_dir / "journal.json",
        (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def _read_systemd_journal(transaction_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((transaction_dir / "journal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _advance_systemd_transaction(
    transaction_dir: Path,
    journal: dict[str, Any],
    phase: str,
    *,
    status: str | None = None,
) -> None:
    journal["phase"] = phase
    if status is not None:
        journal["status"] = status
    _write_systemd_journal(transaction_dir, journal)


def _begin_systemd_transaction(
    paths: RuntimePaths,
    *,
    action: str,
    units: list[UserUnit],
    unit_dir: Path,
    prior_states: dict[str, dict[str, Any]],
    transaction_context: dict[str, str] | None,
) -> tuple[Path, dict[str, Any]]:
    transaction_id = uuid.uuid4().hex
    owner_process_identity = _systemd_transaction_process_identity(os.getpid())
    if owner_process_identity is None:
        raise SystemdUserError("systemd transaction owner identity is unavailable")
    transaction_root = _systemd_transaction_root(paths)
    transaction_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction_root.chmod(0o700)
    transaction_dir = transaction_root / transaction_id
    staging_dir = transaction_root.parent / f".{transaction_root.name}-{transaction_id}.pending"
    context = transaction_context if isinstance(transaction_context, dict) else {}
    owner_id = context.get("ownerId")
    if owner_id is not None and (
        not isinstance(owner_id, str)
        or not owner_id
        or len(owner_id) > 256
        or any(character in owner_id for character in "\x00\r\n")
    ):
        raise SystemdUserError("systemd transaction owner identity is invalid")
    settings_transaction_id = context.get("id")
    if settings_transaction_id is not None and (
        not isinstance(settings_transaction_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", settings_transaction_id) is None
    ):
        raise SystemdUserError("coupled Settings transaction identity is invalid")
    staging_dir.mkdir(mode=0o700)
    try:
        staging_dir.chmod(0o700)
        records: list[dict[str, Any]] = []
        for unit in units:
            target = unit_dir / unit.name
            before = _read_optional_bytes(target)
            if before is not None:
                _atomic_write_bytes(staging_dir / f"{unit.name}.before", before)
            desired = unit.content.encode("utf-8") if action == "install" else None
            records.append(
                {
                    "name": unit.name,
                    "enableNow": unit.enable_now,
                    "beforeExists": before is not None,
                    "beforeHash": _bytes_hash(before),
                    "afterHash": _bytes_hash(desired),
                    "beforeMode": (target.stat().st_mode & 0o777) if before is not None else None,
                    "priorState": prior_states[unit.name],
                }
            )
        journal = {
            "schemaVersion": 1,
            "id": transaction_id,
            "ownerId": owner_id,
            "ownerPid": os.getpid(),
            "ownerProcessIdentity": owner_process_identity,
            "status": "active",
            "phase": "prior-captured",
            "action": action,
            "provider": "systemd-user",
            "unitDirectory": str(unit_dir),
            "settingsTransactionId": settings_transaction_id,
            "settingsBeforeHash": context.get("settingsBeforeHash"),
            "settingsAfterHash": context.get("settingsAfterHash"),
            "units": records,
        }
        _write_systemd_journal(staging_dir, journal)
        _fsync_directory(staging_dir)
        systemd_transaction_checkpoint("before-transaction-publish", transaction_id)
        os.rename(staging_dir, transaction_dir)
        _fsync_directory(transaction_root)
        _fsync_directory(transaction_root.parent)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    systemd_transaction_checkpoint("after-prior-captured", transaction_id)
    return transaction_dir, journal


def _transaction_targets(journal: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    root = Path(str(journal.get("unitDirectory") or ""))
    if not root.is_absolute():
        raise SystemdUserError("systemd transaction unit directory is unsafe")
    records = journal.get("units") if isinstance(journal.get("units"), list) else []
    if not records:
        raise SystemdUserError("systemd transaction has no units")
    for record in records:
        if not isinstance(record, dict) or not UNIT_NAME_RE.fullmatch(str(record.get("name") or "")):
            raise SystemdUserError("systemd transaction unit name is unsafe")
    return root, records


def _transaction_has_conflict(journal: dict[str, Any]) -> bool:
    root, records = _transaction_targets(journal)
    return any(
        _resource_hash(root / str(record["name"]))
        not in {record.get("beforeHash"), record.get("afterHash")}
        for record in records
    )


def _transaction_runtime_state_has_conflict(
    journal: dict[str, Any],
    *,
    runner: Runner,
) -> bool:
    _root, records = _transaction_targets(journal)
    try:
        current = _snapshot_transaction_unit_states(
            (str(record["name"]) for record in records),
            runner=runner,
            normalize_failed=True,
        )
    except SystemdUserError:
        return True
    action = str(journal.get("action") or "")
    phase = str(journal.get("phase") or "")
    for record in records:
        name = str(record["name"])
        prior = record.get("priorState") if isinstance(record.get("priorState"), dict) else {}
        before = (bool(prior.get("enabled")), bool(prior.get("active")))
        after = (
            (False, False)
            if action == "uninstall"
            else (True, True)
            if action == "install" and bool(record.get("enableNow"))
            else before
        )
        if phase == "compensation-armed":
            allowed = {
                (enabled, active)
                for enabled in {before[0], after[0]}
                for active in {before[1], after[1]}
            }
        elif phase in {"prior-captured", "definitions-applied"}:
            allowed = {before}
        elif phase == "external-apply-armed":
            # A killed systemctl may have changed either dimension first, but
            # an unchanged dimension must never drift outside its before/after
            # values.
            allowed = {
                (enabled, active)
                for enabled in {before[0], after[0]}
                for active in {before[1], after[1]}
            }
        elif phase == "restart-armed" and name in set(journal.get("restartNames") or []):
            # restart can synchronously stop an otherwise unchanged active
            # unit before its replacement process reaches active.
            allowed = {after, (after[0], False)}
        elif action == "install" and phase == "external-applied" and bool(record.get("enableNow")):
            # The candidate can fail after enable --now returned but before
            # readiness verification. Preserve that owned failure for safe
            # compensation without accepting arbitrary enable-state drift.
            allowed = {after, (after[0], False)}
        else:
            allowed = {after}
        observed = (bool(current[name]["enabled"]), bool(current[name]["active"]))
        if observed not in allowed:
            return True
    return False


def _require_transaction_definition_before(
    root: Path,
    journal: dict[str, Any],
    name: str,
) -> None:
    _journal_root, records = _transaction_targets(journal)
    record = next((item for item in records if item.get("name") == name), None)
    if record is None or _resource_hash(root / name) != record.get("beforeHash"):
        raise SystemdUserError(
            f"systemd unit definition changed before transaction apply: {name}"
        )


def _restore_systemd_transaction(
    transaction_dir: Path,
    journal: dict[str, Any],
    *,
    runner: Runner,
) -> None:
    if _transaction_has_conflict(journal):
        raise SystemdUserError("systemd transaction recovery found a definition conflict")
    if _transaction_runtime_state_has_conflict(journal, runner=runner):
        raise SystemdUserError("systemd transaction recovery found a runtime-state conflict")
    transaction_id = str(journal.get("id") or transaction_dir.name)
    if journal.get("phase") != "compensation-armed":
        _advance_systemd_transaction(
            transaction_dir,
            journal,
            "compensation-armed",
            status="active",
        )
    systemd_transaction_checkpoint("after-compensation-armed", transaction_id)
    root, records = _transaction_targets(journal)
    states: dict[str, dict[str, bool]] = {}
    for record in records:
        name = str(record["name"])
        target = root / name
        if bool(record.get("beforeExists")):
            snapshot = transaction_dir / f"{name}.before"
            if not snapshot.is_file():
                raise SystemdUserError("systemd transaction snapshot is missing")
            _atomic_write_bytes(target, snapshot.read_bytes())
            mode = record.get("beforeMode")
            if isinstance(mode, int):
                target.chmod(mode)
        else:
            target.unlink(missing_ok=True)
        systemd_transaction_checkpoint(
            f"after-compensation-definition:{name}",
            transaction_id,
        )
        prior = record.get("priorState") if isinstance(record.get("priorState"), dict) else {}
        states[name] = {"enabled": bool(prior.get("enabled")), "active": bool(prior.get("active"))}
    _run_systemctl(("daemon-reload",), runner=runner)
    systemd_transaction_checkpoint(
        "after-compensation-daemon-reload",
        transaction_id,
    )
    _restore_unit_states(
        states,
        runner=runner,
        transaction_id=transaction_id,
    )
    restored_states = _snapshot_unit_states(states, runner=runner)
    if restored_states != states:
        raise SystemdUserError("systemd transaction could not restore prior runtime state")


def _desired_systemd_transaction_matches(journal: dict[str, Any], *, runner: Runner) -> bool:
    root, records = _transaction_targets(journal)
    if any(_resource_hash(root / str(record["name"])) != record.get("afterHash") for record in records):
        return False
    try:
        states = _snapshot_transaction_unit_states(
            (str(record["name"]) for record in records),
            runner=runner,
        )
    except SystemdUserError:
        return False
    action = str(journal.get("action") or "")
    for record in records:
        prior = record.get("priorState") if isinstance(record.get("priorState"), dict) else {}
        before = (bool(prior.get("enabled")), bool(prior.get("active")))
        expected = (
            (False, False)
            if action == "uninstall"
            else (True, True)
            if action == "install" and bool(record.get("enableNow"))
            else before
        )
        current = states[str(record["name"])]
        if (bool(current["enabled"]), bool(current["active"])) != expected:
            return False
    return True


def _coupled_settings_transaction_status(
    paths: RuntimePaths,
    journal: dict[str, Any],
) -> tuple[bool, str | None]:
    transaction_id = journal.get("settingsTransactionId")
    if transaction_id is None:
        return False, None
    if (
        not isinstance(transaction_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None
    ):
        return True, "unsafe"
    transaction_dir = paths.state_dir / "settings-transactions" / transaction_id
    try:
        metadata = transaction_dir.stat(follow_symlinks=False)
        payload = json.loads(
            (transaction_dir / "journal.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True, "unavailable"
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or not isinstance(payload, dict)
        or payload.get("id") != transaction_id
    ):
        return True, "unsafe"
    status = payload.get("status")
    return True, str(status) if isinstance(status, str) else "unsafe"


def _recover_user_unit_transactions_locked(
    paths: RuntimePaths,
    *,
    runner: Runner = subprocess.run,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recover interrupted mutations, optionally limited to one durable owner."""

    root = _systemd_transaction_root(paths)
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    with _systemd_transaction_lock(paths):
        for transaction_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            journal = _read_systemd_journal(transaction_dir)
            if owner_id is not None and (
                not journal or journal.get("ownerId") != owner_id
            ):
                continue
            if not journal:
                results.append({"id": transaction_dir.name, "status": "conflict", "phase": "journal-unreadable"})
                continue
            if journal.get("status") in {"committed", "compensated"}:
                continue
            transaction_id = str(journal.get("id") or transaction_dir.name)
            try:
                definition_conflict = _transaction_has_conflict(journal)
            except Exception:
                definition_conflict = True
            if definition_conflict:
                _advance_systemd_transaction(transaction_dir, journal, "recovery-conflict", status="conflict")
                results.append({"id": transaction_id, "status": "conflict", "phase": "definition-conflict"})
                continue
            settings_before = journal.get("settingsBeforeHash")
            settings_after = journal.get("settingsAfterHash")
            settings_hash = _resource_hash(paths.config_dir / "settings.json")
            coupled, settings_transaction_status = _coupled_settings_transaction_status(
                paths,
                journal,
            )
            if coupled and settings_transaction_status not in {
                "active",
                "committed",
                "compensated",
            }:
                _advance_systemd_transaction(
                    transaction_dir,
                    journal,
                    "settings-transaction-unavailable",
                    status="conflict",
                )
                results.append(
                    {
                        "id": transaction_id,
                        "status": "conflict",
                        "phase": "settings-transaction-unavailable",
                    }
                )
                continue
            if coupled and settings_transaction_status == "active":
                # Hash equality is ambiguous for a no-op Settings payload.
                # Wait for Settings recovery/finalization to make the durable
                # commit-vs-compensate decision first.
                results.append(
                    {
                        "id": transaction_id,
                        "status": "active",
                        "phase": "awaiting-settings-transaction",
                    }
                )
                continue
            if (
                settings_after
                and settings_hash == settings_after
                and (not coupled or settings_transaction_status == "committed")
            ):
                if _desired_systemd_transaction_matches(journal, runner=runner):
                    _advance_systemd_transaction(transaction_dir, journal, "recovered-committed", status="committed")
                    results.append({"id": transaction_id, "status": "committed", "phase": "recovered-committed"})
                else:
                    _advance_systemd_transaction(transaction_dir, journal, "desired-state-conflict", status="conflict")
                    results.append({"id": transaction_id, "status": "conflict", "phase": "desired-state-conflict"})
                continue
            if coupled and settings_transaction_status == "committed":
                _advance_systemd_transaction(
                    transaction_dir,
                    journal,
                    "committed-settings-cas-conflict",
                    status="conflict",
                )
                results.append(
                    {
                        "id": transaction_id,
                        "status": "conflict",
                        "phase": "committed-settings-cas-conflict",
                    }
                )
                continue
            if owner_id is None and _same_systemd_transaction_owner(
                int(journal.get("ownerPid") or 0),
                journal.get("ownerProcessIdentity"),
            ):
                # Before Settings commits, a matching live owner still owns
                # the precommit transaction. Once Settings-after and the
                # exact desired systemd state are visible, the durable
                # postcondition above is authoritative even if the final ACK
                # journal write raced another caller.
                results.append(
                    {
                        "id": transaction_id,
                        "status": "active",
                        "phase": str(journal.get("phase") or "active"),
                    }
                )
                continue
            if settings_before and settings_hash != settings_before:
                _advance_systemd_transaction(transaction_dir, journal, "settings-cas-conflict", status="conflict")
                results.append({"id": transaction_id, "status": "conflict", "phase": "settings-cas-conflict"})
                continue
            if _transaction_runtime_state_has_conflict(journal, runner=runner):
                _advance_systemd_transaction(
                    transaction_dir,
                    journal,
                    "runtime-state-conflict",
                    status="conflict",
                )
                results.append(
                    {
                        "id": transaction_id,
                        "status": "conflict",
                        "phase": "runtime-state-conflict",
                    }
                )
                continue
            try:
                _restore_systemd_transaction(transaction_dir, journal, runner=runner)
            except Exception:
                if journal.get("phase") == "compensation-armed":
                    results.append(
                        {
                            "id": transaction_id,
                            "status": "active",
                            "phase": "compensation-armed",
                        }
                    )
                else:
                    _advance_systemd_transaction(
                        transaction_dir,
                        journal,
                        "recovery-incomplete",
                        status="conflict",
                    )
                    results.append(
                        {
                            "id": transaction_id,
                            "status": "conflict",
                            "phase": "recovery-incomplete",
                        }
                    )
            else:
                _advance_systemd_transaction(transaction_dir, journal, "recovered-prior", status="compensated")
                results.append({"id": transaction_id, "status": "compensated", "phase": "recovered-prior"})
    return results


def recover_user_unit_transactions(
    paths: RuntimePaths,
    *,
    runner: Runner = subprocess.run,
    owner_id: str | None = None,
    _runtime_guard_held: bool = False,
) -> list[dict[str, Any]]:
    """Recover interrupted mutations without racing a Runtime transaction."""

    try:
        if _runtime_guard_held:
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            return _recover_user_unit_transactions_locked(
                paths,
                runner=runner,
                owner_id=owner_id,
            )
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            return _recover_user_unit_transactions_locked(
                paths,
                runner=runner,
                owner_id=owner_id,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def finalize_user_unit_transaction(
    paths: RuntimePaths,
    transaction_id: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    transaction_dir = _systemd_transaction_root(paths) / transaction_id
    journal = _read_systemd_journal(transaction_dir)
    if not journal or str(journal.get("id")) != transaction_id:
        raise SystemdUserError("systemd transaction journal is unavailable")
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            owner_id = journal.get("ownerId") if isinstance(journal.get("ownerId"), str) else None
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            journal = _read_systemd_journal(transaction_dir)
            if not journal or str(journal.get("id")) != transaction_id:
                raise SystemdUserError("systemd transaction journal is unavailable")
            if not _desired_systemd_transaction_matches(journal, runner=runner):
                raise SystemdUserError("systemd transaction desired state is no longer aligned")
            try:
                _advance_systemd_transaction(transaction_dir, journal, "committed", status="committed")
            except Exception:
                durable = _read_systemd_journal(transaction_dir)
                if durable.get("status") == "committed":
                    return
                recovery = recover_user_unit_transactions(
                    paths,
                    runner=runner,
                    owner_id=owner_id,
                    _runtime_guard_held=True,
                )
                if any(
                    item.get("id") == transaction_id and item.get("status") == "committed"
                    for item in recovery
                ):
                    return
                raise
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def rollback_user_unit_transaction(
    paths: RuntimePaths,
    transaction_id: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    transaction_dir = _systemd_transaction_root(paths) / transaction_id
    journal = _read_systemd_journal(transaction_dir)
    if not journal or str(journal.get("id")) != transaction_id:
        raise SystemdUserError("systemd transaction journal is unavailable")
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            owner_id = journal.get("ownerId") if isinstance(journal.get("ownerId"), str) else None
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            _restore_systemd_transaction(transaction_dir, journal, runner=runner)
            _advance_systemd_transaction(transaction_dir, journal, "compensated", status="compensated")
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def linger_status(*, runner: Runner = subprocess.run) -> dict:
    loginctl = shutil.which("loginctl")
    if platform.system() != "Linux" or not loginctl:
        return {"status": "unknown", "enabled": None, "changed": False}
    result = runner(
        [loginctl, "show-user", str(os.getuid()), "--property=Linger", "--value"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    value = result.stdout.strip().lower() if result.returncode == 0 else ""
    enabled = value == "yes" if value in {"yes", "no"} else None
    return {
        "status": "enabled" if enabled is True else "disabled" if enabled is False else "unknown",
        "enabled": enabled,
        "changed": False,
        "note": "Actanara changes linger only after explicit user authorization.",
    }


def enable_linger(*, runner: Runner = subprocess.run) -> dict:
    """Enable linger for the current user without invoking sudo.

    Linger is shared user-level host state rather than an Actanara-owned
    resource.  Callers must obtain explicit operator authorization before
    crossing this boundary, and uninstall workflows must never disable it.
    """

    if platform.system() != "Linux" and os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        raise SystemdUserError("systemd linger is only supported on Linux")
    loginctl = shutil.which("loginctl")
    if not loginctl:
        raise SystemdUserError("loginctl is unavailable; linger could not be enabled")
    before = linger_status(runner=runner)
    if before.get("enabled") is True:
        return {
            **before,
            "action": "already-enabled",
            "authorization": "explicit-user-choice",
        }
    command = [loginctl, "enable-linger", str(os.getuid())]
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemdUserError("loginctl could not request linger for the current user") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        if len(detail) > 500:
            detail = detail[:497] + "..."
        suffix = f": {detail}" if detail else ""
        raise SystemdUserError(
            f"loginctl enable-linger failed with status {result.returncode}{suffix}"
        )
    after = linger_status(runner=runner)
    if after.get("enabled") is not True:
        raise SystemdUserError("loginctl returned success but linger is not enabled")
    return {
        **after,
        "changed": True,
        "action": "enabled",
        "authorization": "explicit-user-choice",
    }


def install_user_units(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
    restart_active: bool = True,
    defer_commit: bool = False,
    transaction_context: dict[str, str] | None = None,
    recover_transactions: bool = True,
    readiness_verifier: Callable[[], Any] | None = None,
    normalize_failed_prior_states: bool = False,
) -> dict:
    owner_id = (
        transaction_context.get("ownerId")
        if isinstance(transaction_context, dict)
        and isinstance(transaction_context.get("ownerId"), str)
        else None
    )
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            return _install_user_units_guarded(
                paths,
                units,
                unit_dir=unit_dir,
                runner=runner,
                restart_active=restart_active,
                defer_commit=defer_commit,
                transaction_context=transaction_context,
                recover_transactions=recover_transactions,
                readiness_verifier=readiness_verifier,
                normalize_failed_prior_states=normalize_failed_prior_states,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def _install_user_units_guarded(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
    restart_active: bool = True,
    defer_commit: bool = False,
    transaction_context: dict[str, str] | None = None,
    recover_transactions: bool = True,
    readiness_verifier: Callable[[], Any] | None = None,
    normalize_failed_prior_states: bool = False,
) -> dict:
    _require_linux()
    selected_units = _validated_units(units)
    root = unit_dir or default_user_unit_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    backup_root = paths.state_dir / "backups" / "systemd" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    enabled_names = [unit.name for unit in selected_units if unit.enable_now]
    names = [unit.name for unit in selected_units]
    owner_id = (
        transaction_context.get("ownerId")
        if isinstance(transaction_context, dict)
        and isinstance(transaction_context.get("ownerId"), str)
        else None
    )
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            recovery_owner = owner_id if durable_runtime_mutation_owner(paths.home) else None
            recovery = (
                recover_user_unit_transactions(
                    paths,
                    runner=runner,
                    owner_id=recovery_owner,
                    _runtime_guard_held=True,
                )
                if recover_transactions
                else []
            )
            if any(item.get("status") in {"active", "conflict"} for item in recovery):
                raise SystemdUserError(
                    "systemd transaction recovery is blocked by an active transaction or state conflict"
                )
            prior_content: dict[str, bytes | None] = {}
            for unit in selected_units:
                target = root / unit.name
                state = _unit_file_state(target, expected=unit.content)
                if state["exists"] and not state["managed"]:
                    raise SystemdUserError(
                        f"refusing to replace an unmanaged systemd unit: {unit.name}"
                    )
                prior_content[unit.name] = _read_optional_bytes(target)
            prior_states = _snapshot_transaction_unit_states(
                names,
                runner=runner,
                normalize_failed=normalize_failed_prior_states,
            )
            changed_names = [
                unit.name
                for unit in selected_units
                if prior_content[unit.name] != unit.content.encode("utf-8")
            ]
            transaction_dir, journal = _begin_systemd_transaction(
                paths,
                action="install",
                units=selected_units,
                unit_dir=root,
                prior_states=prior_states,
                transaction_context=transaction_context,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc
    readiness: Any = None
    try:
        failed_names = [
            name
            for name, state in prior_states.items()
            if state.get("normalizedFailed") is True
        ]
        if failed_names:
            _run_systemctl(("reset-failed", *failed_names), runner=runner)
        for unit in selected_units:
            target = root / unit.name
            _require_transaction_definition_before(root, journal, unit.name)
            if target.exists():
                backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup = backup_root / unit.name
                shutil.copy2(target, backup, follow_symlinks=False)
                backup.chmod(0o600)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{unit.name}.", dir=root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(unit.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                _require_transaction_definition_before(root, journal, unit.name)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        _advance_systemd_transaction(transaction_dir, journal, "definitions-applied")
        systemd_transaction_checkpoint("after-definitions-applied", str(journal["id"]))
        _run_systemctl(("daemon-reload",), runner=runner)
        _advance_systemd_transaction(transaction_dir, journal, "external-apply-armed")
        systemd_transaction_checkpoint("before-external-apply", str(journal["id"]))
        if enabled_names:
            _run_systemctl(("enable", "--now", *enabled_names), runner=runner)
        restarted_names = [
            name
            for name in enabled_names
            if restart_active and prior_states[name]["active"]
        ]
        if restarted_names:
            journal["restartNames"] = restarted_names
            _advance_systemd_transaction(
                transaction_dir,
                journal,
                "restart-armed",
            )
            systemd_transaction_checkpoint("before-restart", str(journal["id"]))
            _run_systemctl(("restart", *restarted_names), runner=runner)
        _advance_systemd_transaction(transaction_dir, journal, "external-applied")
        systemd_transaction_checkpoint("after-external-applied", str(journal["id"]))
        probe = _wait_for_registered_user_units(selected_units, runner=runner)
        if enabled_names and probe.get("actualRegistered") is not True:
            raise SystemdUserError("systemd user units did not become enabled and active")
        alignment = inspect_user_units(selected_units, unit_dir=root, runner=runner)
        if alignment.get("definitionsAligned") is not True:
            raise SystemdUserError("systemd user-unit definitions are not aligned after install")
        if readiness_verifier is not None:
            try:
                readiness = readiness_verifier()
            except Exception as exc:
                raise SystemdUserError("systemd user-unit readiness verification failed") from exc
            if isinstance(readiness, dict) and readiness.get("ready") is not True:
                raise SystemdUserError("systemd user-unit readiness verification failed")
        _advance_systemd_transaction(transaction_dir, journal, "external-verified")
        systemd_transaction_checkpoint("after-external-verified", str(journal["id"]))
        if not defer_commit:
            _advance_systemd_transaction(transaction_dir, journal, "committed", status="committed")
    except Exception as exc:
        try:
            _restore_systemd_transaction(transaction_dir, journal, runner=runner)
        except Exception as recovery_exc:
            if journal.get("phase") != "compensation-armed":
                _advance_systemd_transaction(
                    transaction_dir,
                    journal,
                    "compensation-incomplete",
                    status="conflict",
                )
            raise SystemdUserCompensationError(
                "systemd unit install failed and prior state restoration is incomplete"
            ) from recovery_exc
        else:
            _advance_systemd_transaction(transaction_dir, journal, "compensated", status="compensated")
        raise exc
    return {
        "status": "installed",
        "provider": "systemd-user",
        "unitDirectory": str(root),
        "units": [unit.name for unit in selected_units],
        "enabledUnits": [unit.name for unit in selected_units if unit.enable_now],
        "changedUnits": changed_names,
        "restartedUnits": restarted_names,
        "backupDirectory": str(backup_root) if backup_root.exists() else None,
        "probe": probe,
        "alignment": alignment,
        "readiness": readiness,
        "transactionId": str(journal["id"]),
        "transactionStatus": "pending-settings" if defer_commit else "committed",
        "recoveredTransactions": [item.get("id") for item in recovery],
        "linger": linger_status(runner=runner),
    }


def uninstall_user_units(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
    defer_commit: bool = False,
    transaction_context: dict[str, str] | None = None,
    recover_transactions: bool = True,
    normalize_failed_prior_states: bool = False,
) -> dict:
    owner_id = (
        transaction_context.get("ownerId")
        if isinstance(transaction_context, dict)
        and isinstance(transaction_context.get("ownerId"), str)
        else None
    )
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            return _uninstall_user_units_guarded(
                paths,
                units,
                unit_dir=unit_dir,
                runner=runner,
                defer_commit=defer_commit,
                transaction_context=transaction_context,
                recover_transactions=recover_transactions,
                normalize_failed_prior_states=normalize_failed_prior_states,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc


def _uninstall_user_units_guarded(
    paths: RuntimePaths,
    units: Iterable[UserUnit],
    *,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
    defer_commit: bool = False,
    transaction_context: dict[str, str] | None = None,
    recover_transactions: bool = True,
    normalize_failed_prior_states: bool = False,
) -> dict:
    _require_linux()
    selected_units = _validated_units(units)
    root = unit_dir or default_user_unit_dir()
    names = [unit.name for unit in selected_units]
    backup_root = paths.state_dir / "backups" / "systemd" / (
        datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-remove"
    )
    owner_id = (
        transaction_context.get("ownerId")
        if isinstance(transaction_context, dict)
        and isinstance(transaction_context.get("ownerId"), str)
        else None
    )
    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=owner_id)
            recovery_owner = owner_id if durable_runtime_mutation_owner(paths.home) else None
            recovery = (
                recover_user_unit_transactions(
                    paths,
                    runner=runner,
                    owner_id=recovery_owner,
                    _runtime_guard_held=True,
                )
                if recover_transactions
                else []
            )
            if any(item.get("status") in {"active", "conflict"} for item in recovery):
                raise SystemdUserError(
                    "systemd transaction recovery is blocked by an active transaction or state conflict"
                )
            targets: list[Path] = []
            for unit in selected_units:
                target = root / unit.name
                state = _unit_file_state(target)
                if not state["exists"]:
                    continue
                if not state["managed"]:
                    raise SystemdUserError(
                        f"refusing to remove an unmanaged systemd unit: {unit.name}"
                    )
                targets.append(target)
            prior_states = _snapshot_transaction_unit_states(
                names,
                runner=runner,
                normalize_failed=normalize_failed_prior_states,
            )
            transaction_dir, journal = _begin_systemd_transaction(
                paths,
                action="uninstall",
                units=selected_units,
                unit_dir=root,
                prior_states=prior_states,
                transaction_context=transaction_context,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise SystemdUserError(str(exc)) from exc
    try:
        failed_names = [
            name
            for name, state in prior_states.items()
            if state.get("normalizedFailed") is True
        ]
        if failed_names:
            _run_systemctl(("reset-failed", *failed_names), runner=runner)
        for target in targets:
            _require_transaction_definition_before(root, journal, target.name)
            backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = backup_root / target.name
            shutil.copy2(target, backup, follow_symlinks=False)
            backup.chmod(0o600)
        _advance_systemd_transaction(transaction_dir, journal, "external-apply-armed")
        systemd_transaction_checkpoint("before-external-apply", str(journal["id"]))
        _run_systemctl(("disable", "--now", *names), runner=runner, allow_status={0, 1, 3, 4, 5})
        for target in targets:
            _require_transaction_definition_before(root, journal, target.name)
            target.unlink()
        _advance_systemd_transaction(transaction_dir, journal, "definitions-removed")
        systemd_transaction_checkpoint("after-definitions-removed", str(journal["id"]))
        _run_systemctl(("daemon-reload",), runner=runner)
        _run_systemctl(("reset-failed", *names), runner=runner, allow_status={0, 1, 3, 4, 5})
        _advance_systemd_transaction(transaction_dir, journal, "external-applied")
        systemd_transaction_checkpoint("after-external-applied", str(journal["id"]))
        remaining = _snapshot_unit_states(names, runner=runner)
        if any(state["enabled"] or state["active"] for state in remaining.values()):
            raise SystemdUserError("systemd user units remained enabled or active after removal")
        if any((root / name).exists() for name in names):
            raise SystemdUserError("systemd user-unit definitions remained after removal")
        _advance_systemd_transaction(transaction_dir, journal, "external-verified")
        systemd_transaction_checkpoint("after-external-verified", str(journal["id"]))
        if not defer_commit:
            _advance_systemd_transaction(transaction_dir, journal, "committed", status="committed")
    except Exception as exc:
        try:
            _restore_systemd_transaction(transaction_dir, journal, runner=runner)
        except Exception as recovery_exc:
            if journal.get("phase") != "compensation-armed":
                _advance_systemd_transaction(
                    transaction_dir,
                    journal,
                    "compensation-incomplete",
                    status="conflict",
                )
            raise SystemdUserCompensationError(
                "systemd unit uninstall failed and prior state restoration is incomplete"
            ) from recovery_exc
        else:
            _advance_systemd_transaction(transaction_dir, journal, "compensated", status="compensated")
        raise exc
    return {
        "status": "uninstalled",
        "provider": "systemd-user",
        "unitDirectory": str(root),
        "units": names,
        "removedUnits": [target.name for target in targets],
        "backupDirectory": str(backup_root) if backup_root.exists() else None,
        "transactionId": str(journal["id"]),
        "transactionStatus": "pending-settings" if defer_commit else "committed",
        "recoveredTransactions": [item.get("id") for item in recovery],
        "probe": {
            "status": "not-registered",
            "actualRegistered": False,
            "units": [
                {"name": name, **remaining[name]}
                for name in names
            ],
        },
        "linger": linger_status(runner=runner),
    }


def _service_action_units(paths: RuntimePaths, kind: str) -> list[UserUnit]:
    from .settings import read_settings

    settings = read_settings(paths, redact_secrets=False, persist_defaults=False)
    return _service_action_units_from_settings(paths, kind, settings)


def _service_action_units_from_settings(
    paths: RuntimePaths,
    kind: str,
    settings: dict[str, Any],
) -> list[UserUnit]:
    if kind == "dashboard":
        dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
        return [dashboard_unit(paths, dashboard)]
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    return [rag_unit(paths, server)]


def _queued_registration_from_settings(
    settings: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    if kind == "dashboard":
        dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
        registration = dashboard.get("systemdUser")
    else:
        rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
        server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
        registration = server.get("systemdUser")
    return registration if isinstance(registration, dict) else {}


def _queued_registration(paths: RuntimePaths, kind: str) -> dict[str, Any]:
    from .settings import read_settings

    settings = read_settings(paths, redact_secrets=False, persist_defaults=False)
    return _queued_registration_from_settings(settings, kind)


def _queued_previous_registration_state(
    registration: dict[str, Any],
) -> dict[str, Any] | None:
    previous = registration.get("pendingPreviousState")
    if not isinstance(previous, dict):
        return None
    enabled = previous.get("serverEnabled")
    registered = previous.get("registered")
    units = previous.get("units")
    if (
        type(enabled) is not bool
        or type(registered) is not bool
        or not isinstance(units, list)
        or any(not isinstance(name, str) or not UNIT_NAME_RE.fullmatch(name) for name in units)
    ):
        return None
    return {
        "serverEnabled": enabled,
        "registered": registered,
        "units": list(units),
    }


def _record_queued_registration_result(
    paths: RuntimePaths,
    *,
    kind: str,
    action: str,
    request_id: str,
    error: str | None,
    precommit_side_effects: Callable[[dict[str, str]], Callable[[], None] | None]
    | None = None,
    postcommit_side_effects: Callable[[dict[str, str]], None] | None = None,
    expected_units: list[tuple[str, str, bool]] | None = None,
) -> dict[str, Any] | None:
    from .settings import write_service_manager_settings

    registration = _queued_registration(paths, kind)
    if (
        registration.get("pendingRequestId") != request_id
        or registration.get("pendingAction") != action
    ):
        return None
    previous = _queued_previous_registration_state(registration) if error else None
    metadata = {
        **registration,
        **(
            {
                "registered": previous["registered"],
                "units": previous["units"],
            }
            if previous is not None
            else {}
        ),
        "lastActionStatus": "failed" if error else "success",
        "lastError": error,
        "lastErrorAt": datetime.now().astimezone().isoformat() if error else None,
        "pendingAction": None,
        "pendingRequestId": None,
        "pendingJobUnit": None,
        "pendingPreviousState": None,
    }
    if kind == "dashboard":
        dashboard_update: dict[str, Any] = {"systemdUser": metadata}
        if previous is not None:
            dashboard_update["server"] = {"enabled": previous["serverEnabled"]}
        update = {"dashboard": dashboard_update}
    else:
        server_update: dict[str, Any] = {"systemdUser": metadata}
        if previous is not None:
            server_update["enabled"] = previous["serverEnabled"]
        update = {"rag": {"server": server_update}}
    def require_pending(current: dict[str, Any]) -> None:
        current_registration = _queued_registration_from_settings(current, kind)
        if (
            current_registration.get("pendingRequestId") != request_id
            or current_registration.get("pendingAction") != action
        ):
            raise _QueuedActionStale("queued systemd service action is stale")
        if expected_units is not None:
            actual_units = [
                (unit.name, unit.content, unit.enable_now)
                for unit in _service_action_units_from_settings(paths, kind, current)
            ]
            if actual_units != expected_units:
                raise _QueuedActionStale(
                    "queued systemd service Settings changed while the unit handoff was prepared"
                )

    try:
        return write_service_manager_settings(
            update,
            paths,
            precommit_side_effects=(
                precommit_side_effects
                if precommit_side_effects is not None
                else lambda _context: None
            ),
            postcommit_side_effects=postcommit_side_effects,
            current_validator=require_pending,
        )
    except _QueuedActionStale:
        return None


def execute_queued_user_unit_action(
    paths: RuntimePaths,
    *,
    kind: str,
    action: str,
    request_id: str,
    unit_dir: Path,
    runner: Runner = subprocess.run,
    loaded_source_root: Path | None = None,
    expected_unit_sha256: str | None = None,
) -> dict[str, Any]:
    """Execute a previously accepted transient action in the helper cgroup."""

    _require_linux()
    try:
        with runtime_mutation_guard(paths.home, blocking=True):
            require_runtime_mutation_owner(paths.home, owner_id=None)
            if loaded_source_root is not None:
                try:
                    current_source_root = (paths.home / "app" / "source").resolve(
                        strict=True
                    )
                except OSError as exc:
                    raise SystemdUserError(
                        "queued systemd helper cannot resolve the current Runtime source"
                    ) from exc
                if current_source_root != loaded_source_root.resolve(strict=False):
                    raise _QueuedHelperReloadRequired(
                        "queued systemd helper must reload the current Runtime generation"
                    )
            return _execute_queued_user_unit_action_guarded(
                paths,
                kind=kind,
                action=action,
                request_id=request_id,
                unit_dir=unit_dir,
                runner=runner,
                expected_unit_sha256=expected_unit_sha256,
            )
    except RuntimeMutationBusy as exc:
        raise _QueuedActionRetryable(str(exc)) from exc
    except RuntimeMutationUnsafe as exc:
        raise SystemdUserError(str(exc)) from exc


def _execute_queued_user_unit_action_guarded(
    paths: RuntimePaths,
    *,
    kind: str,
    action: str,
    request_id: str,
    unit_dir: Path,
    runner: Runner = subprocess.run,
    expected_unit_sha256: str | None = None,
) -> dict[str, Any]:
    if kind not in {"dashboard", "rag"} or action not in {
        "install",
        "uninstall",
        "start",
        "stop",
        "restart",
    }:
        raise SystemdUserError("queued systemd service action is invalid")
    if action in {"install", "uninstall"}:
        registration = _queued_registration(paths, kind)
        if (
            registration.get("pendingRequestId") != request_id
            or registration.get("pendingAction") != action
        ):
            raise SystemdUserError("queued systemd service action is stale")
    units = _service_action_units(paths, kind)
    if expected_unit_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_unit_sha256):
            raise SystemdUserError(
                "queued systemd service unit identity is invalid"
            )
        if user_unit_set_sha256(units) != expected_unit_sha256:
            raise _QueuedActionStale(
                "queued systemd service Settings changed after the action was accepted"
            )
    expected_units = [
        (unit.name, unit.content, unit.enable_now)
        for unit in units
    ]
    holder: dict[str, Any] = {}
    settings_committed = False

    def registration_precommit(context: dict[str, str]):
        result = (
            install_user_units(
                paths,
                units,
                unit_dir=unit_dir,
                runner=runner,
                defer_commit=True,
                transaction_context=context,
            )
            if action == "install"
            else uninstall_user_units(
                paths,
                units,
                unit_dir=unit_dir,
                runner=runner,
                defer_commit=True,
                transaction_context=context,
            )
        )
        holder["result"] = result

        def cleanup() -> None:
            rollback_user_unit_transaction(
                paths,
                str(result["transactionId"]),
                runner=runner,
            )

        return cleanup

    def registration_postcommit(_context: dict[str, str]) -> None:
        result = holder.get("result")
        if not isinstance(result, dict):
            raise SystemdUserError(
                "queued systemd registration did not create a transaction"
            )
        finalize_user_unit_transaction(
            paths,
            str(result["transactionId"]),
            runner=runner,
        )

    try:
        if action in {"install", "uninstall"}:
            saved = _record_queued_registration_result(
                paths,
                kind=kind,
                action=action,
                request_id=request_id,
                error=None,
                precommit_side_effects=registration_precommit,
                postcommit_side_effects=registration_postcommit,
                expected_units=expected_units,
            )
            if saved is None:
                raise SystemdUserError("queued systemd service action is stale")
            settings_committed = True
            result = holder.get("result")
            if not isinstance(result, dict):
                raise SystemdUserError(
                    "queued systemd registration did not create a transaction"
                )
        else:
            result = control_user_units(paths, units, action, unit_dir=unit_dir, runner=runner)
    except Exception as exc:
        if action in {"install", "uninstall"} and not settings_committed:
            try:
                _record_queued_registration_result(
                    paths,
                    kind=kind,
                    action=action,
                    request_id=request_id,
                    error=str(exc),
                )
            except Exception:
                pass
        if isinstance(exc, SystemdUserError):
            raise
        raise SystemdUserError(str(exc)) from exc
    return result


def _reexec_current_service_action(paths: RuntimePaths, args) -> None:
    attempts = int(os.environ.get("ACTANARA_SERVICE_HELPER_REEXEC_COUNT", "0") or "0")
    if attempts >= 2:
        raise SystemdUserError(
            "queued systemd helper could not converge on the current Runtime generation"
        )
    source = (paths.home / "app" / "source").resolve(strict=True)
    python = paths.home / ".venv" / "bin" / "python"
    if not source.is_dir() or not python.exists():
        raise SystemdUserError(
            "queued systemd helper cannot reload the current Runtime generation"
        )
    environment = dict(os.environ)
    environment["ACTANARA_SERVICE_HELPER_REEXEC_COUNT"] = str(attempts + 1)
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(paths.home / "app" / "source"),
            str(paths.home / "app" / "source" / "src"),
            str(paths.home / "app" / "source" / "src" / "dashboard"),
        )
    )
    command = [
        str(python),
        "-m",
        "data_foundation.systemd_user",
        "service-action",
        "--runtime-home",
        str(paths.home),
        "--kind",
        args.kind,
        "--action",
        args.action,
        "--request-id",
        args.request_id,
        "--source-generation",
        str(source),
        "--expected-unit-sha256",
        args.expected_unit_sha256,
        "--unit-dir",
        args.unit_dir,
    ]
    os.chdir(source)
    os.execve(str(python), command, environment)
    raise SystemdUserError("queued systemd helper reload unexpectedly returned")


def _service_action_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data_foundation.systemd_user")
    subcommands = parser.add_subparsers(dest="command", required=True)
    action_parser = subcommands.add_parser("service-action")
    action_parser.add_argument("--runtime-home", required=True)
    action_parser.add_argument("--kind", choices=("dashboard", "rag"), required=True)
    action_parser.add_argument(
        "--action",
        choices=("install", "uninstall", "start", "stop", "restart"),
        required=True,
    )
    action_parser.add_argument("--request-id", required=True)
    action_parser.add_argument("--source-generation", required=True)
    action_parser.add_argument("--expected-unit-sha256", required=True)
    action_parser.add_argument("--unit-dir", required=True)
    args = parser.parse_args(argv)
    paths = runtime_paths_for_home(Path(args.runtime_home))
    loaded_source_root = Path(args.source_generation)
    try:
        result = execute_queued_user_unit_action(
            paths,
            kind=args.kind,
            action=args.action,
            request_id=args.request_id,
            unit_dir=Path(args.unit_dir),
            loaded_source_root=loaded_source_root,
            expected_unit_sha256=args.expected_unit_sha256,
        )
    except _QueuedHelperReloadRequired:
        try:
            _reexec_current_service_action(paths, args)
        except Exception as exc:
            sys.stderr.write(f"systemd service helper reload failed: {exc}\n")
            return 1
        return 1
    except _QueuedActionRetryable as exc:
        sys.stderr.write(f"systemd service helper deferred: {exc}\n")
        return 75
    except Exception as exc:
        sys.stderr.write(f"systemd service helper failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_service_action_main())
