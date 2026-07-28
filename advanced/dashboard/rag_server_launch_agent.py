#!/usr/bin/env python3
"""Install or run the Actanara nova-RAG server LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


sys.dont_write_bytecode = True


MODULE_PROJECT_ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(MODULE_PROJECT_ROOT))
sys.path.insert(0, str(MODULE_PROJECT_ROOT / "src"))

DEFAULT_ACTANARA_HOME = Path.home() / ".actanara"
DEFAULT_PROJECT_ROOT = DEFAULT_ACTANARA_HOME / "app" / "source"
DEFAULT_PYTHON = DEFAULT_ACTANARA_HOME / ".venv" / "bin" / "python"
DEFAULT_SERVICE_LABEL = "com.actanara.rag-server"
_MAX_SETTINGS_BYTES = 2 * 1024 * 1024


class ManagedRuntimeConfigurationError(RuntimeError):
    """Raised when managed service defaults cannot be tied to one Runtime."""


def _require_selected_runtime(selected) -> None:
    explicit_home = os.environ.get("ACTANARA_HOME")
    if explicit_home:
        expected = Path(explicit_home).expanduser().absolute()
        if selected.home != expected:
            raise ManagedRuntimeConfigurationError(
                "selected Runtime does not match the explicit ACTANARA_HOME"
            )

    settings_path = selected.config_dir / "settings.json"
    try:
        details = settings_path.lstat()
    except FileNotFoundError:
        details = None
    except OSError as exc:
        raise ManagedRuntimeConfigurationError("Runtime settings are unreadable") from exc
    if details is not None:
        try:
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise ManagedRuntimeConfigurationError("Runtime settings must be a regular file")
            if details.st_size > _MAX_SETTINGS_BYTES:
                raise ManagedRuntimeConfigurationError("Runtime settings exceed the safe read limit")
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except ManagedRuntimeConfigurationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagedRuntimeConfigurationError("Runtime settings are unreadable") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("schemaVersion")) is not int
            or payload.get("schemaVersion") != 1
        ):
            raise ManagedRuntimeConfigurationError("Runtime settings have an unsupported schema")
    _require_runtime_pointers(selected.home)


def _require_runtime_pointers(actanara_home: Path) -> None:
    pointers = (
        (actanara_home / "app" / "source", actanara_home / "app" / "releases"),
        (actanara_home / ".venv", actanara_home / "app" / "venvs"),
    )
    for pointer, container in pointers:
        try:
            if not pointer.is_symlink():
                raise ManagedRuntimeConfigurationError("managed Runtime pointer is unavailable")
            target = Path(os.readlink(pointer))
            if target.is_absolute():
                raise ManagedRuntimeConfigurationError("managed Runtime pointer must be relative")
            resolved = pointer.resolve(strict=True)
            expected_container = container.resolve(strict=True)
        except ManagedRuntimeConfigurationError:
            raise
        except (OSError, RuntimeError) as exc:
            raise ManagedRuntimeConfigurationError("managed Runtime pointer is unreadable") from exc
        if resolved.parent != expected_container or not resolved.is_dir():
            raise ManagedRuntimeConfigurationError("managed Runtime pointer target is outside its store")
    if not (actanara_home / ".venv" / "bin" / "python").is_file():
        raise ManagedRuntimeConfigurationError("managed Runtime Python is unavailable")


def _require_stable_runtime_binding(*, project_root: Path, python: Path, actanara_home: Path) -> None:
    expected_source = actanara_home / "app" / "source"
    expected_python = actanara_home / ".venv" / "bin" / "python"
    if project_root != expected_source or python != expected_python:
        raise ManagedRuntimeConfigurationError(
            "managed service writes require stable Runtime source and venv paths"
        )
    _require_runtime_pointers(actanara_home)


def rag_launch_defaults() -> dict:
    try:
        from data_foundation.paths import load_paths
        from data_foundation.settings import read_settings

        selected = load_paths()
        _require_selected_runtime(selected)
        settings = read_settings(selected, persist_defaults=False)
        dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
        return {
            "project_root": selected.home / "app" / "source",
            "python": selected.home / ".venv" / "bin" / "python",
            "actanara_home": selected.home,
            "logs_dir": Path(str(dashboard.get("logsDir") or (Path.home() / "Library" / "Logs" / "Actanara"))),
            "label": DEFAULT_SERVICE_LABEL,
        }
    except ManagedRuntimeConfigurationError:
        raise
    except Exception as exc:
        raise ManagedRuntimeConfigurationError(
            "managed nova-RAG defaults could not be read from the selected Runtime"
        ) from exc


def launch_agents_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def service_plist_path(label: str, home: Path | None = None) -> Path:
    return launch_agents_dir(home) / f"{label}.plist"


def build_service_plist(
    *,
    label: str,
    python: Path,
    project_root: Path,
    actanara_home: Path,
    script: Path,
    logs_dir: Path | None = None,
) -> dict:
    logs = logs_dir or Path.home() / "Library" / "Logs" / "Actanara"
    return {
        "Label": label,
        "ProgramArguments": [
            str(python),
            str(script),
            "run",
            "--project-root",
            str(project_root),
            "--actanara-home",
            str(actanara_home),
        ],
        "EnvironmentVariables": {
            "ACTANARA_HOME": str(actanara_home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{project_root}:{project_root / 'src'}",
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "rag-server-launchagent.out.log"),
        "StandardErrorPath": str(logs / "rag-server-launchagent.err.log"),
    }


def write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=False)


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    binary = os.environ.get("ACTANARA_INSTALL_LAUNCHCTL") or shutil.which("launchctl") or "/bin/launchctl"
    return subprocess.run([binary, *args], text=True, capture_output=True, check=False)


def write_agent(args: argparse.Namespace) -> Path:
    project_root = args.project_root.expanduser().absolute()
    python = args.python.expanduser().absolute()
    actanara_home = args.actanara_home.expanduser().absolute()
    logs_dir = args.logs_dir.expanduser().absolute()
    _require_stable_runtime_binding(
        project_root=project_root,
        python=python,
        actanara_home=actanara_home,
    )
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = service_plist_path(args.label)
    write_plist(
        path,
        build_service_plist(
            label=args.label,
            python=python,
            project_root=project_root,
            actanara_home=actanara_home,
            script=project_root / "advanced" / "dashboard" / "rag_server_launch_agent.py",
            logs_dir=logs_dir,
        ),
    )
    return path


def install_agent(args: argparse.Namespace) -> int:
    path = write_agent(args)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", domain, str(path))
    result = launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        return result.returncode
    print(path)
    return 0


def uninstall_agent(args: argparse.Namespace) -> int:
    domain = f"gui/{os.getuid()}"
    result = launchctl("bootout", domain, str(service_plist_path(args.label)))
    if result.returncode not in (0, 3, 113):
        sys.stderr.write(result.stderr or result.stdout)
        return result.returncode
    return 0


def run_server(args: argparse.Namespace) -> int:
    os.environ["ACTANARA_HOME"] = str(args.actanara_home.resolve())
    project_root = args.project_root.resolve()
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root / "src"))

    from agentic_rag.rag_server_lifecycle import read_server_process_state, start_rag_server, stop_rag_server
    from agentic_rag.rag_settings import resolve_rag_settings

    if sys.platform.startswith("linux"):
        return _run_server_linux(
            resolve_rag_settings=resolve_rag_settings,
            read_server_process_state=read_server_process_state,
            start_rag_server=start_rag_server,
            stop_rag_server=stop_rag_server,
        )

    stopping = False

    def handle_stop(signum, frame):  # noqa: ARG001
        nonlocal stopping
        stopping = True
        try:
            stop_rag_server(resolve_rag_settings(), requested_by="launchd")
        finally:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    settings = resolve_rag_settings()
    result = start_rag_server(settings, requested_by="launchd", wait_timeout_seconds=20.0)
    if not result.get("accepted"):
        print(result.get("reason") or result.get("status") or "nova-RAG server start was not accepted", file=sys.stderr)
        return 1

    while not stopping:
        state = read_server_process_state(settings, probe_health=False)
        if not state.get("running"):
            print("nova-RAG server process is not running", file=sys.stderr)
            return 1
        time.sleep(5)
    return 0


def _run_server_linux(
    *,
    resolve_rag_settings,
    read_server_process_state,
    start_rag_server,
    stop_rag_server,
) -> int:
    """Run the Linux service wrapper with cancellation-safe child ownership."""

    stop_requested = threading.Event()

    def handle_stop(signum, frame):  # noqa: ARG001
        # Python delivers this on the main thread.  Keeping the handler to an
        # event mutation lets dependency probes and cold model downloads unwind
        # through their normal cancellation paths before the child group is
        # terminated below.
        stop_requested.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    settings = resolve_rag_settings()
    result = start_rag_server(
        settings,
        requested_by="systemd",
        wait_timeout_seconds=20.0,
        cancel_event=stop_requested,
    )
    if stop_requested.is_set() or result.get("status") == "canceled":
        stop_rag_server(
            settings,
            requested_by="systemd",
            wait_timeout_seconds=5.0,
        )
        return 0
    if not result.get("accepted"):
        print(result.get("reason") or result.get("status") or "nova-RAG server start was not accepted", file=sys.stderr)
        return 1

    while not stop_requested.wait(5.0):
        state = read_server_process_state(settings, probe_health=False)
        if not state.get("running"):
            print("nova-RAG server process is not running", file=sys.stderr)
            return 1
    stop_rag_server(
        settings,
        requested_by="systemd",
        wait_timeout_seconds=5.0,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    defaults = rag_launch_defaults()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--label", default=defaults["label"])
        p.add_argument("--python", type=Path, default=defaults["python"])
        p.add_argument("--project-root", type=Path, default=defaults["project_root"])
        p.add_argument("--actanara-home", type=Path, default=defaults["actanara_home"])
        p.add_argument("--logs-dir", type=Path, default=defaults["logs_dir"])

    for name in ("write", "install", "uninstall", "run"):
        add_common(sub.add_parser(name))

    args = parser.parse_args(argv)
    if args.command == "write":
        print(write_agent(args))
        return 0
    if args.command == "install":
        return install_agent(args)
    if args.command == "uninstall":
        return uninstall_agent(args)
    if args.command == "run":
        return run_server(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
