#!/usr/bin/env python3
"""Fresh-install adapter for Actanara on Linux user sessions."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from install import dependency_contract


ALLOWED_SOURCE_ENTRIES = (
    "advanced",
    "config.py",
    "install",
    "LICENSE",
    "MANIFEST.in",
    "pyproject.toml",
    "src",
)
EXCLUDED_NAMES = {
    ".DS_Store",
    ".env",
    ".git",
    ".mypy_cache",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "cache",
    "data",
    "dist",
    "htmlcov",
    "logs",
    "reserved",
    "snapshots",
    "state",
    "tmp",
    "venv",
    "wheelhouse",
}
EXCLUDED_SUFFIXES = (
    ".db",
    ".egg-info",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)
UPDATE_RESULT_PREFIX = "ACTANARA_UPDATE_RESULT_JSON="
FRESH_INSTALL_STAGING_NAME = "install-staging"
FRESH_INSTALL_JOURNAL_NAME = "journal.json"
FRESH_INSTALL_SCHEMA_VERSION = 1
FRESH_INSTALL_LOCK_NAME = ".update-transaction.lock"
FRESH_MISSING_HASH = "missing"
FRESH_TRANSACTION_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9]+-[0-9a-f]{8}\Z")


class LinuxInstallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        rollback_complete: bool | None = None,
        state_certain: bool | None = None,
        stage: str | None = None,
        source_updated: bool | None = None,
        dependencies_installed: bool | None = None,
        reuses_runtime_venv: bool | None = None,
        services_stopped: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete
        self.state_certain = state_certain
        self.stage = stage
        self.source_updated = source_updated
        self.dependencies_installed = dependencies_installed
        self.reuses_runtime_venv = reuses_runtime_venv
        self.services_stopped = services_stopped


@dataclass(frozen=True)
class InstallPlan:
    source_root: Path
    runtime: Path
    python: Path
    profiles: tuple[str, ...]
    language: str
    dashboard_host: str
    dashboard_port: int
    dashboard_service: bool
    scheduler: bool
    rag_enabled: bool
    rag_embedding_mode: str
    linger_policy: str
    dev_test: bool
    offline: bool
    dry_run: bool
    update_mode: str
    force_rebuild: bool
    profile_evidence: dict | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(ROOT))
    parser.add_argument("--runtime", default=os.environ.get("ACTANARA_INSTALL_RUNTIME", "~/.actanara"))
    parser.add_argument("--python", default=os.environ.get("ACTANARA_INSTALL_PYTHON", sys.executable))
    parser.add_argument("--language", choices=("zh-CN", "en-US"), default="zh-CN")
    parser.add_argument("--dashboard-host")
    parser.add_argument("--dashboard-port", type=int)
    parser.add_argument("--no-dashboard-server", action="store_true")
    parser.add_argument("--no-dashboard", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--enable-rag", action="store_true")
    parser.add_argument("--rag-embedding-mode", choices=("local", "cloud"))
    linger = parser.add_mutually_exclusive_group()
    linger.add_argument(
        "--enable-linger",
        action="store_true",
        help="Explicitly allow a no-sudo loginctl request for always-on user services.",
    )
    linger.add_argument(
        "--require-linger",
        action="store_true",
        help="Fail before Runtime writes unless linger is already enabled.",
    )
    linger.add_argument(
        "--no-linger-prompt",
        action="store_true",
        help="Preserve the current linger state without prompting.",
    )
    parser.add_argument("--enable-dev-test", action="store_true")
    parser.add_argument("--no-shell-path", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--repair-existing", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--result-json", action="store_true", help=argparse.SUPPRESS)
    return parser


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _requested_update_mode(args: argparse.Namespace) -> str:
    if args.repair_existing:
        if args.upgrade or args.source_only or args.force_rebuild:
            raise LinuxInstallError(
                "--repair-existing cannot be combined with --upgrade, --source-only, or --force-rebuild"
            )
        if not args.dry_run and not args.yes:
            raise LinuxInstallError("--repair-existing requires --yes")
        return "repair"
    if args.source_only:
        if args.force_rebuild:
            raise LinuxInstallError("--source-only and --force-rebuild are mutually exclusive")
        return "source-only"
    if args.force_rebuild and not args.upgrade:
        raise LinuxInstallError("--force-rebuild requires --upgrade")
    if args.upgrade:
        return "upgrade"
    return "fresh"


def _read_update_settings(runtime: Path) -> dict:
    settings_path = runtime / "config" / "settings.json"
    if settings_path.is_symlink() or not settings_path.is_file():
        raise LinuxInstallError("existing Runtime Settings are missing or unsafe")
    metadata = settings_path.stat(follow_symlinks=False)
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise LinuxInstallError("existing Runtime Settings have unsafe ownership or permissions")
    try:
        value = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinuxInstallError("existing Runtime Settings are unreadable") from exc
    if not isinstance(value, dict):
        raise LinuxInstallError("existing Runtime Settings must be a JSON object")
    return value


def _settings_bool(mapping: dict, key: str, fallback: bool) -> bool:
    value = mapping.get(key)
    return value if type(value) is bool else fallback


def build_plan(args: argparse.Namespace) -> InstallPlan:
    source_root = Path(args.source_root).expanduser().absolute()
    runtime = Path(args.runtime).expanduser().absolute()
    python = Path(shutil.which(args.python) or args.python).expanduser().absolute()
    update_mode = _requested_update_mode(args)
    profile_evidence: dict | None = None
    if update_mode == "fresh":
        dashboard_host = args.dashboard_host or "127.0.0.1"
        dashboard_port = args.dashboard_port if args.dashboard_port is not None else 3036
        dashboard_service = not (args.no_dashboard_server or args.no_dashboard)
        scheduler = not args.no_scheduler
        rag_enabled = bool(args.enable_rag)
        rag_embedding_mode = args.rag_embedding_mode or ("local" if rag_enabled else "cloud")
        profiles = {"dashboard"}
        if rag_enabled:
            profiles.add("rag-server")
            if rag_embedding_mode == "local":
                profiles.add("rag-local")
        if args.enable_dev_test:
            profiles.add("dev-test")
    else:
        settings = _read_update_settings(runtime)
        try:
            inherited = dependency_contract.runtime_dependency_profiles(
                runtime,
                allow_untrusted_active_venv=update_mode != "source-only",
                allow_legacy_settings=update_mode == "repair",
            )
        except dependency_contract.ContractError as exc:
            raise LinuxInstallError(
                f"Runtime dependency profile could not be inherited safely: {exc.message}"
            ) from exc
        profile_evidence = dict(inherited["evidence"])
        profiles = set(inherited["profiles"])
        if args.enable_dev_test:
            profiles.add("dev-test")
        rag_enabled = bool(inherited["rag"]["enabled"])
        rag_embedding_mode = str(inherited["rag"].get("embeddingMode") or "cloud")
        dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
        dashboard_server = (
            dashboard.get("server") if isinstance(dashboard.get("server"), dict) else {}
        )
        dashboard_registration = (
            dashboard.get("systemdUser")
            if isinstance(dashboard.get("systemdUser"), dict)
            else {}
        )
        schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
        timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
        rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
        rag_server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
        dashboard_service = _settings_bool(
            dashboard_server,
            "enabled",
            _settings_bool(dashboard_registration, "registered", True),
        )
        scheduler = _settings_bool(
            schedule,
            "enabled",
            _settings_bool(timer, "registered", False),
        )
        rag_service_enabled = _settings_bool(rag_server, "enabled", rag_enabled)
        rag_enabled = rag_enabled and rag_service_enabled
        dashboard_host = str(dashboard.get("host") or "127.0.0.1")
        try:
            dashboard_port = int(dashboard.get("port") or 3036)
        except (TypeError, ValueError) as exc:
            raise LinuxInstallError("existing Dashboard port is invalid") from exc
        conflicts = []
        if args.dashboard_host is not None and args.dashboard_host != dashboard_host:
            conflicts.append("--dashboard-host")
        if args.dashboard_port is not None and args.dashboard_port != dashboard_port:
            conflicts.append("--dashboard-port")
        if (args.no_dashboard_server or args.no_dashboard) and dashboard_service:
            conflicts.append("--no-dashboard-server")
        if args.no_scheduler and scheduler:
            conflicts.append("--no-scheduler")
        if args.enable_rag and not inherited["rag"]["enabled"]:
            conflicts.append("--enable-rag")
        if args.rag_embedding_mode is not None and (
            not inherited["rag"]["enabled"]
            or args.rag_embedding_mode != inherited["rag"]["embeddingMode"]
        ):
            conflicts.append("--rag-embedding-mode")
        if conflicts:
            raise LinuxInstallError(
                "update arguments conflict with Runtime Settings: " + ", ".join(conflicts)
            )
    linger_policy = (
        "enable"
        if args.enable_linger
        else "require"
        if args.require_linger
        else "preserve"
        if args.no_linger_prompt
        else "preserve"
        if update_mode != "fresh"
        else "prompt"
    )
    return InstallPlan(
        source_root=source_root,
        runtime=runtime,
        python=python,
        profiles=tuple(sorted(profiles)),
        language=args.language,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        dashboard_service=dashboard_service,
        scheduler=scheduler,
        rag_enabled=rag_enabled,
        rag_embedding_mode=rag_embedding_mode,
        linger_policy=linger_policy,
        dev_test=args.enable_dev_test,
        offline=bool(args.offline or _env_flag("ACTANARA_INSTALL_OFFLINE")),
        dry_run=bool(args.dry_run or _env_flag("ACTANARA_INSTALL_DRY_RUN")),
        update_mode=update_mode,
        force_rebuild=bool(args.force_rebuild),
        profile_evidence=profile_evidence,
    )


def _managed_services_requested(plan: InstallPlan) -> bool:
    return bool(plan.scheduler or plan.dashboard_service or plan.rag_enabled)


def _prompt_enable_linger(language: str) -> bool | None:
    if language == "zh-CN":
        prompt = (
            "是否允许 Actanara 在你退出登录后继续运行 Dashboard 和定时任务？\n"
            "这会为当前 Linux 用户启用 systemd linger，可能持续使用少量 CPU、内存和网络。"
            " [y/N] "
        )
    else:
        prompt = (
            "Keep Actanara Dashboard and scheduled jobs running after you log out?\n"
            "This enables systemd linger for the current Linux user and may continue using a small amount "
            "of CPU, memory, and network. [y/N] "
        )
    try:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write(prompt)
            answer = terminal.readline()
    except OSError:
        return None
    return answer.strip().lower() in {"y", "yes"}


def _prepare_linger(plan: InstallPlan) -> dict:
    from data_foundation.systemd_user import SystemdUserError, enable_linger, linger_status

    if not _managed_services_requested(plan):
        return {
            "status": "not-required",
            "enabled": None,
            "changed": False,
            "action": "not-required",
            "requestedPolicy": plan.linger_policy,
            "sudoInvoked": False,
        }
    current = linger_status()
    base = {
        **current,
        "requestedPolicy": plan.linger_policy,
        "sudoInvoked": False,
    }
    if current.get("enabled") is True:
        return {**base, "action": "already-enabled"}
    if plan.linger_policy == "require":
        raise LinuxInstallError(
            "linger is required but is not enabled; run `sudo loginctl enable-linger \"$USER\"` "
            "and retry"
        )
    if plan.linger_policy == "preserve":
        return {
            **base,
            "action": "preserved",
            "manualCommand": 'sudo loginctl enable-linger "$USER"',
        }
    if plan.dry_run:
        return {
            **base,
            "action": "planned-enable" if plan.linger_policy == "enable" else "would-prompt",
            "wouldChange": plan.linger_policy == "enable",
        }
    if plan.linger_policy == "prompt":
        accepted = _prompt_enable_linger(plan.language)
        if accepted is not True:
            return {
                **base,
                "action": "declined" if accepted is False else "non-interactive-preserved",
                "manualCommand": 'sudo loginctl enable-linger "$USER"',
            }
    try:
        enabled = enable_linger()
    except SystemdUserError as exc:
        raise LinuxInstallError(
            f"{exc}; Actanara did not invoke sudo. Run `sudo loginctl enable-linger \"$USER\"` "
            "and retry, or use --no-linger-prompt to keep session-only services"
        ) from exc
    return {
        **enabled,
        "requestedPolicy": plan.linger_policy,
        "sudoInvoked": False,
    }


def _preflight_linux_services(plan: InstallPlan, *, check_dashboard_port: bool = True) -> None:
    if not (plan.scheduler or plan.dashboard_service or plan.rag_enabled):
        return
    systemctl = os.environ.get("ACTANARA_INSTALL_SYSTEMCTL") or shutil.which("systemctl")
    if not systemctl:
        raise LinuxInstallError(
            "systemctl is required for requested Linux user services; disable those services or install systemd"
        )
    try:
        manager = subprocess.run(
            [systemctl, "--user", "show-environment"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LinuxInstallError("the systemd user manager preflight could not run") from exc
    if manager.returncode != 0:
        raise LinuxInstallError(
            "the systemd user manager is unavailable; start a user session or disable managed services"
        )

    if not check_dashboard_port:
        return
    ports: list[tuple[str, str, int]] = []
    if plan.dashboard_service:
        ports.append(("Dashboard", plan.dashboard_host, plan.dashboard_port))
    if plan.rag_enabled:
        ports.append(("RAG", "127.0.0.1", 3037))
    # Every supported service host is loopback-only.  Treat localhost, IPv4,
    # and IPv6 spellings as the same bind boundary instead of comparing the
    # user-provided host strings literally.
    selected_ports = [port for _label, _host, port in ports]
    if len(selected_ports) != len(set(selected_ports)):
        raise LinuxInstallError("Dashboard and RAG services cannot use the same loopback port")
    for label, host, port in ports:
        _require_available_service_port(label, host, port)


def _require_available_service_port(label: str, host: str, port: int) -> None:
    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise LinuxInstallError(f"the {label} loopback host could not be resolved") from exc
    checked: set[tuple[int, tuple]] = set()
    for family, socktype, protocol, _canonical, address in addresses:
        key = (family, address)
        if key in checked:
            continue
        checked.add(key)
        probe = socket.socket(family, socktype, protocol)
        try:
            probe.bind(address)
        except OSError as exc:
            raise LinuxInstallError(
                f"{label} port {port} is unavailable on {host}"
            ) from exc
        finally:
            probe.close()


def _preflight_fresh_dependencies(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
) -> None:
    """Fail an offline fresh install before its Runtime receives any writes."""

    if plan.update_mode != "fresh" or not plan.offline:
        return
    cache_root = plan.runtime / "app" / "dependency-cache" / "v1"
    try:
        cache = dependency_contract.dependency_cache_status(cache_root, selection)
    except dependency_contract.ContractError as exc:
        raise LinuxInstallError(
            f"offline fresh install dependency cache is not trustworthy: {exc.message}"
        ) from exc
    if cache.get("status") != "hit" or cache.get("usable") is not True:
        raise LinuxInstallError(
            "offline fresh install requires a complete trusted dependency cache; "
            "no Runtime changes were made"
        )
    with tempfile.TemporaryDirectory(prefix="actanara-linux-pip-preflight-") as temporary:
        probe_venv = Path(temporary) / "venv"
        created = subprocess.run(
            [str(plan.python), "-I", "-m", "venv", "--without-pip", str(probe_venv)],
            capture_output=True,
            text=False,
            check=False,
        )
        if created.returncode != 0:
            raise LinuxInstallError(
                "offline fresh install pip bootstrap preflight could not create an isolated venv; "
                "no Runtime changes were made"
            )
        ensurepip = subprocess.run(
            [str(probe_venv / "bin" / "python"), "-I", "-m", "ensurepip", "--upgrade"],
            capture_output=True,
            text=False,
            check=False,
        )
        if ensurepip.returncode != 0:
            raise LinuxInstallError(
                "offline fresh install pip bootstrap is unavailable for this Python; "
                "rerun online or use a Python build with ensurepip. No Runtime changes were made"
            )


def _validate_plan(plan: InstallPlan, args: argparse.Namespace) -> dependency_contract.ContractSelection:
    host_platform = platform.system()
    if host_platform != "Linux" and not _env_flag("ACTANARA_INSTALL_TEST_MODE"):
        raise LinuxInstallError("the Linux installer can only run on Linux")
    if not 1 <= plan.dashboard_port <= 65535:
        raise LinuxInstallError("dashboard port must be between 1 and 65535")
    if plan.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
        raise LinuxInstallError("Linux phase 1 Dashboard binding must remain loopback-only")
    if (
        plan.update_mode == "fresh"
        and plan.rag_enabled
        and plan.rag_embedding_mode == "cloud"
    ):
        raise LinuxInstallError(
            "fresh managed cloud RAG is not available until a credential-backed provider "
            "profile is configured; use --rag-embedding-mode local or install without --enable-rag. "
            "No Runtime changes were made"
        )
    if not plan.python.is_file() or not os.access(plan.python, os.X_OK):
        raise LinuxInstallError(f"Python executable is unavailable: {plan.python}")
    version = subprocess.run(
        [str(plan.python), "-I", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        python_version = tuple(int(item) for item in version.stdout.strip().split("."))
    except ValueError:
        python_version = ()
    if version.returncode != 0 or python_version != (3, 13):
        raise LinuxInstallError("the current Linux Runtime lock requires CPython 3.13")
    required = (
        "pyproject.toml",
        "install/dependency_contract.py",
        "install/runtime-dependencies.lock.json",
        "src/data_foundation/migrations/0001_initial.sql",
        "src/dashboard/app/static/index.html",
    )
    missing = [name for name in required if not (plan.source_root / name).is_file()]
    if missing:
        raise LinuxInstallError("source payload is incomplete: " + ", ".join(missing))
    if plan.runtime.is_symlink() or (plan.runtime.exists() and not plan.runtime.is_dir()):
        raise LinuxInstallError("Runtime root must be a real directory, not a symlink or file")
    markers = (
        plan.runtime / "app" / "source",
        plan.runtime / ".venv",
        plan.runtime / "config" / "settings.json",
        plan.runtime / "data" / "actanara_data.sqlite3",
        plan.runtime / "data" / "actanara_data.sqlite3-wal",
        plan.runtime / "data" / "actanara_data.sqlite3-shm",
        plan.runtime / "config" / "runtime.json",
    )
    if plan.update_mode == "fresh":
        if any(path.exists() or path.is_symlink() for path in markers):
            raise LinuxInstallError("existing Runtime state requires --upgrade or --repair-existing")
    elif plan.update_mode == "repair":
        if not markers[2].is_file() or markers[2].is_symlink():
            raise LinuxInstallError("repair requires trustworthy existing Runtime Settings")
    elif not all(path.exists() or path.is_symlink() for path in markers[:3]):
        raise LinuxInstallError("selected Runtime is incomplete and cannot be updated safely")
    try:
        selection = dependency_contract.load_contract_selection(
            plan.source_root / "install" / "runtime-dependencies.lock.json",
            plan.source_root / "pyproject.toml",
            plan.profiles,
            python=plan.python,
        )
    except dependency_contract.ContractError as exc:
        raise LinuxInstallError(f"runtime dependency lock rejected this host: {exc.message}") from exc
    if host_platform == "Linux":
        _preflight_linux_services(
            plan,
            check_dashboard_port=plan.update_mode == "fresh",
        )
    _preflight_fresh_dependencies(plan, selection)
    return selection


def _secure_directory(path: Path, *, parents: bool = True) -> None:
    path.mkdir(parents=parents, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise LinuxInstallError(f"unsafe directory: {path}")
    path.chmod(0o700)


def _ignore_source(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED_NAMES
        or name.startswith(".env.")
        or any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)
    }


def _migration_evidence(target: Path) -> dict:
    contract_path = target / "src" / "data_foundation" / "migration_compatibility.json"
    migrations_root = target / "src" / "data_foundation" / "migrations"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    records = contract.get("migrations") if isinstance(contract.get("migrations"), list) else []
    if (
        contract.get("schemaVersion") != 1
        or contract.get("policy") != "rollback-compatible-additive-only"
        or not records
    ):
        raise LinuxInstallError("migration compatibility contract is unsupported")
    normalized = []
    digest = hashlib.sha256()
    for record in records:
        version = str(record.get("version") or "")
        expected = str(record.get("sha256") or "")
        rollback_class = str(record.get("rollbackClass") or "")
        migration = migrations_root / f"{version}.sql"
        if (
            not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", version)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or rollback_class not in {"rollback-compatible-additive", "breaking"}
            or not migration.is_file()
            or migration.is_symlink()
            or hashlib.sha256(migration.read_bytes()).hexdigest() != expected
        ):
            raise LinuxInstallError(f"migration compatibility evidence is invalid: {version}")
        normalized.append({"version": version, "sha256": expected, "rollbackClass": rollback_class})
        digest.update(f"{version}\0{expected}\0{rollback_class}\n".encode("ascii"))
    if [item["version"] for item in normalized] != sorted(path.stem for path in migrations_root.glob("*.sql")):
        raise LinuxInstallError("migration inventory does not match its compatibility contract")
    return {
        "schemaVersion": 1,
        "policy": contract["policy"],
        "preCommitWriterContract": contract["preCommitWriterContract"],
        "minimumReadableSchema": contract["minimumReadableSchema"],
        "maximumReadableSchema": contract["maximumReadableSchema"],
        "migrationSetSha256": digest.hexdigest(),
        "migrations": normalized,
    }


def _source_identity(source: Path) -> tuple[str, str | None]:
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    version = str((project.get("project") or {}).get("version") or "unknown")
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    commit = result.stdout.strip().lower() if result.returncode == 0 else None
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        commit = None
    if commit is not None:
        worktree = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            capture_output=True,
            check=False,
        )
        if worktree.returncode != 0 or worktree.stdout.strip():
            commit = None
    suffix = commit[:12] if commit else hashlib.sha256(str(source).encode()).hexdigest()[:12]
    return f"actanara-{version}-{suffix}", commit


def _stage_source(
    plan: InstallPlan,
    release_target: Path,
    commit: str | None,
    *,
    manifest_release_id: str | None = None,
    precreated: bool = False,
) -> dict:
    if precreated:
        if release_target.is_symlink() or not release_target.is_dir():
            raise LinuxInstallError("reserved release generation is unavailable or unsafe")
        if {path.name for path in release_target.iterdir()} - {".actanara-update-owner"}:
            raise LinuxInstallError("reserved release generation contains unexpected content")
    else:
        if release_target.exists() or release_target.is_symlink():
            raise LinuxInstallError(f"release generation already exists: {release_target}")
        _secure_directory(release_target)
    for name in ALLOWED_SOURCE_ENTRIES:
        source_path = plan.source_root / name
        if not source_path.exists():
            continue
        target_path = release_target / name
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, ignore=_ignore_source, symlinks=False)
        else:
            shutil.copy2(source_path, target_path, follow_symlinks=False)
    if any(path.is_symlink() for path in release_target.rglob("*")):
        raise LinuxInstallError("runtime source payload must not contain symlinks")
    from data_foundation.release_clean import repository_clean_deployment_check

    clean = repository_clean_deployment_check(release_target)
    if clean.get("status") != "passed":
        raise LinuxInstallError("runtime source payload failed release-clean validation")
    payload_files = []
    payload_digest = hashlib.sha256()
    for path in sorted(release_target.rglob("*")):
        if not path.is_file() or path.name in {
            ".actanara-runtime-source.json",
            ".actanara-update-owner",
        }:
            continue
        relative = path.relative_to(release_target).as_posix()
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        payload_files.append({"path": relative, "sha256": sha256, "size": len(content)})
        payload_digest.update(f"{relative}\0{sha256}\n".encode("utf-8"))
    release_id = manifest_release_id or release_target.name
    version = tomllib.loads((release_target / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = {
        "schemaVersion": 2,
        "product": "actanara",
        "sourceLocator": {"kind": "unavailable", "issue": "outside-login-home"},
        "deployedSourceLocator": {"kind": "runtime-relative", "pathComponents": ["app", "source"]},
        "releaseLocator": {"kind": "runtime-relative", "pathComponents": ["app", "releases", release_id]},
        "deploymentMode": "release-symlink",
        "copiedAt": datetime.now().astimezone().isoformat(),
        "pyprojectVersion": version,
        "git": {"available": commit is not None, "commit": commit, "branch": None, "remote": None, "dirty": None},
        "databaseCompatibility": _migration_evidence(release_target),
        "payload": {"fileCount": len(payload_files), "files": payload_files, "sha256": payload_digest.hexdigest()},
        "cleanScan": {
            "status": "passed",
            "scanner": "data_foundation.release_clean.repository_clean_deployment_check",
            "scannedFiles": int(clean.get("scannedFiles") or 0),
            "findingCount": 0,
        },
    }
    manifest_path = release_target / ".actanara-runtime-source.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def _linux_parent_death_preexec() -> Callable[[], None] | None:
    if sys.platform != "linux":
        return None
    parent_pid = os.getpid()

    def configure() -> None:  # pragma: no cover - exercised on Debian
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(126)
        if os.getppid() != parent_pid:
            os._exit(126)

    return configure


def _run(command: Iterable[str], *, env: dict[str, str] | None = None) -> None:
    child_options: dict[str, object] = {}
    preexec = _linux_parent_death_preexec()
    if preexec is not None:
        child_options["preexec_fn"] = preexec
    result = subprocess.run(
        list(command),
        env=env,
        text=True,
        check=False,
        **child_options,
    )
    if result.returncode != 0:
        raise LinuxInstallError(f"command failed with status {result.returncode}: {command}")


def _run_tracked_database_command(
    command: list[str],
    *,
    env: dict[str, str],
    worker_started: Callable[[dict[str, object]], None] | None,
) -> None:
    """Run fresh DB migration behind a durable, parent-death-safe PID gate."""

    if worker_started is None or sys.platform != "linux" or not hasattr(os, "fork"):
        _run(command, env=env)
        return

    gate_read, gate_write = os.pipe()
    parent_pid = os.getpid()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised by Debian crash tests
        try:
            os.close(gate_write)
            os.setsid()
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
                os._exit(126)
            if os.getppid() != parent_pid:
                os._exit(126)
            token = os.read(gate_read, 1)
            os.close(gate_read)
            if token != b"1":
                os._exit(125)
            os.execve(command[0], command, env)
        except BaseException:
            os._exit(127)

    os.close(gate_read)
    gate_open = True
    waited = False
    try:
        from install.update_transaction import _process_identity

        identity = None
        deadline = time.monotonic() + 2.0
        while identity is None and time.monotonic() < deadline:
            identity = _process_identity(pid)
            if identity is None:
                time.sleep(0.01)
        if identity is None:
            raise LinuxInstallError(
                "fresh database migration worker identity is unavailable"
            )
        worker_started(
            {
                "pid": pid,
                "processGroup": pid,
                "processIdentity": identity,
            }
        )
        os.write(gate_write, b"1")
        os.close(gate_write)
        gate_open = False
        while True:
            try:
                _waited_pid, status = os.waitpid(pid, 0)
                waited = True
                break
            except InterruptedError:
                continue
        returncode = os.waitstatus_to_exitcode(status)
        if returncode != 0:
            raise LinuxInstallError(
                f"command failed with status {returncode}: {command}"
            )
    except BaseException:
        if gate_open:
            os.close(gate_write)
            gate_open = False
        if not waited:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        raise


def _seed_venv_pip(plan: InstallPlan, venv: Path) -> Path:
    _run([str(plan.python), "-m", "venv", "--without-pip", str(venv)])
    venv_python = venv / "bin" / "python"
    ensurepip_options: dict[str, object] = {}
    ensurepip_preexec = _linux_parent_death_preexec()
    if ensurepip_preexec is not None:
        ensurepip_options["preexec_fn"] = ensurepip_preexec
    ensurepip = subprocess.run(
        [str(venv_python), "-I", "-m", "ensurepip", "--upgrade"],
        text=True,
        capture_output=True,
        check=False,
        **ensurepip_options,
    )
    if ensurepip.returncode != 0:
        if plan.offline:
            raise LinuxInstallError("offline install cannot seed pip into this Python venv")
        _run(
            [
                str(plan.python),
                "-I",
                "-m",
                "pip",
                "--python",
                str(venv),
                "install",
                "--disable-pip-version-check",
                "pip==26.1.2",
            ]
        )
    return venv_python


def _cli_shim_content(runtime: Path) -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"ACTANARA_HOME={str(runtime)!r}\n"
        'SOURCE="$ACTANARA_HOME/app/source"\n'
        'export ACTANARA_HOME PYTHONDONTWRITEBYTECODE=1\n'
        'export PYTHONPATH="$SOURCE:$SOURCE/src:$SOURCE/src/dashboard"\n'
        'exec "$ACTANARA_HOME/.venv/bin/python" -m data_foundation.cli "$@"\n'
    )


def _write_cli_shim(runtime: Path, *, staging: Path | None = None) -> None:
    shim = runtime / "bin" / "actanara"
    content = _cli_shim_content(runtime)
    if staging is not None:
        temporary = staging / ".runtime-cli.next"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o755)
            os.replace(temporary, shim)
            directory = os.open(shim.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".actanara.",
            dir=shim.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o755)
            os.replace(temporary, shim)
        finally:
            temporary.unlink(missing_ok=True)
    shim.chmod(0o755)


def _runtime_settings_update(plan: InstallPlan) -> dict:
    from data_foundation.pipeline_language import resolve_pipeline_language_profile

    source = plan.runtime / "app" / "source"
    language = resolve_pipeline_language_profile(plan.language)
    embedding: dict[str, object] = {
        "mode": plan.rag_embedding_mode,
        "provider": plan.rag_embedding_mode,
    }
    if plan.rag_embedding_mode == "local":
        embedding.update(
            {
                "providerId": "local",
                "model": "intfloat/multilingual-e5-small",
                "dimension": 384,
                "device": "auto",
            }
        )
    return {
        "general": {
            "locale": language.locale,
            "workspaceRoot": str(source),
            "tmpWorkspace": str(plan.runtime / "state" / "tmp"),
        },
        "schedule": {
            "systemTimer": {
                "provider": "systemd",
                "label": "actanara.daily",
                "registered": False,
            }
        },
        "dashboard": {
            "host": plan.dashboard_host,
            "port": plan.dashboard_port,
            "projectRoot": str(source),
            "pythonExecutable": str(plan.runtime / ".venv" / "bin" / "python"),
            "appDir": str(source / "src" / "dashboard"),
            "server": {"enabled": plan.dashboard_service},
        },
        "pipeline": {
            "pythonExecutable": str(plan.runtime / ".venv" / "bin" / "python"),
            "workingDirectory": str(source),
            "languageProfile": language.profile_id,
            "englishEnabled": language.profile_id == "en",
            "diarySchemaVersion": language.diary_schema_version,
            "promptPayloadProfile": language.prompt_payload_profile,
        },
        "features": {
            "rag": plan.rag_enabled,
            "embeddingServer": False,
        },
        "rag": {
            "enabled": plan.rag_enabled,
            "mode": "v2" if plan.rag_enabled else "disabled",
            "embedding": embedding,
            "server": {"enabled": plan.rag_enabled},
            "languageProfile": language.rag_language_profile,
        },
    }


def _configure_runtime(
    plan: InstallPlan,
    *,
    staging: Path,
    journal: dict,
    transaction_id: str,
) -> None:
    from data_foundation.paths import RUNTIME_SCHEMA_VERSION, runtime_paths_for_home
    from data_foundation.settings import write_linux_fresh_install_settings

    def arm_settings(context: dict[str, str]):
        records = journal.get("managedMutableHashes")
        if not isinstance(records, dict):
            raise LinuxInstallError("fresh install mutable journal is unavailable")
        next_records = {key: dict(value) for key, value in records.items()}
        next_records["settings"]["afterSha256"] = context["settingsAfterHash"]
        next_records["runtimeManifest"]["afterSha256"] = context[
            "runtimeManifestAfterHash"
        ]
        _advance_fresh_install_journal(
            staging,
            journal,
            "runtime-configuration-armed",
            managedMutableHashes=next_records,
            configurationSettingsTransactionId=context["id"],
        )
        fresh_install_checkpoint("runtime-configuration-armed", transaction_id)
        return None

    write_linux_fresh_install_settings(
        _runtime_settings_update(plan),
        runtime_paths_for_home(plan.runtime),
        precommit_side_effects=arm_settings,
        runtime_mutation_owner_id=transaction_id,
    )

    location = Path(str(journal["managedMutableHashes"]["location"]["path"]))
    selected_at = datetime.now().astimezone().isoformat()
    location_content = (
        json.dumps(
            {
                "actanaraHome": str(plan.runtime),
                "selectedAt": selected_at,
                "version": RUNTIME_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _arm_fresh_mutable_hash(
        staging,
        journal,
        "location",
        location_content,
        "location-write-armed",
    )
    fresh_install_checkpoint("location-write-armed", transaction_id)
    _secure_directory(location.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{location.name}.{transaction_id}.",
        dir=location.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(location_content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, location)
        directory = os.open(location.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _initialize_database(
    plan: InstallPlan,
    *,
    worker_started: Callable[[dict[str, object]], None] | None = None,
) -> Path:
    database = plan.runtime / "data" / "actanara_data.sqlite3"
    env = {
        **os.environ,
        "ACTANARA_HOME": str(plan.runtime),
        "PYTHONPATH": os.pathsep.join(
            (
                str(plan.runtime / "app" / "source"),
                str(plan.runtime / "app" / "source" / "src"),
            )
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = """
from pathlib import Path
from data_foundation.db import migrate
from data_foundation.paths import runtime_paths_for_home
runtime = Path(__import__('os').environ['ACTANARA_HOME'])
migrate(runtime_paths_for_home(runtime))
"""
    _run_tracked_database_command(
        [str(plan.runtime / ".venv" / "bin" / "python"), "-c", script],
        env=env,
        worker_started=worker_started,
    )
    if not database.is_file():
        raise LinuxInstallError("database migration completed without creating the Runtime database")
    database.chmod(0o600)
    return database


def _desired_systemd_units(plan: InstallPlan, settings: dict) -> list:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.systemd_user import dashboard_unit, rag_unit, scheduler_units

    paths = runtime_paths_for_home(plan.runtime)
    schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
    timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    units = []
    if plan.scheduler:
        units.extend(scheduler_units(paths, schedule, timer))
    if plan.dashboard_service:
        units.append(dashboard_unit(paths, dashboard))
    if plan.rag_enabled:
        units.append(rag_unit(paths, server))
    return units


def _systemd_unit_inventory(plan: InstallPlan, settings: dict) -> tuple[list, tuple[str, ...]]:
    from data_foundation.systemd_user import (
        MANAGED_UNIT_HEADER,
        UNIT_NAME_RE,
        UserUnit,
        _quote,
        default_user_unit_dir,
    )

    desired = _desired_systemd_units(plan, settings)
    names = {unit.name for unit in desired}
    schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
    timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    for registration in (
        timer,
        dashboard.get("systemdUser") if isinstance(dashboard.get("systemdUser"), dict) else {},
        server.get("systemdUser") if isinstance(server.get("systemdUser"), dict) else {},
    ):
        configured = registration.get("jobs") or registration.get("units") or []
        if not isinstance(configured, list):
            raise LinuxInstallError("Runtime Settings contain an unsafe systemd unit inventory")
        for name in configured:
            if not isinstance(name, str) or not UNIT_NAME_RE.fullmatch(name):
                raise LinuxInstallError("Runtime Settings contain an unsafe systemd unit name")
            names.add(name)
    unit_root = default_user_unit_dir()
    if unit_root.is_dir() and not unit_root.is_symlink():
        runtime_binding = f"Environment={_quote(f'ACTANARA_HOME={plan.runtime}')}"
        for target in unit_root.iterdir():
            if not UNIT_NAME_RE.fullmatch(target.name) or target.is_symlink() or not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if (
                content.splitlines()[:1] == [MANAGED_UNIT_HEADER]
                and runtime_binding in content.splitlines()
            ):
                names.add(target.name)
    by_name = {unit.name: unit for unit in desired}
    inventory = [
        by_name.get(name) or UserUnit(name=name, content="", enable_now=False)
        for name in sorted(names)
    ]
    return desired, tuple(unit.name for unit in inventory)


def _reconcile_existing_systemd_units(
    plan: InstallPlan,
    settings: dict,
    *,
    prior_inventory: tuple[str, ...] = (),
    normalize_failed_prior_states: bool = False,
    transaction_owner_id: str | None = None,
    deferred_enable_names: frozenset[str] = frozenset(),
) -> dict:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.systemd_user import (
        SystemdUserError,
        SystemdUserCompensationError,
        UserUnit,
        inspect_user_units,
        install_user_units,
        uninstall_user_units,
    )

    paths = runtime_paths_for_home(plan.runtime)
    desired, inventory = _systemd_unit_inventory(plan, settings)
    if deferred_enable_names:
        desired = [
            UserUnit(
                name=unit.name,
                content=unit.content,
                enable_now=unit.enable_now and unit.name not in deferred_enable_names,
            )
            for unit in desired
        ]
    inventory = tuple(sorted(set(inventory).union(prior_inventory)))
    desired_names = {unit.name for unit in desired}
    removed = [
        UserUnit(name=name, content="", enable_now=False)
        for name in inventory
        if name not in desired_names
    ]
    try:
        current = inspect_user_units(desired) if desired else None
        registration_aligned = current is not None and (
            current.get("actualRegistered") is True
            or not any(unit.enable_now for unit in desired)
        )
        installed_result = (
            None
            if current is not None
            and current.get("definitionsAligned") is True
            and registration_aligned
            else install_user_units(
                paths,
                desired,
                normalize_failed_prior_states=normalize_failed_prior_states,
                transaction_context=(
                    {"ownerId": transaction_owner_id}
                    if transaction_owner_id is not None
                    else None
                ),
            )
            if desired
            else None
        )
        # Establish every desired definition before pruning stale managed
        # definitions. A second-step failure can leave only a harmless stale
        # unit, never remove the service the repaired Runtime needs.
        removed_result = (
            uninstall_user_units(
                paths,
                removed,
                normalize_failed_prior_states=normalize_failed_prior_states,
                transaction_context=(
                    {"ownerId": transaction_owner_id}
                    if transaction_owner_id is not None
                    else None
                ),
            )
            if removed
            else None
        )
    except SystemdUserCompensationError as exc:
        raise LinuxInstallError(
            str(exc),
            rollback_complete=False,
            state_certain=False,
            stage="systemd-compensation-incomplete",
        ) from exc
    except SystemdUserError as exc:
        raise LinuxInstallError(str(exc)) from exc
    return {
        "installed": installed_result,
        "removed": removed_result,
        "units": sorted(desired_names),
    }


def _rag_install_readiness_timeout_seconds() -> float:
    raw = os.environ.get("ACTANARA_INSTALL_RAG_READINESS_TIMEOUT_SECONDS", "600")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise LinuxInstallError("RAG readiness timeout must be numeric") from exc
    if not 1.0 <= timeout <= 1800.0:
        raise LinuxInstallError("RAG readiness timeout must be between 1 and 1800 seconds")
    return timeout


def _install_systemd_user_services(
    plan: InstallPlan,
    *,
    expected_source_commit: str | None = None,
    transaction_started: Callable[[str], None] | None = None,
    settings_transaction_started: Callable[[dict[str, str]], None] | None = None,
    transaction_owner_id: str | None = None,
    normalize_failed_prior_states: bool = False,
    deferred_enable_names: frozenset[str] = frozenset(),
) -> dict:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.settings import (
        read_settings,
        write_linux_installer_handoff_settings,
    )
    from data_foundation.systemd_user import (
        SystemdUserError,
        SystemdUserCompensationError,
        finalize_user_unit_transaction,
        install_user_units,
        rollback_user_unit_transaction,
    )

    paths = runtime_paths_for_home(plan.runtime)
    settings = read_settings(paths, redact_secrets=False, persist_defaults=False)
    schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
    timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    units, inventory = _systemd_unit_inventory(plan, settings)
    if deferred_enable_names:
        from data_foundation.systemd_user import UserUnit

        units = [
            UserUnit(
                name=unit.name,
                content=unit.content,
                enable_now=unit.enable_now and unit.name not in deferred_enable_names,
            )
            for unit in units
        ]
    inventory_names = set(inventory)
    scheduler_names = [
        unit.name
        for unit in units
        if unit.name.endswith((".timer", ".service"))
        and str(timer.get("label") or "actanara.daily") in unit.name
    ]
    readiness_verifier = None
    if plan.rag_enabled:
        from agentic_rag.rag_server_lifecycle import require_rag_server_readiness
        from agentic_rag.rag_settings import resolve_rag_settings
        from data_foundation.systemd_user import rag_unit

        rag_settings = resolve_rag_settings(paths, settings)
        rag_deferred = rag_unit(paths, server).name in deferred_enable_names

        if not rag_deferred:
            def readiness_verifier():
                return require_rag_server_readiness(
                    rag_settings,
                    expected_source_commit=expected_source_commit,
                    timeout_seconds=_rag_install_readiness_timeout_seconds(),
                )

    now = datetime.now().astimezone().isoformat()
    update: dict = {}
    if plan.scheduler:
        update["schedule"] = {
            "enabled": True,
            "mode": "system",
            "systemTimer": {
                "provider": "systemd",
                "label": str(timer.get("label") or "actanara.daily"),
                "registered": True,
                "registrationManagedBy": "linux-installer",
                "registeredAt": now,
                "jobs": scheduler_names,
                "lastAction": "install",
                "lastActionStatus": "success",
                "lastError": None,
                "lastErrorAt": None,
                "stale": False,
                "reinstallRequired": False,
            },
        }
    elif (
        timer.get("registered") is True
        or bool(timer.get("jobs"))
        or any(name.startswith(f"{str(timer.get('label') or 'actanara.daily')}.") for name in inventory_names)
    ):
        update["schedule"] = {
            "enabled": False,
            "systemTimer": {
                "provider": "systemd",
                "label": str(timer.get("label") or "actanara.daily"),
                "registered": False,
                "registrationManagedBy": "linux-installer",
                "registeredAt": None,
                "jobs": [],
                "lastAction": "uninstall",
                "lastActionStatus": "success",
                "lastError": None,
                "lastErrorAt": None,
                "stale": False,
                "reinstallRequired": False,
            },
        }
    if plan.dashboard_service:
        from data_foundation.systemd_user import dashboard_unit

        update.setdefault("dashboard", {})["systemdUser"] = {
            "registered": True,
            "registrationManagedBy": "linux-installer",
            "registeredAt": now,
            "units": [dashboard_unit(paths, dashboard).name],
        }
    else:
        from data_foundation.systemd_user import dashboard_unit

        dashboard_registration = (
            dashboard.get("systemdUser")
            if isinstance(dashboard.get("systemdUser"), dict)
            else {}
        )
        dashboard_name = dashboard_unit(paths, dashboard).name
        if not (
            dashboard_registration.get("registered") is True
            or bool(dashboard_registration.get("units"))
            or dashboard_name in inventory_names
        ):
            dashboard_name = ""
    if not plan.dashboard_service and dashboard_name:
        update.setdefault("dashboard", {}).update(
            {
                "server": {"enabled": False},
                "systemdUser": {
                    "registered": False,
                    "registrationManagedBy": "linux-installer",
                    "registeredAt": None,
                    "units": [],
                },
            }
        )
    if plan.rag_enabled:
        from data_foundation.systemd_user import rag_unit

        rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
        server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
        update["rag"] = {
            "server": {
                **server,
                "systemdUser": {
                    "registered": True,
                    "registrationManagedBy": "linux-installer",
                    "registeredAt": now,
                    "units": [rag_unit(paths, server).name],
                },
            }
        }
    else:
        rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
        server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
        from data_foundation.systemd_user import rag_unit

        rag_registration = (
            server.get("systemdUser")
            if isinstance(server.get("systemdUser"), dict)
            else {}
        )
        rag_name = rag_unit(paths, server).name
        if not (
            rag_registration.get("registered") is True
            or bool(rag_registration.get("units"))
            or rag_name in inventory_names
        ):
            rag_name = ""
    if not plan.rag_enabled and rag_name:
        update["rag"] = {
            "server": {
                **server,
                "enabled": False,
                "systemdUser": {
                    "registered": False,
                    "registrationManagedBy": "linux-installer",
                    "registeredAt": None,
                    "units": [],
                },
            }
        }

    if not units and not update:
        return {
            "status": "not-requested",
            "provider": "systemd-user",
            "units": [],
            "linger": {"status": "not-probed", "enabled": None, "changed": False},
        }

    if not units:
        def record_settings_transaction(context: dict[str, str]):
            if settings_transaction_started is not None:
                settings_transaction_started(context)

        saved = write_linux_installer_handoff_settings(
            update,
            paths,
            precommit_side_effects=record_settings_transaction,
            runtime_mutation_owner_id=transaction_owner_id,
        )
        return {
            "status": "not-requested",
            "provider": "systemd-user",
            "units": [],
            "settingsTransaction": saved.get("settingsTransaction"),
            "linger": {"status": "not-probed", "enabled": None, "changed": False},
        }
    holder: dict[str, object] = {}

    def precommit(context: dict[str, str]):
        if settings_transaction_started is not None:
            settings_transaction_started(context)
        transaction_context = dict(context)
        transaction_options: dict[str, object] = {
            "defer_commit": True,
            "transaction_context": transaction_context,
        }
        if transaction_owner_id is not None:
            transaction_context["ownerId"] = transaction_owner_id
            transaction_options["recover_transactions"] = False
        result: dict | None = None
        try:
            result = (
                install_user_units(
                    paths,
                    units,
                    readiness_verifier=readiness_verifier,
                    normalize_failed_prior_states=normalize_failed_prior_states,
                    **transaction_options,
                )
                if readiness_verifier is not None
                else install_user_units(
                    paths,
                    units,
                    normalize_failed_prior_states=normalize_failed_prior_states,
                    **transaction_options,
                )
            )
            transaction_id = str(result.get("transactionId") or "")
            if not transaction_id:
                raise LinuxInstallError("systemd install did not return a transaction identity")
            if transaction_started is not None:
                transaction_started(transaction_id)
        except Exception as exc:
            holder["error"] = exc
            transaction_id = str((result or {}).get("transactionId") or "")
            if transaction_id:
                try:
                    rollback_user_unit_transaction(paths, transaction_id)
                except SystemdUserError as rollback_exc:
                    raise LinuxInstallError(
                        f"systemd install handoff failed and rollback is incomplete: {rollback_exc}",
                        rollback_complete=False,
                        state_certain=False,
                        stage="systemd-compensation-incomplete",
                    ) from exc
            raise
        holder["result"] = result

        def cleanup() -> None:
            rollback_user_unit_transaction(paths, transaction_id)

        return cleanup

    def postcommit(_context: dict[str, str]) -> None:
        result = holder.get("result")
        if not isinstance(result, dict):
            raise LinuxInstallError(
                "systemd Settings handoff did not create a unit transaction"
            )
        finalize_user_unit_transaction(
            paths,
            str(result.get("transactionId") or ""),
        )

    try:
        saved = write_linux_installer_handoff_settings(
            update,
            paths,
            precommit_side_effects=precommit,
            postcommit_side_effects=postcommit,
            runtime_mutation_owner_id=transaction_owner_id,
        )
    except Exception as exc:
        from data_foundation.settings_transaction import SettingsTransactionError

        if isinstance(exc, SettingsTransactionError):
            compensation = (
                exc.summary.get("compensation")
                if isinstance(exc.summary.get("compensation"), dict)
                else {}
            )
            if (
                compensation.get("status") == "compensation-incomplete"
                or compensation.get("sideEffects") == "cleanup-failed"
            ):
                raise LinuxInstallError(
                    str(exc),
                    rollback_complete=False,
                    state_certain=False,
                    stage="systemd-compensation-incomplete",
                ) from exc
        handoff_error = holder.get("error")
        if isinstance(handoff_error, SystemdUserError):
            from agentic_rag.rag_server_lifecycle import RagServerReadinessError

            cause = handoff_error.__cause__
            if isinstance(cause, RagServerReadinessError):
                reason = str(
                    cause.result.get("reasonCode")
                    or cause.result.get("status")
                    or "not-ready"
                )
                raise LinuxInstallError(f"RAG semantic readiness failed: {reason}") from exc
        if isinstance(handoff_error, SystemdUserCompensationError):
            raise LinuxInstallError(
                str(handoff_error),
                rollback_complete=False,
                state_certain=False,
                stage="systemd-compensation-incomplete",
            ) from exc
        if isinstance(handoff_error, LinuxInstallError):
            raise handoff_error from exc
        raise LinuxInstallError(str(handoff_error or exc)) from exc

    result = holder.get("result")
    if not isinstance(result, dict):
        raise LinuxInstallError("systemd Settings handoff did not create a unit transaction")
    return {
        **result,
        "transactionStatus": "committed",
        "settingsTransaction": saved.get("settingsTransaction"),
    }


def _transaction_command(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    child_options: dict[str, object] = {}
    child_environment: dict[str, str] | None = None
    if sys.platform == "linux":
        from data_foundation.runtime_mutation import (
            current_runtime_mutation_guard_fd,
        )

        guard_fd = current_runtime_mutation_guard_fd()
        if guard_fd is not None:
            child_options["pass_fds"] = (guard_fd,)
            child_environment = {
                **os.environ,
                "ACTANARA_RUNTIME_MUTATION_GUARD_FD": str(guard_fd),
            }
        preexec = _linux_parent_death_preexec()
        if preexec is not None:
            child_options["preexec_fn"] = preexec
    result = subprocess.run(
        [sys.executable, str(ROOT / "install" / "update_transaction.py"), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=1200,
        env=child_environment,
        **child_options,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LinuxInstallError(
            f"update transaction command failed ({arguments[0]}): {detail or result.returncode}"
        )
    return result


def _recover_update_runtime(runtime: Path) -> None:
    if not runtime.is_dir() or runtime.is_symlink():
        return
    from data_foundation.runtime_mutation import (
        durable_runtime_mutation_owner,
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        runtime_mutation_guard,
    )

    try:
        # Stale rollback can mutate pointers, SQLite files, Settings and
        # systemd. Keep the same inherited flock across the recovery child and
        # every coupled journal recovery so Dashboard/service writes cannot
        # interleave with it.
        with runtime_mutation_guard(runtime, blocking=True):
            _transaction_command("recover", "--runtime", str(runtime))
            recovery_owner_id = durable_runtime_mutation_owner(runtime)
            _recover_settings_transactions_before_update(
                runtime,
                runtime_guard_held=True,
            )
            _recover_systemd_transactions_before_update(
                runtime,
                runtime_guard_held=True,
                owner_id=recovery_owner_id,
            )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise LinuxInstallError(
            f"update recovery could not lock the Runtime: {exc}",
            rollback_complete=False,
            state_certain=False,
            stage="update-recovery",
        ) from exc


def _dependency_update_plan(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
) -> dict:
    mode = (
        "explicit-source-only"
        if plan.update_mode == "source-only"
        else "force-rebuild"
        if plan.update_mode == "repair" or plan.force_rebuild
        else "auto"
    )
    payload, return_code = dependency_contract.plan_update(
        plan.runtime,
        selection,
        mode=mode,
        offline=plan.offline,
        cache_root=plan.runtime / "app" / "dependency-cache" / "v1",
    )
    if return_code != 0 or payload.get("status") != "ready":
        raise LinuxInstallError(
            f"runtime dependency plan blocked before service stop: {payload.get('reason') or 'unknown'}"
        )
    return payload


def _verify_updated_systemd_units(plan: InstallPlan, settings: dict) -> dict:
    from data_foundation.systemd_user import SystemdUserError, inspect_user_units

    desired = _desired_systemd_units(plan, settings)
    if not desired:
        return {"status": "not-requested", "definitionsAligned": True, "actualRegistered": False}
    try:
        result = inspect_user_units(desired)
    except SystemdUserError as exc:
        raise LinuxInstallError(str(exc)) from exc
    if (
        result.get("definitionsPresent") is not True
        or result.get("definitionsManaged") is not True
        or result.get("definitionsAligned") is not True
    ):
        raise LinuxInstallError("managed systemd user-unit definitions are not aligned after update")
    return result


def _validate_existing_systemd_units_for_update(
    plan: InstallPlan,
    desired: list,
    inventory: tuple[str, ...],
) -> dict:
    """Reject definition drift before a rollback-capable standard update."""

    from data_foundation.systemd_user import SystemdUserError, inspect_user_units

    desired_names = tuple(sorted(unit.name for unit in desired))
    if tuple(sorted(inventory)) != desired_names:
        raise LinuxInstallError(
            "managed systemd unit inventory is stale; run --repair-existing before upgrade"
        )
    if not desired:
        return {
            "status": "not-requested",
            "definitionsPresent": True,
            "definitionsManaged": True,
            "definitionsAligned": True,
        }
    try:
        result = inspect_user_units(desired)
    except SystemdUserError as exc:
        raise LinuxInstallError(str(exc)) from exc
    if (
        result.get("definitionsPresent") is not True
        or result.get("definitionsManaged") is not True
        or result.get("definitionsAligned") is not True
    ):
        raise LinuxInstallError(
            "managed systemd unit definitions have drifted; run --repair-existing before upgrade"
        )
    return result


def _validate_existing_systemd_unit_ownership(
    plan: InstallPlan,
    inventory: tuple[str, ...],
) -> None:
    """Fail before repair/upgrade can stop a different Runtime's units."""

    from data_foundation.systemd_user import (
        MANAGED_UNIT_HEADER,
        _quote,
        default_user_unit_dir,
    )

    root = default_user_unit_dir()
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise LinuxInstallError("managed systemd user-unit directory is unsafe")
    expected_binding = f"Environment={_quote(f'ACTANARA_HOME={plan.runtime}')}"
    contents: dict[str, list[str]] = {}
    for name in inventory:
        target = root / name
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or not target.is_file():
            raise LinuxInstallError(
                f"managed systemd unit target is unsafe: {name}"
            )
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise LinuxInstallError(
                f"managed systemd unit target is unreadable: {name}"
            ) from exc
        if not lines or lines[0] != MANAGED_UNIT_HEADER:
            raise LinuxInstallError(
                f"systemd unit is not managed by Actanara: {name}"
            )
        contents[name] = lines

    for name, lines in contents.items():
        if name.endswith(".service"):
            bindings = [
                line
                for line in lines
                if line.startswith("Environment=") and "ACTANARA_HOME=" in line
            ]
            if bindings != [expected_binding]:
                raise LinuxInstallError(
                    "managed systemd unit belongs to a different or ambiguous Runtime: "
                    f"{name}"
                )
        elif name.endswith(".timer"):
            targets = [line.removeprefix("Unit=") for line in lines if line.startswith("Unit=")]
            if len(targets) != 1 or targets[0] not in contents:
                raise LinuxInstallError(
                    "managed systemd timer has no Runtime-bound service pair: "
                    f"{name}"
                )


def _run_update_doctor(plan: InstallPlan) -> dict:
    env = {
        **os.environ,
        "ACTANARA_HOME": str(plan.runtime),
        "ACTANARA_LOCATION_FILE": os.environ.get(
            "ACTANARA_LOCATION_FILE",
            str(Path.home() / ".config" / "actanara" / "location.json"),
        ),
        "PYTHONPATH": os.pathsep.join(
            (
                str(plan.runtime / "app" / "source"),
                str(plan.runtime / "app" / "source" / "src"),
                str(plan.runtime / "app" / "source" / "src" / "dashboard"),
            )
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        str(plan.runtime / ".venv" / "bin" / "python"),
        "-m",
        "data_foundation.cli",
        "doctor",
        "--installer",
        "--runtime",
        str(plan.runtime),
        "--json",
    ]
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise LinuxInstallError(
            f"post-update installer doctor failed: {detail[:500] or result.returncode}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LinuxInstallError("post-update installer doctor returned invalid JSON") from exc
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict) or int(summary.get("errors") or 0) != 0:
        raise LinuxInstallError("post-update installer doctor reported a blocking error")
    return {
        "profile": str(payload.get("doctorProfile") or "installer"),
        "status": str(summary.get("status") or "unknown"),
        "errors": int(summary.get("errors") or 0),
        "warnings": int(summary.get("warnings") or 0),
        "checks": int(summary.get("checks") or 0),
    }


def _wait_for_update_service_health(
    plan: InstallPlan,
    settings: dict,
    *,
    active_units: set[str] | None = None,
    expected_source_commit: str | None = None,
) -> None:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.systemd_user import dashboard_unit, rag_unit

    endpoints: list[tuple[str, str, int, str]] = []
    paths = runtime_paths_for_home(plan.runtime)
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    dashboard_active = (
        active_units is None
        or dashboard_unit(paths, dashboard).name in active_units
    )
    rag_active = active_units is None or rag_unit(paths, server).name in active_units
    if plan.dashboard_service and dashboard_active:
        endpoints.append(
            (
                "dashboard",
                str(dashboard.get("host") or "127.0.0.1"),
                int(dashboard.get("port") or 3036),
                str(dashboard.get("healthPath") or "/health"),
            )
        )
    if plan.rag_enabled and rag_active:
        from agentic_rag.rag_server_lifecycle import (
            RagServerReadinessError,
            require_rag_server_readiness,
        )
        from agentic_rag.rag_settings import resolve_rag_settings

        try:
            require_rag_server_readiness(
                resolve_rag_settings(paths, settings),
                expected_source_commit=expected_source_commit,
                timeout_seconds=_rag_install_readiness_timeout_seconds(),
            )
        except RagServerReadinessError as exc:
            reason = str(exc.result.get("reasonCode") or exc.result.get("status") or "not-ready")
            raise LinuxInstallError(
                f"managed RAG semantic health check failed after update: {reason}"
            ) from exc
    for kind, host, port, path in endpoints:
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if not path.startswith("/"):
            path = "/" + path
        deadline = time.monotonic() + 30.0
        last_error = "unavailable"
        while True:
            connection: http.client.HTTPConnection | None = None
            try:
                connection = http.client.HTTPConnection(host, port, timeout=2)
                connection.request("GET", path, headers={"Accept": "application/json"})
                response = connection.getresponse()
                body = response.read(65536)
                payload = json.loads(body) if response.status == 200 else {}
                if response.status == 200 and isinstance(payload, dict) and str(
                    payload.get("status") or ""
                ).lower() in {"ok", "healthy"}:
                    break
                last_error = f"HTTP {response.status}"
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
                last_error = type(exc).__name__
            finally:
                if connection is not None:
                    connection.close()
            if time.monotonic() >= deadline:
                raise LinuxInstallError(
                    f"managed {kind} health check failed after update: {last_error}"
                )
            time.sleep(0.2)


def _repair_transaction_postcondition(
    runtime: Path,
    *,
    journal: Path | None = None,
) -> dict | None:
    """Return a fail-closed view of a committed repair's durable state."""

    from install import update_transaction

    try:
        runtime = runtime.expanduser().absolute().resolve(strict=True)
        marker = runtime / "app" / update_transaction.REPAIR_CONFIGURATION_PENDING_NAME
        marker_payload: bytes | None
        marker_metadata: os.stat_result | None
        try:
            marker_payload, marker_metadata = (
                update_transaction._read_repair_configuration_pending(marker)
            )
        except FileNotFoundError:
            marker_payload = None
            marker_metadata = None

        marker_tx_id = (
            marker_payload.decode("ascii").removesuffix("\n")
            if marker_payload is not None
            else None
        )
        if journal is None:
            if marker_tx_id is None:
                return None
            journal = (
                runtime
                / "app"
                / "update-transactions"
                / marker_tx_id
                / "journal.json"
            )
        journal = journal.expanduser().absolute()
        state = update_transaction._load_state(journal)
        tx_id = str(state.get("txId") or "")
        if (
            Path(str(state.get("runtime") or "")) != runtime
            or state.get("mode") != "repair"
            or state.get("status") != "committed"
            or (marker_tx_id is not None and marker_tx_id != tx_id)
        ):
            return {"status": "unsafe", "journal": str(journal)}

        for name in ("source", "venv"):
            record = state.get(name) if isinstance(state.get(name), dict) else {}
            pointer = Path(str(record.get("path") or ""))
            candidate = Path(str(record.get("candidateTarget") or ""))
            if (
                not pointer.is_symlink()
                or not candidate.is_dir()
                or pointer.resolve(strict=True) != candidate.resolve(strict=True)
            ):
                return {"status": "unsafe", "journal": str(journal)}

        pending = (
            state.get("repairConfigurationPending")
            if isinstance(state.get("repairConfigurationPending"), dict)
            else {}
        )
        expected_payload = f"{tx_id}\n".encode("ascii")
        marker_bound = (
            marker_payload == expected_payload
            and marker_metadata is not None
            and Path(str(pending.get("path") or "")) == marker
            and pending.get("txId") == tx_id
            and pending.get("sha256")
            == hashlib.sha256(expected_payload).hexdigest()
            and pending.get("device") == marker_metadata.st_dev
            and pending.get("inode") == marker_metadata.st_ino
        )
        complete = state.get("repairConfigurationComplete") is True
        lock = runtime / "app" / ".update-transaction.lock"
        if marker_payload is None:
            status = "complete" if complete else "unsafe"
        elif not marker_bound:
            status = "unsafe"
        else:
            status = (
                "completion-cleanup-pending"
                if complete
                else "configuration-pending"
            )
        return {
            "status": status,
            "journal": str(journal),
            "transactionId": tx_id,
            "lockPresent": lock.exists() or lock.is_symlink(),
            "servicesStopped": any(
                isinstance(unit, dict)
                and unit.get("stoppedByTransaction") is True
                for unit in state.get("systemdUnits", [])
            ),
        }
    except Exception:
        return None


def _pending_repair_error(
    error: Exception,
    *,
    state_certain: bool,
    rollback_complete: bool | None = None,
    services_stopped: bool | None = None,
) -> LinuxInstallError:
    nested_rollback = (
        error.rollback_complete if isinstance(error, LinuxInstallError) else None
    )
    return LinuxInstallError(
        "repair candidate source, venv, and dependencies are committed, but "
        f"Runtime configuration is still pending: {error}. Resolve the reported "
        "service or configuration problem, then rerun the same setup command "
        "with --repair-existing --yes for this Runtime",
        rollback_complete=(
            rollback_complete
            if rollback_complete is not None
            else False
            if nested_rollback is False
            else None
        ),
        state_certain=state_certain,
        stage=(
            "repair-configuration-pending"
            if state_certain
            else "repair-configuration-pending-uncertain"
        ),
        source_updated=True,
        dependencies_installed=True,
        reuses_runtime_venv=False,
        services_stopped=services_stopped,
    )


def _commit_repair_with_postcondition(plan: InstallPlan, journal: Path) -> None:
    try:
        _transaction_command("commit-repair", "--state", str(journal))
    except Exception:
        postcondition = _repair_transaction_postcondition(
            plan.runtime,
            journal=journal,
        )
        if postcondition is None or postcondition.get("status") != "configuration-pending":
            raise


def _complete_repair_with_postcondition(plan: InstallPlan, journal: Path) -> None:
    try:
        _transaction_command("complete-repair", "--state", str(journal))
    except Exception:
        postcondition = _repair_transaction_postcondition(
            plan.runtime,
            journal=journal,
        )
        if postcondition is None or postcondition.get("status") not in {
            "complete",
            "completion-cleanup-pending",
        }:
            raise
        # The journal is complete. Retry the idempotent finalizer to clear a
        # still-bound marker or a stale global lock before acknowledging it.
        _transaction_command("complete-repair", "--state", str(journal))


def _finish_committed_repair(
    plan: InstallPlan,
    *,
    journal: Path,
    source_commit: str | None,
) -> tuple[Path, dict, dict]:
    """Finish the retryable configuration phase of a committed repair.

    ``commit-repair`` intentionally makes the candidate source and venv
    durable before legacy-compatible configuration can run.  Keep the pending
    marker until every service/readiness check and doctor check has succeeded;
    ``complete-repair`` is the final gate and must remain last.
    """

    from install import update_transaction

    repair_state = update_transaction._load_state(journal)
    repair_owner_id = str(repair_state.get("txId") or "")
    if not repair_owner_id:
        raise LinuxInstallError("committed repair transaction owner is unavailable")
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        require_runtime_mutation_owner,
        runtime_mutation_guard,
    )
    from data_foundation.settings_transaction import (
        SettingsTransactionError,
        settings_mutation_barrier,
    )

    try:
        with runtime_mutation_guard(plan.runtime, blocking=True):
            require_runtime_mutation_owner(
                plan.runtime,
                owner_id=repair_owner_id,
            )
            with settings_mutation_barrier(
                runtime_paths_for_home(plan.runtime)
            ):
                dependency_contract.migrate_legacy_runtime_settings(
                    plan.runtime,
                    scheduler_enabled=plan.scheduler,
                    dashboard_enabled=plan.dashboard_service,
                    dashboard_server_enabled=plan.dashboard_service,
                    rag_server_enabled=plan.rag_enabled,
                )
    except (RuntimeMutationBusy, RuntimeMutationUnsafe, SettingsTransactionError) as exc:
        raise LinuxInstallError(
            f"repair Settings migration could not acquire a consistent transaction boundary: {exc}",
            rollback_complete=False,
            state_certain=False,
            stage="repair-configuration-pending-settings",
        ) from exc
    _write_cli_shim(plan.runtime)
    database = _initialize_database(plan)
    settings = _read_update_settings(plan.runtime)
    current_desired, current_inventory = _systemd_unit_inventory(plan, settings)
    # The committed update journal is the durable inventory captured before
    # any Settings audit can clear stale registration names.  Reuse it on a
    # repair retry so a crash between the Settings handoff and stale-unit
    # pruning cannot orphan an enabled managed unit.
    journal_inventory = tuple(
        str(unit.get("name"))
        for unit in repair_state.get("systemdUnits", [])
        if isinstance(unit, dict) and isinstance(unit.get("name"), str)
    )
    prior_inventory = tuple(sorted(set(current_inventory).union(journal_inventory)))
    desired_names = {unit.name for unit in current_desired}
    prior_units = {
        str(unit.get("name")): unit
        for unit in repair_state.get("systemdUnits", [])
        if isinstance(unit, dict) and isinstance(unit.get("name"), str)
    }
    retained_names = frozenset(
        name
        for name, unit in prior_units.items()
        if (
            name in desired_names
            and unit.get("definitionExisted") is True
            and unit.get("enableState") in {"enabled", "enabled-runtime", "disabled"}
            and unit.get("activeState") in {"active", "inactive"}
        )
    )
    handoff_result = _install_systemd_user_services(
        plan,
        expected_source_commit=source_commit,
        normalize_failed_prior_states=True,
        transaction_owner_id=repair_owner_id,
        deferred_enable_names=retained_names,
    )
    settings = _read_update_settings(plan.runtime)
    reconciliation_result = _reconcile_existing_systemd_units(
        plan,
        settings,
        prior_inventory=prior_inventory,
        normalize_failed_prior_states=True,
        transaction_owner_id=repair_owner_id,
        deferred_enable_names=retained_names,
    )
    if retained_names:
        restore_arguments = [
            "restore-repair-services",
            "--state",
            str(journal),
        ]
        for name in sorted(retained_names):
            restore_arguments.extend(("--unit", name))
        _transaction_command(*restore_arguments)
    systemd_result = {
        **reconciliation_result,
        "settingsHandoff": handoff_result,
        "restoredPriorStateUnits": sorted(retained_names),
    }
    active_units = {
        name
        for name in retained_names
        if prior_units[name].get("activeState") == "active"
    }
    active_units.update(
        unit.name
        for unit in current_desired
        if unit.enable_now and unit.name not in retained_names
    )
    _wait_for_update_service_health(
        plan,
        settings,
        active_units=active_units,
        expected_source_commit=source_commit,
    )
    _verify_updated_systemd_units(plan, settings)
    doctor = _run_update_doctor(plan)
    _complete_repair_with_postcondition(plan, journal)
    return database, systemd_result, doctor


def _recover_systemd_transactions_before_update(
    plan: InstallPlan | Path,
    *,
    runtime_guard_held: bool = False,
    owner_id: str | None = None,
) -> list[dict]:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.systemd_user import (
        SystemdUserError,
        recover_user_unit_transactions,
    )

    runtime = plan if isinstance(plan, Path) else plan.runtime
    try:
        recovery = recover_user_unit_transactions(
            runtime_paths_for_home(runtime),
            owner_id=owner_id,
            _runtime_guard_held=runtime_guard_held,
        )
    except SystemdUserError as exc:
        raise LinuxInstallError(
            f"managed systemd transaction preflight failed: {exc}",
            rollback_complete=False,
            state_certain=False,
            stage="systemd-transaction-preflight",
        ) from exc
    blocker = next(
        (
            item
            for item in recovery
            if item.get("status") in {"active", "conflict"}
        ),
        None,
    )
    if blocker is not None:
        raise LinuxInstallError(
            "managed systemd transaction preflight found an active transaction "
            "or state conflict; no update transaction was started",
            rollback_complete=True,
            state_certain=blocker.get("status") == "active",
            stage="systemd-transaction-preflight",
        )
    return recovery


def _recover_settings_transactions_before_update(
    runtime: Path,
    *,
    runtime_guard_held: bool = False,
) -> list[dict]:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        runtime_mutation_guard,
    )
    from data_foundation.settings_transaction import (
        SettingsTransactionError,
        recover_settings_transactions,
    )

    def recover() -> list[dict]:
        try:
            recovery = recover_settings_transactions(
                runtime_paths_for_home(runtime)
            )
        except SettingsTransactionError as exc:
            raise LinuxInstallError(
                f"Settings transaction preflight failed: {exc}",
                rollback_complete=False,
                state_certain=False,
                stage="settings-transaction-preflight",
            ) from exc
        blocker = next(
            (
                item
                for item in recovery
                if item.get("status")
                in {"conflict", "compensation-incomplete"}
            ),
            None,
        )
        if blocker is not None:
            raise LinuxInstallError(
                "Settings transaction preflight found a state conflict; "
                "no update transaction was started",
                rollback_complete=False,
                state_certain=False,
                stage="settings-transaction-preflight",
            )
        return recovery

    if runtime_guard_held:
        return recover()
    try:
        with runtime_mutation_guard(runtime, blocking=True):
            return recover()
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise LinuxInstallError(
            f"Settings transaction preflight could not lock the Runtime: {exc}",
            rollback_complete=False,
            state_certain=False,
            stage="settings-transaction-preflight",
        ) from exc


def _update(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
    args: argparse.Namespace,
) -> dict:
    if plan.dry_run:
        return _update_guarded(plan, selection, args)
    from data_foundation.runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        runtime_mutation_guard,
    )

    try:
        # Keep one inherited flock across every child mutation, health check,
        # commit, and rollback. Durable owner records still provide recovery
        # identity; the flock prevents recovery racing an orphaned child.
        with runtime_mutation_guard(plan.runtime, blocking=True):
            return _update_guarded(plan, selection, args)
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise LinuxInstallError(str(exc)) from exc


def _update_guarded(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
    args: argparse.Namespace,
) -> dict:
    if plan.profile_evidence is None:
        raise LinuxInstallError("update dependency profile evidence is missing")
    existing_repair = _repair_transaction_postcondition(plan.runtime)
    inherited_repair_inventory: tuple[str, ...] = ()
    existing_repair_journal: Path | None = None
    existing_repair_state: dict | None = None
    if existing_repair is not None:
        existing_status = str(existing_repair.get("status") or "")
        if existing_status == "unsafe":
            raise LinuxInstallError(
                "the committed Runtime repair marker or journal is unsafe; "
                "preserved state requires operator review",
                rollback_complete=False,
                state_certain=False,
                stage="repair-configuration-pending-uncertain",
                source_updated=True,
                dependencies_installed=True,
                reuses_runtime_venv=False,
                services_stopped=None,
            )
        if existing_status in {
            "configuration-pending",
            "completion-cleanup-pending",
        }:
            if plan.update_mode != "repair":
                raise LinuxInstallError(
                    "a committed Runtime repair is still awaiting configuration; "
                    "rerun setup with --repair-existing --yes before any other update"
                )
            existing_repair_journal = Path(str(existing_repair["journal"]))
            from install import update_transaction

            existing_repair_state = update_transaction._load_state(
                existing_repair_journal
            )
            inherited_repair_inventory = tuple(
                sorted(
                    {
                        str(unit.get("name"))
                        for unit in existing_repair_state.get("systemdUnits", [])
                        if isinstance(unit, dict)
                        and isinstance(unit.get("name"), str)
                    }
                )
            )
    settings = _read_update_settings(plan.runtime)
    from data_foundation.source_identity import loaded_source_commit

    prior_source_commit = loaded_source_commit(
        plan.runtime / "app" / "source" / "src" / "data_foundation" / "operator_cli.py"
    )
    _candidate_release_id, candidate_source_commit = _source_identity(plan.source_root)
    if plan.rag_enabled and candidate_source_commit is None:
        raise LinuxInstallError(
            "managed RAG update requires a clean source tree with an exact Git commit identity"
        )
    desired, inventory = _systemd_unit_inventory(plan, settings)
    inventory = tuple(sorted(set(inventory).union(inherited_repair_inventory)))
    if existing_repair_journal is not None and existing_repair_state is not None:
        if (
            candidate_source_commit is None
            or prior_source_commit is None
            or prior_source_commit != candidate_source_commit
        ):
            raise LinuxInstallError(
                "the requested source cannot be proven identical to the committed repair candidate; "
                "rerun the same exact Git source/ref to finish Runtime configuration"
            )
        if plan.dry_run:
            return {
                "schemaVersion": 1,
                "status": "planned",
                "platform": "linux",
                "runtime": str(plan.runtime),
                "updateMode": "repair",
                "reason": "resume-committed-repair-configuration",
                "profiles": list(plan.profiles),
                "reusesRuntimeVenv": False,
                "plannedDependenciesInstalled": False,
                "managedUnits": list(inventory),
                "transactionJournal": str(existing_repair_journal),
                "writes": False,
            }
        _recover_systemd_transactions_before_update(
            plan,
            owner_id=str(existing_repair_state.get("txId") or ""),
        )
        _validate_existing_systemd_unit_ownership(plan, inventory)
        database, systemd_result, doctor = _finish_committed_repair(
            plan,
            journal=existing_repair_journal,
            source_commit=prior_source_commit,
        )
        return {
            "schemaVersion": 1,
            "status": "repaired",
            "platform": "linux",
            "runtime": str(plan.runtime),
            "database": str(database),
            "profiles": list(plan.profiles),
            "updateMode": "repair",
            "reason": "resumed-committed-repair-configuration",
            "dependenciesInstalled": False,
            "reusesRuntimeVenv": False,
            "systemdUser": systemd_result,
            "doctor": doctor,
            "transactionJournal": str(existing_repair_journal),
        }

    dependency_plan = _dependency_update_plan(plan, selection)
    cache_root = plan.runtime / "app" / "dependency-cache" / "v1"
    rebuild = dependency_plan["updateMode"] == "rebuild-candidate-venv"
    transaction_mode = "repair" if plan.update_mode == "repair" else "upgrade" if rebuild else "source-only"
    if not plan.dry_run:
        _recover_systemd_transactions_before_update(plan)
    systemctl = os.environ.get("ACTANARA_INSTALL_SYSTEMCTL") or shutil.which("systemctl") or ""
    tx_id = (
        datetime.now().strftime("%Y%m%dT%H%M%S")
        + f"-{os.getpid()}-{secrets.token_hex(4)}"
    )
    dependency_log = plan.runtime / "state" / "logs" / f"dependencies-{tx_id}.log"
    if plan.dry_run:
        return {
            "schemaVersion": 1,
            "status": "planned",
            "platform": "linux",
            "runtime": str(plan.runtime),
            "updateMode": transaction_mode,
            "reason": dependency_plan["reason"],
            "profiles": list(plan.profiles),
            "reusesRuntimeVenv": not rebuild,
            "plannedDependenciesInstalled": rebuild,
            "managedUnits": list(inventory),
            "writes": False,
        }
    if rebuild:
        dependency_contract.materialize_dependency_cache(
            cache_root,
            selection,
            python=plan.python,
            offline=plan.offline,
            timeout=900,
            diagnostic_log=dependency_log,
        )
    _validate_existing_systemd_unit_ownership(plan, inventory)
    if plan.update_mode != "repair":
        _validate_existing_systemd_units_for_update(plan, desired, inventory)
    evidence = plan.profile_evidence
    begin_arguments = [
        "begin",
        "--runtime",
        str(plan.runtime),
        "--home",
        str(Path.home()),
        "--source-pointer",
        str(plan.runtime / "app" / "source"),
        "--venv-pointer",
        str(plan.runtime / ".venv"),
        "--expected-settings-sha256",
        str(evidence["settingsSha256"]),
        "--mode",
        transaction_mode,
        "--tx-id",
        tx_id,
        "--owner-pid",
        str(os.getpid()),
        "--platform",
        "Linux",
        "--uid",
        str(os.getuid()),
    ]
    if evidence.get("activeMarkerStatus") == "unavailable":
        begin_arguments.append("--settings-only-profile-evidence")
    else:
        begin_arguments.extend(
            (
                "--expected-active-venv-target",
                str(evidence["activeVenvTarget"]),
                "--expected-active-marker-status",
                str(evidence["activeMarkerStatus"]),
            )
        )
        if evidence.get("activeMarkerSha256"):
            begin_arguments.extend(
                ("--expected-active-marker-sha256", str(evidence["activeMarkerSha256"]))
            )
    if inventory:
        if not systemctl:
            raise LinuxInstallError("systemctl is unavailable for the managed update service inventory")
        begin_arguments.extend(("--systemctl", systemctl))
        for name in inventory:
            begin_arguments.extend(("--systemd-unit", name))
        if plan.update_mode != "repair":
            for unit in desired:
                definition_sha256 = hashlib.sha256(
                    unit.content.encode("utf-8")
                ).hexdigest()
                begin_arguments.extend(
                    (
                        "--expected-systemd-unit-sha256",
                        f"{unit.name}={definition_sha256}",
                    )
                )
    journal: Path | None = None
    prior_active_units: set[str] = set()
    committed = False
    repair_configuration_pending = False
    try:
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(plan.runtime, blocking=True):
                _recover_settings_transactions_before_update(
                    plan.runtime,
                    runtime_guard_held=True,
                )
                _recover_systemd_transactions_before_update(
                    plan,
                    runtime_guard_held=True,
                )
                current_settings = _read_update_settings(plan.runtime)
                current_desired, current_inventory = _systemd_unit_inventory(
                    plan,
                    current_settings,
                )
                current_inventory = tuple(
                    sorted(set(current_inventory).union(inherited_repair_inventory))
                )
                if current_settings != settings or current_inventory != inventory:
                    raise LinuxInstallError(
                        "Runtime Settings or managed systemd inventory changed while the update was prepared; "
                        "no update transaction was started"
                    )
                if plan.update_mode != "repair":
                    _validate_existing_systemd_units_for_update(
                        plan,
                        current_desired,
                        current_inventory,
                    )
                journal = Path(_transaction_command(*begin_arguments).stdout.strip())
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise LinuxInstallError(str(exc)) from exc
        journal_state = json.loads(journal.read_text(encoding="utf-8"))
        prior_active_units = {
            str(unit.get("name"))
            for unit in journal_state.get("systemdUnits", [])
            if isinstance(unit, dict) and unit.get("active") is True
        }
        temporary = Path(
            _transaction_command(
                "reserve-artifact",
                "--state",
                str(journal),
                "--kind",
                "source-temp",
            ).stdout.strip()
        )
        commit = candidate_source_commit
        _stage_source(
            plan,
            temporary,
            commit,
            manifest_release_id=tx_id,
            precreated=True,
        )
        candidate_source = Path(
            _transaction_command(
                "promote-source-artifact",
                "--state",
                str(journal),
            ).stdout.strip()
        )
        _transaction_command(
            "record-candidate",
            "--state",
            str(journal),
            "--kind",
            "source",
            "--candidate",
            str(candidate_source),
        )
        migration_arguments = [
            "verify-migration-compatibility",
            "--state",
            str(journal),
        ]
        if plan.update_mode == "repair":
            migration_arguments.append("--allow-legacy-repair")
        _transaction_command(*migration_arguments)
        candidate_venv: Path | None = None
        if rebuild:
            candidate_venv = Path(
                _transaction_command(
                    "reserve-artifact",
                    "--state",
                    str(journal),
                    "--kind",
                    "venv",
                ).stdout.strip()
            )
            venv_python = _seed_venv_pip(plan, candidate_venv)
            dependency_contract.install_locked_dependencies(
                cache_root,
                selection,
                venv_python=venv_python,
                timeout=900,
                diagnostic_log=dependency_log,
            )
            dependency_contract.write_dependency_marker(candidate_venv, selection)
            dependency_contract.verify_dependency_marker(candidate_venv, selection)
            _transaction_command(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(candidate_venv),
            )
        _transaction_command("stop", "--state", str(journal))
        home = Path.home()
        _transaction_command(
            "capture-mutable",
            "--state",
            str(journal),
            "--location",
            os.environ.get(
                "ACTANARA_LOCATION_FILE",
                str(home / ".config" / "actanara" / "location.json"),
            ),
            "--cli-shim",
            str(plan.runtime / "bin" / "actanara"),
            "--user-cli-shim",
            str(home / ".local" / "bin" / "actanara"),
            "--desktop-link",
            str(home / "Desktop" / "Actanara"),
            "--shell-profile",
            "" if args.no_shell_path else str(home / ".profile"),
        )
        _transaction_command("normalize-service-plists", "--state", str(journal))
        _transaction_command("promote", "--state", str(journal))
        if plan.update_mode == "repair":
            _commit_repair_with_postcondition(plan, journal)
            committed = True
            repair_configuration_pending = True
            database, systemd_result, doctor = _finish_committed_repair(
                plan,
                journal=journal,
                source_commit=commit,
            )
            repair_configuration_pending = False
            return {
                "schemaVersion": 1,
                "status": "repaired",
                "platform": "linux",
                "runtime": str(plan.runtime),
                "database": str(database),
                "profiles": list(plan.profiles),
                "updateMode": "repair",
                "dependenciesInstalled": True,
                "reusesRuntimeVenv": False,
                "systemdUser": systemd_result,
                "doctor": doctor,
                "transactionJournal": str(journal),
            }
        _initialize_database(plan)
        _transaction_command("restore-services", "--state", str(journal))
        _wait_for_update_service_health(
            plan,
            settings,
            active_units=prior_active_units,
            expected_source_commit=commit,
        )
        systemd_probe = _verify_updated_systemd_units(plan, settings)
        doctor = _run_update_doctor(plan)
        _transaction_command("verify", "--state", str(journal))
        _transaction_command("commit", "--state", str(journal))
        committed = True
        return {
            "schemaVersion": 1,
            "status": "updated",
            "platform": "linux",
            "runtime": str(plan.runtime),
            "profiles": list(plan.profiles),
            "updateMode": transaction_mode,
            "reason": dependency_plan["reason"],
            "dependenciesInstalled": rebuild,
            "reusesRuntimeVenv": not rebuild,
            "systemdUser": systemd_probe,
            "doctor": doctor,
            "transactionJournal": str(journal),
        }
    except Exception as exc:
        if journal is not None and plan.update_mode == "repair" and not committed:
            commit_postcondition = _repair_transaction_postcondition(
                plan.runtime,
                journal=journal,
            )
            if commit_postcondition is not None and commit_postcondition.get("status") in {
                "configuration-pending",
                "completion-cleanup-pending",
            }:
                committed = True
                repair_configuration_pending = True
        if journal is not None and not committed:
            rollback = _transaction_command(
                "rollback",
                "--state",
                str(journal),
                check=False,
            )
            if rollback.returncode != 0:
                detail = (rollback.stderr or rollback.stdout).strip()
                raise LinuxInstallError(
                    f"update failed and rollback is incomplete: {detail or rollback.returncode}",
                    rollback_complete=False,
                    state_certain=False,
                    stage="rollback-incomplete",
                ) from exc
            try:
                _wait_for_update_service_health(
                    plan,
                    settings,
                    active_units=prior_active_units,
                    expected_source_commit=prior_source_commit,
                )
            except Exception as recovery_exc:
                raise LinuxInstallError(
                    f"update rollback restored transaction state but prior service health is unconfirmed: "
                    f"{recovery_exc}",
                    rollback_complete=False,
                    state_certain=False,
                    stage="rollback-incomplete",
                ) from exc
            restored_pending = (
                _repair_transaction_postcondition(plan.runtime)
                if plan.update_mode == "repair"
                else None
            )
            if restored_pending is not None and restored_pending.get("status") in {
                "configuration-pending",
                "completion-cleanup-pending",
            }:
                raise _pending_repair_error(
                    exc,
                    state_certain=True,
                    rollback_complete=True,
                    services_stopped=restored_pending.get("servicesStopped"),
                ) from exc
            raise LinuxInstallError(
                str(exc),
                rollback_complete=True,
                state_certain=True,
                stage="rollback-complete",
            ) from exc
        if repair_configuration_pending:
            pending_postcondition = (
                _repair_transaction_postcondition(
                    plan.runtime,
                    journal=journal,
                )
                if journal is not None
                else None
            )
            pending_state_certain = (
                pending_postcondition is not None
                and pending_postcondition.get("status")
                in {"configuration-pending", "completion-cleanup-pending"}
                and not (
                    isinstance(exc, LinuxInstallError)
                    and exc.state_certain is False
                )
            )
            raise _pending_repair_error(
                exc,
                state_certain=pending_state_certain,
                services_stopped=(
                    pending_postcondition.get("servicesStopped")
                    if pending_postcondition is not None
                    else None
                ),
            ) from exc
        if isinstance(exc, LinuxInstallError):
            raise
        if isinstance(exc, dependency_contract.ContractError):
            raise LinuxInstallError(f"dependency contract failed: {exc.message}") from exc
        raise LinuxInstallError(str(exc)) from exc


def fresh_install_checkpoint(phase: str, transaction_id: str) -> None:
    """No-op hook used by deterministic fresh-install interruption tests."""

    if os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        return
    if os.environ.get("ACTANARA_INSTALL_TEST_KILL_PHASE") == phase:
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get("ACTANARA_INSTALL_TEST_FAIL_PHASE") == phase:
        raise LinuxInstallError(
            f"synthetic fresh install failure at phase {phase} ({transaction_id})"
        )


def _fresh_install_transaction_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}-{secrets.token_hex(4)}"


def _fresh_install_lock_path(runtime: Path) -> Path:
    return runtime / "app" / FRESH_INSTALL_LOCK_NAME


def _fresh_install_owner_path(staging: Path) -> Path:
    return staging / "owner.json"


def _fresh_install_lock_payload(staging: Path, transaction_id: str) -> dict:
    from install.update_transaction import _process_identity

    process_identity = _process_identity(os.getpid())
    if process_identity is None:
        raise LinuxInstallError("fresh install process identity is unavailable")
    return {
        "txId": transaction_id,
        "journal": str(staging / FRESH_INSTALL_JOURNAL_NAME),
        "ownerPid": os.getpid(),
        "ownerProcessIdentity": process_identity,
    }


def _acquire_fresh_install_lock(
    runtime: Path,
    staging: Path,
    transaction_id: str,
) -> dict:
    lock = _fresh_install_lock_path(runtime)
    owner = _fresh_install_owner_path(staging)
    payload = _fresh_install_lock_payload(staging, transaction_id)
    raw = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(owner, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LinuxInstallError("fresh install owner record could not be created safely")
        os.write(descriptor, raw)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(owner, lock)
    except FileExistsError as exc:
        owner.unlink(missing_ok=True)
        raise LinuxInstallError(
            "another Runtime install or update is active or requires recovery"
        ) from exc
    lock_metadata = lock.stat(follow_symlinks=False)
    owner_metadata = owner.stat(follow_symlinks=False)
    if (
        lock_metadata.st_dev != owner_metadata.st_dev
        or lock_metadata.st_ino != owner_metadata.st_ino
        or lock_metadata.st_nlink != 2
    ):
        raise LinuxInstallError("fresh install lock ownership could not be verified")
    return payload


def _release_fresh_install_lock(runtime: Path, staging: Path, payload: dict) -> None:
    lock = _fresh_install_lock_path(runtime)
    owner = _fresh_install_owner_path(staging)
    expected = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    try:
        lock_metadata = lock.stat(follow_symlinks=False)
        owner_metadata = owner.stat(follow_symlinks=False)
        lock_raw = lock.read_bytes()
        owner_raw = owner.read_bytes()
    except OSError as exc:
        raise LinuxInstallError("fresh install lock ownership is unavailable") from exc
    if (
        not stat.S_ISREG(lock_metadata.st_mode)
        or not stat.S_ISREG(owner_metadata.st_mode)
        or lock_metadata.st_uid != os.getuid()
        or owner_metadata.st_uid != os.getuid()
        or lock_metadata.st_dev != owner_metadata.st_dev
        or lock_metadata.st_ino != owner_metadata.st_ino
        or lock_raw != expected
        or owner_raw != expected
    ):
        raise LinuxInstallError("fresh install lock ownership changed")
    lock.unlink()


def _fresh_file_hash(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return FRESH_MISSING_HASH
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise LinuxInstallError(f"fresh install found an unsafe mutable file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fresh_mutable_paths(runtime: Path, location: Path) -> dict[str, Path]:
    database = runtime / "data" / "actanara_data.sqlite3"
    return {
        "location": location,
        "runtimeCli": runtime / "bin" / "actanara",
        "settings": runtime / "config" / "settings.json",
        "runtimeManifest": runtime / "config" / "runtime.json",
        "database": database,
        "databaseWal": Path(str(database) + "-wal"),
        "databaseShm": Path(str(database) + "-shm"),
    }


def _record_fresh_mutable_hashes(
    staging: Path,
    journal: dict,
    keys: tuple[str, ...],
    phase: str,
    **updates: object,
) -> None:
    records = journal.get("managedMutableHashes")
    if not isinstance(records, dict):
        raise LinuxInstallError("fresh install mutable journal is unavailable")
    next_records = {key: dict(value) for key, value in records.items()}
    for key in keys:
        record = next_records.get(key)
        if not isinstance(record, dict):
            raise LinuxInstallError("fresh install mutable journal key is invalid")
        record["afterSha256"] = _fresh_file_hash(Path(str(record["path"])))
    _advance_fresh_install_journal(
        staging,
        journal,
        phase,
        managedMutableHashes=next_records,
        **updates,
    )


def _arm_fresh_mutable_hash(
    staging: Path,
    journal: dict,
    key: str,
    expected_content: bytes,
    phase: str,
) -> None:
    records = journal.get("managedMutableHashes")
    if not isinstance(records, dict) or not isinstance(records.get(key), dict):
        raise LinuxInstallError("fresh install mutable journal key is invalid")
    next_records = {name: dict(value) for name, value in records.items()}
    next_records[key]["afterSha256"] = hashlib.sha256(expected_content).hexdigest()
    _advance_fresh_install_journal(
        staging,
        journal,
        phase,
        managedMutableHashes=next_records,
    )


def _fresh_generation_identity(
    path: Path,
    *,
    marker_name: str,
    expected_path: Path | None = None,
) -> dict:
    metadata = path.stat(follow_symlinks=False)
    marker = path / marker_name
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise LinuxInstallError(f"fresh generation is unsafe: {path}")
    marker_hash = _fresh_file_hash(marker)
    if marker_hash == FRESH_MISSING_HASH:
        raise LinuxInstallError(f"fresh generation identity marker is missing: {path}")
    return {
        "path": str(expected_path if expected_path is not None else path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "marker": marker_name,
        "markerSha256": marker_hash,
    }


def _write_fresh_install_journal(staging: Path, journal: dict) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{FRESH_INSTALL_JOURNAL_NAME}.",
        dir=staging,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(journal, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, staging / FRESH_INSTALL_JOURNAL_NAME)
        directory = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _advance_fresh_install_journal(
    staging: Path,
    journal: dict,
    phase: str,
    **updates: object,
) -> None:
    journal.update(updates)
    journal["phase"] = phase
    journal["updatedAt"] = datetime.now().astimezone().isoformat()
    _write_fresh_install_journal(staging, journal)


def _capture_fresh_file_snapshot(staging: Path, key: str, path: Path) -> dict:
    record: dict[str, object] = {
        "path": str(path),
        "existed": False,
        "backup": None,
        "mode": None,
        "beforeSha256": FRESH_MISSING_HASH,
    }
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return record
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise LinuxInstallError(f"fresh install cannot safely preserve existing file: {path}")
    if metadata.st_size > 8 * 1024 * 1024:
        raise LinuxInstallError(f"fresh install preservation file is unexpectedly large: {path}")
    backup_root = staging / "backups"
    _secure_directory(backup_root)
    backup = backup_root / key
    shutil.copy2(path, backup, follow_symlinks=False)
    backup.chmod(0o600)
    record.update(
        {
            "existed": True,
            "backup": str(backup.relative_to(staging)),
            "mode": stat.S_IMODE(metadata.st_mode),
            "beforeSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
    return record


def _restore_fresh_file_snapshot(
    staging: Path,
    record: dict,
    *,
    expected_path: Path,
    snapshot_key: str,
    after_sha256: str | None,
) -> None:
    if Path(str(record.get("path") or "")) != expected_path:
        raise LinuxInstallError("fresh install recovery snapshot path is inconsistent")
    current_hash = _fresh_file_hash(expected_path)
    allowed_hashes = {
        str(record.get("beforeSha256") or ""),
        str(after_sha256 or ""),
    }
    allowed_hashes.discard("")
    if current_hash not in allowed_hashes:
        raise LinuxInstallError(
            f"fresh install recovery found a concurrent mutable-file change: {expected_path}"
        )
    if record.get("existed") is not True:
        try:
            metadata = expected_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            raise LinuxInstallError(f"fresh install recovery refuses to remove a directory: {expected_path}")
        expected_path.unlink()
        return
    relative = Path(str(record.get("backup") or ""))
    backup = staging / relative
    expected_backup = Path("backups") / snapshot_key
    if (
        relative != expected_backup
        or backup.is_symlink()
        or not backup.is_file()
        or backup.stat(follow_symlinks=False).st_uid != os.getuid()
        or backup.stat(follow_symlinks=False).st_nlink != 1
        or hashlib.sha256(backup.read_bytes()).hexdigest()
        != record.get("beforeSha256")
    ):
        raise LinuxInstallError("fresh install recovery snapshot is unavailable")
    expected_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{expected_path.name}.", dir=expected_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(backup.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        mode = record.get("mode")
        temporary.chmod(mode if isinstance(mode, int) else 0o600)
        os.replace(temporary, expected_path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_fresh_pointer(path: Path, expected: Path, identity: dict | None) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or Path(os.readlink(path)) != expected
        or not isinstance(identity, dict)
        or metadata.st_dev != identity.get("device")
        or metadata.st_ino != identity.get("inode")
    ):
        raise LinuxInstallError(f"fresh install recovery found a changed pointer: {path}")
    path.unlink()


def _remove_fresh_generation(
    path: Path,
    *,
    parent: Path,
    transaction_id: str,
    identity: dict | None,
) -> None:
    if path.parent != parent or not path.name.endswith(f"-{transaction_id}"):
        raise LinuxInstallError("fresh install recovery generation target is inconsistent")
    if not path.exists() and not path.is_symlink():
        return
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not path.is_dir()
        or not isinstance(identity, dict)
        or Path(str(identity.get("path") or "")) != path
        or metadata.st_dev != identity.get("device")
        or metadata.st_ino != identity.get("inode")
    ):
        raise LinuxInstallError(f"fresh install recovery found an unsafe generation: {path}")
    marker = path / str(identity.get("marker") or "")
    if _fresh_file_hash(marker) != identity.get("markerSha256"):
        raise LinuxInstallError(f"fresh install recovery found a changed generation: {path}")
    shutil.rmtree(path)


def _fresh_pointer_identity(path: Path, *, expected_path: Path | None = None) -> dict:
    metadata = path.lstat()
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise LinuxInstallError(f"fresh install pointer is unsafe: {path}")
    return {
        "path": str(expected_path if expected_path is not None else path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "rawTarget": os.readlink(path),
    }


def _fresh_database_identity(path: Path) -> dict:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise LinuxInstallError("fresh Runtime database identity is unsafe")
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _fresh_user_shim_staging_path(transaction_id: str) -> Path:
    return Path.home() / ".local" / "bin" / f".actanara-{transaction_id}.next"


def _prune_fresh_staging_root(root: Path) -> None:
    # The root is shared by independent fresh transactions. ENOTEMPTY means
    # another transaction owns a sibling, not that cleanup of this one failed.
    try:
        root.rmdir()
    except OSError:
        pass


def _validate_fresh_rollback_cas(staging: Path, journal: dict) -> None:
    runtime = Path(str(journal["runtime"]))
    records = journal["managedMutableHashes"]
    database_identity = journal.get("databaseIdentity")
    for key, record in records.items():
        path = Path(str(record["path"]))
        if key in {"databaseWal", "databaseShm"}:
            # Candidate services can legitimately rotate or delete SQLite
            # sidecars after their last journaled hash. Validate/remove them
            # only after the service transaction has been quiesced below.
            continue
        current = _fresh_file_hash(path)
        allowed = {str(record["beforeSha256"])}
        if record.get("afterSha256") is not None:
            allowed.add(str(record["afterSha256"]))
        if current in allowed:
            continue
        if (
            key == "database"
            and journal.get("databaseMutationArmed") is True
            and database_identity is None
            and current != FRESH_MISSING_HASH
        ):
            continue
        if key == "database" and isinstance(database_identity, dict):
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                metadata = None
            if (
                metadata is not None
                and metadata.st_dev == database_identity.get("device")
                and metadata.st_ino == database_identity.get("inode")
            ):
                continue
        raise LinuxInstallError(
            f"fresh install recovery found a concurrent mutable-file change: {path}"
        )
    pointer_records = (
        (runtime / "app" / "source", journal.get("sourcePointerIdentity")),
        (runtime / ".venv", journal.get("venvPointerIdentity")),
    )
    for path, identity in pointer_records:
        if identity is None:
            if path.exists() or path.is_symlink():
                raise LinuxInstallError(
                    f"fresh install recovery found an unjournaled pointer: {path}"
                )
            continue
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_dev != identity.get("device")
            or metadata.st_ino != identity.get("inode")
            or os.readlink(path) != identity.get("rawTarget")
        ):
            raise LinuxInstallError(f"fresh install recovery found a changed pointer: {path}")
    for key, path in (
        ("releaseIdentity", Path(str(journal["releaseTarget"]))),
        ("venvIdentity", Path(str(journal["venvTarget"]))),
    ):
        identity = journal.get(key)
        if identity is None:
            if path.exists() or path.is_symlink():
                raise LinuxInstallError(
                    f"fresh install recovery found an unjournaled generation: {path}"
                )
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        marker = path / str(identity.get("marker") or "")
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != identity.get("device")
            or metadata.st_ino != identity.get("inode")
            or _fresh_file_hash(marker) != identity.get("markerSha256")
        ):
            raise LinuxInstallError(f"fresh install recovery found a changed generation: {path}")
    shim_identity = journal.get("userShimCreatedIdentity")
    shim = Path.home() / ".local" / "bin" / "actanara"
    shim_staging = Path(str(journal["userShimStagingPath"]))
    if journal.get("userShimExisted") is False:
        for path in (shim, shim_staging):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or os.readlink(path) != str(runtime / "bin" / "actanara")
                or (
                    isinstance(shim_identity, dict)
                    and (
                        metadata.st_dev != shim_identity.get("device")
                        or metadata.st_ino != shim_identity.get("inode")
                    )
                )
                or (path == shim and not isinstance(shim_identity, dict))
            ):
                raise LinuxInstallError("fresh install recovery found a changed user CLI shim")
    elif shim_staging.exists() or shim_staging.is_symlink():
        raise LinuxInstallError("fresh install recovery found an unexpected user CLI staging shim")


def _fresh_install_committed_postcondition(
    runtime: Path,
    staging: Path,
    journal: dict,
) -> bool:
    journal_path = staging / FRESH_INSTALL_JOURNAL_NAME
    try:
        durable = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(durable, dict):
            return False
        _validated_fresh_install_journal(runtime, staging, durable)
        if durable.get("status") != "committed" or durable.get("phase") != "committed":
            return False
        if durable.get("databaseWorker") is not None:
            return False
        _validate_fresh_rollback_cas(staging, durable)
        for identity_key in (
            "sourcePointerIdentity",
            "venvPointerIdentity",
            "releaseIdentity",
            "venvIdentity",
            "databaseIdentity",
        ):
            if not isinstance(durable.get(identity_key), dict):
                return False
        for key in ("location", "runtimeCli", "settings", "runtimeManifest", "database"):
            record = durable["managedMutableHashes"][key]
            after_hash = record.get("afterSha256")
            if (
                after_hash in {None, FRESH_MISSING_HASH}
                or (key != "database" and _fresh_file_hash(Path(record["path"])) != after_hash)
            ):
                return False
        for identity_key in (
            "sourcePointerIdentity",
            "venvPointerIdentity",
            "releaseIdentity",
            "venvIdentity",
        ):
            identity_path = Path(str(durable[identity_key]["path"]))
            if not identity_path.exists() and not identity_path.is_symlink():
                return False
        shim_identity = durable.get("userShimCreatedIdentity")
        shim_staging = Path(str(durable["userShimStagingPath"]))
        if shim_staging.exists() or shim_staging.is_symlink():
            return False
        if isinstance(shim_identity, dict):
            shim_path = Path(str(shim_identity["path"]))
            if not shim_path.is_symlink():
                return False
        database_path = Path(durable["managedMutableHashes"]["database"]["path"])
        database_metadata = database_path.stat(follow_symlinks=False)
        if (
            database_metadata.st_dev != durable["databaseIdentity"]["device"]
            or database_metadata.st_ino != durable["databaseIdentity"]["inode"]
        ):
            return False
        journal.clear()
        journal.update(durable)
        return True
    except Exception:
        return False


def _rollback_fresh_service_transaction(
    runtime: Path,
    transaction_id: str | None,
    *,
    owner_id: str,
) -> None:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.systemd_user import (
        SystemdUserError,
        recover_user_unit_transactions,
        rollback_user_unit_transaction,
    )

    paths = runtime_paths_for_home(runtime)
    try:
        if transaction_id:
            rollback_user_unit_transaction(paths, transaction_id)
            return
        recovery = recover_user_unit_transactions(paths, owner_id=owner_id)
    except SystemdUserError as exc:
        raise LinuxInstallError(
            f"fresh install systemd rollback is incomplete: {exc}"
        ) from exc
    if any(item.get("status") == "conflict" for item in recovery):
        raise LinuxInstallError("fresh install systemd recovery found a state conflict")


def _recover_fresh_settings_transactions(runtime: Path) -> None:
    from data_foundation.paths import runtime_paths_for_home
    from data_foundation.settings_transaction import recover_settings_transactions

    try:
        recovery = recover_settings_transactions(runtime_paths_for_home(runtime))
    except Exception as exc:
        raise LinuxInstallError(
            f"fresh install Settings recovery could not run: {exc}"
        ) from exc
    incomplete = [
        item
        for item in recovery
        if item.get("status") not in {"compensated"}
    ]
    if incomplete:
        raise LinuxInstallError("fresh install Settings recovery found a state conflict")


def _validated_fresh_install_journal(runtime: Path, staging: Path, journal: dict) -> dict:
    transaction_id = str(journal.get("transactionId") or "")
    expected_staging = runtime / "app" / FRESH_INSTALL_STAGING_NAME / transaction_id
    release_target = Path(str(journal.get("releaseTarget") or ""))
    venv_target = Path(str(journal.get("venvTarget") or ""))
    allowed_fields = {
        "schemaVersion",
        "product",
        "transactionId",
        "runtime",
        "stagingRoot",
        "status",
        "phase",
        "createdAt",
        "updatedAt",
        "releaseTarget",
        "venvTarget",
        "sourcePointer",
        "venvPointer",
        "serviceTransactionId",
        "configurationSettingsTransactionId",
        "serviceSettingsTransactionId",
        "databaseMutationArmed",
        "databaseWorker",
        "userShimExisted",
        "userShimStagingPath",
        "snapshots",
        "ownerPid",
        "ownerProcessIdentity",
        "managedMutableHashes",
        "sourcePointerIdentity",
        "venvPointerIdentity",
        "releaseIdentity",
        "venvIdentity",
        "databaseIdentity",
        "userShimCreatedIdentity",
    }
    if (
        set(journal) - allowed_fields
        or
        journal.get("schemaVersion") != FRESH_INSTALL_SCHEMA_VERSION
        or journal.get("product") != "actanara"
        or not FRESH_TRANSACTION_ID_RE.fullmatch(transaction_id)
        or Path(str(journal.get("runtime") or "")) != runtime
        or staging != expected_staging
        or Path(str(journal.get("stagingRoot") or "")) != expected_staging
        or release_target.parent != runtime / "app" / "releases"
        or venv_target.parent != runtime / "app" / "venvs"
        or not release_target.name.endswith(f"-{transaction_id}")
        or not venv_target.name.endswith(f"-{transaction_id}")
        or Path(str(journal.get("sourcePointer") or "")) != runtime / "app" / "source"
        or Path(str(journal.get("venvPointer") or "")) != runtime / ".venv"
        or Path(str(journal.get("userShimStagingPath") or ""))
        != _fresh_user_shim_staging_path(transaction_id)
        or not isinstance(journal.get("ownerPid"), int)
        or not isinstance(journal.get("ownerProcessIdentity"), str)
        or journal.get("status") not in {"active", "committed"}
        or type(journal.get("databaseMutationArmed")) is not bool
        or (
            journal.get("configurationSettingsTransactionId") is not None
            and not re.fullmatch(
                r"[0-9a-f]{32}",
                str(journal.get("configurationSettingsTransactionId") or ""),
            )
        )
        or (
            journal.get("serviceSettingsTransactionId") is not None
            and not re.fullmatch(
                r"[0-9a-f]{32}",
                str(journal.get("serviceSettingsTransactionId") or ""),
            )
        )
    ):
        raise LinuxInstallError("fresh install recovery journal is unsafe")
    database_worker = journal.get("databaseWorker")
    if database_worker is not None and (
        not isinstance(database_worker, dict)
        or set(database_worker) != {"pid", "processGroup", "processIdentity"}
        or not isinstance(database_worker.get("pid"), int)
        or int(database_worker["pid"]) <= 1
        or database_worker.get("processGroup") != database_worker.get("pid")
        or not isinstance(database_worker.get("processIdentity"), str)
        or not database_worker.get("processIdentity")
    ):
        raise LinuxInstallError("fresh install database worker identity is unsafe")
    snapshots = journal.get("snapshots")
    if (
        not isinstance(snapshots, dict)
        or set(snapshots) != {"location", "runtimeCli"}
        or not all(isinstance(value, dict) for value in snapshots.values())
    ):
        raise LinuxInstallError("fresh install recovery snapshots are invalid")
    location = Path(
        os.environ.get(
            "ACTANARA_LOCATION_FILE",
            str(Path.home() / ".config" / "actanara" / "location.json"),
        )
    ).expanduser().absolute()
    if Path(str(snapshots["location"].get("path") or "")) != location:
        raise LinuxInstallError(
            "fresh install recovery needs the original ACTANARA_LOCATION_FILE setting"
        )
    if Path(str(snapshots["runtimeCli"].get("path") or "")) != runtime / "bin" / "actanara":
        raise LinuxInstallError("fresh install recovery Runtime CLI snapshot is invalid")
    for key, backup_name in (("location", "location"), ("runtimeCli", "runtime-cli")):
        record = snapshots[key]
        if set(record) != {"path", "existed", "backup", "mode", "beforeSha256"}:
            raise LinuxInstallError("fresh install recovery snapshot schema is invalid")
        before_hash = record.get("beforeSha256")
        if before_hash != FRESH_MISSING_HASH and not (
            isinstance(before_hash, str) and re.fullmatch(r"[0-9a-f]{64}", before_hash)
        ):
            raise LinuxInstallError("fresh install recovery snapshot hash is invalid")
        if record.get("existed") is True:
            if (
                record.get("backup") != f"backups/{backup_name}"
                or not isinstance(record.get("mode"), int)
                or before_hash == FRESH_MISSING_HASH
            ):
                raise LinuxInstallError("fresh install recovery snapshot binding is invalid")
            backup = staging / str(record["backup"])
            try:
                backup_metadata = backup.stat(follow_symlinks=False)
            except OSError as exc:
                raise LinuxInstallError(
                    "fresh install recovery snapshot backup is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(backup_metadata.st_mode)
                or backup_metadata.st_uid != os.getuid()
                or backup_metadata.st_nlink != 1
                or hashlib.sha256(backup.read_bytes()).hexdigest() != before_hash
            ):
                raise LinuxInstallError("fresh install recovery snapshot backup changed")
        elif (
            record.get("existed") is not False
            or record.get("backup") is not None
            or record.get("mode") is not None
            or before_hash != FRESH_MISSING_HASH
        ):
            raise LinuxInstallError("fresh install recovery missing snapshot is invalid")
    mutable_paths = _fresh_mutable_paths(runtime, location)
    mutable_records = journal.get("managedMutableHashes")
    if (
        not isinstance(mutable_records, dict)
        or set(mutable_records) != set(mutable_paths)
    ):
        raise LinuxInstallError("fresh install recovery mutable inventory is invalid")
    for key, expected_path in mutable_paths.items():
        record = mutable_records.get(key)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "beforeSha256", "afterSha256"}
            or Path(str(record.get("path") or "")) != expected_path
        ):
            raise LinuxInstallError("fresh install recovery mutable binding is invalid")
        for hash_key in ("beforeSha256", "afterSha256"):
            value = record.get(hash_key)
            if value is None and hash_key == "afterSha256":
                continue
            if value != FRESH_MISSING_HASH and not (
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise LinuxInstallError("fresh install recovery mutable hash is invalid")
    if (
        mutable_records["location"]["beforeSha256"]
        != snapshots["location"]["beforeSha256"]
        or mutable_records["runtimeCli"]["beforeSha256"]
        != snapshots["runtimeCli"]["beforeSha256"]
        or any(
            mutable_records[key]["beforeSha256"] != FRESH_MISSING_HASH
            for key in (
                "settings",
                "runtimeManifest",
                "database",
                "databaseWal",
                "databaseShm",
            )
        )
    ):
        raise LinuxInstallError("fresh install recovery mutable preimage is invalid")
    identity_contracts = (
        (
            "sourcePointerIdentity",
            runtime / "app" / "source",
            {"path", "device", "inode", "rawTarget"},
        ),
        (
            "venvPointerIdentity",
            runtime / ".venv",
            {"path", "device", "inode", "rawTarget"},
        ),
        (
            "releaseIdentity",
            release_target,
            {"path", "device", "inode", "marker", "markerSha256"},
        ),
        (
            "venvIdentity",
            venv_target,
            {"path", "device", "inode", "marker", "markerSha256"},
        ),
        (
            "databaseIdentity",
            runtime / "data" / "actanara_data.sqlite3",
            {"path", "device", "inode"},
        ),
        (
            "userShimCreatedIdentity",
            Path.home() / ".local" / "bin" / "actanara",
            {"path", "device", "inode", "rawTarget"},
        ),
    )
    for key, expected_path, fields in identity_contracts:
        identity = journal.get(key)
        if identity is None:
            continue
        if (
            not isinstance(identity, dict)
            or set(identity) != fields
            or Path(str(identity.get("path") or "")) != expected_path
            or not isinstance(identity.get("device"), int)
            or not isinstance(identity.get("inode"), int)
        ):
            raise LinuxInstallError("fresh install recovery resource identity is invalid")
    if journal.get("sourcePointerIdentity") is not None and (
        journal["sourcePointerIdentity"].get("rawTarget")
        != str(Path("releases") / release_target.name)
    ):
        raise LinuxInstallError("fresh install recovery source pointer identity is invalid")
    if journal.get("venvPointerIdentity") is not None and (
        journal["venvPointerIdentity"].get("rawTarget")
        != str(Path("app") / "venvs" / venv_target.name)
    ):
        raise LinuxInstallError("fresh install recovery venv pointer identity is invalid")
    if journal.get("releaseIdentity") is not None and (
        journal["releaseIdentity"].get("marker") != ".actanara-runtime-source.json"
    ):
        raise LinuxInstallError("fresh install recovery release identity is invalid")
    if journal.get("venvIdentity") is not None and (
        journal["venvIdentity"].get("marker") != dependency_contract.MARKER_NAME
    ):
        raise LinuxInstallError("fresh install recovery venv identity is invalid")
    return journal


def _quiesce_fresh_database_worker(journal: dict) -> None:
    worker = journal.get("databaseWorker")
    if not isinstance(worker, dict):
        return
    from install.update_transaction import _same_process

    pid = int(worker["pid"])
    identity = worker["processIdentity"]
    if not _same_process(pid, identity):
        return
    try:
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return
    if process_group != int(worker["processGroup"]) or process_group != pid:
        raise LinuxInstallError(
            "fresh database migration worker process group changed"
        )
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while _same_process(pid, identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _same_process(pid, identity):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while _same_process(pid, identity) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _same_process(pid, identity):
        raise LinuxInstallError(
            "fresh database migration worker could not be terminated"
        )


def _rollback_fresh_install(staging: Path, journal: dict) -> None:
    runtime = Path(str(journal["runtime"]))
    transaction_id = str(journal["transactionId"])
    _quiesce_fresh_database_worker(journal)
    # Validate every locally managed resource before the first compensating
    # mutation.  A late conflict therefore preserves the complete transaction
    # for operator review instead of producing a half-rollback.
    _validate_fresh_rollback_cas(staging, journal)
    _recover_fresh_settings_transactions(runtime)
    _rollback_fresh_service_transaction(
        runtime,
        str(journal.get("serviceTransactionId") or "") or None,
        owner_id=transaction_id,
    )
    database_record = journal["managedMutableHashes"]["database"]
    database_path = Path(str(database_record["path"]))
    database_identity = journal.get("databaseIdentity")
    try:
        database_metadata = database_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        database_metadata = None
    if database_metadata is not None and (
        not stat.S_ISREG(database_metadata.st_mode)
        or database_metadata.st_uid != os.getuid()
        or database_metadata.st_nlink != 1
        or (
            isinstance(database_identity, dict)
            and (
                database_metadata.st_dev != database_identity.get("device")
                or database_metadata.st_ino != database_identity.get("inode")
            )
        )
        or (
            database_identity is None
            and journal.get("databaseMutationArmed") is not True
        )
    ):
        raise LinuxInstallError(
            "fresh install recovery found a concurrent database replacement"
        )
    _remove_fresh_pointer(
        runtime / "app" / "source",
        Path("releases") / Path(journal["releaseTarget"]).name,
        journal.get("sourcePointerIdentity"),
    )
    _remove_fresh_pointer(
        runtime / ".venv",
        Path("app") / "venvs" / Path(journal["venvTarget"]).name,
        journal.get("venvPointerIdentity"),
    )
    _remove_fresh_generation(
        Path(journal["releaseTarget"]),
        parent=runtime / "app" / "releases",
        transaction_id=transaction_id,
        identity=journal.get("releaseIdentity"),
    )
    _remove_fresh_generation(
        Path(journal["venvTarget"]),
        parent=runtime / "app" / "venvs",
        transaction_id=transaction_id,
        identity=journal.get("venvIdentity"),
    )
    for key in (
        "settings",
        "runtimeManifest",
        "database",
        "databaseWal",
        "databaseShm",
    ):
        mutable_record = journal["managedMutableHashes"][key]
        mutable = Path(str(mutable_record["path"]))
        if mutable.is_symlink() or (mutable.exists() and not mutable.is_file()):
            raise LinuxInstallError(f"fresh install recovery found an unsafe mutable file: {mutable}")
        current_hash = _fresh_file_hash(mutable)
        allowed_hashes = {
            FRESH_MISSING_HASH,
            str(mutable_record.get("beforeSha256") or ""),
            str(mutable_record.get("afterSha256") or ""),
        }
        if current_hash not in allowed_hashes and key not in {
            "database",
            "databaseWal",
            "databaseShm",
        }:
            raise LinuxInstallError(
                f"fresh install recovery found a concurrent mutable-file change: {mutable}"
            )
        if key == "database" and current_hash not in allowed_hashes:
            identity = journal.get("databaseIdentity")
            metadata = mutable.stat(follow_symlinks=False)
            if (
                (
                    isinstance(identity, dict)
                    and (
                        metadata.st_dev != identity.get("device")
                        or metadata.st_ino != identity.get("inode")
                    )
                )
                or (
                    identity is None
                    and journal.get("databaseMutationArmed") is not True
                )
            ):
                raise LinuxInstallError(
                    f"fresh install recovery found a concurrent database replacement: {mutable}"
                )
        if key in {"databaseWal", "databaseShm"} and current_hash != FRESH_MISSING_HASH:
            metadata = mutable.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise LinuxInstallError(
                    f"fresh install recovery found an unsafe SQLite sidecar: {mutable}"
                )
        mutable.unlink(missing_ok=True)
    snapshots = journal["snapshots"]
    location = Path(str(snapshots["location"]["path"]))
    _restore_fresh_file_snapshot(
        staging,
        snapshots["location"],
        expected_path=location,
        snapshot_key="location",
        after_sha256=str(journal["managedMutableHashes"]["location"].get("afterSha256") or "") or None,
    )
    _restore_fresh_file_snapshot(
        staging,
        snapshots["runtimeCli"],
        expected_path=runtime / "bin" / "actanara",
        snapshot_key="runtime-cli",
        after_sha256=str(journal["managedMutableHashes"]["runtimeCli"].get("afterSha256") or "") or None,
    )
    user_shim = Path.home() / ".local" / "bin" / "actanara"
    user_shim_staging = Path(str(journal["userShimStagingPath"]))
    shim_identity = journal.get("userShimCreatedIdentity")
    if journal.get("userShimExisted") is False:
        for path in (user_shim, user_shim_staging):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or Path(os.readlink(path)) != runtime / "bin" / "actanara"
                or (
                    isinstance(shim_identity, dict)
                    and (
                        metadata.st_dev != shim_identity.get("device")
                        or metadata.st_ino != shim_identity.get("inode")
                    )
                )
                or (path == user_shim and not isinstance(shim_identity, dict))
            ):
                raise LinuxInstallError(
                    "fresh install recovery found a changed user CLI shim"
                )
            path.unlink()


def _recover_fresh_install(runtime: Path) -> list[dict[str, str]]:
    from data_foundation.runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        runtime_mutation_guard,
    )

    runtime = runtime.expanduser().absolute()
    staging_root = runtime / "app" / FRESH_INSTALL_STAGING_NAME
    durable_lock = _fresh_install_lock_path(runtime)
    if not (
        staging_root.exists()
        or staging_root.is_symlink()
        or durable_lock.exists()
        or durable_lock.is_symlink()
    ):
        return []
    try:
        with runtime_mutation_guard(runtime, blocking=True):
            return _recover_fresh_install_locked(runtime)
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise LinuxInstallError(
            f"fresh install recovery could not lock the Runtime: {exc}"
        ) from exc


def _recover_fresh_install_locked(runtime: Path) -> list[dict[str, str]]:
    from install.update_transaction import _same_process

    root = runtime / "app" / FRESH_INSTALL_STAGING_NAME
    lock = _fresh_install_lock_path(runtime)
    lock_payload: dict | None = None
    lock_staging: Path | None = None
    if lock.exists() or lock.is_symlink():
        try:
            metadata = lock.stat(follow_symlinks=False)
            lock_payload = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinuxInstallError("Runtime mutation lock is unreadable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or not isinstance(lock_payload, dict)
            or set(lock_payload)
            != {"txId", "journal", "ownerPid", "ownerProcessIdentity"}
            or not isinstance(lock_payload.get("ownerPid"), int)
            or not isinstance(lock_payload.get("ownerProcessIdentity"), str)
            or not FRESH_TRANSACTION_ID_RE.fullmatch(str(lock_payload.get("txId") or ""))
        ):
            raise LinuxInstallError("Runtime mutation lock is unsafe")
        lock_staging = root / str(lock_payload["txId"])
        if Path(str(lock_payload.get("journal") or "")) != lock_staging / FRESH_INSTALL_JOURNAL_NAME:
            raise LinuxInstallError(
                "another Runtime update transaction is active or requires its own recovery"
            )
        owner = _fresh_install_owner_path(lock_staging)
        try:
            owner_metadata = owner.stat(follow_symlinks=False)
            owner_payload = json.loads(owner.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinuxInstallError("fresh install lock owner record is unavailable") from exc
        if (
            owner_payload != lock_payload
            or metadata.st_dev != owner_metadata.st_dev
            or metadata.st_ino != owner_metadata.st_ino
        ):
            raise LinuxInstallError("fresh install lock owner binding changed")
        if _same_process(
            int(lock_payload["ownerPid"]),
            lock_payload["ownerProcessIdentity"],
        ):
            raise LinuxInstallError("another fresh install process is still active")
    if not root.exists():
        if lock_payload is not None:
            raise LinuxInstallError("stale fresh install lock has no recoverable staging directory")
        return []
    if root.is_symlink() or not root.is_dir():
        raise LinuxInstallError("fresh install staging root is unsafe")
    candidates: list[tuple[Path, dict]] = []
    for staging in sorted(root.iterdir()):
        if staging.is_symlink() or not staging.is_dir() or not FRESH_TRANSACTION_ID_RE.fullmatch(staging.name):
            raise LinuxInstallError("fresh install staging contains an unknown entry")
        journal_path = staging / FRESH_INSTALL_JOURNAL_NAME
        if (
            staging == lock_staging
            and not journal_path.exists()
            and not journal_path.is_symlink()
        ):
            continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinuxInstallError("fresh install recovery journal is unreadable") from exc
        if not isinstance(journal, dict):
            raise LinuxInstallError("fresh install recovery journal is invalid")
        _validated_fresh_install_journal(runtime, staging, journal)
        if staging == lock_staging and (
            journal.get("ownerPid") != lock_payload.get("ownerPid")
            or journal.get("ownerProcessIdentity")
            != lock_payload.get("ownerProcessIdentity")
        ):
            raise LinuxInstallError("fresh install journal owner binding changed")
        if _same_process(
            int(journal["ownerPid"]),
            journal["ownerProcessIdentity"],
        ):
            raise LinuxInstallError("another fresh install process is still active")
        candidates.append((staging, journal))

    recovered: list[dict[str, str]] = []
    for staging, journal in candidates:
        outcome = (
            "committed"
            if journal.get("status") == "committed"
            else "rolled-back"
        )
        if journal.get("status") == "committed":
            if not _fresh_install_committed_postcondition(runtime, staging, journal):
                raise LinuxInstallError(
                    "committed fresh install postcondition is incomplete; preserved staging requires review"
                )
        else:
            _rollback_fresh_install(staging, journal)
        if lock_payload is not None and staging == lock_staging:
            _release_fresh_install_lock(runtime, staging, lock_payload)
            lock_payload = None
        shutil.rmtree(staging)
        recovered.append(
            {"transactionId": staging.name, "outcome": outcome}
        )
    if lock_payload is not None:
        # A crash can occur after the owner hard-link is acquired but before
        # the first full journal replace.  No Runtime resource is mutated in
        # that interval, so only that exact private staging directory is safe
        # to discard.
        if lock_staging is None or not lock_staging.is_dir() or lock_staging.is_symlink():
            raise LinuxInstallError("stale fresh install lock has no safe staging directory")
        unknown = {path.name for path in lock_staging.iterdir()} - {"owner.json"}
        if unknown:
            raise LinuxInstallError("stale fresh install staging has no recoverable journal")
        _release_fresh_install_lock(runtime, lock_staging, lock_payload)
        shutil.rmtree(lock_staging)
        recovered.append(
            {
                "transactionId": lock_staging.name,
                "outcome": "rolled-back",
            }
        )
    try:
        root.rmdir()
    except OSError:
        pass
    return recovered


def _relocate_candidate_venv(candidate: Path, target: Path) -> None:
    """Rewrite venv text launchers before the candidate directory is renamed."""

    old = os.fsencode(str(candidate))
    new = os.fsencode(str(target))
    bin_directory = candidate / "bin"
    if not bin_directory.is_dir():
        raise LinuxInstallError("candidate venv has no executable directory")
    for path in bin_directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        content = path.read_bytes()
        if old not in content:
            continue
        path.write_bytes(content.replace(old, new))


def _promote_fresh_generation(candidate: Path, target: Path) -> None:
    from install.update_transaction import TransactionError, _fsync_dir, _rename_exclusive

    try:
        _rename_exclusive(candidate, target)
        _fsync_dir(candidate.parent)
        if target.parent != candidate.parent:
            _fsync_dir(target.parent)
    except TransactionError as exc:
        raise LinuxInstallError(f"fresh generation promotion failed: {exc}") from exc


def _install(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
    args: argparse.Namespace,
    *,
    linger: dict | None = None,
) -> dict:
    if plan.dry_run:
        return _install_guarded(plan, selection, args, linger=linger)
    # Keep every no-write eligibility check ahead of a guard whose creation
    # would otherwise materialize Runtime/app for a rejected fresh install.
    _base_release_id, source_commit = _source_identity(plan.source_root)
    if plan.rag_enabled and source_commit is None:
        raise LinuxInstallError(
            "managed RAG fresh install requires a clean source tree with an exact Git commit identity; "
            "no Runtime changes were made"
        )
    from data_foundation.runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        runtime_mutation_guard,
    )

    try:
        with runtime_mutation_guard(plan.runtime, blocking=True):
            return _install_guarded(plan, selection, args, linger=linger)
    except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
        raise LinuxInstallError(str(exc)) from exc


def _install_guarded(
    plan: InstallPlan,
    selection: dependency_contract.ContractSelection,
    args: argparse.Namespace,
    *,
    linger: dict | None = None,
) -> dict:
    base_release_id, commit = _source_identity(plan.source_root)
    if plan.rag_enabled and commit is None:
        raise LinuxInstallError(
            "managed RAG fresh install requires a clean source tree with an exact Git commit identity; "
            "no Runtime changes were made"
        )
    transaction_id = _fresh_install_transaction_id()
    generation_id = f"{base_release_id}-{transaction_id}"
    release_target = plan.runtime / "app" / "releases" / generation_id
    venv_target = plan.runtime / "app" / "venvs" / generation_id
    cache_root = plan.runtime / "app" / "dependency-cache" / "v1"
    if plan.dry_run:
        return {
            "schemaVersion": 1,
            "status": "planned",
            "platform": "linux",
            "architecture": selection.lock_environment["architecture"],
            "environmentId": selection.environment_id,
            "runtime": str(plan.runtime),
            "sourceRoot": str(plan.source_root),
            "profiles": list(plan.profiles),
            "releaseTarget": str(release_target),
            "venvTarget": str(venv_target),
            "schedulerProvider": "systemd",
            "linger": linger,
            "writes": False,
        }

    previous_umask = os.umask(0o077)
    staging = plan.runtime / "app" / FRESH_INSTALL_STAGING_NAME / transaction_id
    journal: dict | None = None
    lock_payload: dict | None = None
    database: Path | None = None
    systemd_result: dict | None = None
    committed = False
    try:
        for directory in (
            plan.runtime,
            plan.runtime / "app",
            plan.runtime / "app" / "releases",
            plan.runtime / "app" / "venvs",
            plan.runtime / "bin",
            plan.runtime / "state" / "logs",
            staging,
        ):
            _secure_directory(directory)
        from data_foundation.runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(plan.runtime, blocking=True):
                lock_payload = _acquire_fresh_install_lock(
                    plan.runtime,
                    staging,
                    transaction_id,
                )
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise LinuxInstallError(str(exc)) from exc
        # Fresh preflight ran before lock acquisition. Recheck every publish
        # marker while holding the Runtime-wide mutation lock so two installers
        # cannot both act on the same empty observation.
        fresh_markers = (
            plan.runtime / "app" / "source",
            plan.runtime / ".venv",
            plan.runtime / "config" / "settings.json",
            plan.runtime / "config" / "runtime.json",
            plan.runtime / "data" / "actanara_data.sqlite3",
            plan.runtime / "data" / "actanara_data.sqlite3-wal",
            plan.runtime / "data" / "actanara_data.sqlite3-shm",
        )
        if any(path.exists() or path.is_symlink() for path in fresh_markers):
            raise LinuxInstallError(
                "Runtime state changed while the fresh install lock was acquired"
            )
        location = Path(
            os.environ.get(
                "ACTANARA_LOCATION_FILE",
                str(Path.home() / ".config" / "actanara" / "location.json"),
            )
        ).expanduser().absolute()
        snapshots = {
            "location": _capture_fresh_file_snapshot(staging, "location", location),
            "runtimeCli": _capture_fresh_file_snapshot(
                staging,
                "runtime-cli",
                plan.runtime / "bin" / "actanara",
            ),
        }
        mutable_paths = _fresh_mutable_paths(plan.runtime, location)
        journal = {
            "schemaVersion": FRESH_INSTALL_SCHEMA_VERSION,
            "product": "actanara",
            "transactionId": transaction_id,
            "runtime": str(plan.runtime),
            "stagingRoot": str(staging),
            "status": "active",
            "phase": "prepared",
            "createdAt": datetime.now().astimezone().isoformat(),
            "updatedAt": datetime.now().astimezone().isoformat(),
            "releaseTarget": str(release_target),
            "venvTarget": str(venv_target),
            "sourcePointer": str(plan.runtime / "app" / "source"),
            "venvPointer": str(plan.runtime / ".venv"),
            "serviceTransactionId": None,
            "configurationSettingsTransactionId": None,
            "serviceSettingsTransactionId": None,
            "databaseMutationArmed": False,
            "databaseWorker": None,
            "ownerPid": lock_payload["ownerPid"],
            "ownerProcessIdentity": lock_payload["ownerProcessIdentity"],
            "userShimExisted": bool(
                (Path.home() / ".local" / "bin" / "actanara").exists()
                or (Path.home() / ".local" / "bin" / "actanara").is_symlink()
            ),
            "userShimStagingPath": str(
                _fresh_user_shim_staging_path(transaction_id)
            ),
            "snapshots": snapshots,
            "managedMutableHashes": {
                key: {
                    "path": str(path),
                    "beforeSha256": (
                        snapshots[key]["beforeSha256"]
                        if key in {"location", "runtimeCli"}
                        else FRESH_MISSING_HASH
                    ),
                    "afterSha256": None,
                }
                for key, path in mutable_paths.items()
            },
            "sourcePointerIdentity": None,
            "venvPointerIdentity": None,
            "releaseIdentity": None,
            "venvIdentity": None,
            "databaseIdentity": None,
            "userShimCreatedIdentity": None,
        }
        _write_fresh_install_journal(staging, journal)
        dependency_log = plan.runtime / "state" / "logs" / f"dependencies-{transaction_id}.log"
        dependency_contract.materialize_dependency_cache(
            cache_root,
            selection,
            python=plan.python,
            offline=plan.offline,
            timeout=900,
            diagnostic_log=dependency_log,
        )
        _advance_fresh_install_journal(staging, journal, "cache-ready")
        fresh_install_checkpoint("cache-ready", transaction_id)
        candidate_venv = staging / "venv"
        venv_python = _seed_venv_pip(plan, candidate_venv)
        _advance_fresh_install_journal(staging, journal, "venv-bootstrap-ready")
        fresh_install_checkpoint("venv-bootstrap-ready", transaction_id)
        dependency_contract.install_locked_dependencies(
            cache_root,
            selection,
            venv_python=venv_python,
            timeout=900,
            diagnostic_log=dependency_log,
        )
        dependency_contract.write_dependency_marker(candidate_venv, selection)
        dependency_contract.verify_dependency_marker(candidate_venv, selection)
        _advance_fresh_install_journal(staging, journal, "dependencies-ready")
        fresh_install_checkpoint("dependencies-ready", transaction_id)
        candidate_release = staging / "release"
        _stage_source(
            plan,
            candidate_release,
            commit,
            manifest_release_id=generation_id,
        )
        _advance_fresh_install_journal(staging, journal, "source-staged")
        fresh_install_checkpoint("source-staged", transaction_id)
        _relocate_candidate_venv(candidate_venv, venv_target)
        release_identity = _fresh_generation_identity(
            candidate_release,
            marker_name=".actanara-runtime-source.json",
            expected_path=release_target,
        )
        _advance_fresh_install_journal(
            staging,
            journal,
            "release-promotion-armed",
            releaseIdentity=release_identity,
        )
        fresh_install_checkpoint("release-promotion-armed", transaction_id)
        _promote_fresh_generation(candidate_release, release_target)
        _advance_fresh_install_journal(
            staging,
            journal,
            "release-promoted",
        )
        fresh_install_checkpoint("release-promoted", transaction_id)
        venv_identity = _fresh_generation_identity(
            candidate_venv,
            marker_name=dependency_contract.MARKER_NAME,
            expected_path=venv_target,
        )
        _advance_fresh_install_journal(
            staging,
            journal,
            "venv-promotion-armed",
            venvIdentity=venv_identity,
        )
        fresh_install_checkpoint("venv-promotion-armed", transaction_id)
        _promote_fresh_generation(candidate_venv, venv_target)
        _advance_fresh_install_journal(
            staging,
            journal,
            "venv-promoted",
        )
        fresh_install_checkpoint("venv-promoted", transaction_id)
        source_pointer = plan.runtime / "app" / "source"
        venv_pointer = plan.runtime / ".venv"
        source_pointer_candidate = staging / ".source-pointer.next"
        venv_pointer_candidate = staging / ".venv-pointer.next"
        source_pointer_candidate.symlink_to(Path("releases") / generation_id)
        venv_pointer_candidate.symlink_to(Path("app") / "venvs" / generation_id)
        _advance_fresh_install_journal(
            staging,
            journal,
            "pointers-promotion-armed",
            sourcePointerIdentity=_fresh_pointer_identity(
                source_pointer_candidate,
                expected_path=source_pointer,
            ),
            venvPointerIdentity=_fresh_pointer_identity(
                venv_pointer_candidate,
                expected_path=venv_pointer,
            ),
        )
        fresh_install_checkpoint("pointers-promotion-armed", transaction_id)
        _promote_fresh_generation(source_pointer_candidate, source_pointer)
        fresh_install_checkpoint("source-pointer-promoted", transaction_id)
        _promote_fresh_generation(venv_pointer_candidate, venv_pointer)
        _advance_fresh_install_journal(
            staging,
            journal,
            "pointers-published",
        )
        fresh_install_checkpoint("pointers-published", transaction_id)
        cli_content = _cli_shim_content(plan.runtime).encode("utf-8")
        _arm_fresh_mutable_hash(
            staging,
            journal,
            "runtimeCli",
            cli_content,
            "runtime-cli-write-armed",
        )
        fresh_install_checkpoint("runtime-cli-write-armed", transaction_id)
        _write_cli_shim(plan.runtime, staging=staging)
        _record_fresh_mutable_hashes(
            staging,
            journal,
            ("runtimeCli",),
            "runtime-cli-written",
        )
        _configure_runtime(
            plan,
            staging=staging,
            journal=journal,
            transaction_id=transaction_id,
        )
        _record_fresh_mutable_hashes(
            staging,
            journal,
            ("location", "settings", "runtimeManifest"),
            "runtime-configured",
        )
        fresh_install_checkpoint("runtime-configured", transaction_id)
        _advance_fresh_install_journal(
            staging,
            journal,
            "database-migration-armed",
            databaseMutationArmed=True,
        )
        fresh_install_checkpoint("database-migration-armed", transaction_id)

        def record_database_worker(worker: dict[str, object]) -> None:
            _advance_fresh_install_journal(
                staging,
                journal,
                "database-migration-running",
                databaseWorker=worker,
            )
            fresh_install_checkpoint("database-migration-running", transaction_id)

        database = _initialize_database(
            plan,
            worker_started=record_database_worker,
        )
        _record_fresh_mutable_hashes(
            staging,
            journal,
            ("database", "databaseWal", "databaseShm"),
            "database-ready",
            databaseIdentity=_fresh_database_identity(database),
            databaseWorker=None,
        )
        fresh_install_checkpoint("database-ready", transaction_id)
        def record_service_transaction(service_transaction_id: str) -> None:
            _advance_fresh_install_journal(
                staging,
                journal,
                "service-transaction-started",
                serviceTransactionId=service_transaction_id,
            )

        def record_service_settings_transaction(context: dict[str, str]) -> None:
            records = journal.get("managedMutableHashes")
            if not isinstance(records, dict):
                raise LinuxInstallError("fresh install mutable journal is unavailable")
            next_records = {key: dict(value) for key, value in records.items()}
            next_records["settings"]["afterSha256"] = context["settingsAfterHash"]
            next_records["runtimeManifest"]["afterSha256"] = context[
                "runtimeManifestAfterHash"
            ]
            _advance_fresh_install_journal(
                staging,
                journal,
                "service-settings-armed",
                managedMutableHashes=next_records,
                serviceSettingsTransactionId=context["id"],
            )
            fresh_install_checkpoint("service-settings-armed", transaction_id)

        systemd_result = _install_systemd_user_services(
            plan,
            expected_source_commit=commit,
            transaction_started=record_service_transaction,
            settings_transaction_started=record_service_settings_transaction,
            transaction_owner_id=transaction_id,
        )
        _record_fresh_mutable_hashes(
            staging,
            journal,
            (
                "settings",
                "runtimeManifest",
                "database",
                "databaseWal",
                "databaseShm",
            ),
            "services-ready",
            serviceTransactionId=(
                str(systemd_result.get("transactionId"))
                if isinstance(systemd_result, dict) and systemd_result.get("transactionId")
                else None
            ),
        )
        fresh_install_checkpoint("services-ready", transaction_id)
        if not args.no_shell_path:
            user_bin = Path.home() / ".local" / "bin"
            _secure_directory(user_bin)
            user_shim = user_bin / "actanara"
            if not user_shim.exists() and not user_shim.is_symlink():
                user_shim_staging = _fresh_user_shim_staging_path(transaction_id)
                user_shim_staging.symlink_to(plan.runtime / "bin" / "actanara")
                _advance_fresh_install_journal(
                    staging,
                    journal,
                    "user-shim-promotion-armed",
                    userShimCreatedIdentity=_fresh_pointer_identity(
                        user_shim_staging,
                        expected_path=user_shim,
                    ),
                )
                fresh_install_checkpoint("user-shim-promotion-armed", transaction_id)
                _promote_fresh_generation(user_shim_staging, user_shim)
                _advance_fresh_install_journal(
                    staging,
                    journal,
                    "user-shim-created",
                )
        _advance_fresh_install_journal(
            staging,
            journal,
            "committed",
            status="committed",
        )
        committed = True
    except Exception as exc:
        if journal is not None and _fresh_install_committed_postcondition(
            plan.runtime,
            staging,
            journal,
        ):
            # The durable commit is authoritative across the final ACK window.
            # Continue through normal lock/staging cleanup and report success.
            committed = True
        else:
            rollback_error: Exception | None = None
            if journal is not None:
                try:
                    _validated_fresh_install_journal(plan.runtime, staging, journal)
                    _rollback_fresh_install(staging, journal)
                except Exception as recovery_exc:
                    rollback_error = recovery_exc
            if rollback_error is None and lock_payload is not None:
                try:
                    _release_fresh_install_lock(plan.runtime, staging, lock_payload)
                    lock_payload = None
                except Exception as recovery_exc:
                    rollback_error = recovery_exc
            if rollback_error is None and staging.is_dir() and not staging.is_symlink():
                try:
                    shutil.rmtree(staging, ignore_errors=False)
                    _prune_fresh_staging_root(staging.parent)
                except OSError as recovery_exc:
                    rollback_error = recovery_exc
            if rollback_error is not None:
                raise LinuxInstallError(
                    f"fresh install failed and recovery is incomplete: {rollback_error}",
                    rollback_complete=False,
                    state_certain=False,
                    stage="fresh-install-recovery-incomplete",
                ) from exc
            if isinstance(exc, LinuxInstallError):
                raise
            if isinstance(exc, dependency_contract.ContractError):
                raise LinuxInstallError(f"dependency contract failed: {exc.message}") from exc
            raise LinuxInstallError(str(exc)) from exc
    finally:
        os.umask(previous_umask)
    if committed:
        try:
            if lock_payload is not None:
                _release_fresh_install_lock(plan.runtime, staging, lock_payload)
                lock_payload = None
            shutil.rmtree(staging)
            _prune_fresh_staging_root(staging.parent)
        except (OSError, LinuxInstallError) as exc:
            raise LinuxInstallError(
                f"fresh install committed but transaction cleanup failed: {staging}",
                rollback_complete=None,
                state_certain=True,
                stage="fresh-install-commit-cleanup",
                source_updated=True,
                dependencies_installed=True,
                reuses_runtime_venv=False,
            ) from exc
    if database is None or systemd_result is None:
        raise LinuxInstallError("fresh install did not produce complete Runtime evidence")
    return {
        "schemaVersion": 1,
        "status": "installed",
        "platform": "linux",
        "architecture": selection.lock_environment["architecture"],
        "environmentId": selection.environment_id,
        "runtime": str(plan.runtime),
        "database": str(database),
        "databaseInitialized": True,
        "profiles": list(plan.profiles),
        "schedulerProvider": "systemd",
        "schedulerRegistration": (
            "registered" if plan.scheduler else "disabled"
        ),
        "dashboardServiceRegistration": (
            "registered" if plan.dashboard_service else "disabled"
        ),
        "linger": linger,
        "systemdUser": systemd_result,
    }


def _result_envelope(
    *,
    payload: dict | None,
    requested_mode: str,
    error: str | LinuxInstallError | None = None,
) -> dict:
    completed = payload is not None and error is None
    error_text = str(error) if error is not None else None
    rollback_complete = (
        error.rollback_complete if isinstance(error, LinuxInstallError) else None
    )
    state_certain = (
        error.state_certain if isinstance(error, LinuxInstallError) else None
    )
    error_stage = error.stage if isinstance(error, LinuxInstallError) else None
    error_source_updated = (
        error.source_updated if isinstance(error, LinuxInstallError) else None
    )
    error_dependencies_installed = (
        error.dependencies_installed if isinstance(error, LinuxInstallError) else None
    )
    error_reuses_runtime_venv = (
        error.reuses_runtime_venv if isinstance(error, LinuxInstallError) else None
    )
    error_services_stopped = (
        error.services_stopped if isinstance(error, LinuxInstallError) else None
    )
    status = str((payload or {}).get("status") or "")
    update_mode = str((payload or {}).get("updateMode") or requested_mode or "unknown")
    dependencies_installed = (
        error_dependencies_installed
        if error_dependencies_installed is not None
        else bool((payload or {}).get("dependenciesInstalled"))
    )
    reuses_runtime_venv = (
        error_reuses_runtime_venv
        if error_reuses_runtime_venv is not None
        else bool((payload or {}).get("reusesRuntimeVenv", False))
    )
    planned_dependencies = bool(
        (payload or {}).get("plannedDependenciesInstalled", dependencies_installed)
    )
    systemd = (payload or {}).get("systemdUser")
    services_stopped = (
        error_services_stopped
        if error_services_stopped is not None
        else bool(
            status in {"updated", "repaired"}
            and isinstance(systemd, dict)
            and systemd.get("units")
        )
    )
    return {
        "schemaVersion": 1,
        "status": "completed" if completed else "failed",
        "updateMode": update_mode,
        "dependenciesInstalled": dependencies_installed,
        "reusesRuntimeVenv": reuses_runtime_venv,
        "sourceUpdated": (
            status in {"updated", "repaired"}
            if completed
            else error_source_updated
            if error_source_updated is not None
            else False
            if rollback_complete is True
            else None
        ),
        "reason": str((payload or {}).get("reason") or error_text or status or "unknown"),
        "cacheUsed": dependencies_installed,
        "servicesStopped": services_stopped,
        "plannedDependenciesInstall": planned_dependencies,
        "managedServiceDefinitionsNormalized": (
            requested_mode == "repair" if completed else None
        ),
        "rollbackComplete": rollback_complete,
        "stateCertain": completed if state_certain is None else state_certain,
        "stage": error_stage
        or (
            "preflight"
            if status == "planned"
            else "complete"
            if completed
            else "installer"
        ),
    }


def _print_result_envelope(payload: dict) -> None:
    print(UPDATE_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _classify_existing_pending_repair_error(
    args: argparse.Namespace | None,
    requested_mode: str,
    error: LinuxInstallError,
) -> LinuxInstallError:
    if (
        args is None
        or requested_mode != "repair"
        or str(error.stage or "").startswith("repair-configuration-pending")
    ):
        return error
    postcondition = _repair_transaction_postcondition(
        Path(args.runtime).expanduser().absolute()
    )
    if postcondition is None:
        return error
    status = str(postcondition.get("status") or "")
    if status not in {
        "configuration-pending",
        "completion-cleanup-pending",
        "unsafe",
    }:
        return error
    return _pending_repair_error(
        error,
        state_certain=(status != "unsafe" and error.state_certain is not False),
        rollback_complete=error.rollback_complete,
        services_stopped=postcondition.get("servicesStopped"),
    )


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    requested_mode = "unknown"
    try:
        args = _parser().parse_args(argv)
        requested_mode = _requested_update_mode(args)
        requested_runtime = Path(args.runtime).expanduser().absolute()
        fresh_staging = (
            requested_runtime / "app" / FRESH_INSTALL_STAGING_NAME
        )
        fresh_lock = _fresh_install_lock_path(requested_runtime)
        recovery_pending = (
            fresh_staging.exists()
            or fresh_staging.is_symlink()
            or (
                requested_mode == "fresh"
                and (fresh_lock.exists() or fresh_lock.is_symlink())
            )
        )
        if recovery_pending and (
            args.dry_run or _env_flag("ACTANARA_INSTALL_DRY_RUN")
        ):
            raise LinuxInstallError(
                "an interrupted fresh install requires recovery; dry-run made no Runtime changes"
            )
        fresh_recovery: list[dict[str, str]] = []
        if fresh_staging.exists() or fresh_staging.is_symlink():
            # A partially published fresh Runtime can already satisfy the
            # public managed-runtime probe. Recover it before choosing the
            # fresh-vs-update path.
            fresh_recovery = _recover_fresh_install(requested_runtime)
        elif requested_mode == "fresh":
            # Also detect an unsafe/orphan lock without a staging root.
            fresh_recovery = _recover_fresh_install(requested_runtime)
        if (
            requested_mode == "fresh"
            and any(item.get("outcome") == "committed" for item in fresh_recovery)
        ):
            payload = {
                "schemaVersion": 1,
                "status": "installed",
                "platform": "linux",
                "runtime": str(requested_runtime),
                "recoveredFreshInstall": fresh_recovery,
                "writes": False,
            }
            if args.result_json:
                _print_result_envelope(
                    _result_envelope(
                        payload=payload,
                        requested_mode=requested_mode,
                    )
                )
            else:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if requested_mode != "fresh" and not (
            args.dry_run or _env_flag("ACTANARA_INSTALL_DRY_RUN")
        ):
            _recover_update_runtime(requested_runtime)
        plan = build_plan(args)
        selection = _validate_plan(plan, args)
        linger = _prepare_linger(plan)
        if plan.update_mode == "fresh":
            payload = _install(plan, selection, args, linger=linger)
        else:
            previous_umask = os.umask(0o077)
            try:
                payload = _update(plan, selection, args)
            finally:
                os.umask(previous_umask)
            payload["linger"] = linger
        if args.result_json:
            _print_result_envelope(
                _result_envelope(payload=payload, requested_mode=requested_mode)
            )
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except LinuxInstallError as exc:
        exc = _classify_existing_pending_repair_error(
            args,
            requested_mode,
            exc,
        )
        if args is not None and args.result_json:
            _print_result_envelope(
                _result_envelope(
                    payload=None,
                    requested_mode=requested_mode,
                    error=exc,
                )
            )
        print(
            json.dumps(
                {"schemaVersion": 1, "status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except dependency_contract.ContractError as exc:
        classified_error = _classify_existing_pending_repair_error(
            args,
            requested_mode,
            LinuxInstallError(f"dependency contract failed: {exc.message}"),
        )
        if args is not None and args.result_json:
            _print_result_envelope(
                _result_envelope(
                    payload=None,
                    requested_mode=requested_mode,
                    error=classified_error,
                )
            )
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "status": "error",
                    "error": str(classified_error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
