#!/usr/bin/env python3
"""Crash-recoverable state journal for guarded Actanara updates.

This helper intentionally handles only update-owned pointers, small runtime control
files, evidence-only database snapshots, and managed launchd jobs.  It never reads
Keychain items, stores launchctl output, or records environment values.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import http.client
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = 2
TERMINAL_STATUSES = {"committed", "rolled-back"}
RUNNING_STATES = {"running"}
STATE_RE = re.compile(r"^\s*state\s*=\s*([^\s]+)", re.MULTILINE)
SAFE_TX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SQLITE_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_BACKUP_TIMEOUT_SECONDS = 30.0
SQLITE_SNAPSHOT_POLICY = "online-backup-evidence-only-preserve-live"
SERVICE_HEALTH_TIMEOUT_SECONDS = 20.0
SERVICE_STATE_TIMEOUT_SECONDS = 30.0
CANDIDATE_CHILD_TERM_TIMEOUT_SECONDS = 3.0
MIGRATION_CONTRACT_RELATIVE_PATH = Path("src/data_foundation/migration_compatibility.json")
MIGRATIONS_RELATIVE_PATH = Path("src/data_foundation/migrations")
MIGRATION_POLICY = "rollback-compatible-additive-only"
PRE_COMMIT_WRITER_CONTRACT = "prior-reader-compatible-v1"
MIGRATION_CLASSES = {"rollback-compatible-additive", "breaking"}
SAFE_MIGRATION_VERSION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
ARTIFACT_MARKER_NAME = ".actanara-update-owner"
REPAIR_CONFIGURATION_PENDING_NAME = ".repair-configuration-pending"
SAFE_ARTIFACT_NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
FORBIDDEN_PAYLOAD_NAMES = {
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
    "location.json",
    "logs",
    "runtime.json",
    "reserved",
    "settings.json",
    "snapshots",
    "state",
    "tests",
    "tmp",
    "venv",
    "wheelhouse",
}
FORBIDDEN_PAYLOAD_SUFFIXES = (
    ".db",
    ".egg-info",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)
RUNTIME_SOURCE_FINAL_FIELDS = {
    "schemaVersion",
    "product",
    "sourceLocator",
    "deployedSourceLocator",
    "releaseLocator",
    "deploymentMode",
    "copiedAt",
    "pyprojectVersion",
    "git",
    "databaseCompatibility",
    "payload",
    "cleanScan",
}
DEPENDENCY_MARKER_NAME = ".actanara-dependencies.json"
DEPENDENCY_MARKER_MAX_BYTES = 16 * 1024 * 1024
DEPENDENCY_MARKER_FIELDS = {
    "schemaVersion",
    "product",
    "fingerprintAlgorithm",
    "dependencyFingerprint",
    "lockSha256",
    "environmentId",
    "lockEnvironment",
    "profiles",
    "directDependencies",
    "distributions",
}
DEPENDENCY_MARKER_ENVIRONMENT_FIELDS = {
    "implementation",
    "pythonMajorMinor",
    "abi",
    "platformFamily",
    "architecture",
    "minimumMacOS",
}
DEPENDENCY_MARKER_DIRECT_FIELDS = {"profile", "requirements"}
DEPENDENCY_MARKER_DISTRIBUTION_FIELDS = {"name", "version", "hashes"}
DEPENDENCY_FINGERPRINT_ALGORITHM = "actanara-runtime-dependencies-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEPENDENCY_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DEPENDENCY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
DEPENDENCY_PYTHON_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+$")
DEPENDENCY_MACOS_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
DEPENDENCY_DISTRIBUTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SYSTEMD_UNIT_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,126}\.(?:service|timer)$"
)
SYSTEMD_MANAGED_UNIT_HEADER = "# Managed by Actanara. Do not edit by hand."
SYSTEMD_ENABLED_STATES = {
    "enabled",
    "enabled-runtime",
    "linked",
    "linked-runtime",
    "alias",
    "static",
    "indirect",
    "generated",
    "transient",
    "disabled",
    "masked",
    "masked-runtime",
    "not-found",
}
SYSTEMD_ACTIVE_STATES = {
    "active",
    "reloading",
    "inactive",
    "failed",
    "activating",
    "deactivating",
    "maintenance",
    "not-found",
    "unknown",
}


class TransactionError(RuntimeError):
    pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _require_managed_directory(path: Path, runtime: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise TransactionError(f"managed Runtime directory is unsafe: {path.name}")
    if not _is_within(path, runtime):
        raise TransactionError(f"managed Runtime directory escaped the selected Runtime: {path.name}")


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_identity(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise TransactionError(f"managed directory is unavailable or unsafe: {path.name}")
    metadata = path.stat(follow_symlinks=False)
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _verify_bound_directories(state: dict[str, Any], expected: dict[str, Path]) -> None:
    identities = state.get("managedDirectoryIdentities")
    if state.get("legacySchemaVersion"):
        for path in expected.values():
            if path.is_symlink() or not path.is_dir():
                raise TransactionError("legacy update transaction directory boundary is unsafe")
        return
    if not isinstance(identities, dict) or set(identities) != set(expected):
        raise TransactionError("update transaction managed directory inventory is incomplete")
    for key, path in expected.items():
        record = identities.get(key)
        if not isinstance(record, dict) or Path(str(record.get("path") or "")) != path:
            raise TransactionError(f"update transaction managed directory binding is invalid: {key}")
        current = _directory_identity(path)
        if (
            current["device"] != record.get("device")
            or current["inode"] != record.get("inode")
        ):
            raise TransactionError(f"update transaction managed directory identity changed: {key}")


def _expected_bound_directories(runtime: Path, tx_id: str) -> dict[str, Path]:
    tx_root = runtime / "app" / "update-transactions"
    return {
        "runtime": runtime,
        "app": runtime / "app",
        "releases": runtime / "app" / "releases",
        "venvs": runtime / "app" / "venvs",
        "transactions": tx_root,
        "transaction": tx_root / tx_id,
        "config": runtime / "config",
        "data": runtime / "data",
    }


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _maybe_test_fail(phase: str) -> None:
    if os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        return
    requested_kill = os.environ.get("ACTANARA_INSTALL_TEST_KILL_PHASE", "")
    if requested_kill == phase:
        os.kill(os.getpid(), signal.SIGKILL)
    requested = os.environ.get("ACTANARA_INSTALL_TEST_FAIL_PHASE", "")
    if requested == phase:
        raise TransactionError(f"synthetic update helper failure at phase {phase}")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_with_platform_flags(
    source: Path,
    target: Path,
    *,
    darwin_flags: int,
    linux_flags: int,
) -> None:
    try:
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        at_fdcwd = -2
        if sys.platform == "darwin":
            rename = library.renameatx_np
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            flags = darwin_flags
        elif sys.platform.startswith("linux"):
            rename = library.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            flags = linux_flags
        else:
            raise AttributeError("atomic rename flags are unsupported")
        result = rename(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(target),
            flags,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise TransactionError("atomic guarded rename is unavailable") from exc
    raise OSError(
        error_number,
        os.strerror(error_number),
    )


def _rename_exclusive(source: Path, target: Path) -> None:
    """Atomically rename without ever replacing an existing target."""
    try:
        # Managed parent directories are separately dev/inode bound.  Use
        # RENAME_EXCL for no-clobber; RENAME_NOFOLLOW_ANY would also reject
        # harmless canonical aliases such as macOS /var -> /private/var.
        _rename_with_platform_flags(
            source,
            target,
            darwin_flags=0x00000004,  # RENAME_EXCL
            linux_flags=0x00000001,  # RENAME_NOREPLACE
        )
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise TransactionError(
                "atomic no-clobber rename refused an occupied target"
            ) from exc
        raise TransactionError("atomic no-clobber rename failed") from exc


def _rename_swap(source: Path, target: Path) -> None:
    """Atomically exchange two paths without deleting either object."""
    try:
        _rename_with_platform_flags(
            source,
            target,
            darwin_flags=0x00000002,  # RENAME_SWAP
            linux_flags=0x00000002,  # RENAME_EXCHANGE
        )
    except OSError as exc:
        raise TransactionError("atomic pointer swap failed") from exc


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _repair_configuration_pending_path(state: dict[str, Any]) -> Path:
    runtime = Path(str(state.get("runtime") or ""))
    expected = runtime / "app" / REPAIR_CONFIGURATION_PENDING_NAME
    configured = state.get("repairConfigurationPending")
    if isinstance(configured, dict) and configured.get("path") is not None:
        if Path(str(configured["path"])) != expected:
            raise TransactionError("repair configuration marker escaped the managed Runtime")
    return expected


def _repair_configuration_pending_payload(tx_id: str) -> bytes:
    if not SAFE_TX_ID_RE.fullmatch(tx_id) or tx_id in {".", ".."}:
        raise TransactionError("repair configuration marker transaction id is invalid")
    return f"{tx_id}\n".encode("ascii")


def _read_repair_configuration_pending(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TransactionError("repair configuration marker cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 2
            or before.st_size > 129
        ):
            raise TransactionError("repair configuration marker identity is unsafe")
        raw = os.read(descriptor, 130)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or any(getattr(before, field) != getattr(after, field) for field in identity_fields)
        ):
            raise TransactionError("repair configuration marker changed while it was read")
        try:
            tx_id = raw.decode("ascii").removesuffix("\n")
        except UnicodeDecodeError as exc:
            raise TransactionError("repair configuration marker content is invalid") from exc
        if raw != _repair_configuration_pending_payload(tx_id):
            raise TransactionError("repair configuration marker content is invalid")
        return raw, before
    finally:
        os.close(descriptor)


def _systemd_config_home(home: Path) -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else home / ".config"
    if (
        not root.is_absolute()
        or os.path.normpath(str(root)) != str(root)
        or any(character in str(root) for character in "\0\r\n\t")
    ):
        raise TransactionError("XDG_CONFIG_HOME is unsafe for systemd user units")
    if root.is_symlink():
        raise TransactionError("XDG_CONFIG_HOME is unsafe for systemd user units")
    return root.resolve(strict=False)


def _systemd_unit_root(home: Path, config_home: Path | None = None) -> Path:
    return (config_home or _systemd_config_home(home)) / "systemd" / "user"


def _validate_systemd_journal_bindings(state: dict[str, Any]) -> None:
    """Keep every Linux service target inside the selected user's unit root."""

    units = state.get("systemdUnits", [])
    if state.get("platform") != "Linux":
        if units:
            raise TransactionError("non-Linux update journal contains systemd units")
        return
    home = Path(str(state.get("home") or ""))
    configured_root = state.get("systemdConfigHome")
    config_home = (
        Path(str(configured_root))
        if configured_root is not None
        else home / ".config"
    )
    root = Path(str(state.get("systemdUnitRoot") or ""))
    if (
        not home.is_absolute()
        or not config_home.is_absolute()
        or os.path.normpath(str(config_home)) != str(config_home)
        or root != _systemd_unit_root(home, config_home)
        or not _is_within(root, config_home)
    ):
        raise TransactionError("systemd user-unit root escaped the selected XDG config root")
    if root.exists() and (
        root.is_symlink()
        or not root.is_dir()
        or root.resolve(strict=False) != root
    ):
        raise TransactionError("systemd user-unit root is unsafe")
    if not units and not state.get("systemctl") and state.get("systemctlIdentity") is None:
        # Backward-compatible helper-only transaction with no service surface.
        return
    binary = Path(str(state.get("systemctl") or ""))
    identity = state.get("systemctlIdentity")
    if (
        not binary.is_absolute()
        or not isinstance(identity, dict)
        or set(identity) != {"path", "device", "inode"}
        or Path(str(identity.get("path") or "")) != binary
    ):
        raise TransactionError("systemctl binding is invalid")
    try:
        metadata = binary.stat(follow_symlinks=False)
    except OSError as exc:
        raise TransactionError("bound systemctl executable is unavailable") from exc
    if (
        binary.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(binary, os.X_OK)
        or metadata.st_dev != identity.get("device")
        or metadata.st_ino != identity.get("inode")
    ):
        raise TransactionError("bound systemctl executable identity changed")
    if not isinstance(units, list):
        raise TransactionError("systemd unit inventory is invalid")
    records = state.get("files") or []
    seen: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise TransactionError("systemd unit inventory is invalid")
        name = str(unit.get("name") or "")
        path = Path(str(unit.get("unitPath") or ""))
        if (
            not SYSTEMD_UNIT_NAME_RE.fullmatch(name)
            or name in seen
            or path != root / name
            or not isinstance(unit.get("definitionExisted"), bool)
            or str(unit.get("enableState") or "") not in SYSTEMD_ENABLED_STATES
            or str(unit.get("activeState") or "") not in SYSTEMD_ACTIVE_STATES
            or not isinstance(unit.get("active"), bool)
            or unit.get("active") != (unit.get("activeState") == "active")
            or (
                "disabledByTransaction" in unit
                and not isinstance(unit.get("disabledByTransaction"), bool)
            )
            or (
                "enableRestoreMode" in unit
                and unit.get("enableRestoreMode") not in {"persistent", "runtime"}
            )
        ):
            raise TransactionError("systemd unit inventory binding is invalid")
        matches = [
            item
            for item in records
            if item.get("key") == "managed-systemd-unit"
            and item.get("path") == str(path.absolute())
        ]
        if len(matches) != 1:
            raise TransactionError("systemd unit has no unique durable definition snapshot")
        if matches[0].get("kind") not in {"missing", "file"}:
            raise TransactionError("systemd unit definition snapshot is unsafe")
        if unit["definitionExisted"] != (matches[0].get("kind") == "file"):
            raise TransactionError("systemd unit definition evidence is inconsistent")
        expected_definition_sha256 = unit.get("expectedDefinitionSha256")
        if expected_definition_sha256 is not None and (
            not isinstance(expected_definition_sha256, str)
            or not SHA256_RE.fullmatch(expected_definition_sha256)
            or matches[0].get("sha256") != expected_definition_sha256
        ):
            raise TransactionError("systemd unit definition contract is inconsistent")
        seen.add(name)


def _load_state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise TransactionError("update transaction journal must not be a symlink")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError(f"update transaction journal is unreadable: {path}") from exc
    source_schema = state.get("schemaVersion")
    if source_schema not in {1, SCHEMA_VERSION}:
        raise TransactionError("unsupported update transaction journal schema")
    if source_schema == 1:
        state["legacySchemaVersion"] = 1
        state["schemaVersion"] = SCHEMA_VERSION
    runtime = Path(str(state.get("runtime") or ""))
    tx_id = str(state.get("txId") or "")
    if not SAFE_TX_ID_RE.fullmatch(tx_id) or tx_id in {".", ".."}:
        raise TransactionError("update transaction journal has an unsafe transaction id")
    tx_root = runtime / "app" / "update-transactions"
    tx_dir = tx_root / tx_id
    expected_state_path = tx_dir / "journal.json"
    if (
        not runtime.is_absolute()
        or _lexical_absolute(path) != expected_state_path
    ):
        raise TransactionError("update transaction journal escaped its Runtime transaction root")
    runtime_aliases = state.get("runtimeAliases", [str(runtime)])
    if (
        not isinstance(runtime_aliases, list)
        or not 1 <= len(runtime_aliases) <= 4
        or len(runtime_aliases) != len(set(runtime_aliases))
        or any(
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or os.path.normpath(value) != value
            or Path(value).resolve(strict=False) != runtime
            for value in runtime_aliases
        )
    ):
        raise TransactionError("update transaction Runtime alias inventory is invalid")
    expected_directories = _expected_bound_directories(runtime, tx_id)
    _verify_bound_directories(state, expected_directories)
    command_lock_path = tx_dir / "command.lock"
    if not state.get("legacySchemaVersion"):
        command_lock = state.get("commandLockIdentity")
        if (
            not isinstance(command_lock, dict)
            or Path(str(command_lock.get("path") or "")) != command_lock_path
            or command_lock_path.is_symlink()
            or not command_lock_path.is_file()
        ):
            raise TransactionError("update transaction command lock binding is invalid")
        command_lock_metadata = command_lock_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(command_lock_metadata.st_mode)
            or command_lock_metadata.st_nlink != 1
            or command_lock_metadata.st_dev != command_lock.get("device")
            or command_lock_metadata.st_ino != command_lock.get("inode")
        ):
            raise TransactionError("update transaction command lock identity changed")
    if Path(str(state.get("lockPath") or "")) != runtime / "app" / ".update-transaction.lock":
        raise TransactionError("update transaction lock path does not match its Runtime")
    owner_path = tx_dir / "owner.json"
    if owner_path.is_symlink() or not owner_path.is_file():
        raise TransactionError("update transaction owner record is missing or unsafe")
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("update transaction owner record is unreadable") from exc
    try:
        owner_pid = int(owner.get("ownerPid") or 0)
        state_owner_pid = int(state.get("ownerPid") or 0)
    except (TypeError, ValueError) as exc:
        raise TransactionError("update transaction owner pid is invalid") from exc
    if (
        owner.get("txId") != tx_id
        or owner_pid != state_owner_pid
        or Path(str(owner.get("journal") or "")) != expected_state_path
        or (
            not state.get("legacySchemaVersion")
            and owner.get("ownerProcessIdentity") != state.get("ownerProcessIdentity")
        )
    ):
        raise TransactionError("update transaction owner record does not match its journal")
    if not state.get("legacySchemaVersion") and not isinstance(
        state.get("ownerProcessIdentity"), str
    ):
        raise TransactionError("update transaction owner process identity is missing")
    lock_path = runtime / "app" / ".update-transaction.lock"
    if lock_path.exists() or lock_path.is_symlink():
        if lock_path.is_symlink() or not lock_path.is_file():
            raise TransactionError("update transaction lock is unsafe")
        try:
            lock_owner = json.loads(lock_path.read_text(encoding="utf-8"))
            lock_stat = lock_path.stat(follow_symlinks=False)
            owner_stat = owner_path.stat(follow_symlinks=False)
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionError("update transaction lock is unreadable") from exc
        if (
            lock_owner != owner
            or lock_stat.st_dev != owner_stat.st_dev
            or lock_stat.st_ino != owner_stat.st_ino
        ):
            raise TransactionError("update transaction lock is not paired with its owner record")
    expected_pointers = {
        "source": runtime / "app" / "source",
        "venv": runtime / ".venv",
    }
    for name, expected in expected_pointers.items():
        pointer = state.get(name) if isinstance(state.get(name), dict) else {}
        if Path(str(pointer.get("path") or "")) != expected:
            raise TransactionError(f"update transaction {name} pointer escaped its managed path")
        candidate = pointer.get("candidateTarget")
        if candidate:
            expected_candidate = runtime / "app" / ("releases" if name == "source" else "venvs") / tx_id
            if Path(str(candidate)) != expected_candidate:
                raise TransactionError(f"update transaction {name} candidate escaped its managed root")
        prior_backup = pointer.get("priorBackupPath")
        if prior_backup:
            expected_backup = tx_dir / "pointer-backups" / f"prior-{name}"
            if Path(str(prior_backup)) != expected_backup:
                raise TransactionError(f"update transaction {name} prior backup escaped its reserved path")
    for item in state.get("files") or []:
        backup = item.get("backupPath") if isinstance(item, dict) else None
        if backup and not _is_within(Path(str(backup)), path.parent / "backups"):
            raise TransactionError("update transaction file backup escaped its transaction directory")
    if not state.get("legacySchemaVersion"):
        _validate_systemd_journal_bindings(state)
    expected_artifacts = {
        "source": runtime / "app" / "releases" / tx_id,
        "source-temp": runtime / "app" / "releases" / f".tmp-{tx_id}",
        "venv": runtime / "app" / "venvs" / tx_id,
        "validation-runtime": tx_root / tx_id / "candidate-runtime",
    }
    artifacts = state.get("candidateArtifacts") or []
    seen_artifacts: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise TransactionError("update transaction candidate artifact record is invalid")
        kind = str(item.get("kind") or "")
        if (
            kind in seen_artifacts
            or kind not in expected_artifacts
            or Path(str(item.get("path") or "")) != expected_artifacts[kind]
        ):
            raise TransactionError("update transaction candidate artifact escaped its reserved path")
        if not state.get("legacySchemaVersion"):
            if not SAFE_ARTIFACT_NONCE_RE.fullmatch(str(item.get("ownerNonce") or "")):
                raise TransactionError("update transaction candidate artifact owner is invalid")
            if (
                not isinstance(item.get("created"), bool)
                or not isinstance(item.get("markerRemoved"), bool)
                or not isinstance(item.get("transferred"), bool)
                or not isinstance(item.get("cleaned"), bool)
                or not isinstance(item.get("transferStarted"), bool)
                or not isinstance(item.get("reservationStarted"), bool)
                or not isinstance(item.get("abandonedReservationAttemptNonces"), list)
            ):
                raise TransactionError("update transaction candidate artifact state is invalid")
            reservation_attempt = item.get("reservationAttemptNonce")
            abandoned_attempts = item.get("abandonedReservationAttemptNonces")
            if (
                (reservation_attempt is not None and not SAFE_ARTIFACT_NONCE_RE.fullmatch(str(reservation_attempt)))
                or any(
                    not SAFE_ARTIFACT_NONCE_RE.fullmatch(str(nonce))
                    for nonce in abandoned_attempts
                )
                or len(abandoned_attempts) != len(set(abandoned_attempts))
                or len(abandoned_attempts) > 64
                or reservation_attempt in abandoned_attempts
                or item["reservationStarted"] != (reservation_attempt is not None)
            ):
                raise TransactionError("update transaction candidate reservation attempts are invalid")
            cleanup_path = item.get("cleanupPath")
            expected_cleanup = tx_dir / f".cleanup-{kind}"
            if cleanup_path is not None and Path(str(cleanup_path)) != expected_cleanup:
                raise TransactionError("update transaction candidate cleanup path escaped its transaction")
            if item["created"] and item["cleaned"]:
                raise TransactionError("update transaction candidate artifact state is contradictory")
            if item["reservationStarted"] and (
                item["created"] or item["cleaned"] or item["transferred"]
            ):
                raise TransactionError("update transaction candidate reservation state is contradictory")
            if item["created"] and (
                not isinstance(item.get("device"), int)
                or not isinstance(item.get("inode"), int)
            ):
                raise TransactionError("update transaction candidate artifact identity is missing")
        seen_artifacts.add(kind)
    if not state.get("legacySchemaVersion") and seen_artifacts != set(expected_artifacts):
        raise TransactionError("update transaction candidate artifact inventory is incomplete")
    return state


def _save_state(path: Path, state: dict[str, Any], *, event: str | None = None) -> None:
    if state.get("managedDirectoryIdentities"):
        _verify_bound_directories(
            state,
            _expected_bound_directories(Path(state["runtime"]), str(state["txId"])),
        )
    if event:
        state["phase"] = event
        state["updatedAt"] = _now()
        events_path = path.parent / "events.jsonl"
        record = {
            "at": state["updatedAt"],
            "event": event,
            "status": state.get("status"),
        }
        if events_path.is_symlink() or (events_path.exists() and not events_path.is_file()):
            raise TransactionError("update transaction event log is unsafe")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(events_path, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(events_path, 0o600)
    _atomic_json(path, state)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dependency_marker_exact_fields(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise TransactionError(f"staged venv dependency marker {label} has an invalid exact schema")
    return value


def _dependency_marker_reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _dependency_marker_unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _dependency_marker_canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _dependency_marker_canonical_distribution(value: Any) -> str:
    if not isinstance(value, str) or not DEPENDENCY_DISTRIBUTION_RE.fullmatch(value):
        raise TransactionError("staged venv dependency marker distribution name is invalid")
    return re.sub(r"[-_.]+", "-", value).lower()


def _dependency_marker_normalize_requirement(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\0" in value
        or "\n" in value
        or "\r" in value
    ):
        raise TransactionError("staged venv dependency marker requirement is invalid")
    raw = value.strip()
    if any(token in raw for token in (";", "@", "[", "]")):
        raise TransactionError("staged venv dependency marker requirement is invalid")
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(.*)", raw)
    if match is None:
        raise TransactionError("staged venv dependency marker requirement is invalid")
    name = _dependency_marker_canonical_distribution(match.group(1))
    specifier = re.sub(r"\s+", "", match.group(2))
    if not specifier:
        return name
    parts = specifier.split(",")
    if any(
        not part or not re.fullmatch(r"(?:===|==|!=|~=|<=|>=|<|>)[^,]+", part)
        for part in parts
    ):
        raise TransactionError("staged venv dependency marker requirement is invalid")
    normalized = name + ",".join(sorted(parts))
    if len(normalized) > 1024:
        raise TransactionError("staged venv dependency marker requirement is invalid")
    return normalized


def _dependency_marker_file_bytes(path: Path) -> bytes:
    try:
        inspected = path.lstat()
    except FileNotFoundError as exc:
        raise TransactionError("staged venv dependency marker is missing") from exc
    except OSError as exc:
        raise TransactionError("staged venv dependency marker cannot be inspected") from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
        raise TransactionError("staged venv dependency marker must be a regular non-symlink file")
    if inspected.st_nlink != 1:
        raise TransactionError("staged venv dependency marker must not be hard linked")
    if inspected.st_uid != os.getuid():
        raise TransactionError("staged venv dependency marker is not owned by the current user")
    if stat.S_IMODE(inspected.st_mode) != 0o444:
        raise TransactionError("staged venv dependency marker must have immutable 0444 permissions")
    if inspected.st_size <= 0 or inspected.st_size > DEPENDENCY_MARKER_MAX_BYTES:
        raise TransactionError("staged venv dependency marker is empty or exceeds the size limit")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TransactionError("staged venv dependency marker cannot be read safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o444
            or opened.st_size <= 0
            or opened.st_size > DEPENDENCY_MARKER_MAX_BYTES
            or opened.st_dev != inspected.st_dev
            or opened.st_ino != inspected.st_ino
        ):
            raise TransactionError("staged venv dependency marker changed while being opened")
        raw = bytearray()
        while len(raw) <= DEPENDENCY_MARKER_MAX_BYTES:
            block = os.read(descriptor, min(1024 * 1024, DEPENDENCY_MARKER_MAX_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        finished = os.fstat(descriptor)
        if (
            len(raw) != opened.st_size
            or len(raw) > DEPENDENCY_MARKER_MAX_BYTES
            or finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
            or finished.st_nlink != 1
            or finished.st_uid != os.getuid()
            or stat.S_IMODE(finished.st_mode) != 0o444
        ):
            raise TransactionError("staged venv dependency marker changed while being read")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _validate_dependency_marker_payload(value: Any) -> dict[str, Any]:
    marker = _dependency_marker_exact_fields(
        value,
        DEPENDENCY_MARKER_FIELDS,
        label="root",
    )
    if (
        type(marker["schemaVersion"]) is not int
        or marker["schemaVersion"] != 1
        or marker["product"] != "actanara"
        or marker["fingerprintAlgorithm"] != DEPENDENCY_FINGERPRINT_ALGORITHM
    ):
        raise TransactionError("staged venv dependency marker version/product is unsupported")
    for key in ("dependencyFingerprint", "lockSha256"):
        if not isinstance(marker[key], str) or not SHA256_RE.fullmatch(marker[key]):
            raise TransactionError(f"staged venv dependency marker field is invalid: {key}")
    environment_id = marker["environmentId"]
    if not isinstance(environment_id, str) or not DEPENDENCY_SAFE_ID_RE.fullmatch(environment_id):
        raise TransactionError("staged venv dependency marker environmentId is invalid")

    environment = _dependency_marker_exact_fields(
        marker["lockEnvironment"],
        DEPENDENCY_MARKER_ENVIRONMENT_FIELDS,
        label="lockEnvironment",
    )
    for key in ("implementation", "abi", "platformFamily", "architecture"):
        if not isinstance(environment[key], str) or not DEPENDENCY_SAFE_ID_RE.fullmatch(environment[key]):
            raise TransactionError(f"staged venv dependency marker environment field is invalid: {key}")
    if (
        not isinstance(environment["pythonMajorMinor"], str)
        or not DEPENDENCY_PYTHON_VERSION_RE.fullmatch(environment["pythonMajorMinor"])
        or environment["platformFamily"] not in {"macos", "linux", "windows"}
    ):
        raise TransactionError("staged venv dependency marker Python/platform identity is invalid")
    minimum_macos = environment["minimumMacOS"]
    if environment["platformFamily"] == "macos":
        if not isinstance(minimum_macos, str) or not DEPENDENCY_MACOS_VERSION_RE.fullmatch(minimum_macos):
            raise TransactionError("staged venv dependency marker minimumMacOS is invalid")
    elif minimum_macos is not None:
        raise TransactionError("staged venv dependency marker minimumMacOS must be null off macOS")
    profiles = marker["profiles"]
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(
            not isinstance(profile, str) or not DEPENDENCY_SAFE_ID_RE.fullmatch(profile)
            for profile in profiles
        )
        or profiles != sorted(set(profiles))
    ):
        raise TransactionError("staged venv dependency marker profiles are invalid")

    direct_dependencies = marker["directDependencies"]
    if not isinstance(direct_dependencies, list):
        raise TransactionError("staged venv dependency marker directDependencies are invalid")
    direct_profiles: list[str] = []
    for item in direct_dependencies:
        record = _dependency_marker_exact_fields(
            item,
            DEPENDENCY_MARKER_DIRECT_FIELDS,
            label="direct dependency record",
        )
        profile = record["profile"]
        if not isinstance(profile, str) or profile not in profiles or profile in direct_profiles:
            raise TransactionError("staged venv dependency marker direct dependency profile is invalid")
        requirements = record["requirements"]
        if (
            not isinstance(requirements, list)
            or any(not isinstance(item, str) for item in requirements)
            or requirements != sorted(set(requirements))
            or any(_dependency_marker_normalize_requirement(item) != item for item in requirements)
        ):
            raise TransactionError("staged venv dependency marker requirements are invalid")
        direct_profiles.append(profile)
    if direct_profiles != profiles:
        raise TransactionError("staged venv dependency marker does not cover every selected profile")

    distributions = marker["distributions"]
    if not isinstance(distributions, list):
        raise TransactionError("staged venv dependency marker distributions are invalid")
    distribution_names: list[str] = []
    for item in distributions:
        record = _dependency_marker_exact_fields(
            item,
            DEPENDENCY_MARKER_DISTRIBUTION_FIELDS,
            label="distribution record",
        )
        name = _dependency_marker_canonical_distribution(record["name"])
        if name != record["name"] or name in distribution_names:
            raise TransactionError("staged venv dependency marker distribution name is invalid")
        if not isinstance(record["version"], str) or not DEPENDENCY_VERSION_RE.fullmatch(record["version"]):
            raise TransactionError("staged venv dependency marker distribution version is invalid")
        hashes = record["hashes"]
        if (
            not isinstance(hashes, list)
            or not hashes
            or any(not isinstance(item_hash, str) for item_hash in hashes)
            or hashes != sorted(set(hashes))
            or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", item_hash)
                for item_hash in hashes
            )
        ):
            raise TransactionError("staged venv dependency marker distribution hashes are invalid")
        distribution_names.append(name)
    if distribution_names != sorted(distribution_names):
        raise TransactionError("staged venv dependency marker distributions are not sorted")

    fingerprint_payload = {
        "schemaVersion": 1,
        "algorithm": DEPENDENCY_FINGERPRINT_ALGORITHM,
        "runtimeEnvironment": {
            key: environment[key]
            for key in (
                "implementation",
                "pythonMajorMinor",
                "abi",
                "platformFamily",
                "architecture",
            )
        }
        | {"environmentId": environment_id},
        "lockEnvironment": dict(environment),
        "profiles": list(profiles),
        "directDependencies": [dict(item) for item in direct_dependencies],
        "runtimeLockSha256": marker["lockSha256"],
        "resolvedDistributions": [dict(item) for item in distributions],
    }
    computed_fingerprint = hashlib.sha256(
        _dependency_marker_canonical_json(fingerprint_payload)
    ).hexdigest()
    if marker["dependencyFingerprint"] != computed_fingerprint:
        raise TransactionError("staged venv dependency marker fingerprint does not match its content")
    return marker


def _validate_dependency_marker(
    venv: Path,
    *,
    expected_sha256: str | None = None,
) -> str:
    raw = _dependency_marker_file_bytes(venv / DEPENDENCY_MARKER_NAME)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_dependency_marker_unique_object,
            parse_constant=_dependency_marker_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise TransactionError("staged venv dependency marker is not strict JSON") from exc
    _validate_dependency_marker_payload(payload)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
            raise TransactionError("recorded venv candidate has no valid dependency marker hash binding")
        if actual_sha256 != expected_sha256:
            raise TransactionError("staged venv dependency marker bytes changed after recording")
    return actual_sha256


def _migration_set_sha256(records: Iterable[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["version"].encode("ascii"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(record["rollbackClass"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _prior_reader_binding_sha256(prior_digest: str, candidate_digest: str) -> str:
    digest = hashlib.sha256()
    digest.update(PRE_COMMIT_WRITER_CONTRACT.encode("ascii"))
    digest.update(b"\0")
    digest.update(prior_digest.encode("ascii"))
    digest.update(b"\0")
    digest.update(candidate_digest.encode("ascii"))
    return digest.hexdigest()


def _source_migration_inventory(source: Path) -> list[dict[str, str]]:
    migrations_root = source / MIGRATIONS_RELATIVE_PATH
    if not migrations_root.is_dir() or migrations_root.is_symlink():
        raise TransactionError("source migration inventory is missing")
    records: list[dict[str, str]] = []
    for path in sorted(migrations_root.glob("*.sql")):
        version = path.stem
        if not SAFE_MIGRATION_VERSION_RE.fullmatch(version) or not path.is_file() or path.is_symlink():
            raise TransactionError("source migration inventory contains an unsafe entry")
        records.append(
            {
                "version": version,
                "sha256": _sha256(path),
                "rollbackClass": "prior-source",
            }
        )
    if not records:
        raise TransactionError("source migration inventory is empty")
    return records


def _split_sql_statements(body: str, version: str) -> list[str]:
    statements: list[str] = []
    pending: list[str] = []
    state = "normal"
    index = 0
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if state == "line-comment":
            if character in "\r\n":
                pending.append("\n")
                state = "normal"
            index += 1
            continue
        if state == "block-comment":
            if character == "*" and following == "/":
                pending.append(" ")
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if state == "normal":
            if character == "-" and following == "-":
                state = "line-comment"
                index += 2
                continue
            if character == "/" and following == "*":
                state = "block-comment"
                index += 2
                continue
            if character in {"'", '"', "`", "["}:
                state = {"'": "single", '"': "double", "`": "backtick", "[": "bracket"}[
                    character
                ]
                pending.append(character)
                index += 1
                continue
            if character == ";":
                text = "".join(pending).strip()
                if text:
                    statements.append(text)
                pending = []
                index += 1
                continue
            pending.append(character)
            index += 1
            continue
        closing = {"single": "'", "double": '"', "backtick": "`", "bracket": "]"}[state]
        pending.append(character)
        if character == closing:
            if following == closing and state != "bracket":
                pending.append(following)
                index += 2
                continue
            state = "normal"
        index += 1
    if state not in {"normal", "line-comment"}:
        raise TransactionError(f"candidate additive migration has unterminated SQL syntax: {version}")
    text = "".join(pending).strip()
    if text:
        statements.append(text)
    return statements


def _validate_additive_migration_body(path: Path, version: str) -> None:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TransactionError(f"candidate additive migration is unreadable: {version}") from exc
    statements = _split_sql_statements(body, version)
    if not statements:
        raise TransactionError(f"candidate additive migration is empty: {version}")
    for statement in statements:
        create_allowed = re.match(
            r"(?is)^\s*CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX|VIEW)\b",
            statement,
        )
        alter_allowed = re.match(
            r"(?is)^\s*ALTER\s+TABLE\s+(?:\S+)\s+ADD\s+(?:COLUMN\s+)?\S+",
            statement,
        )
        if not create_allowed and not alter_allowed:
            raise TransactionError(
                f"candidate additive migration contains a prior-reader-unsafe statement: {version}"
            )


def _candidate_migration_contract(candidate: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    contract_path = candidate / MIGRATION_CONTRACT_RELATIVE_PATH
    if contract_path.is_symlink():
        raise TransactionError("candidate migration compatibility contract must not be a symlink")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("candidate migration compatibility contract is missing or invalid") from exc
    raw_records = contract.get("migrations") if isinstance(contract.get("migrations"), list) else []
    if (
        contract.get("schemaVersion") != 1
        or contract.get("policy") != MIGRATION_POLICY
        or contract.get("preCommitWriterContract") != PRE_COMMIT_WRITER_CONTRACT
        or contract.get("minimumReadableSchema") != "unversioned"
        or not raw_records
    ):
        raise TransactionError("candidate migration compatibility policy is unsupported")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    migrations_root = candidate / MIGRATIONS_RELATIVE_PATH
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise TransactionError("candidate migration compatibility record is invalid")
        version = str(raw.get("version") or "")
        expected_hash = str(raw.get("sha256") or "")
        rollback_class = str(raw.get("rollbackClass") or "")
        if (
            not SAFE_MIGRATION_VERSION_RE.fullmatch(version)
            or version in seen
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or rollback_class not in MIGRATION_CLASSES
        ):
            raise TransactionError("candidate migration compatibility record is unsafe")
        migration_path = migrations_root / f"{version}.sql"
        if not migration_path.is_file() or migration_path.is_symlink():
            raise TransactionError(f"candidate migration is missing from its contract: {version}")
        if _sha256(migration_path) != expected_hash:
            raise TransactionError(f"candidate migration body changed without a new version: {version}")
        if rollback_class == "rollback-compatible-additive":
            _validate_additive_migration_body(migration_path, version)
        seen.add(version)
        records.append(
            {"version": version, "sha256": expected_hash, "rollbackClass": rollback_class}
        )
    actual_paths = list(migrations_root.glob("*.sql"))
    if any(not path.is_file() or path.is_symlink() for path in actual_paths):
        raise TransactionError("candidate migration set contains an unsafe entry")
    actual_versions = {path.stem for path in actual_paths}
    versions = [record["version"] for record in records]
    if actual_versions != seen or versions != sorted(seen):
        raise TransactionError("candidate migration set does not exactly match its compatibility contract")
    if contract.get("maximumReadableSchema") != versions[-1]:
        raise TransactionError("candidate readable-schema bound does not match its migration set")
    normalized = {
        "schemaVersion": 1,
        "policy": MIGRATION_POLICY,
        "preCommitWriterContract": PRE_COMMIT_WRITER_CONTRACT,
        "minimumReadableSchema": "unversioned",
        "maximumReadableSchema": versions[-1],
        "migrationSetSha256": _migration_set_sha256(records),
        "migrations": records,
    }
    if manifest.get("databaseCompatibility") != normalized:
        raise TransactionError("candidate source manifest migration contract does not match its payload")
    return normalized


def _runtime_database_path(runtime: Path) -> Path:
    manifest_path = runtime / "config" / "runtime.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("Runtime manifest is unreadable during migration compatibility check") from exc
    configured = manifest.get("databasePath") if isinstance(manifest, dict) else None
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            raise TransactionError("Runtime database path is not absolute")
    else:
        path = runtime / "data" / "actanara_data.sqlite3"
    try:
        runtime_root = runtime.resolve(strict=True)
        database_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise TransactionError(
            "selected Runtime or configured database parent is unavailable during migration compatibility check"
        ) from exc
    if database_parent.is_symlink() or not database_parent.is_dir():
        raise TransactionError("Runtime database parent is not a regular directory")
    path = database_parent / path.name
    data_root = runtime / "data"
    if data_root.is_symlink() or data_root.resolve(strict=False) != runtime_root / "data":
        raise TransactionError("Runtime database root escaped the selected Runtime")
    if path.is_symlink():
        raise TransactionError("Runtime database path must not be a symlink")
    return path


def _runtime_database_identity(runtime: Path) -> dict[str, Any]:
    runtime_root = runtime.resolve(strict=True)
    database = _runtime_database_path(runtime)
    parent_metadata = database.parent.stat(follow_symlinks=False)
    if _is_within(database, runtime_root):
        locator = {
            "kind": "runtime-relative",
            "value": database.relative_to(runtime_root).as_posix(),
        }
    else:
        locator = {
            "kind": "external-absolute-sha256",
            "value": hashlib.sha256(str(database).encode("utf-8")).hexdigest(),
        }
    base = {
        "locator": locator,
        "parentDevice": parent_metadata.st_dev,
        "parentInode": parent_metadata.st_ino,
    }
    if not database.exists() and not database.is_symlink():
        return {**base, "kind": "missing"}
    if database.is_symlink() or not database.is_file():
        raise TransactionError("Runtime database is not a regular file")
    metadata = database.stat(follow_symlinks=False)
    if metadata.st_nlink != 1:
        raise TransactionError("Runtime database must not be hard-linked")
    return {
        **base,
        "kind": "file",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _database_identity_matches_gate(prior: dict[str, Any], current: dict[str, Any]) -> bool:
    if (
        prior.get("locator") != current.get("locator")
        or prior.get("parentDevice") != current.get("parentDevice")
        or prior.get("parentInode") != current.get("parentInode")
    ):
        return False
    if prior.get("kind") == "missing":
        return current.get("kind") in {"missing", "file"}
    return prior == current


def _applied_migration_versions(runtime: Path) -> list[str]:
    database = _runtime_database_path(runtime)
    if not database.exists():
        return []
    if not database.is_file() or database.is_symlink():
        raise TransactionError("Runtime database is not a regular file")
    connection: sqlite3.Connection | None = None
    try:
        connection = _sqlite_read_only_connection(database)
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            user_tables = connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if user_tables:
                raise TransactionError("nonempty Runtime database has no migration ledger")
            return []
        versions = [str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")]
        if not versions:
            unknown_tables = connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'schema_migrations' ORDER BY name"
            ).fetchall()
            if unknown_tables:
                raise TransactionError("nonempty Runtime database has an empty migration ledger")
    except TransactionError:
        raise
    except sqlite3.Error as exc:
        raise TransactionError("Runtime migration ledger is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()
    if len(set(versions)) != len(versions) or any(
        not SAFE_MIGRATION_VERSION_RE.fullmatch(version) for version in versions
    ):
        raise TransactionError("Runtime migration ledger contains an unknown version")
    return sorted(versions)


def _valid_locator_components(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str)
        and bool(item)
        and item not in {".", ".."}
        and "/" not in item
        and "\\" not in item
        and "\0" not in item
        for item in value
    )


def _validate_source_manifest_privacy(manifest: dict[str, Any], *, candidate_name: str) -> None:
    if type(manifest.get("schemaVersion")) is not int or manifest.get("schemaVersion") != 2:
        raise TransactionError("staged source manifest has an unsupported privacy schema")
    if set(manifest) != RUNTIME_SOURCE_FINAL_FIELDS:
        raise TransactionError("staged source manifest has an invalid exact schema")
    if manifest.get("product") != "actanara" or manifest.get("deploymentMode") != "release-symlink":
        raise TransactionError("staged source manifest has invalid release semantics")
    try:
        datetime.fromisoformat(manifest.get("copiedAt"))
    except (TypeError, ValueError) as exc:
        raise TransactionError("staged source manifest has an invalid copied timestamp") from exc
    version = manifest.get("pyprojectVersion")
    if version is not None and (
        not isinstance(version, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version)
    ):
        raise TransactionError("staged source manifest has an invalid project version")
    source_locator = manifest.get("sourceLocator")
    if not isinstance(source_locator, dict):
        raise TransactionError("staged source manifest has no source locator")
    source_kind = source_locator.get("kind")
    if source_kind == "login-home-relative":
        source_valid = set(source_locator) == {"kind", "pathComponents"} and _valid_locator_components(
            source_locator.get("pathComponents")
        )
    elif source_kind == "unavailable":
        source_valid = set(source_locator) == {"kind", "issue"} and source_locator.get("issue") in {
            "outside-login-home", "invalid-relative-components"
        }
    else:
        source_valid = False
    if not source_valid:
        raise TransactionError("staged source manifest has an invalid source locator")
    for field in ("deployedSourceLocator", "releaseLocator"):
        locator = manifest.get(field)
        if (
            not isinstance(locator, dict)
            or set(locator) != {"kind", "pathComponents"}
            or locator.get("kind") != "runtime-relative"
            or not _valid_locator_components(locator.get("pathComponents"))
        ):
            raise TransactionError("staged source manifest has an invalid runtime locator")
    if manifest["deployedSourceLocator"]["pathComponents"] != ["app", "source"]:
        raise TransactionError("staged source manifest has an invalid deployed source locator")
    if manifest["releaseLocator"]["pathComponents"] != ["app", "releases", candidate_name]:
        raise TransactionError("staged source manifest release locator does not match its candidate")
    git = manifest.get("git")
    if not isinstance(git, dict) or set(git) != {"available", "commit", "branch", "remote", "dirty"}:
        raise TransactionError("staged source manifest has invalid git provenance")
    if type(git.get("available")) is not bool:
        raise TransactionError("staged source manifest has invalid git provenance")
    if git.get("dirty") is not None and type(git.get("dirty")) is not bool:
        raise TransactionError("staged source manifest has invalid git provenance")
    commit = git.get("commit")
    if commit is not None and (
        not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit)
    ):
        raise TransactionError("staged source manifest has invalid git provenance")
    branch = git.get("branch")
    if branch is not None and (
        not isinstance(branch, str)
        or not branch
        or branch.startswith(("/", "~/", "file:"))
        or "/Users/" in branch
        or any(character in branch for character in "\0\r\n")
    ):
        raise TransactionError("staged source manifest has invalid git provenance")
    remote = git.get("remote")
    if remote is not None:
        if not isinstance(remote, str):
            raise TransactionError("staged source manifest has an unsafe git remote")
        try:
            parsed = urlsplit(remote)
        except (TypeError, ValueError) as exc:
            raise TransactionError("staged source manifest has an unsafe git remote") from exc
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise TransactionError("staged source manifest has an unsafe git remote")
    clean = manifest.get("cleanScan")
    payload = manifest.get("payload")
    if (
        not isinstance(clean, dict)
        or set(clean) != {"status", "scanner", "scannedFiles", "findingCount"}
        or not isinstance(payload, dict)
        or set(payload) != {"fileCount", "files", "sha256"}
    ):
        raise TransactionError("staged source manifest has invalid scan evidence")
    if (
        clean.get("status") != "passed"
        or clean.get("scanner")
        != "data_foundation.release_clean.repository_clean_deployment_check"
        or type(clean.get("scannedFiles")) is not int
        or clean.get("scannedFiles") < 0
        or clean.get("findingCount") != 0
        or type(payload.get("fileCount")) is not int
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256") or ""))
    ):
        raise TransactionError("staged source manifest has invalid scan evidence")


def _validate_source_payload(pointer: dict[str, Any]) -> None:
    candidate = Path(str(pointer.get("candidateTarget") or ""))
    if candidate.is_symlink() or not candidate.is_dir():
        raise TransactionError("staged source candidate is not a regular directory")
    manifest_path = candidate / ".actanara-runtime-source.json"
    if manifest_path.is_symlink():
        raise TransactionError("staged source manifest must not be a symlink")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("staged source manifest is unreadable") from exc
    expected_manifest_hash = pointer.get("candidateSha256")
    actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if not isinstance(expected_manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
        raise TransactionError("staged source manifest has no bound release-clean hash")
    if actual_manifest_hash != expected_manifest_hash:
        raise TransactionError("staged source manifest changed after release-clean scan")
    if not isinstance(manifest, dict):
        raise TransactionError("staged source manifest is not an object")
    _validate_source_manifest_privacy(manifest, candidate_name=candidate.name)
    clean = manifest.get("cleanScan") if isinstance(manifest.get("cleanScan"), dict) else {}
    payload = manifest.get("payload") if isinstance(manifest.get("payload"), dict) else {}
    records = payload.get("files") if isinstance(payload.get("files"), list) else []
    if clean.get("status") != "passed" or clean.get("findingCount") != 0:
        raise TransactionError("staged source manifest has no passing clean scan")
    if not records or payload.get("fileCount") != len(records):
        raise TransactionError("staged source manifest has no complete payload inventory")
    expected_paths: set[str] = set()
    aggregate = hashlib.sha256()
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise TransactionError("staged source payload inventory contains an invalid record")
        relative_text = str(item.get("path") or "")
        relative = Path(relative_text)
        if (
            not relative_text
            or "\0" in relative_text
            or relative_text.startswith(("~/", "file:"))
            or re.match(r"^[A-Za-z]:[\\/]", relative_text)
            or relative.is_absolute()
            or "." in relative.parts
            or ".." in relative.parts
        ):
            raise TransactionError("staged source payload inventory contains an unsafe path")
        if any(
            part in FORBIDDEN_PAYLOAD_NAMES
            or part.startswith(".env.")
            or part == ARTIFACT_MARKER_NAME
            or any(part.endswith(suffix) for suffix in FORBIDDEN_PAYLOAD_SUFFIXES)
            for part in relative.parts
        ):
            raise TransactionError(
                f"staged source payload inventory contains a release-forbidden path: {relative_text}"
            )
        path = candidate / relative
        if not _is_within(path, candidate) or path.is_symlink() or not path.is_file():
            raise TransactionError(f"staged source payload file is missing: {relative_text}")
        content = path.read_bytes()
        file_hash = hashlib.sha256(content).hexdigest()
        expected_size = item.get("size")
        if file_hash != item.get("sha256") or not isinstance(expected_size, int) or len(content) != expected_size:
            raise TransactionError(f"staged source payload changed after scan: {relative_text}")
        if relative.as_posix() in expected_paths:
            raise TransactionError(f"staged source payload inventory contains a duplicate path: {relative_text}")
        expected_paths.add(relative.as_posix())
        aggregate.update(relative.as_posix().encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(file_hash.encode("ascii"))
        aggregate.update(b"\n")
    candidate_entries = list(candidate.rglob("*"))
    if any(path.is_symlink() for path in candidate_entries):
        raise TransactionError("staged source payload contains a symlink")
    actual_paths = {
        path.relative_to(candidate).as_posix()
        for path in candidate_entries
        if path != manifest_path and path.is_file()
    }
    if actual_paths != expected_paths:
        raise TransactionError("staged source payload file set changed after release-clean scan")
    if aggregate.hexdigest() != payload.get("sha256"):
        raise TransactionError("staged source aggregate payload hash does not match its manifest")
    _candidate_migration_contract(candidate, manifest)


def _path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.exists():
        return "file"
    return "missing"


def _pointer_state(path: Path) -> dict[str, Any]:
    kind = _path_kind(path)
    result: dict[str, Any] = {
        "path": str(path),
        "priorKind": kind,
        "priorRawTarget": os.readlink(path) if kind == "symlink" else None,
        "priorResolvedTarget": str(path.resolve(strict=False)) if kind != "missing" else None,
        "priorBackupPath": None,
        "candidateTarget": None,
        "candidateSha256": None,
        "promotionStarted": False,
        "promoted": False,
        "rollbackComplete": False,
    }
    return result


def _active_dependency_profile_binding(pointer: Path) -> dict[str, Any]:
    """Bind the active venv identity and optional immutable marker.

    A legacy Runtime legitimately has no marker.  That absence is itself
    bound so a concurrent marker/pointer change cannot alter profile evidence
    between dependency planning and the final pre-stop gate.
    """

    pointer = _lexical_absolute(pointer)
    try:
        pointer_metadata = pointer.lstat()
    except OSError as exc:
        raise TransactionError("active reused venv pointer is unavailable") from exc
    if stat.S_ISLNK(pointer_metadata.st_mode):
        pointer_kind = "symlink"
        try:
            pointer_raw_target = os.readlink(pointer)
        except OSError as exc:
            raise TransactionError("active reused venv pointer cannot be inspected") from exc
    elif stat.S_ISDIR(pointer_metadata.st_mode):
        pointer_kind = "directory"
        pointer_raw_target = None
    else:
        raise TransactionError("active reused venv pointer is not a directory or symlink")
    try:
        target = pointer.resolve(strict=True)
        target_metadata = target.stat(follow_symlinks=False)
    except OSError as exc:
        raise TransactionError("active reused venv target is unavailable") from exc
    if target.is_symlink() or not stat.S_ISDIR(target_metadata.st_mode):
        raise TransactionError("active reused venv target is not a regular directory")

    python = target / "bin" / "python"
    try:
        python_metadata = python.stat()
    except OSError as exc:
        raise TransactionError("active reused venv Python is unavailable") from exc
    if not stat.S_ISREG(python_metadata.st_mode):
        raise TransactionError("active reused venv Python is not a regular file")
    marker = target / DEPENDENCY_MARKER_NAME
    if marker.exists() or marker.is_symlink():
        marker_status = "trusted"
        marker_sha256: str | None = _validate_dependency_marker(target)
    else:
        marker_status = "missing"
        marker_sha256 = None

    try:
        final_pointer_metadata = pointer.lstat()
        final_target_metadata = target.stat(follow_symlinks=False)
        final_python_metadata = python.stat()
        final_raw_target = os.readlink(pointer) if pointer_kind == "symlink" else None
    except OSError as exc:
        raise TransactionError("active reused venv identity changed while being bound") from exc
    if (
        final_pointer_metadata.st_dev != pointer_metadata.st_dev
        or final_pointer_metadata.st_ino != pointer_metadata.st_ino
        or final_target_metadata.st_dev != target_metadata.st_dev
        or final_target_metadata.st_ino != target_metadata.st_ino
        or final_python_metadata.st_dev != python_metadata.st_dev
        or final_python_metadata.st_ino != python_metadata.st_ino
        or final_raw_target != pointer_raw_target
    ):
        raise TransactionError("active reused venv identity changed while being bound")
    return {
        "pointerKind": pointer_kind,
        "pointerRawTarget": pointer_raw_target,
        "pointerDevice": pointer_metadata.st_dev,
        "pointerInode": pointer_metadata.st_ino,
        "targetPath": str(target),
        "targetDevice": target_metadata.st_dev,
        "targetInode": target_metadata.st_ino,
        "pythonDevice": python_metadata.st_dev,
        "pythonInode": python_metadata.st_ino,
        "markerStatus": marker_status,
        "dependencyMarkerSha256": marker_sha256,
    }


def _untrusted_venv_pointer_binding(pointer: Path) -> dict[str, Any]:
    """Bind an untrusted legacy pointer without following or executing it."""

    pointer = _lexical_absolute(pointer)
    try:
        metadata = pointer.lstat()
    except FileNotFoundError:
        return {
            "bindingKind": "pointer-only",
            "pointerKind": "missing",
            "pointerDevice": None,
            "pointerInode": None,
            "pointerMode": None,
            "pointerRawTarget": None,
        }
    except OSError as exc:
        raise TransactionError("legacy Runtime venv pointer cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode):
        pointer_kind = "symlink"
        try:
            raw_target = os.readlink(pointer)
        except OSError as exc:
            raise TransactionError("legacy Runtime venv pointer cannot be read") from exc
    elif stat.S_ISDIR(metadata.st_mode):
        pointer_kind = "directory"
        raw_target = None
    elif stat.S_ISREG(metadata.st_mode):
        pointer_kind = "file"
        raw_target = None
    else:
        raise TransactionError("legacy Runtime venv pointer has an unsupported type")
    return {
        "bindingKind": "pointer-only",
        "pointerKind": pointer_kind,
        "pointerDevice": metadata.st_dev,
        "pointerInode": metadata.st_ino,
        "pointerMode": stat.S_IMODE(metadata.st_mode),
        "pointerRawTarget": raw_target,
    }


def _active_venv_reuse_binding(pointer: Path) -> dict[str, Any]:
    binding = _active_dependency_profile_binding(pointer)
    if binding["markerStatus"] != "trusted":
        raise TransactionError("active reused venv dependency marker is missing")
    binding.pop("markerStatus")
    return binding


def _verify_dependency_profile_binding(state: dict[str, Any]) -> None:
    if state.get("mode") == "source-only":
        # Source-only transactions already revalidate the stronger active
        # reuse binding (pointer, target, Python, and marker) at every gate.
        return
    expected = state.get("dependencyProfileBinding")
    if expected is None:
        # Backward-compatible helper-only transactions created by older tests
        # and recovery tooling do not carry installer profile evidence.
        return
    if not isinstance(expected, dict):
        raise TransactionError("dependency profile evidence binding is invalid")
    pointer = state.get("venv") if isinstance(state.get("venv"), dict) else {}
    if expected.get("bindingKind") == "pointer-only":
        current = _untrusted_venv_pointer_binding(
            Path(str(pointer.get("path") or ""))
        )
        if current != expected:
            raise TransactionError(
                "legacy Runtime venv pointer changed after transaction begin"
            )
        return
    current = _active_dependency_profile_binding(Path(str(pointer.get("path") or "")))
    if current != expected:
        raise TransactionError(
            "active dependency profile evidence changed after transaction begin"
        )


def _verify_active_venv_reuse_binding(state: dict[str, Any]) -> None:
    pointer = state.get("venv") if isinstance(state.get("venv"), dict) else {}
    expected = pointer.get("activeReuseBinding")
    expected_fields = {
        "pointerKind",
        "pointerRawTarget",
        "pointerDevice",
        "pointerInode",
        "targetPath",
        "targetDevice",
        "targetInode",
        "pythonDevice",
        "pythonInode",
        "dependencyMarkerSha256",
    }
    if not isinstance(expected, dict) or set(expected) != expected_fields:
        raise TransactionError("active reused venv has no complete begin-time identity binding")
    try:
        current = _active_venv_reuse_binding(Path(str(pointer.get("path") or "")))
    except TransactionError as exc:
        raise TransactionError(f"active reused venv failed identity revalidation: {exc}") from exc
    if current["dependencyMarkerSha256"] != expected["dependencyMarkerSha256"]:
        raise TransactionError("active reused venv dependency marker bytes changed after begin")
    if current != expected:
        raise TransactionError("active reused venv pointer, target, or Python identity changed after begin")


def _candidate_artifact_state(kind: str, path: Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "ownerNonce": secrets.token_hex(8),
        "created": False,
        "device": None,
        "inode": None,
        "markerRemoved": False,
        "transferred": False,
        "transferStarted": False,
        "reservationStarted": False,
        "reservationAttemptNonce": None,
        "abandonedReservationAttemptNonces": [],
        "cleaned": False,
        "cleanupPath": None,
    }


def _candidate_artifact_record(state: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [
        item
        for item in state.get("candidateArtifacts") or []
        if isinstance(item, dict) and item.get("kind") == kind
    ]
    if len(matches) != 1:
        raise TransactionError(f"candidate artifact record is missing or duplicated: {kind}")
    return matches[0]


def _reservation_staging_path(state_path: Path, record: dict[str, Any]) -> Path:
    kind = str(record.get("kind") or "")
    nonce = str(record.get("reservationAttemptNonce") or "")
    if not SAFE_TX_ID_RE.fullmatch(kind) or not SAFE_ARTIFACT_NONCE_RE.fullmatch(nonce):
        raise TransactionError("candidate artifact reservation attempt is invalid")
    return state_path.parent / f".reserve-{kind}-{nonce}"


def _authorize_reservation_attempt(
    state_path: Path,
    state: dict[str, Any],
    record: dict[str, Any],
) -> None:
    abandoned = record.get("abandonedReservationAttemptNonces")
    if not isinstance(abandoned, list) or len(abandoned) >= 64:
        raise TransactionError("candidate artifact reservation retry limit was exceeded")
    for _attempt in range(16):
        nonce = secrets.token_hex(8)
        if nonce not in abandoned:
            break
    else:
        raise TransactionError("candidate artifact reservation nonce could not be allocated")
    record["reservationStarted"] = True
    record["reservationAttemptNonce"] = nonce
    _save_state(
        state_path,
        state,
        event=f"candidate-artifact-reservation-started:{record['kind']}",
    )


def _abandon_reservation_attempt(
    state_path: Path,
    state: dict[str, Any],
    record: dict[str, Any],
) -> None:
    nonce = str(record.get("reservationAttemptNonce") or "")
    abandoned = record.get("abandonedReservationAttemptNonces")
    if (
        not SAFE_ARTIFACT_NONCE_RE.fullmatch(nonce)
        or not isinstance(abandoned, list)
        or nonce in abandoned
        or len(abandoned) >= 64
    ):
        raise TransactionError("candidate artifact reservation attempt cannot be abandoned safely")
    abandoned.append(nonce)
    record["reservationAttemptNonce"] = None
    record["reservationStarted"] = False
    _save_state(
        state_path,
        state,
        event=f"candidate-artifact-reservation-attempt-preserved:{record['kind']}",
    )


def _artifact_marker_matches(path: Path, record: dict[str, Any]) -> bool:
    marker = path / ARTIFACT_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="ascii") == f"ON:{record['ownerNonce']}\n"
    except (OSError, UnicodeDecodeError, KeyError):
        return False


def _artifact_identity(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_dir():
        raise TransactionError("candidate artifact is not a regular directory")
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _record_artifact_identity(record: dict[str, Any], path: Path) -> None:
    device, inode = _artifact_identity(path)
    record["created"] = True
    record["device"] = device
    record["inode"] = inode


def _verify_artifact_ownership(
    record: dict[str, Any],
    *,
    require_marker: bool,
    path_override: Path | None = None,
) -> Path:
    path = path_override or Path(str(record.get("path") or ""))
    device, inode = _artifact_identity(path)
    if record.get("created"):
        if device != record.get("device") or inode != record.get("inode"):
            raise TransactionError("candidate artifact identity changed")
    elif not _artifact_marker_matches(path, record):
        raise TransactionError("candidate artifact exists without transaction ownership")
    if require_marker and not _artifact_marker_matches(path, record):
        raise TransactionError("candidate artifact owner marker is missing")
    return path


def _verify_recorded_candidate_artifacts(state: dict[str, Any]) -> None:
    source_record = _candidate_artifact_record(state, "source")
    source = _verify_artifact_ownership(source_record, require_marker=False)
    if Path(str(state["source"].get("candidateTarget") or "")) != source:
        raise TransactionError("recorded source candidate no longer matches its owned artifact")
    _validate_source_payload(state["source"])
    if state.get("mode") not in {"upgrade", "repair"}:
        _verify_active_venv_reuse_binding(state)
        return
    venv_record = _candidate_artifact_record(state, "venv")
    venv = _verify_artifact_ownership(venv_record, require_marker=False)
    if Path(str(state["venv"].get("candidateTarget") or "")) != venv:
        raise TransactionError("recorded venv candidate no longer matches its owned artifact")
    python = venv / "bin" / "python"
    if not python.is_file():
        raise TransactionError("staged venv Python is missing or unreadable")
    marker_sha256 = state["venv"].get("dependencyMarkerSha256")
    if not isinstance(marker_sha256, str) or not SHA256_RE.fullmatch(marker_sha256):
        raise TransactionError("recorded venv candidate has no valid dependency marker hash binding")
    _validate_dependency_marker(venv, expected_sha256=marker_sha256)


def _service_kind(label: str) -> str:
    if label.endswith("dashboard.watchdog"):
        return "watchdog"
    if label.endswith("dashboard-aggregation"):
        return "scheduler-aggregation"
    if label.endswith(".pipeline"):
        return "scheduler-pipeline"
    if "rag" in label.lower():
        return "rag"
    if "dashboard" in label.lower():
        return "dashboard"
    return "managed"


def _configured_service_kind(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "dashboard-service": "dashboard",
        "dashboard-watchdog": "watchdog",
        "rag-server": "rag",
        "daily-pipeline": "scheduler-pipeline",
        "dashboard-aggregation": "scheduler-aggregation",
    }.get(normalized, fallback)


def _safe_settings_inventory(
    settings_path: Path,
    home: Path,
) -> tuple[list[tuple[str, str, Path]], dict[str, Any]]:
    labels: list[tuple[str, str, Path]] = []
    summary: dict[str, Any] = {
        "dashboardHost": None,
        "dashboardPort": None,
        "dashboardHealthPath": None,
        "ragHost": None,
        "ragPort": None,
        "ragHealthPath": None,
    }
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return labels, summary
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    summary["dashboardHost"] = str(dashboard.get("host") or "") or None
    port = dashboard.get("port")
    summary["dashboardPort"] = int(port) if isinstance(port, int) else None
    summary["dashboardHealthPath"] = str(dashboard.get("healthPath") or "/health")
    for kind, key, default in (
        ("dashboard", "serviceLabel", "com.actanara.dashboard"),
        ("watchdog", "watchdogLabel", "com.actanara.dashboard.watchdog"),
    ):
        label = str(dashboard.get(key) or default).strip()
        if label:
            labels.append((kind, label, home / "Library" / "LaunchAgents" / f"{label}.plist"))

    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    server = rag.get("server") if isinstance(rag.get("server"), dict) else {}
    summary["ragHost"] = str(server.get("host") or "") or None
    rag_port = server.get("port")
    summary["ragPort"] = int(rag_port) if isinstance(rag_port, int) else None
    summary["ragHealthPath"] = str(server.get("healthPath") or "/health")
    launch_agent = server.get("launchAgent") if isinstance(server.get("launchAgent"), dict) else {}
    rag_jobs = launch_agent.get("jobs") if isinstance(launch_agent.get("jobs"), list) else []
    if rag_jobs:
        for item in rag_jobs:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            path = Path(str(item.get("plistPath") or "")).expanduser()
            if label:
                labels.append((
                    _configured_service_kind(item.get("kind"), "rag"),
                    label,
                    path if str(path) not in {"", "."} else home / "Library" / "LaunchAgents" / f"{label}.plist",
                ))
    else:
        label = "com.actanara.rag-server"
        labels.append(("rag", label, home / "Library" / "LaunchAgents" / f"{label}.plist"))

    schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
    timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    jobs = timer.get("jobs") if isinstance(timer.get("jobs"), list) else []
    if jobs:
        for item in jobs:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            path = Path(str(item.get("plistPath") or "")).expanduser()
            if label:
                labels.append((
                    _configured_service_kind(item.get("kind"), _service_kind(label)),
                    label,
                    path if str(path) not in {"", "."} else home / "Library" / "LaunchAgents" / f"{label}.plist",
                ))
    else:
        base = str(timer.get("label") or "actanara.daily").strip() or "actanara.daily"
        for kind, suffix in (
            ("scheduler-pipeline", "pipeline"),
            ("scheduler-aggregation", "dashboard-aggregation"),
        ):
            label = f"{base}.{suffix}"
            labels.append((kind, label, home / "Library" / "LaunchAgents" / f"{label}.plist"))
    return labels, summary


def _plist_service_kind(payload: dict[str, Any], label: str) -> str:
    arguments = payload.get("ProgramArguments")
    joined = " ".join(item for item in arguments if isinstance(item, str)).lower() if isinstance(arguments, list) else ""
    if "rag_server_launch_agent.py" in joined or "agentic_rag/embedding_server.py" in joined:
        return "rag"
    if "dashboard_launch_agent.py" in joined:
        return "watchdog" if " check " in f" {joined} " or label.endswith("watchdog") else "dashboard"
    if "uvicorn" in joined and "app.main" in joined:
        return "dashboard"
    if "run_dashboard_foundation_refresh.py" in joined:
        return "scheduler-aggregation"
    if "run_daily_pipeline.py" in joined:
        return "scheduler-pipeline"
    return _service_kind(label)


def _discover_services(runtime: Path, home: Path) -> tuple[list[tuple[str, str, Path]], dict[str, Any]]:
    settings_path = runtime / "config" / "settings.json"
    configured, summary = _safe_settings_inventory(settings_path, home)
    launch_agents = home / "Library" / "LaunchAgents"
    candidates: list[tuple[str, str, Path]] = list(configured)
    if launch_agents.is_dir():
        for path in sorted(launch_agents.glob("*.plist")):
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            label = path.stem
            kind = _service_kind(label)
            try:
                parsed = plistlib.loads(payload)
                candidate = parsed.get("Label") if isinstance(parsed, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    label = candidate.strip()
                if isinstance(parsed, dict):
                    kind = _plist_service_kind(parsed, label)
            except Exception:
                pass
            if not _plist_targets_runtime(path, runtime, label):
                continue
            candidates.append((kind, label, path))
    seen: set[str] = set()
    result: list[tuple[str, str, Path]] = []
    for kind, label, path in candidates:
        if not label or label in seen:
            continue
        seen.add(label)
        result.append((kind, label, path))
    return result, summary


def _plist_targets_runtime(path: Path, runtime: Path, label: str) -> bool:
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("Label") or "").strip() != label:
        return False
    environment = payload.get("EnvironmentVariables")
    if isinstance(environment, dict):
        actanara_home = environment.get("ACTANARA_HOME")
        if isinstance(actanara_home, str) and actanara_home.strip():
            candidate = Path(actanara_home).expanduser()
            return candidate.is_absolute() and candidate.resolve(strict=False) == runtime.resolve(strict=False)
    return _legacy_watchdog_targets_runtime(payload, runtime)


def _legacy_repair_plist_targets_runtime(path: Path, runtime: Path, label: str) -> bool:
    """Accept an old service only when its plist is concretely Runtime-bound."""

    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(payload, dict) or str(payload.get("Label") or "").strip() != label:
        return False
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        return False
    candidates = [Path(item).expanduser() for item in arguments if Path(item).expanduser().is_absolute()]
    working_directory = payload.get("WorkingDirectory")
    if isinstance(working_directory, str) and Path(working_directory).expanduser().is_absolute():
        candidates.append(Path(working_directory).expanduser())
    return any(candidate != runtime and _is_within(candidate, runtime) for candidate in candidates)


def _legacy_watchdog_targets_runtime(payload: dict[str, Any], runtime: Path) -> bool:
    """Recognize only the exact pre-v1 watchdog shape that omitted ACTANARA_HOME."""
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) != 8 or not all(
        isinstance(item, str) for item in arguments
    ):
        return False
    python, script, command, url_flag, url, label_flag, service_label, restart = arguments
    python_path = Path(python).expanduser()
    script_path = Path(script).expanduser()
    if (
        not python_path.is_absolute()
        or python_path.resolve(strict=False)
        != (runtime / ".venv" / "bin" / "python").resolve(strict=False)
        or not script_path.is_absolute()
        or script_path.resolve(strict=False)
        != (
            runtime / "app" / "source" / "advanced" / "dashboard" / "dashboard_launch_agent.py"
        ).resolve(strict=False)
        or command != "check"
        or url_flag != "--url"
        or label_flag != "--label"
        or restart != "--restart"
        or not service_label.strip()
        or any(character in service_label for character in "/\0\r\n")
    ):
        return False
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
    )


NORMALIZED_PYTHON_SERVICE_KINDS = {
    "dashboard",
    "watchdog",
    "rag",
    "scheduler-pipeline",
    "scheduler-aggregation",
}


def _service_binding_roots(state: dict[str, Any]) -> dict[str, Path]:
    runtime = Path(state["runtime"])
    aliases: list[Path] = [runtime]
    for value in state.get("runtimeAliases") or []:
        alias = Path(str(value))
        if (
            not alias.is_absolute()
            or os.path.normpath(str(alias)) != str(alias)
            or alias.resolve(strict=False) != runtime
        ):
            raise TransactionError("managed service binding has an unsafe Runtime alias")
        if alias not in aliases:
            aliases.append(alias)
    candidate_source = Path(str(state.get("source", {}).get("candidateTarget") or ""))
    if not candidate_source.is_absolute() or candidate_source.is_symlink() or not candidate_source.is_dir():
        raise TransactionError("managed service binding has no valid source candidate")
    if state.get("mode") in {"upgrade", "repair"}:
        candidate_venv = Path(str(state.get("venv", {}).get("candidateTarget") or ""))
        if not candidate_venv.is_absolute() or candidate_venv.is_symlink() or not candidate_venv.is_dir():
            raise TransactionError("managed service binding has no valid venv candidate")
    else:
        candidate_venv = runtime / ".venv"
        if not candidate_venv.exists():
            raise TransactionError("managed service binding has no active venv")
    return {
        "runtime": runtime,
        "runtimeAliases": tuple(aliases),
        "sourceStable": runtime / "app" / "source",
        "sourceGenerations": runtime / "app" / "releases",
        "sourceCandidate": candidate_source,
        "venvStable": runtime / ".venv",
        "venvGenerations": runtime / "app" / "venvs",
        "venvCandidate": candidate_venv,
    }


def _strict_service_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\0\r\n"):
        raise TransactionError(f"managed service {field} is not a safe path")
    if not value.startswith("/") or os.path.normpath(value) != value:
        raise TransactionError(f"managed service {field} is not a canonical absolute path")
    return value


def _binding_counterpart_exists(root: Path, remainder: tuple[str, ...]) -> bool:
    counterpart = root.joinpath(*remainder)
    return counterpart.exists() or counterpart.is_symlink()


def _canonical_runtime_path(value: Any, *, field: str, roots: dict[str, Path]) -> str:
    text = _strict_service_path(value, field=field)
    if Path(text) not in roots["runtimeAliases"]:
        raise TransactionError(f"managed service {field} does not match the selected Runtime")
    return str(roots["runtime"])


def _rebind_service_path(
    value: Any,
    *,
    field: str,
    roots: dict[str, Path],
    counts: dict[str, int],
) -> str:
    text = _strict_service_path(value, field=field)
    for alias in roots["runtimeAliases"]:
        source_stable = str(alias / "app" / "source")
        source_generations = str(alias / "app" / "releases")
        venv_stable = str(alias / ".venv")
        venv_generations = str(alias / "app" / "venvs")

        for prefix, canonical, binding, candidate_root in (
            (source_stable, roots["sourceStable"], "source", roots["sourceCandidate"]),
            (venv_stable, roots["venvStable"], "venv", roots["venvCandidate"]),
        ):
            if text == prefix or text.startswith(prefix + "/"):
                remainder = tuple(Path(text).parts[len(Path(prefix).parts) :])
                if not _binding_counterpart_exists(candidate_root, remainder):
                    raise TransactionError(
                        f"managed service {field} has no candidate {binding} counterpart"
                    )
                counts[binding] += 1
                return str(canonical.joinpath(*remainder))
            if text.startswith(prefix):
                raise TransactionError(f"managed service {field} uses a confusing {binding} path prefix")

        for prefix, stable, binding, candidate_root in (
            (source_generations, roots["sourceStable"], "source", roots["sourceCandidate"]),
            (venv_generations, roots["venvStable"], "venv", roots["venvCandidate"]),
        ):
            if text == prefix:
                raise TransactionError(f"managed service {field} has no {binding} generation")
            if text.startswith(prefix + "/"):
                relative = tuple(Path(text).parts[len(Path(prefix).parts) :])
                generation, *remainder_list = relative
                if not SAFE_TX_ID_RE.fullmatch(generation) or generation in {".", ".."}:
                    raise TransactionError(f"managed service {field} has an unsafe {binding} generation")
                remainder = tuple(remainder_list)
                if not _binding_counterpart_exists(candidate_root, remainder):
                    raise TransactionError(
                        f"managed service {field} has no candidate {binding} counterpart"
                    )
                counts[binding] += 1
                return str(stable.joinpath(*remainder))
            if text.startswith(prefix):
                raise TransactionError(f"managed service {field} uses a confusing {binding} path prefix")
    return text


def _reject_residual_generation_paths(value: Any, roots: dict[str, Path], *, field: str = "plist") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_residual_generation_paths(child, roots, field=f"{field}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_residual_generation_paths(child, roots, field=f"{field}[{index}]")
        return
    if not isinstance(value, str):
        return
    for alias in roots["runtimeAliases"]:
        for suffix in (("app", "releases"), ("app", "venvs")):
            if str(alias.joinpath(*suffix)) in value:
                raise TransactionError(
                    f"managed service {field} retains an embedded concrete generation path"
                )


def _normalize_dashboard_arguments(
    arguments: Any,
    *,
    roots: dict[str, Path],
    counts: dict[str, int],
) -> list[str]:
    if (
        not isinstance(arguments, list)
        or len(arguments) != 3
        or arguments[:2] != ["/bin/zsh", "-lc"]
        or not isinstance(arguments[2], str)
    ):
        raise TransactionError("managed dashboard service has an invalid ProgramArguments shape")
    try:
        tokens = shlex.split(arguments[2], posix=True)
    except ValueError as exc:
        raise TransactionError("managed dashboard service command is malformed") from exc
    if (
        len(tokens) != 14
        or tokens[0] != "cd"
        or tokens[2] != "&&"
        or tokens[3] != "exec"
        or tokens[5:9] != ["-m", "uvicorn", "app.main:app", "--app-dir"]
        or tokens[10] != "--host"
        or tokens[12] != "--port"
        or not tokens[13].isdigit()
        or not 1 <= int(tokens[13]) <= 65535
    ):
        raise TransactionError("managed dashboard service command is not the strict uvicorn shape")
    project_root = _rebind_service_path(
        tokens[1], field="dashboard project root", roots=roots, counts=counts
    )
    python = _rebind_service_path(
        tokens[4], field="dashboard Python", roots=roots, counts=counts
    )
    app_dir = _rebind_service_path(
        tokens[9], field="dashboard app directory", roots=roots, counts=counts
    )
    if project_root != str(roots["sourceStable"]):
        raise TransactionError("managed dashboard project root is not the stable source pointer")
    if python != str(roots["venvStable"] / "bin" / "python"):
        raise TransactionError("managed dashboard Python is not the stable venv pointer")
    if app_dir != str(roots["sourceStable"] / "src" / "dashboard"):
        raise TransactionError("managed dashboard app directory is not candidate-bound")
    normalized_tokens = list(tokens)
    normalized_tokens[1] = project_root
    normalized_tokens[4] = python
    normalized_tokens[9] = app_dir
    command = " ".join(
        token if token == "&&" else shlex.quote(token)
        for token in normalized_tokens
    )
    return ["/bin/zsh", "-lc", command]


def _normalize_direct_arguments(
    arguments: Any,
    *,
    kind: str,
    roots: dict[str, Path],
    counts: dict[str, int],
) -> list[str]:
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise TransactionError(f"managed {kind} service has invalid ProgramArguments")
    normalized = list(arguments)
    if kind == "watchdog":
        if (
            len(normalized) != 8
            or normalized[2] != "check"
            or normalized[3] != "--url"
            or normalized[5] != "--label"
            or normalized[7] != "--restart"
        ):
            raise TransactionError("managed watchdog service has an invalid ProgramArguments shape")
        normalized[0] = _rebind_service_path(
            normalized[0], field="watchdog Python", roots=roots, counts=counts
        )
        normalized[1] = _rebind_service_path(
            normalized[1], field="watchdog script", roots=roots, counts=counts
        )
        expected_script = roots["sourceStable"] / "advanced" / "dashboard" / "dashboard_launch_agent.py"
        if normalized[0] != str(roots["venvStable"] / "bin" / "python") or normalized[1] != str(expected_script):
            raise TransactionError("managed watchdog service is not bound to stable runtime pointers")
    elif kind == "rag":
        if (
            len(normalized) != 7
            or normalized[2:4] != ["run", "--project-root"]
            or normalized[5] != "--actanara-home"
        ):
            raise TransactionError("managed RAG service has an invalid ProgramArguments shape")
        for index, field in ((0, "RAG Python"), (1, "RAG script"), (4, "RAG project root")):
            normalized[index] = _rebind_service_path(
                normalized[index], field=field, roots=roots, counts=counts
            )
        normalized[6] = _canonical_runtime_path(
            normalized[6], field="RAG Runtime", roots=roots
        )
        expected_script = roots["sourceStable"] / "advanced" / "dashboard" / "rag_server_launch_agent.py"
        if (
            normalized[0] != str(roots["venvStable"] / "bin" / "python")
            or normalized[1] != str(expected_script)
            or normalized[4] != str(roots["sourceStable"])
        ):
            raise TransactionError("managed RAG service is not bound to stable runtime pointers")
    elif kind in {"scheduler-pipeline", "scheduler-aggregation"}:
        if len(normalized) != 2:
            raise TransactionError("managed scheduler service has an invalid ProgramArguments shape")
        normalized[0] = _rebind_service_path(
            normalized[0], field="scheduler Python", roots=roots, counts=counts
        )
        normalized[1] = _rebind_service_path(
            normalized[1], field="scheduler script", roots=roots, counts=counts
        )
        script_name = (
            "run_daily_pipeline.py"
            if kind == "scheduler-pipeline"
            else "run_dashboard_foundation_refresh.py"
        )
        expected_script = roots["sourceStable"] / "advanced" / "pipeline" / script_name
        if normalized[0] != str(roots["venvStable"] / "bin" / "python") or normalized[1] != str(expected_script):
            raise TransactionError("managed scheduler service is not bound to stable runtime pointers")
    else:
        raise TransactionError("managed service kind has no binding policy")
    return normalized


def _normalize_service_plist_payload(
    payload: dict[str, Any],
    *,
    service: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int], bool]:
    roots = _service_binding_roots(state)
    normalized = copy.deepcopy(payload)
    kind = str(service.get("kind") or "")
    counts = {"source": 0, "venv": 0}
    if kind == "dashboard":
        normalized["ProgramArguments"] = _normalize_dashboard_arguments(
            normalized.get("ProgramArguments"), roots=roots, counts=counts
        )
    else:
        normalized["ProgramArguments"] = _normalize_direct_arguments(
            normalized.get("ProgramArguments"), kind=kind, roots=roots, counts=counts
        )

    environment = normalized.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        if kind != "watchdog" or not _legacy_watchdog_targets_runtime(payload, roots["runtime"]):
            raise TransactionError(f"managed service environment is invalid: {service['label']}")
        environment = {}
    else:
        environment = dict(environment)
    if environment.get("ACTANARA_HOME") is None:
        if kind != "watchdog":
            raise TransactionError(f"managed service lost Runtime provenance: {service['label']}")
        environment["ACTANARA_HOME"] = str(roots["runtime"])
    environment["ACTANARA_HOME"] = _canonical_runtime_path(
        environment.get("ACTANARA_HOME"), field="Runtime provenance", roots=roots
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    for key, field in (
        ("ACTANARA_DASHBOARD_PROJECT_ROOT", "dashboard environment project root"),
        ("ACTANARA_DASHBOARD_PYTHON", "dashboard environment Python"),
    ):
        if key in environment:
            environment[key] = _rebind_service_path(
                environment[key], field=field, roots=roots, counts=counts
            )
    if kind == "dashboard":
        if environment.get("ACTANARA_DASHBOARD_PROJECT_ROOT") != str(roots["sourceStable"]):
            raise TransactionError("managed dashboard environment is missing the stable source binding")
        if environment.get("ACTANARA_DASHBOARD_PYTHON") != str(roots["venvStable"] / "bin" / "python"):
            raise TransactionError("managed dashboard environment is missing the stable venv binding")

    if kind == "watchdog" and "PYTHONPATH" in environment:
        raise TransactionError(f"managed watchdog service has an unexpected PYTHONPATH: {service['label']}")
    if "PYTHONPATH" in environment:
        python_path = environment["PYTHONPATH"]
        if not isinstance(python_path, str) or not python_path:
            raise TransactionError(f"managed service PYTHONPATH is invalid: {service['label']}")
        components = python_path.split(os.pathsep)
        if any(not component for component in components):
            raise TransactionError(f"managed service PYTHONPATH is invalid: {service['label']}")
        normalized_components = [
            _rebind_service_path(
                component,
                field="PYTHONPATH component",
                roots=roots,
                counts=counts,
            )
            for component in components
        ]
        expected_components = {
            "dashboard": [
                str(roots["sourceStable"]),
                str(roots["sourceStable"] / "src"),
                str(roots["sourceStable"] / "src" / "dashboard"),
            ],
            "rag": [
                str(roots["sourceStable"]),
                str(roots["sourceStable"] / "src"),
            ],
            "scheduler-pipeline": [
                str(roots["sourceStable"]),
                str(roots["sourceStable"] / "src"),
                str(roots["sourceStable"] / "src" / "dashboard"),
            ],
            "scheduler-aggregation": [
                str(roots["sourceStable"]),
                str(roots["sourceStable"] / "src"),
                str(roots["sourceStable"] / "src" / "dashboard"),
            ],
        }.get(kind)
        if normalized_components != expected_components:
            raise TransactionError(f"managed service PYTHONPATH is not the exact product path set: {service['label']}")
        environment["PYTHONPATH"] = os.pathsep.join(normalized_components)
    elif kind != "watchdog":
        raise TransactionError(f"managed service PYTHONPATH is missing: {service['label']}")
    normalized["EnvironmentVariables"] = environment

    if "WorkingDirectory" in normalized:
        normalized["WorkingDirectory"] = _rebind_service_path(
            normalized["WorkingDirectory"],
            field="WorkingDirectory",
            roots=roots,
            counts=counts,
        )
    if kind.startswith("scheduler-") and normalized.get("WorkingDirectory") != str(roots["sourceStable"]):
        raise TransactionError("managed scheduler WorkingDirectory is not the stable source pointer")
    if counts["source"] < 1 or counts["venv"] < 1:
        raise TransactionError(f"managed service binding inventory is incomplete: {service['label']}")
    _reject_residual_generation_paths(normalized, roots)
    return normalized, counts, normalized != payload


def _managed_plist_snapshot(state: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    plist = str(Path(service["plistPath"]).absolute())
    matches = [
        item
        for item in state.get("files") or []
        if item.get("key") == "managed-plist" and item.get("path") == plist
    ]
    if len(matches) != 1:
        raise TransactionError("managed service definition has no unique durable snapshot")
    return matches[0]


def _service_plist_matches_normalized(service: dict[str, Any]) -> bool:
    path = Path(str(service.get("plistPath") or ""))
    expected_hash = service.get("normalizedPlistSha256")
    expected_mode = service.get("normalizedPlistMode")
    if (
        not service.get("plistNormalizationStarted")
        or not isinstance(expected_hash, str)
        or not isinstance(expected_mode, int)
        or path.is_symlink()
        or not path.is_file()
    ):
        return False
    try:
        return (
            _sha256(path) == expected_hash
            and (path.stat(follow_symlinks=False).st_mode & 0o777) == expected_mode
        )
    except OSError:
        return False


def _verify_service_plist_bindings(state: dict[str, Any]) -> None:
    if state.get("platform") != "Darwin":
        return
    if not state.get("servicePlistNormalizationComplete"):
        raise TransactionError("managed service plist binding gate was not completed")
    if state.get("mode") == "repair":
        for service in state.get("services") or []:
            if service.get("kind") not in NORMALIZED_PYTHON_SERVICE_KINDS:
                continue
            if not _file_matches_snapshot(_managed_plist_snapshot(state, service)):
                raise TransactionError(
                    f"legacy managed service definition changed during repair: {service['label']}"
                )
        return
    for service in state.get("services") or []:
        if service.get("kind") not in NORMALIZED_PYTHON_SERVICE_KINDS:
            continue
        record = _managed_plist_snapshot(state, service)
        path = Path(service["plistPath"])
        if record.get("kind") == "missing":
            if (
                not service.get("plistBindingComplete")
                or service.get("plistBindingStatus") != "absent"
                or path.exists()
                or path.is_symlink()
            ):
                raise TransactionError(
                    f"managed service absent plist binding changed: {service['label']}"
                )
            continue
        if (
            record.get("kind") != "file"
            or not service.get("plistNormalizationComplete")
            or not service.get("plistBindingComplete")
            or not _service_plist_matches_normalized(service)
        ):
            raise TransactionError(f"managed service plist binding is incomplete: {service['label']}")
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            raise TransactionError(f"managed service plist binding is unreadable: {service['label']}") from exc
        if not isinstance(payload, dict) or str(payload.get("Label") or "").strip() != service["label"]:
            raise TransactionError(f"managed service plist binding identity changed: {service['label']}")
        _normalized, counts, changed = _normalize_service_plist_payload(
            payload,
            service=service,
            state=state,
        )
        if (
            changed
            or counts["source"] != service.get("plistSourceBindingCount")
            or counts["venv"] != service.get("plistVenvBindingCount")
        ):
            raise TransactionError(f"managed service plist binding changed: {service['label']}")


def _restore_normalized_service_plist(
    state: dict[str, Any],
    service: dict[str, Any],
) -> None:
    record = _managed_plist_snapshot(state, service)
    if record.get("kind") != "file":
        raise TransactionError("normalized managed plist has no file snapshot")
    backup = Path(str(record.get("backupPath") or ""))
    if backup.is_symlink() or not backup.is_file() or _sha256(backup) != record.get("sha256"):
        raise TransactionError("normalized managed plist backup is missing or changed")
    _atomic_bytes(
        Path(record["path"]),
        backup.read_bytes(),
        mode=int(record["mode"]),
    )
    if not _file_matches_snapshot(record):
        raise TransactionError("normalized managed plist prior bytes were not restored")


def normalize_service_plists(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "stopped" or not state.get("mutableStateCaptured"):
        raise TransactionError("managed plist normalization requires stopped services and captured state")
    _verify_critical_control_state(state)
    if state.get("mode") == "repair":
        state["servicePlistNormalizationComplete"] = True
        _verify_service_plist_bindings(state)
        _save_state(state_path, state, event="legacy-managed-plists-preserved")
        return 0
    if state.get("platform") != "Darwin":
        state["servicePlistNormalizationComplete"] = True
        _save_state(state_path, state, event="managed-plists-normalized")
        return 0
    for service in state.get("services") or []:
        if service.get("kind") not in NORMALIZED_PYTHON_SERVICE_KINDS:
            continue
        path = Path(service["plistPath"])
        record = _managed_plist_snapshot(state, service)
        if record.get("kind") == "missing":
            service.update(
                {
                    "plistBindingComplete": True,
                    "plistBindingStatus": "absent",
                    "plistNormalizationComplete": True,
                    "plistNormalizationRequired": False,
                }
            )
            continue
        if record.get("kind") != "file" or path.is_symlink() or not path.is_file():
            raise TransactionError(f"managed service plist is not a regular file: {service['label']}")
        if (
            service.get("plistBindingComplete")
            and service.get("plistNormalizationComplete")
            and _service_plist_matches_normalized(service)
        ):
            continue
        if not _file_matches_snapshot(record):
            raise TransactionError(f"managed service definition changed before normalization: {service['label']}")
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            raise TransactionError(f"managed service plist is unreadable: {service['label']}") from exc
        if not isinstance(payload, dict) or str(payload.get("Label") or "").strip() != service["label"]:
            raise TransactionError(f"managed service plist identity changed: {service['label']}")
        payload, counts, _semantic_change = _normalize_service_plist_payload(
            payload,
            service=service,
            state=state,
        )
        normalized = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
        bytes_changed = path.read_bytes() != normalized
        mode = path.stat(follow_symlinks=False).st_mode & 0o777
        service.update(
            {
                "plistNormalizationRequired": bytes_changed,
                "plistNormalizationStarted": True,
                "plistNormalizationComplete": False,
                "plistBindingComplete": False,
                "plistBindingStatus": "candidate-stable",
                "plistSourceBindingCount": counts["source"],
                "plistVenvBindingCount": counts["venv"],
                "normalizedPlistSha256": hashlib.sha256(normalized).hexdigest(),
                "normalizedPlistMode": mode,
            }
        )
        _save_state(state_path, state, event=f"managed-plist-normalization-planned:{service['kind']}")
        _maybe_test_fail(f"managed-plist-normalization-planned-{service['kind']}")
        if bytes_changed:
            _atomic_bytes(path, normalized, mode=mode)
        if not _service_plist_matches_normalized(service):
            raise TransactionError(f"managed service plist normalization failed: {service['label']}")
        _maybe_test_fail(f"managed-plist-normalization-written-{service['kind']}")
        service["plistNormalizationComplete"] = True
        service["plistBindingComplete"] = True
        _save_state(state_path, state, event=f"managed-plist-normalized:{service['kind']}")
    state["servicePlistNormalizationComplete"] = True
    _verify_service_plist_bindings(state)
    _save_state(state_path, state, event="managed-plists-normalized")
    return 0


def _launch_state(launchctl: str, domain: str, label: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [launchctl, "print", f"{domain}/{label}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError(f"launchctl state probe failed for managed label {label}") from exc
    if result.returncode in {3, 113}:
        return False, "unloaded"
    if result.returncode != 0:
        raise TransactionError(
            f"launchctl state probe returned an unexpected error for managed label {label} (exit {result.returncode})"
        )
    match = STATE_RE.search(result.stdout or "")
    if not match:
        raise TransactionError(f"launchctl returned an unparseable state for managed label {label}")
    state = match.group(1).strip().lower()
    return True, state


def _health_timeout_seconds() -> float:
    if os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        return SERVICE_HEALTH_TIMEOUT_SECONDS
    try:
        configured = float(os.environ.get("ACTANARA_INSTALL_TEST_HEALTH_TIMEOUT_SECONDS", "1.0"))
    except ValueError:
        configured = 1.0
    return max(0.1, min(configured, SERVICE_HEALTH_TIMEOUT_SECONDS))


def _service_state_timeout_seconds() -> float:
    if os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        return SERVICE_STATE_TIMEOUT_SECONDS
    try:
        configured = float(os.environ.get("ACTANARA_INSTALL_TEST_SERVICE_STATE_TIMEOUT_SECONDS", "1.0"))
    except ValueError:
        configured = 1.0
    return max(0.1, min(configured, SERVICE_STATE_TIMEOUT_SECONDS))


def _wait_for_service_state(
    launchctl: str,
    domain: str,
    label: str,
    *,
    expected_loaded: bool,
    require_running: bool = False,
) -> tuple[bool, str]:
    deadline = time.monotonic() + _service_state_timeout_seconds()
    while True:
        loaded, launch_state = _launch_state(launchctl, domain, label)
        if loaded == expected_loaded and (
            not expected_loaded or not require_running or launch_state in RUNNING_STATES
        ):
            return loaded, launch_state
        if time.monotonic() >= deadline:
            return loaded, launch_state
        time.sleep(0.1)


def _bound_executable(value: str, *, label: str) -> tuple[str, dict[str, Any]]:
    raw = Path(str(value or ""))
    if not raw.is_absolute() or any(character in str(raw) for character in "\0\r\n\t"):
        raise TransactionError(f"{label} executable path is invalid")
    try:
        resolved = raw.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise TransactionError(f"{label} executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise TransactionError(f"{label} executable is unavailable")
    return str(resolved), {
        "path": str(resolved),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _run_systemctl(
    state: dict[str, Any],
    *args: str,
    allowed: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    _validate_systemd_journal_bindings(state)
    child_options: dict[str, Any] = {}
    if sys.platform == "linux":
        raw_guard_fd = os.environ.get("ACTANARA_RUNTIME_MUTATION_GUARD_FD", "")
        if raw_guard_fd:
            try:
                guard_fd = int(raw_guard_fd)
                guard_metadata = os.fstat(guard_fd)
            except (ValueError, OSError) as exc:
                raise TransactionError(
                    "inherited Runtime mutation guard is unavailable"
                ) from exc
            if guard_fd < 3 or not stat.S_ISREG(guard_metadata.st_mode):
                raise TransactionError("inherited Runtime mutation guard is unsafe")
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
    try:
        result = subprocess.run(
            [state["systemctl"], "--user", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
            **child_options,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        operation = args[0] if args else "operation"
        raise TransactionError(f"systemctl --user {operation} failed") from exc
    expected = allowed if allowed is not None else {0}
    if result.returncode not in expected:
        operation = args[0] if args else "operation"
        raise TransactionError(
            f"systemctl --user {operation} failed for a managed unit (exit {result.returncode})"
        )
    return result


def _systemd_probe_state(state: dict[str, Any], name: str) -> tuple[str, str]:
    enabled_result = _run_systemctl(
        state,
        "is-enabled",
        name,
        allowed={0, 1, 3, 4},
    )
    active_result = _run_systemctl(
        state,
        "is-active",
        name,
        allowed={0, 1, 3, 4},
    )
    enabled = (enabled_result.stdout or "").strip().splitlines()
    active = (active_result.stdout or "").strip().splitlines()
    enabled_state = enabled[-1].strip().lower() if enabled else "not-found"
    active_state = active[-1].strip().lower() if active else "not-found"
    if enabled_state not in SYSTEMD_ENABLED_STATES:
        raise TransactionError(f"systemctl returned an unknown enable state for {name}")
    if active_state not in SYSTEMD_ACTIVE_STATES:
        raise TransactionError(f"systemctl returned an unknown active state for {name}")
    return enabled_state, active_state


def _wait_for_systemd_active_state(
    state: dict[str, Any],
    name: str,
    *,
    expected_active: bool,
) -> tuple[str, str]:
    deadline = time.monotonic() + _service_state_timeout_seconds()
    stable = 0
    latest = ("not-found", "not-found")
    while True:
        latest = _systemd_probe_state(state, name)
        matches = (latest[1] == "active") == expected_active and latest[1] not in {
            "activating",
            "deactivating",
            "reloading",
        }
        stable = stable + 1 if matches else 0
        if stable >= 3:
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.1)


def _candidate_full_source_commit(state: dict[str, Any]) -> str | None:
    pointer = state.get("source") if isinstance(state.get("source"), dict) else {}
    candidate = Path(str(pointer.get("candidateTarget") or ""))
    if not candidate.is_absolute():
        return None
    manifest_path = candidate / ".actanara-runtime-source.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("candidate source provenance is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != pointer.get("candidateSha256"):
        raise TransactionError("candidate source provenance changed after validation")
    git = manifest.get("git") if isinstance(manifest, dict) else None
    commit = git.get("commit") if isinstance(git, dict) else None
    available = git.get("available") if isinstance(git, dict) else None
    if available is True:
        if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            raise TransactionError("candidate source provenance is not a full commit id")
        return commit
    if commit is not None:
        raise TransactionError("candidate source provenance availability is inconsistent")
    return None


def _verify_service_health(
    state: dict[str, Any],
    *,
    require_candidate_commit: bool = False,
) -> None:
    running_kinds = {
        str(service.get("kind") or "")
        for service in state.get("services") or []
        if service.get("loaded") and str(service.get("state") or "").lower() in RUNNING_STATES
    }
    summary = state.get("settingsSummary") if isinstance(state.get("settingsSummary"), dict) else {}
    endpoints: list[tuple[str, str, int, str]] = []
    for kind, prefix in (("dashboard", "dashboard"), ("rag", "rag")):
        if kind not in running_kinds:
            continue
        host = str(summary.get(f"{prefix}Host") or "").strip()
        port = summary.get(f"{prefix}Port")
        path = str(summary.get(f"{prefix}HealthPath") or "/health").strip()
        if not host or not isinstance(port, int) or not 1 <= port <= 65535:
            raise TransactionError(f"managed {kind} was running before update but has no valid health endpoint")
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if not path.startswith("/"):
            path = "/" + path
        endpoints.append((kind, host, port, path))

    expected_commit = _candidate_full_source_commit(state) if require_candidate_commit else None
    for kind, host, port, path in endpoints:
        deadline = time.monotonic() + _health_timeout_seconds()
        last_error = "unavailable"
        while True:
            connection: http.client.HTTPConnection | None = None
            try:
                remaining = max(0.1, min(2.0, deadline - time.monotonic()))
                connection = http.client.HTTPConnection(host, port, timeout=remaining)
                connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
                response = connection.getresponse()
                body = response.read(65536)
                if response.status == 200:
                    try:
                        payload = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = {}
                    status = str(payload.get("status") or "").lower() if isinstance(payload, dict) else ""
                    if status in {"ok", "healthy"}:
                        source_commit = payload.get("sourceCommit") if isinstance(payload, dict) else None
                        if expected_commit is None or source_commit == expected_commit:
                            break
                        last_error = "HTTP 200 from a process with non-candidate source provenance"
                    else:
                        last_error = "HTTP 200 without an ok health status"
                else:
                    last_error = f"HTTP {response.status}"
            except (OSError, http.client.HTTPException) as exc:
                last_error = type(exc).__name__
            finally:
                if connection is not None:
                    connection.close()
            if time.monotonic() >= deadline:
                raise TransactionError(
                    f"managed {kind} health check failed on the preserved port {port}: {last_error}"
                )
            time.sleep(0.1)


def _run_launchctl(launchctl: str, *args: str, allow_absent: bool = False) -> None:
    try:
        result = subprocess.run(
            [launchctl, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError(f"launchctl operation failed: {args[0] if args else 'unknown'}") from exc
    allowed = {0}
    if allow_absent:
        allowed.update({3, 5, 113})
    if result.returncode not in allowed:
        raise TransactionError(
            f"launchctl {args[0] if args else 'operation'} failed for a managed service (exit {result.returncode})"
        )


def _snapshot_path(state_path: Path, state: dict[str, Any], key: str, path: Path) -> None:
    canonical = str(path.absolute())
    if any(item.get("path") == canonical for item in state["files"]):
        return
    kind = _path_kind(path)
    index = len(state["files"])
    backup = state_path.parent / "backups" / f"{index:04d}"
    record: dict[str, Any] = {
        "key": key,
        "path": canonical,
        "kind": kind,
        "rawTarget": os.readlink(path) if kind == "symlink" else None,
        "backupPath": None,
        "sha256": None,
        "mode": None,
        "directoryIdentity": None,
    }
    if kind == "file":
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, backup, follow_symlinks=False)
        _fsync_file(backup)
        _fsync_dir(backup.parent)
        record["sha256"] = _sha256(path)
        record["mode"] = path.stat(follow_symlinks=False).st_mode & 0o777
        record["backupPath"] = str(backup)
    elif kind == "directory":
        metadata = path.stat(follow_symlinks=False)
        record["mode"] = metadata.st_mode & 0o777
        record["directoryIdentity"] = {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mtimeNs": metadata.st_mtime_ns,
        }
    state["files"].append(record)


def _is_sqlite_database_path(path: Path) -> bool:
    return path.name.lower().endswith(SQLITE_DATABASE_SUFFIXES)


def _sqlite_read_only_connection(path: Path) -> sqlite3.Connection:
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    except (OSError, ValueError) as exc:
        raise TransactionError(f"SQLite database is unavailable: {path.name}") from exc
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise TransactionError(f"SQLite database could not be opened read-only: {path.name}") from exc


def _validate_sqlite_database(path: Path, *, role: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise TransactionError(f"{role} SQLite database is missing or is not a regular file: {path.name}")
    connection: sqlite3.Connection | None = None
    try:
        connection = _sqlite_read_only_connection(path)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row != ("ok",):
            raise TransactionError(f"{role} SQLite database failed integrity_check: {path.name}")
        # This is intentionally schema-agnostic.  Reading sqlite_schema proves
        # that the snapshot is usable through SQLite instead of merely having a
        # valid-looking file header.
        connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
    except TransactionError:
        raise
    except sqlite3.Error as exc:
        raise TransactionError(f"{role} SQLite database is unreadable: {path.name}") from exc
    finally:
        if connection is not None:
            connection.close()


def _remove_sqlite_temporary_files(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-journal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-wal"),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _snapshot_sqlite_database(
    state_path: Path,
    state: dict[str, Any],
    path: Path,
) -> None:
    canonical = str(path.absolute())
    if any(item.get("path") == canonical for item in state["files"]):
        return
    if path.is_symlink():
        raise TransactionError(f"SQLite database path must not be a symlink: {path.name}")

    index = len(state["files"])
    backup = state_path.parent / "backups" / f"{index:04d}.sqlite3"
    temporary = backup.with_name(f".{backup.name}.partial-{os.getpid()}")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup.exists() or backup.is_symlink():
        raise TransactionError("SQLite evidence snapshot path already exists")
    _remove_sqlite_temporary_files(temporary)

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    deadline = time.monotonic() + SQLITE_BACKUP_TIMEOUT_SECONDS

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TransactionError(f"SQLite online backup timed out: {path.name}")

    try:
        source = _sqlite_read_only_connection(path)
        destination = sqlite3.connect(temporary, timeout=5.0)
        destination.execute("PRAGMA busy_timeout = 5000")
        source.backup(
            destination,
            pages=256,
            progress=check_deadline,
            sleep=0.05,
        )
        journal_mode = destination.execute("PRAGMA journal_mode = DELETE").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise TransactionError(f"SQLite evidence snapshot is not standalone: {path.name}")
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None

        _validate_sqlite_database(temporary, role="snapshot")
        mode = path.stat(follow_symlinks=False).st_mode & 0o777
        os.chmod(temporary, mode)
        _fsync_file(temporary)
        _rename_exclusive(temporary, backup)
        _fsync_dir(backup.parent)
        record = {
            "key": "database",
            "path": canonical,
            "kind": "sqlite-database",
            "rawTarget": None,
            "backupPath": str(backup),
            "sha256": _sha256(backup),
            "mode": mode,
            "snapshotPolicy": SQLITE_SNAPSHOT_POLICY,
            "integrityCheck": "ok",
        }
        state["files"].append(record)
    except TransactionError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise TransactionError(f"SQLite online backup failed: {path.name}") from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        _remove_sqlite_temporary_files(temporary)


def _validate_sqlite_snapshot_record(item: dict[str, Any]) -> None:
    path = Path(str(item.get("path") or ""))
    if item.get("kind") != "sqlite-database" or item.get("snapshotPolicy") != SQLITE_SNAPSHOT_POLICY:
        # Journals produced by the previous helper copied main/WAL/SHM files
        # independently.  They are not coherent recovery sources.  Preserve the
        # live database and validate only a legacy main database path.
        if _is_sqlite_database_path(path):
            _validate_sqlite_database(path, role="live")
        return
    backup = Path(str(item.get("backupPath") or ""))
    if not backup.is_file() or backup.is_symlink():
        raise TransactionError(f"SQLite evidence snapshot is missing: {path.name}")
    if _sha256(backup) != item.get("sha256"):
        raise TransactionError(f"SQLite evidence snapshot hash changed: {path.name}")
    _validate_sqlite_database(backup, role="snapshot")
    _validate_sqlite_database(path, role="live")


def _snapshot_database_files(state_path: Path, state: dict[str, Any], runtime: Path) -> None:
    data = runtime / "data"
    paths: set[Path] = set()
    if data.is_dir():
        paths.update(
            path
            for path in data.rglob("*")
            if path.is_file() and _is_sqlite_database_path(path)
        )
    configured = _runtime_database_path(runtime)
    if configured.is_file():
        paths.add(configured)
    for path in sorted(paths):
        _snapshot_sqlite_database(state_path, state, path)


def _move_aside(path: Path, state_path: Path, tag: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    failed = state_path.parent / "failed-current"
    failed.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = failed / tag
    suffix = 0
    while target.exists() or target.is_symlink():
        suffix += 1
        target = failed / f"{tag}-{suffix}"
    _rename_exclusive(path, target)


def _prepare_transaction_symlink(path: Path, raw_target: str) -> None:
    if path.is_symlink():
        if os.readlink(path) == raw_target:
            return
        raise TransactionError("transaction symlink staging path is occupied")
    if path.exists():
        raise TransactionError("transaction symlink staging path is occupied")
    try:
        os.symlink(raw_target, path)
    except FileExistsError as exc:
        raise TransactionError("transaction symlink staging path was occupied concurrently") from exc
    _fsync_dir(path.parent)


def _candidate_pointer_raw_target(
    state: dict[str, Any],
    *,
    name: str,
    path: Path,
    candidate: Path,
) -> str:
    runtime = Path(state["runtime"])
    if name == "source":
        expected_pointer = runtime / "app" / "source"
        expected_store = runtime / "app" / "releases"
    elif name == "venv":
        expected_pointer = runtime / ".venv"
        expected_store = runtime / "app" / "venvs"
    else:
        raise TransactionError(f"unsupported managed Runtime pointer: {name}")
    expected_candidate = expected_store / str(state["txId"])
    if path != expected_pointer or candidate != expected_candidate:
        raise TransactionError(f"{name} candidate is outside its managed Runtime store")
    try:
        relative = candidate.relative_to(path.parent)
    except ValueError as exc:
        raise TransactionError(f"{name} candidate cannot use a relative Runtime pointer") from exc
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise TransactionError(f"{name} candidate has an unsafe relative Runtime target")
    raw_target = relative.as_posix()
    try:
        resolved_relative = (path.parent / relative).resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TransactionError(f"{name} candidate relative Runtime target is unreadable") from exc
    if resolved_relative != resolved_candidate:
        raise TransactionError(f"{name} candidate relative Runtime target changed")
    return raw_target


def _pointer_matches_candidate(path: Path, raw_target: str, candidate: Path) -> bool:
    try:
        return (
            path.is_symlink()
            and os.readlink(path) == raw_target
            and path.resolve(strict=True) == candidate.resolve(strict=True)
        )
    except (OSError, RuntimeError):
        return False


def _file_matches_snapshot(item: dict[str, Any]) -> bool:
    path = Path(item["path"])
    kind = _path_kind(path)
    if kind != item["kind"]:
        return False
    if kind == "missing":
        return True
    if kind == "symlink":
        return os.readlink(path) == item.get("rawTarget")
    if kind == "file":
        try:
            return (
                _sha256(path) == item.get("sha256")
                and (path.stat(follow_symlinks=False).st_mode & 0o777) == item.get("mode")
            )
        except OSError:
            return False
    if kind == "directory":
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            return False
        return item.get("directoryIdentity") == {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mtimeNs": metadata.st_mtime_ns,
        } and (metadata.st_mode & 0o777) == item.get("mode")
    return False


def _verify_systemd_definition_bindings(state: dict[str, Any]) -> None:
    _validate_systemd_journal_bindings(state)
    if state.get("platform") != "Linux":
        return
    records = state.get("files") or []
    for unit in state.get("systemdUnits") or []:
        path = Path(unit["unitPath"])
        matches = [
            item
            for item in records
            if item.get("key") == "managed-systemd-unit"
            and item.get("path") == str(path.absolute())
        ]
        if len(matches) != 1 or not _file_matches_snapshot(matches[0]):
            raise TransactionError(
                f"managed systemd unit definition changed concurrently: {unit['name']}"
            )
        if path.is_file():
            try:
                first_line = path.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, UnicodeDecodeError, IndexError) as exc:
                raise TransactionError(
                    f"managed systemd unit definition is unreadable: {unit['name']}"
                ) from exc
            metadata = path.stat(follow_symlinks=False)
            if (
                first_line != SYSTEMD_MANAGED_UNIT_HEADER
                or path.is_symlink()
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
            ):
                raise TransactionError(
                    f"managed systemd unit definition provenance changed: {unit['name']}"
                )


def _verify_critical_control_state(state: dict[str, Any]) -> None:
    records = state.get("files") or []
    for key in ("settings", "runtime-manifest"):
        matches = [item for item in records if item.get("key") == key]
        if (
            len(matches) != 1
            or matches[0].get("kind") == "symlink"
            or not _file_matches_snapshot(matches[0])
        ):
            raise TransactionError(f"critical update control state changed concurrently: {key}")
    venv_state = state.get("venv") if isinstance(state.get("venv"), dict) else {}
    if not venv_state.get("promotionStarted"):
        _verify_dependency_profile_binding(state)
    runtime = Path(state["runtime"])
    _verify_systemd_definition_bindings(state)
    by_path = {str(item.get("path") or ""): item for item in records}
    for service in state.get("services") or []:
        plist = Path(service["plistPath"])
        record = by_path.get(str(plist.absolute()))
        if not isinstance(record, dict) or record.get("key") != "managed-plist":
            raise TransactionError("managed service definition has no durable snapshot")
        matches_snapshot = _file_matches_snapshot(record)
        matches_normalized = _service_plist_matches_normalized(service)
        if record.get("kind") == "symlink" or not (matches_snapshot or matches_normalized):
            raise TransactionError(
                f"managed service definition changed concurrently: {service['label']}"
            )
        if plist.is_file() and not (
            _plist_targets_runtime(plist, runtime, service["label"])
            or (
                state.get("mode") == "repair"
                and _legacy_repair_plist_targets_runtime(
                    plist,
                    runtime,
                    service["label"],
                )
            )
        ):
            raise TransactionError(
                f"managed service definition provenance changed: {service['label']}"
            )


def _restore_pointer(state_path: Path, name: str, pointer: dict[str, Any]) -> None:
    if pointer.get("rollbackComplete") or not pointer.get("promotionStarted"):
        return
    path = Path(pointer["path"])
    candidate = pointer.get("candidateTarget")
    kind = pointer["priorKind"]
    current_kind = _path_kind(path)
    already_prior = False
    if kind == "symlink" and current_kind == "symlink":
        already_prior = os.readlink(path) == pointer["priorRawTarget"]
    elif kind in {"directory", "file"} and current_kind == kind and not Path(str(pointer.get("priorBackupPath") or "")).exists():
        # A concrete prior pointer is renamed, never copied. Seeing the same
        # concrete kind at the pointer after rollback means the backup has
        # already been consumed by an earlier recovery attempt.
        already_prior = True
    elif kind == "missing" and current_kind == "missing":
        already_prior = True
    if already_prior:
        pointer["promotionStarted"] = False
        pointer["promoted"] = False
        pointer["rollbackComplete"] = True
        return

    if current_kind != "missing":
        if current_kind != "symlink" or not candidate:
            raise TransactionError(f"rollback conflict: {name} pointer is not transaction-owned")
        current = str(path.resolve(strict=False))
        if current != str(Path(candidate).resolve(strict=False)):
            raise TransactionError(f"rollback conflict: {name} pointer no longer targets this transaction candidate")
        _move_aside(path, state_path, f"{name}-pointer")
        _fsync_dir(path.parent)
    if kind == "symlink":
        tmp = state_path.parent / f".restore-{name}"
        _prepare_transaction_symlink(tmp, pointer["priorRawTarget"])
        _rename_exclusive(tmp, path)
        if not path.is_symlink() or os.readlink(path) != pointer["priorRawTarget"]:
            try:
                _rename_exclusive(path, tmp)
            except TransactionError as exc:
                raise TransactionError(
                    f"rollback {name} pointer staging changed and could not be preserved"
                ) from exc
            raise TransactionError(f"rollback {name} pointer staging changed concurrently")
        _fsync_dir(path.parent)
    elif kind in {"directory", "file"}:
        backup = Path(str(pointer.get("priorBackupPath") or ""))
        if not backup.exists() and not backup.is_symlink():
            raise TransactionError(f"rollback backup is missing for prior {name} {kind}")
        _rename_exclusive(backup, path)
        _fsync_dir(path.parent)
    elif kind != "missing":
        raise TransactionError(f"unsupported prior {name} pointer kind: {kind}")
    pointer["promotionStarted"] = False
    pointer["promoted"] = False
    pointer["rollbackComplete"] = True


def _promote_pointer(state_path: Path, state: dict[str, Any], name: str) -> None:
    pointer = state[name]
    path = Path(pointer["path"])
    candidate_text = pointer.get("candidateTarget")
    if not candidate_text:
        raise TransactionError(f"{name} candidate was not recorded")
    candidate = Path(candidate_text)
    if not candidate.exists():
        raise TransactionError(f"{name} candidate does not exist")
    prior_kind = pointer["priorKind"]
    if name == "venv":
        _verify_dependency_profile_binding(state)
    if prior_kind == "symlink" and (
        not path.is_symlink() or os.readlink(path) != pointer.get("priorRawTarget")
    ):
        raise TransactionError(f"{name} pointer changed before promotion")
    pointer["promotionStarted"] = True
    pointer["rollbackComplete"] = False
    if prior_kind in {"directory", "file"}:
        backup = state_path.parent / "pointer-backups" / f"prior-{name}"
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if backup.exists() or backup.is_symlink():
            raise TransactionError(f"prior {name} pointer backup already exists")
        pointer["priorBackupPath"] = str(backup)
    # Persist rollback intent and the deterministic concrete backup path before
    # the first rename. Recovery can now compensate every crash window.
    _save_state(state_path, state, event=f"{name}-promotion-armed")
    _maybe_test_fail(f"{name}-promotion-armed")
    if prior_kind in {"directory", "file"}:
        backup = Path(pointer["priorBackupPath"])
        _rename_exclusive(path, backup)
        _fsync_dir(path.parent)
        _fsync_dir(backup.parent)
        _maybe_test_fail(f"{name}-prior-moved-before-journal")
        _save_state(state_path, state, event=f"prior-{name}-moved")
    candidate_raw_target = _candidate_pointer_raw_target(
        state,
        name=name,
        path=path,
        candidate=candidate,
    )
    tmp = state_path.parent / f".next-{name}"
    _prepare_transaction_symlink(tmp, candidate_raw_target)
    if prior_kind == "symlink":
        _rename_swap(tmp, path)
        if (
            not tmp.is_symlink()
            or os.readlink(tmp) != pointer.get("priorRawTarget")
            or not _pointer_matches_candidate(path, candidate_raw_target, candidate)
        ):
            try:
                _rename_swap(tmp, path)
            except TransactionError as exc:
                raise TransactionError(
                    f"{name} pointer changed during promotion and could not be restored"
                ) from exc
            raise TransactionError(f"{name} pointer changed concurrently during promotion")
    else:
        _rename_exclusive(tmp, path)
        if not _pointer_matches_candidate(path, candidate_raw_target, candidate):
            try:
                _rename_exclusive(path, tmp)
            except TransactionError as exc:
                raise TransactionError(
                    f"{name} pointer staging changed and could not be preserved"
                ) from exc
            raise TransactionError(f"{name} pointer staging changed concurrently")
    _fsync_dir(path.parent)
    _maybe_test_fail(f"{name}-pointer-replaced-before-journal")
    pointer["promoted"] = True
    _save_state(state_path, state, event=f"{name}-promoted")


def _release_lock(state: dict[str, Any]) -> None:
    lock = Path(state["lockPath"])
    if not lock.exists() and not lock.is_symlink():
        return
    tx_id = str(state.get("txId") or "")
    runtime = Path(state["runtime"])
    journal = runtime / "app" / "update-transactions" / tx_id / "journal.json"
    owner_path = journal.parent / "owner.json"
    if lock.is_symlink() or owner_path.is_symlink() or not owner_path.is_file():
        raise TransactionError("update transaction lock owner pairing is unsafe")
    try:
        current = json.loads(lock.read_text(encoding="utf-8"))
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        lock_stat = lock.stat(follow_symlinks=False)
        owner_stat = owner_path.stat(follow_symlinks=False)
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("update transaction lock owner pairing is unreadable") from exc
    expected = {
        "txId": tx_id,
        "journal": str(journal),
        "ownerPid": int(state.get("ownerPid") or 0),
    }
    if not state.get("legacySchemaVersion"):
        expected["ownerProcessIdentity"] = state.get("ownerProcessIdentity")
    if (
        current != expected
        or owner != expected
        or lock_stat.st_dev != owner_stat.st_dev
        or lock_stat.st_ino != owner_stat.st_ino
    ):
        raise TransactionError("update transaction lock owner pairing changed")
    lock.unlink()
    _fsync_dir(lock.parent)


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _darwin_process_metadata(pid: int) -> tuple[int, int, int, int] | None:
    try:
        import ctypes

        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.proc_pidinfo.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        result = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if result == size and info.pbi_pid == pid and info.pbi_start_tvsec:
            return (
                int(info.pbi_ppid),
                int(info.pbi_uid),
                int(info.pbi_start_tvsec),
                int(info.pbi_start_tvusec),
            )
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    return None


def _process_identity(pid: int) -> str | None:
    if not _pid_alive(pid):
        return None
    if sys.platform == "darwin":
        metadata = _darwin_process_metadata(pid)
        if metadata is None:
            return None
        _parent_pid, uid, start_seconds, start_microseconds = metadata
        return f"darwin-proc-start:{uid}:{start_seconds}:{start_microseconds}"
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
    if result.returncode != 0 or not value:
        return None
    return f"ps-lstart:{value}"


def _process_parent_pid(pid: int) -> int | None:
    if pid <= 1:
        return None
    if sys.platform == "darwin":
        metadata = _darwin_process_metadata(pid)
        return metadata[0] if metadata is not None else None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        if close > 0 and len(fields) > 1:
            return int(fields[1])
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    try:
        return int(value) if result.returncode == 0 and value else None
    except ValueError:
        return None


def _same_process(pid: int, expected_identity: object) -> bool:
    return (
        isinstance(expected_identity, str)
        and bool(expected_identity)
        and _process_identity(pid) == expected_identity
    )


def _is_descendant_of_owner(owner_pid: int, expected_identity: object) -> bool:
    if owner_pid <= 1 or not _same_process(owner_pid, expected_identity):
        return False
    current = os.getpid()
    # zsh may exec the final external command in a script in place.  The exact
    # PID/start-time identity still proves this is the recorded owner process.
    if current == owner_pid:
        return True
    visited: set[int] = set()
    for _depth in range(64):
        parent = _process_parent_pid(current)
        if parent is None or parent <= 1 or parent in visited:
            return False
        if parent == owner_pid:
            return _same_process(owner_pid, expected_identity)
        visited.add(parent)
        current = parent
    return False


def _candidate_child_term_timeout_seconds() -> float:
    if os.environ.get("ACTANARA_INSTALL_TEST_MODE") != "1":
        return CANDIDATE_CHILD_TERM_TIMEOUT_SECONDS
    try:
        configured = float(
            os.environ.get("ACTANARA_INSTALL_TEST_CHILD_TERM_TIMEOUT_SECONDS", "0.5")
        )
    except ValueError:
        configured = 0.5
    return max(0.1, min(configured, CANDIDATE_CHILD_TERM_TIMEOUT_SECONDS))


def _terminate_candidate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _candidate_child_term_timeout_seconds()
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _candidate_process_table() -> dict[int, dict[str, Any]]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError("candidate process provenance could not be inspected") from exc
    if result.returncode != 0:
        raise TransactionError("candidate process provenance probe failed")
    table: dict[int, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = (int(parts[index]) for index in range(3))
            session_id = os.getsid(pid)
        except (OSError, ValueError):
            continue
        table[pid] = {
            "ppid": ppid,
            "pgid": pgid,
            "sessionId": session_id,
            "command": parts[3],
        }
    return table


def _recorded_candidate_group_members(
    state: dict[str, Any], record: dict[str, Any]
) -> list[int]:
    pid = int(record.get("pid") or 0)
    pgid = int(record.get("processGroupId") or 0)
    if pid <= 1 or pgid != pid:
        raise TransactionError("recorded candidate process group is invalid")
    table = _candidate_process_table()
    members = {member_pid for member_pid, row in table.items() if row["pgid"] == pgid}
    if not members:
        return []
    if pid in table and not _same_process(pid, record.get("processIdentity")):
        raise TransactionError("recorded candidate process identity changed")
    if any(int(table[member_pid]["sessionId"]) != pgid for member_pid in members):
        raise TransactionError("recorded candidate process group escaped its dedicated session")
    root = table.get(pid)
    if root is None:
        return sorted(members)
    for member_pid in members:
        current = member_pid
        visited: set[int] = set()
        while current != pid:
            if current in visited or current not in members:
                raise TransactionError("recorded candidate process group has unrelated members")
            visited.add(current)
            current = int(table[current]["ppid"])
    return sorted(members)


def _stop_recorded_candidate_command(
    state_path: Path,
    state: dict[str, Any],
    *,
    event: str = "candidate-command-recovered",
) -> dict[str, Any]:
    record = state.get("candidateCommand")
    if not isinstance(record, dict):
        return state
    pgid = int(record.get("processGroupId") or 0)
    members = _recorded_candidate_group_members(state, record)
    if members:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _candidate_child_term_timeout_seconds()
        while time.monotonic() < deadline:
            if not _recorded_candidate_group_members(state, record):
                break
            time.sleep(0.05)
        remaining = _recorded_candidate_group_members(state, record)
        if remaining:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if not _recorded_candidate_group_members(state, record):
                    break
                time.sleep(0.05)
        if _recorded_candidate_group_members(state, record):
            raise TransactionError("recorded candidate process group did not exit")
    latest = _load_state(state_path)
    current = latest.get("candidateCommand")
    if (
        isinstance(current, dict)
        and int(current.get("pid") or 0) == int(record.get("pid") or 0)
        and current.get("processIdentity") == record.get("processIdentity")
    ):
        latest["candidateCommand"] = None
        _save_state(state_path, latest, event=event)
    return _load_state(state_path)


def run_candidate_child(args: argparse.Namespace) -> int:
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return 125
    gate_fd = int(args.gate_fd)
    try:
        token = os.read(gate_fd, 1)
    finally:
        os.close(gate_fd)
    if token != b"1":
        return 125
    os.execvpe(command[0], command, os.environ.copy())
    return 125


def run_candidate_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    phase = str(args.phase or "")
    if not SAFE_TX_ID_RE.fullmatch(phase):
        raise TransactionError("candidate command phase contains unsafe characters")
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise TransactionError("candidate command is missing")
    owner_pid = int(state.get("ownerPid") or 0)
    if not _is_descendant_of_owner(owner_pid, state.get("ownerProcessIdentity")):
        raise TransactionError("candidate command is not owned by the active installer process")
    read_fd, write_fd = os.pipe()
    process: subprocess.Popen[Any] | None = None
    owner_disappeared = False
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run-candidate-child",
                "--gate-fd",
                str(read_fd),
                "--",
                *command,
            ],
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        os.close(read_fd)
        read_fd = -1
        _maybe_test_fail("candidate-command-started-before-journal")
        state = _load_state(state_path)
        state["candidateCommand"] = {
            "phase": phase,
            "pid": process.pid,
            "processGroupId": process.pid,
            "processIdentity": _process_identity(process.pid),
            "startedAt": _now(),
        }
        if state["candidateCommand"]["processIdentity"] is None:
            _terminate_candidate_process_group(process)
            raise TransactionError("candidate process identity could not be captured")
        _save_state(state_path, state, event=f"candidate-command-started:{phase}")
        os.write(write_fd, b"1")
        os.close(write_fd)
        write_fd = -1
        _maybe_test_fail("candidate-command-released")
        while process.poll() is None:
            if (
                os.getppid() != owner_pid
                or not _same_process(owner_pid, state.get("ownerProcessIdentity"))
            ):
                owner_disappeared = True
                _terminate_candidate_process_group(process)
                break
            time.sleep(0.05)
        return_code = process.poll()
        if return_code is None:
            return_code = process.wait()
    except OSError as exc:
        raise TransactionError(f"candidate command could not start for phase {phase}") from exc
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            latest = _load_state(state_path)
            current = latest.get("candidateCommand")
            if isinstance(current, dict) and int(current.get("pid") or 0) == process.pid:
                event = (
                    f"candidate-command-owner-exited:{phase}"
                    if owner_disappeared
                    else f"candidate-command-finished:{phase}"
                )
                _stop_recorded_candidate_command(state_path, latest, event=event)
            elif process.poll() is None:
                _terminate_candidate_process_group(process)
    if process is None:
        raise TransactionError(f"candidate command could not start for phase {phase}")
    if owner_disappeared:
        return 143
    if return_code < 0:
        return 128 + abs(return_code)
    return int(return_code)


def _create_transaction_command_lock(transaction_dir: Path) -> dict[str, Any]:
    lock_path = transaction_dir / "command.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise TransactionError("update transaction command lock could not be created safely")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fsync_dir(transaction_dir)
        return {
            "path": str(lock_path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        }
    finally:
        os.close(descriptor)


def _acquire_transaction_command_lock(state_path: Path) -> int:
    state = _load_state(state_path)
    lock_path = state_path.parent / "command.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise TransactionError("update transaction command lock is unsafe")
    flags = os.O_RDWR
    if state.get("legacySchemaVersion"):
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileNotFoundError as exc:
        raise TransactionError("update transaction command lock is missing") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise TransactionError("update transaction command lock identity is unsafe")
        expected = state.get("commandLockIdentity")
        if not state.get("legacySchemaVersion") and (
            not isinstance(expected, dict)
            or metadata.st_dev != expected.get("device")
            or metadata.st_ino != expected.get("inode")
        ):
            raise TransactionError("update transaction command lock identity changed")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransactionError("another update transaction helper command is still active") from exc
        os.fchmod(descriptor, 0o600)
        _fsync_dir(lock_path.parent)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _release_transaction_command_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _has_inherited_runtime_mutation_guard(state: dict[str, Any]) -> bool:
    """Authenticate a resumed Linux repair through the inherited user guard."""

    if state.get("platform") != "Linux":
        return False
    raw = os.environ.get("ACTANARA_RUNTIME_MUTATION_GUARD_FD", "")
    try:
        descriptor = int(raw)
        metadata = os.fstat(descriptor)
    except (TypeError, ValueError, OSError):
        return False
    expected = (
        Path(tempfile.gettempdir())
        / f"actanara-runtime-mutation-{os.getuid()}"
        / ".runtime-mutation.guard"
    )
    try:
        expected_metadata = expected.stat(follow_symlinks=False)
    except OSError:
        return False
    if (
        descriptor < 3
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (metadata.st_dev, metadata.st_ino)
        != (expected_metadata.st_dev, expected_metadata.st_ino)
    ):
        return False
    try:
        # A duplicate inherited file description can reacquire its own flock;
        # an unrelated descriptor opened after the parent locked it cannot.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _require_owner_caller(
    state: dict[str, Any],
    *,
    command: str | None = None,
) -> None:
    owner_pid = int(state.get("ownerPid") or 0)
    if _is_descendant_of_owner(owner_pid, state.get("ownerProcessIdentity")):
        return
    repair_resume = (
        command in {"complete-repair", "restore-repair-services"}
        and state.get("mode") == "repair"
        and state.get("status") == "committed"
        and state.get("repairConfigurationComplete") is not True
        and _has_inherited_runtime_mutation_guard(state)
    )
    if repair_resume:
        return
    raise TransactionError(
        "update transaction helper command is not descended from the active installer process"
    )


def _set_active_command(state_path: Path, command: str | None) -> None:
    state = _load_state(state_path)
    if command is None:
        if int(state.get("activeCommandPid") or 0) not in {0, os.getpid()}:
            return
        state["activeCommandPid"] = None
        state["activeCommandProcessIdentity"] = None
        state["activeCommand"] = None
        state["activeCommandStartedAt"] = None
        _save_state(state_path, state, event="helper-command-cleared")
        return
    active_pid = int(state.get("activeCommandPid") or 0)
    if (
        active_pid
        and active_pid != os.getpid()
        and _same_process(active_pid, state.get("activeCommandProcessIdentity"))
    ):
        raise TransactionError("another update transaction helper command is still active")
    state["activeCommandPid"] = os.getpid()
    state["activeCommandProcessIdentity"] = _process_identity(os.getpid())
    if state["activeCommandProcessIdentity"] is None:
        raise TransactionError("update helper process identity could not be captured")
    state["activeCommand"] = command
    state["activeCommandStartedAt"] = _now()
    _save_state(state_path, state, event=f"helper-command:{command}")


def begin(args: argparse.Namespace) -> int:
    runtime_input = Path(args.runtime).expanduser().absolute()
    home_input = Path(args.home).expanduser().absolute()
    try:
        runtime = runtime_input.resolve(strict=True)
        home = home_input.resolve(strict=True)
    except OSError as exc:
        raise TransactionError("update Runtime or HOME is unavailable") from exc
    if not SAFE_TX_ID_RE.fullmatch(args.tx_id) or args.tx_id in {".", ".."}:
        raise TransactionError("update transaction id contains unsafe characters")
    expected_systemd_hashes: dict[str, str] = {}
    for value in args.expected_systemd_unit_sha256:
        name, separator, expected_sha256 = str(value).partition("=")
        if (
            separator != "="
            or not SYSTEMD_UNIT_NAME_RE.fullmatch(name)
            or not SHA256_RE.fullmatch(expected_sha256)
            or name in expected_systemd_hashes
        ):
            raise TransactionError("expected systemd unit definition hash is invalid")
        expected_systemd_hashes[name] = expected_sha256
    active_profile_evidence_values = (
        args.expected_active_venv_target,
        args.expected_active_marker_status,
        args.expected_active_marker_sha256,
    )
    active_profile_evidence_requested = any(
        value is not None for value in active_profile_evidence_values
    )
    settings_only_profile_evidence = bool(args.settings_only_profile_evidence)
    profile_evidence_requested = (
        args.expected_settings_sha256 is not None
        or active_profile_evidence_requested
        or settings_only_profile_evidence
    )
    if settings_only_profile_evidence:
        if (
            args.mode not in {"upgrade", "repair"}
            or active_profile_evidence_requested
            or not isinstance(args.expected_settings_sha256, str)
            or not SHA256_RE.fullmatch(args.expected_settings_sha256)
        ):
            raise TransactionError("settings-only dependency profile evidence is invalid")
    elif profile_evidence_requested:
        if (
            not isinstance(args.expected_settings_sha256, str)
            or not SHA256_RE.fullmatch(args.expected_settings_sha256)
            or not isinstance(args.expected_active_venv_target, str)
            or not Path(args.expected_active_venv_target).is_absolute()
            or any(
                character in args.expected_active_venv_target
                for character in "\0\r\n\t"
            )
            or args.expected_active_marker_status not in {"missing", "trusted"}
        ):
            raise TransactionError("dependency profile evidence arguments are invalid")
        if args.expected_active_marker_status == "trusted":
            if (
                not isinstance(args.expected_active_marker_sha256, str)
                or not SHA256_RE.fullmatch(args.expected_active_marker_sha256)
            ):
                raise TransactionError("trusted dependency profile marker hash is invalid")
        elif args.expected_active_marker_sha256 is not None:
            raise TransactionError("missing dependency profile marker cannot have a hash")
    owner_process_identity = _process_identity(args.owner_pid)
    if not _is_descendant_of_owner(args.owner_pid, owner_process_identity):
        raise TransactionError(
            "update transaction owner must be an ancestor installer process"
        )
    if owner_process_identity is None:
        raise TransactionError("update transaction owner process identity is unavailable")
    source_pointer = Path(args.source_pointer).expanduser().absolute()
    venv_pointer = Path(args.venv_pointer).expanduser().absolute()
    if source_pointer.name != "source" or source_pointer.parent.resolve(strict=False) != runtime / "app":
        raise TransactionError("source pointer must be the managed Runtime app/source path")
    if venv_pointer.name != ".venv" or venv_pointer.parent.resolve(strict=False) != runtime:
        raise TransactionError("venv pointer must be the managed Runtime .venv path")
    source_pointer = runtime / "app" / "source"
    venv_pointer = runtime / ".venv"
    managed_directories = (
        runtime / "app",
        runtime / "app" / "releases",
        runtime / "app" / "venvs",
        runtime / "app" / "update-transactions",
        runtime / "config",
        runtime / "data",
    )
    for managed in managed_directories:
        _require_managed_directory(managed, runtime)
        managed.mkdir(parents=True, exist_ok=True, mode=0o700)
    repair_pending = runtime / "app" / REPAIR_CONFIGURATION_PENDING_NAME
    try:
        _read_repair_configuration_pending(repair_pending)
    except FileNotFoundError:
        pass
    else:
        raise TransactionError(
            "a committed Runtime repair is pending; resume its existing journal"
        )
    tx_root = runtime / "app" / "update-transactions"
    os.chmod(tx_root, 0o700)
    lock = runtime / "app" / ".update-transaction.lock"
    tx_id = args.tx_id
    tx_dir = tx_root / tx_id
    lock_owned = False
    tx_dir_created = False
    tx_dir_identity: tuple[int, int] | None = None
    owner_path: Path | None = None
    try:
        tx_dir.mkdir(mode=0o700)
        tx_dir_created = True
        tx_metadata = tx_dir.stat(follow_symlinks=False)
        tx_dir_identity = (tx_metadata.st_dev, tx_metadata.st_ino)
        state_path = tx_dir / "journal.json"
        command_lock_identity = _create_transaction_command_lock(tx_dir)
        if args.platform not in {"Darwin", "Linux"}:
            raise TransactionError("update transaction platform is unsupported")
        systemd_config_home = (
            _systemd_config_home(home)
            if args.platform == "Linux"
            else home / ".config"
        )
        launchctl = args.launchctl
        systemctl = ""
        systemctl_identity: dict[str, Any] | None = None
        if args.platform == "Darwin":
            if not launchctl:
                raise TransactionError("launchctl is required for a macOS update transaction")
            if args.systemd_unit or expected_systemd_hashes:
                raise TransactionError("macOS update transaction cannot contain systemd units")
        else:
            if not set(expected_systemd_hashes).issubset(set(args.systemd_unit)):
                raise TransactionError(
                    "expected systemd definition hash has no managed unit binding"
                )
            if args.systemd_unit:
                systemctl, systemctl_identity = _bound_executable(
                    args.systemctl,
                    label="systemctl",
                )
            elif args.systemctl:
                raise TransactionError("systemctl was provided without a managed unit inventory")
        domain = f"gui/{args.uid}"
        artifact_paths = {
            "source": runtime / "app" / "releases" / tx_id,
            "source-temp": runtime / "app" / "releases" / f".tmp-{tx_id}",
            "venv": runtime / "app" / "venvs" / tx_id,
            "validation-runtime": tx_dir / "candidate-runtime",
        }
        for artifact_path in artifact_paths.values():
            if artifact_path.exists() or artifact_path.is_symlink():
                raise TransactionError("candidate artifact reserved path already exists")
        state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "txId": tx_id,
            "mode": args.mode,
            "ownerPid": args.owner_pid,
            "ownerProcessIdentity": owner_process_identity,
            "createdAt": _now(),
            "updatedAt": _now(),
            "status": "initializing",
            "phase": "begin",
            "runtime": str(runtime),
            "runtimeAliases": list(dict.fromkeys((str(runtime), str(runtime_input)))),
            "home": str(home),
            "platform": args.platform,
            "launchctl": launchctl,
            "domain": domain,
            "systemctl": systemctl,
            "systemctlIdentity": systemctl_identity,
            "systemdConfigHome": str(systemd_config_home),
            "systemdUnitRoot": str(_systemd_unit_root(home, systemd_config_home)),
            "systemdUnits": [],
            "lockPath": str(lock),
            "commandLockIdentity": command_lock_identity,
            "source": _pointer_state(source_pointer),
            "venv": _pointer_state(venv_pointer),
            "files": [],
            "services": [],
            "settingsSummary": {
                "dashboardHost": None,
                "dashboardPort": None,
                "dashboardHealthPath": None,
                "ragHost": None,
                "ragPort": None,
                "ragHealthPath": None,
            },
            "dependencyProfileBinding": None,
            "dependencyProfileEvidence": None,
            "serviceStopInitiated": False,
            "rollbackErrors": [],
            "activeCommandPid": None,
            "activeCommandProcessIdentity": None,
            "activeCommand": None,
            "activeCommandStartedAt": None,
            "candidateCommand": None,
            "databaseCompatibility": None,
            "sourceCandidateReady": False,
            "venvCandidateReady": False,
            "candidateArtifacts": [
                _candidate_artifact_state(kind, artifact_path)
                for kind, artifact_path in artifact_paths.items()
            ],
            "managedDirectoryIdentities": {
                key: _directory_identity(path)
                for key, path in _expected_bound_directories(runtime, tx_id).items()
            },
        }
        # The journal and complete owner record exist before the atomic hard-link
        # lock. A crash can leave an orphan journal, but never an unrecoverable
        # lock that points at a missing journal.
        _save_state(state_path, state, event="initializing")
        owner_path = tx_dir / "owner.json"
        _atomic_json(
            owner_path,
            {
                "txId": tx_id,
                "journal": str(state_path),
                "ownerPid": args.owner_pid,
                "ownerProcessIdentity": owner_process_identity,
            },
        )
        try:
            os.link(owner_path, lock)
        except FileExistsError as exc:
            raise TransactionError("another update transaction is active or requires recovery") from exc
        lock_owned = True
        _fsync_dir(lock.parent)
        _save_state(state_path, state, event="lock-acquired")

        # Re-capture pointers after the lock is held; only this state may be used
        # for a later promotion or recovery decision.
        state["source"] = _pointer_state(source_pointer)
        state["venv"] = _pointer_state(venv_pointer)
        if settings_only_profile_evidence:
            state["dependencyProfileBinding"] = _untrusted_venv_pointer_binding(
                venv_pointer
            )
        elif active_profile_evidence_requested:
            profile_binding = _active_dependency_profile_binding(venv_pointer)
            if (
                profile_binding.get("targetPath")
                != str(Path(args.expected_active_venv_target).resolve(strict=False))
                or profile_binding.get("markerStatus")
                != args.expected_active_marker_status
                or profile_binding.get("dependencyMarkerSha256")
                != args.expected_active_marker_sha256
            ):
                raise TransactionError(
                    "active dependency profile evidence changed before transaction begin"
                )
            state["dependencyProfileBinding"] = profile_binding
        if args.mode == "source-only":
            state["venv"]["activeReuseBinding"] = _active_venv_reuse_binding(
                venv_pointer
            )
        state["status"] = "preparing"
        if args.mode == "repair":
            repair_pending = runtime / "app" / REPAIR_CONFIGURATION_PENDING_NAME
            try:
                _read_repair_configuration_pending(repair_pending)
            except FileNotFoundError:
                pass
            _snapshot_path(
                state_path,
                state,
                "repair-configuration-pending",
                repair_pending,
            )
            pending_snapshot = state["files"][-1]
            if (
                pending_snapshot.get("key") != "repair-configuration-pending"
                or pending_snapshot.get("kind") not in {"missing", "file"}
                or not _file_matches_snapshot(pending_snapshot)
            ):
                raise TransactionError("repair configuration marker snapshot is unsafe")
            if pending_snapshot["kind"] == "file":
                current_pending, _metadata = _read_repair_configuration_pending(
                    repair_pending
                )
                backup_pending, _metadata = _read_repair_configuration_pending(
                    Path(str(pending_snapshot.get("backupPath") or ""))
                )
                if current_pending != backup_pending:
                    raise TransactionError("repair configuration marker changed during capture")
        _snapshot_path(state_path, state, "settings", runtime / "config" / "settings.json")
        _snapshot_path(state_path, state, "runtime-manifest", runtime / "config" / "runtime.json")
        if profile_evidence_requested:
            settings_records = [
                item for item in state["files"] if item.get("key") == "settings"
            ]
            if (
                len(settings_records) != 1
                or settings_records[0].get("kind") != "file"
                or settings_records[0].get("sha256") != args.expected_settings_sha256
            ):
                raise TransactionError(
                    "Runtime Settings changed after dependency profile selection"
                )
            state["dependencyProfileEvidence"] = {
                "settingsSha256": args.expected_settings_sha256,
                "activeVenvTarget": (
                    None if settings_only_profile_evidence else args.expected_active_venv_target
                ),
                "activeMarkerStatus": (
                    "unavailable"
                    if settings_only_profile_evidence
                    else args.expected_active_marker_status
                ),
                "activeMarkerSha256": (
                    None
                    if settings_only_profile_evidence
                    else args.expected_active_marker_sha256
                ),
            }
        _save_state(state_path, state, event="control-state-captured")
        services: list[dict[str, Any]] = []
        settings_summary: dict[str, Any] = {
            "dashboardHost": None,
            "dashboardPort": None,
            "dashboardHealthPath": None,
            "ragHost": None,
            "ragPort": None,
            "ragHealthPath": None,
        }
        if args.platform == "Darwin":
            launch_agents_root = home / "Library" / "LaunchAgents"
            if (
                launch_agents_root.exists()
                and (
                    launch_agents_root.is_symlink()
                    or not launch_agents_root.is_dir()
                    or not _is_within(launch_agents_root, home)
                )
            ):
                raise TransactionError("LaunchAgents directory escaped the selected HOME")
            inventory, settings_summary = _discover_services(runtime, home)
            for kind, label, plist_path in inventory:
                if not _is_within(plist_path, launch_agents_root):
                    raise TransactionError(f"managed plist path escaped LaunchAgents for label {label}")
                if plist_path.is_file() and not (
                    _plist_targets_runtime(plist_path, runtime, label)
                    or (
                        args.mode == "repair"
                        and _legacy_repair_plist_targets_runtime(
                            plist_path, runtime, label
                        )
                    )
                ):
                    raise TransactionError(
                        f"managed service plist provenance does not match the selected Runtime: {label}"
                    )
                if plist_path.is_file():
                    try:
                        plist_payload = plistlib.loads(plist_path.read_bytes())
                    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
                        raise TransactionError(f"managed service plist is unreadable: {label}") from exc
                    environment = (
                        plist_payload.get("EnvironmentVariables")
                        if isinstance(plist_payload, dict)
                        else None
                    )
                    actanara_home = environment.get("ACTANARA_HOME") if isinstance(environment, dict) else None
                    if isinstance(actanara_home, str) and actanara_home:
                        alias = Path(actanara_home)
                        if (
                            not alias.is_absolute()
                            or os.path.normpath(actanara_home) != actanara_home
                            or alias.resolve(strict=False) != runtime
                        ):
                            raise TransactionError(
                                f"managed service plist has an unsafe Runtime alias: {label}"
                            )
                        aliases = state["runtimeAliases"]
                        if actanara_home not in aliases:
                            if len(aliases) >= 4:
                                raise TransactionError("managed service Runtime alias inventory is too large")
                            aliases.append(actanara_home)
                _snapshot_path(state_path, state, "managed-plist", plist_path)
                loaded, launch_state = _launch_state(launchctl, domain, label)
                if loaded and not plist_path.is_file():
                    raise TransactionError(
                        f"managed service is loaded but its selected Runtime plist is missing: {label}"
                    )
                if kind.startswith("scheduler-") and loaded and launch_state in RUNNING_STATES:
                    raise TransactionError(
                        f"managed scheduler job is currently running; update refused before service stop: {label}"
                    )
                if (
                    args.mode != "repair"
                    and kind in {"dashboard", "rag"}
                    and loaded
                    and launch_state not in RUNNING_STATES
                ):
                    raise TransactionError(
                        f"managed {kind} service is loaded but not running; repair or unload it before update: {label}"
                    )
                services.append(
                    {
                        "kind": kind,
                        "label": label,
                        "plistPath": str(plist_path.absolute()),
                        "plistExisted": plist_path.is_file(),
                        "loaded": loaded,
                        "state": launch_state,
                    }
                )
        elif args.systemd_unit:
            unit_root = Path(state["systemdUnitRoot"])
            if (
                unit_root.exists()
                and (
                    unit_root.is_symlink()
                    or not unit_root.is_dir()
                    or unit_root.resolve(strict=False) != unit_root
                    or not _is_within(unit_root, systemd_config_home)
                )
            ):
                raise TransactionError("systemd user-unit root is unsafe")
            names = list(args.systemd_unit)
            if len(names) != len(set(names)):
                raise TransactionError("managed systemd unit inventory contains duplicates")
            for name in names:
                if not SYSTEMD_UNIT_NAME_RE.fullmatch(name):
                    raise TransactionError("managed systemd unit name is unsafe")
                unit_path = unit_root / name
                if unit_path.is_symlink() or (
                    unit_path.exists() and not unit_path.is_file()
                ):
                    raise TransactionError(f"managed systemd unit definition is unsafe: {name}")
                if unit_path.is_file():
                    try:
                        lines = unit_path.read_text(encoding="utf-8").splitlines()
                        first_line = lines[0] if lines else ""
                        metadata = unit_path.stat(follow_symlinks=False)
                    except (OSError, UnicodeDecodeError) as exc:
                        raise TransactionError(
                            f"managed systemd unit definition is unreadable: {name}"
                        ) from exc
                    if (
                        first_line != SYSTEMD_MANAGED_UNIT_HEADER
                        or metadata.st_nlink != 1
                        or metadata.st_uid != os.getuid()
                    ):
                        raise TransactionError(
                            f"refusing non-Actanara systemd unit definition: {name}"
                        )
                expected_definition_sha256 = expected_systemd_hashes.get(name)
                if expected_definition_sha256 is not None and (
                    not unit_path.is_file()
                    or _sha256(unit_path) != expected_definition_sha256
                ):
                    raise TransactionError(
                        f"managed systemd unit definition does not match the update contract: {name}"
                    )
                _snapshot_path(
                    state_path,
                    state,
                    "managed-systemd-unit",
                    unit_path,
                )
                record = {
                    "name": name,
                    "unitPath": str(unit_path.absolute()),
                    "definitionExisted": unit_path.is_file(),
                    "expectedDefinitionSha256": expected_definition_sha256,
                    "enableState": "not-found",
                    "activeState": "not-found",
                    "active": False,
                    "stoppedByTransaction": False,
                }
                state["systemdUnits"].append(record)
                enable_state, active_state = _systemd_probe_state(state, name)
                record["enableState"] = enable_state
                record["activeState"] = active_state
                record["active"] = active_state == "active"
                if not record["definitionExisted"] and (
                    enable_state != "not-found" or active_state not in {"not-found", "inactive"}
                ):
                    raise TransactionError(
                        f"systemd reports an unmanaged definition for selected unit: {name}"
                    )
                if active_state in {"activating", "deactivating", "reloading"}:
                    raise TransactionError(
                        f"managed systemd unit state is unstable before update: {name}"
                    )
                if args.mode != "repair" and active_state == "failed":
                    raise TransactionError(
                        f"managed systemd unit is failed; repair it before upgrade: {name}"
                    )
        state["services"] = services
        state["settingsSummary"] = settings_summary
        _verify_critical_control_state(state)
        if args.platform == "Darwin" and args.mode != "repair":
            _verify_service_health(state)
        state["status"] = "prepared"
        _save_state(state_path, state, event="prior-captured")
    except Exception:
        if lock_owned and owner_path is not None:
            try:
                current = json.loads(lock.read_text(encoding="utf-8"))
                lock_metadata = lock.stat(follow_symlinks=False)
                owner_metadata = owner_path.stat(follow_symlinks=False)
            except (OSError, json.JSONDecodeError):
                current = {}
                lock_metadata = owner_metadata = None
            if (
                current.get("txId") == tx_id
                and lock_metadata is not None
                and owner_metadata is not None
                and lock_metadata.st_dev == owner_metadata.st_dev
                and lock_metadata.st_ino == owner_metadata.st_ino
            ):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
        if tx_dir_created and tx_dir_identity is not None:
            try:
                current_metadata = tx_dir.stat(follow_symlinks=False)
                if (
                    not tx_dir.is_symlink()
                    and current_metadata.st_dev == tx_dir_identity[0]
                    and current_metadata.st_ino == tx_dir_identity[1]
                ):
                    shutil.rmtree(tx_dir)
            except OSError:
                pass
        raise
    print(state_path)
    return 0


def reserve_candidate_artifact(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") not in {"prepared", "candidate-staged"}:
        raise TransactionError("candidate artifact reservation is not allowed in the current phase")
    record = _candidate_artifact_record(state, args.kind)
    if record.get("created") or record.get("transferred") or record.get("cleaned"):
        raise TransactionError("candidate artifact was already reserved or finalized")
    path = Path(record["path"])
    parent = path.parent
    _require_managed_directory(parent, Path(state["runtime"]))
    if not parent.is_dir():
        raise TransactionError("candidate artifact parent directory is unavailable")
    if not record.get("reservationStarted"):
        if path.exists() or path.is_symlink():
            raise TransactionError("candidate artifact reserved path already exists")
        _authorize_reservation_attempt(state_path, state, record)
        _maybe_test_fail(f"candidate-artifact-reservation-authorized-{args.kind}")

    for _attempt in range(65):
        state = _load_state(state_path)
        record = _candidate_artifact_record(state, args.kind)
        if path.exists() or path.is_symlink():
            if not _artifact_marker_matches(path, record):
                raise TransactionError("partially reserved candidate artifact has no valid owner marker")
            _record_artifact_identity(record, path)
            record["reservationAttemptNonce"] = None
            record["reservationStarted"] = False
            _fsync_dir(path)
            _fsync_dir(parent)
            _save_state(state_path, state, event=f"candidate-artifact-reserved:{args.kind}")
            print(path)
            return 0

        staging = _reservation_staging_path(state_path, record)
        if staging.exists() or staging.is_symlink():
            if not _artifact_marker_matches(staging, record):
                # The final managed path was never exposed.  Preserve this
                # unproven transaction-local attempt as crash evidence and use
                # a fresh nonce without deleting or trusting it.
                _abandon_reservation_attempt(state_path, state, record)
                _authorize_reservation_attempt(state_path, state, record)
                continue
        else:
            staging.mkdir(mode=0o700)
            _fsync_dir(staging.parent)
            _maybe_test_fail(f"candidate-artifact-staging-created-{args.kind}")
            marker = staging / ARTIFACT_MARKER_NAME
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(marker, flags, 0o600)
            try:
                marker_bytes = f"ON:{record['ownerNonce']}\n".encode("ascii")
                written = 0
                while written < len(marker_bytes):
                    count = os.write(descriptor, marker_bytes[written:])
                    if count <= 0:
                        raise TransactionError("candidate artifact owner marker write stalled")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_dir(staging)
            _maybe_test_fail(f"candidate-artifact-marker-created-{args.kind}")

        if path.exists() or path.is_symlink():
            raise TransactionError("candidate artifact reserved path was occupied concurrently")
        if not _artifact_marker_matches(staging, record):
            raise TransactionError("candidate artifact staging owner marker changed before promotion")
        _rename_exclusive(staging, path)
        _fsync_dir(staging.parent)
        _fsync_dir(parent)
        _maybe_test_fail(f"candidate-artifact-renamed-before-journal-{args.kind}")
        _record_artifact_identity(record, path)
        record["reservationAttemptNonce"] = None
        record["reservationStarted"] = False
        _fsync_dir(path)
        _save_state(state_path, state, event=f"candidate-artifact-reserved:{args.kind}")
        print(path)
        return 0
    raise TransactionError("candidate artifact reservation retry limit was exceeded")


def promote_source_artifact(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") not in {"prepared", "candidate-staged"}:
        raise TransactionError("source artifact promotion is not allowed in the current phase")
    temporary_record = _candidate_artifact_record(state, "source-temp")
    source_record = _candidate_artifact_record(state, "source")
    if temporary_record.get("transferred") or (
        source_record.get("created") and not source_record.get("transferStarted")
    ):
        raise TransactionError("candidate source artifact was already promoted")
    temporary = Path(temporary_record["path"])
    source = Path(source_record["path"])
    if not source_record.get("transferStarted"):
        temporary = _verify_artifact_ownership(temporary_record, require_marker=True)
        if source.exists() or source.is_symlink():
            raise TransactionError("candidate source target already exists")
        source_record["ownerNonce"] = temporary_record["ownerNonce"]
        source_record["created"] = True
        source_record["device"] = temporary_record["device"]
        source_record["inode"] = temporary_record["inode"]
        source_record["transferStarted"] = True
        _save_state(state_path, state, event="candidate-source-artifact-transfer-authorized")
        _maybe_test_fail("source-artifact-transfer-authorized")
    if source.exists() or source.is_symlink():
        if temporary.exists() or temporary.is_symlink():
            raise TransactionError("candidate source transfer has both source and temporary paths")
        _verify_artifact_ownership(source_record, require_marker=True)
    else:
        temporary = _verify_artifact_ownership(temporary_record, require_marker=True)
        _rename_exclusive(temporary, source)
    _fsync_dir(source.parent)
    _maybe_test_fail("source-artifact-renamed-before-journal")
    _record_artifact_identity(source_record, source)
    source_record["transferStarted"] = False
    temporary_record["created"] = False
    temporary_record["device"] = None
    temporary_record["inode"] = None
    temporary_record["transferred"] = True
    _save_state(state_path, state, event="candidate-source-artifact-promoted")
    print(source)
    return 0


def record_candidate(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") not in {"prepared", "candidate-staged"}:
        raise TransactionError("candidate recording is not allowed in the current transaction phase")
    pointer = state[args.kind]
    candidate = _lexical_absolute(Path(args.candidate).expanduser())
    expected = Path(state["runtime"]) / "app" / (
        "releases" if args.kind == "source" else "venvs"
    ) / state["txId"]
    if (
        candidate != expected
        or candidate.is_symlink()
        or not candidate.is_dir()
    ):
        raise TransactionError(f"recorded {args.kind} candidate is not the reserved transaction path")
    artifact = _candidate_artifact_record(state, args.kind)
    _verify_artifact_ownership(
        artifact,
        require_marker=not bool(artifact.get("markerRemoved")),
    )
    pointer["candidateTarget"] = str(candidate)
    if args.kind == "source":
        manifest = candidate / ".actanara-runtime-source.json"
        pyproject = candidate / "pyproject.toml"
        if (
            manifest.is_symlink()
            or pyproject.is_symlink()
            or not manifest.is_file()
            or not pyproject.is_file()
        ):
            raise TransactionError("staged source candidate failed manifest validation")
        pointer["candidateSha256"] = _sha256(manifest)
        marker = candidate / ARTIFACT_MARKER_NAME
        if not artifact.get("markerRemoved"):
            artifact["markerRemoved"] = True
            _save_state(
                state_path,
                state,
                event="candidate-artifact-marker-removal-authorized:source",
            )
            marker.unlink()
        elif marker.exists() or marker.is_symlink():
            if not _artifact_marker_matches(candidate, artifact):
                raise TransactionError("candidate source artifact owner marker changed")
            marker.unlink()
        _fsync_dir(candidate)
        _validate_source_payload(pointer)
        state["databaseCompatibility"] = None
        state["sourceCandidateReady"] = True
    else:
        python = candidate / "bin" / "python"
        if not python.is_file():
            raise TransactionError("staged venv candidate has no Python executable")
        pointer["dependencyMarkerSha256"] = _validate_dependency_marker(candidate)
        marker = candidate / ARTIFACT_MARKER_NAME
        if not artifact.get("markerRemoved"):
            artifact["markerRemoved"] = True
            _save_state(
                state_path,
                state,
                event="candidate-artifact-marker-removal-authorized:venv",
            )
            marker.unlink()
        elif marker.exists() or marker.is_symlink():
            if not _artifact_marker_matches(candidate, artifact):
                raise TransactionError("candidate venv artifact owner marker changed")
            marker.unlink()
        _fsync_dir(candidate)
        state["venvCandidateReady"] = True
    state["status"] = "candidate-staged"
    _save_state(state_path, state, event=f"{args.kind}-staged")
    return 0


def clean_candidate_source_build_artifacts(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "candidate-staged" or not state.get("sourceCandidateReady"):
        raise TransactionError("candidate build cleanup requires a recorded source candidate")
    source_record = _candidate_artifact_record(state, "source")
    source = _verify_artifact_ownership(source_record, require_marker=False)
    entries = list(source.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise TransactionError("candidate source build cleanup refused a symlinked payload")
    targets = {
        path
        for path in entries
        if path.is_dir()
        and (
            (path.parent == source and path.name in {"build", "dist"})
            or path.name == "__pycache__"
            or path.name.endswith(".egg-info")
        )
    }
    for target in sorted(targets, key=lambda item: len(item.parts), reverse=True):
        if not target.exists():
            continue
        if not _is_within(target, source) or target.is_symlink() or not target.is_dir():
            raise TransactionError("candidate source build cleanup target is unsafe")
        shutil.rmtree(target)
    _fsync_dir(source)
    _validate_source_payload(state["source"])
    _save_state(state_path, state, event="candidate-source-build-artifacts-cleaned")
    return 0


def verify_migration_compatibility(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "candidate-staged":
        raise TransactionError("migration compatibility requires a staged source candidate")
    if not state.get("sourceCandidateReady"):
        raise TransactionError("migration compatibility requires a validated source candidate")
    _verify_critical_control_state(state)
    pointer = state["source"]
    candidate = Path(str(pointer.get("candidateTarget") or ""))
    prior_text = str(pointer.get("priorResolvedTarget") or "")
    allow_legacy_repair = bool(getattr(args, "allow_legacy_repair", False))
    if allow_legacy_repair != (state.get("mode") == "repair"):
        raise TransactionError(
            "legacy migration compatibility is available only to repair transactions"
        )
    if not candidate.is_dir():
        raise TransactionError("migration compatibility requires a candidate source tree")
    prior = Path(prior_text) if prior_text else None
    if (prior is None or not prior.is_dir()) and not allow_legacy_repair:
        raise TransactionError("prior source tree is unavailable for migration compatibility")
    _verify_artifact_ownership(
        _candidate_artifact_record(state, "source"),
        require_marker=False,
    )
    _validate_source_payload(pointer)
    try:
        manifest = json.loads((candidate / ".actanara-runtime-source.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError("candidate source manifest is unreadable") from exc
    compatibility = _candidate_migration_contract(candidate, manifest)
    prior_records: list[dict[str, str]] = []
    if prior is not None and prior.is_dir():
        try:
            prior_records = _source_migration_inventory(prior)
        except TransactionError:
            if not allow_legacy_repair:
                raise
    prior_by_version = {record["version"]: record for record in prior_records}
    candidate_records = compatibility["migrations"]
    candidate_by_version = {record["version"]: record for record in candidate_records}

    for version, prior_record in prior_by_version.items():
        candidate_record = candidate_by_version.get(version)
        if candidate_record is None:
            raise TransactionError(f"candidate removed a prior migration: {version}")
        if candidate_record["sha256"] != prior_record["sha256"]:
            raise TransactionError(f"candidate rewrote a prior migration body: {version}")

    new_records = [
        record for record in candidate_records if record["version"] not in prior_by_version
    ]
    prior_maximum = max(prior_by_version) if prior_by_version else "unversioned"
    for record in new_records:
        if prior_by_version and record["version"] <= prior_maximum:
            raise TransactionError(
                f"candidate inserted a migration before the prior schema boundary: {record['version']}"
            )
        if not allow_legacy_repair and record["rollbackClass"] != "rollback-compatible-additive":
            raise TransactionError(
                f"candidate migration is not rollback-compatible additive: {record['version']}"
            )

    runtime = Path(state["runtime"])
    database_identity = _runtime_database_identity(runtime)
    applied_versions = _applied_migration_versions(runtime)
    if _runtime_database_identity(runtime) != database_identity:
        raise TransactionError("Runtime database identity changed during migration compatibility check")
    for version in applied_versions:
        if prior_by_version and not allow_legacy_repair and version not in prior_by_version:
            raise TransactionError(f"live database has a migration unknown to the prior source: {version}")
        if version not in candidate_by_version:
            raise TransactionError(f"live database has a migration unreadable by the candidate: {version}")

    prior_digest_records = [
        {
            "version": record["version"],
            "sha256": record["sha256"],
            "rollbackClass": "prior-source",
        }
        for record in prior_records
    ]
    prior_migration_set_sha256 = _migration_set_sha256(prior_digest_records)
    candidate_migration_set_sha256 = compatibility["migrationSetSha256"]
    state["databaseCompatibility"] = {
        "status": "verified",
        "policy": MIGRATION_POLICY,
        "preCommitWriterContract": PRE_COMMIT_WRITER_CONTRACT,
        "candidateManifestSha256": pointer.get("candidateSha256"),
        "priorMigrationSetSha256": prior_migration_set_sha256,
        "candidateMigrationSetSha256": candidate_migration_set_sha256,
        "priorReaderBindingSha256": _prior_reader_binding_sha256(
            prior_migration_set_sha256,
            candidate_migration_set_sha256,
        ),
        "priorMaximumSchema": prior_maximum,
        "candidateMaximumSchema": compatibility["maximumReadableSchema"],
        "databaseIdentity": database_identity,
        "appliedMigrations": applied_versions,
        "priorMigrations": sorted(prior_by_version),
        "candidateMigrations": sorted(candidate_by_version),
        "newMigrations": [record["version"] for record in new_records],
        "verifiedAt": _now(),
        "legacyRepair": allow_legacy_repair,
    }
    _save_state(state_path, state, event="migration-compatibility-verified")
    return 0


def _require_verified_migration_compatibility(state: dict[str, Any]) -> None:
    evidence = state.get("databaseCompatibility")
    pointer = state.get("source") if isinstance(state.get("source"), dict) else {}
    if state.get("legacySchemaVersion") and not isinstance(evidence, dict):
        raise TransactionError(
            "legacy update transaction crossed the service-stop boundary without migration compatibility evidence"
        )
    if (
        not isinstance(evidence, dict)
        or evidence.get("status") != "verified"
        or evidence.get("policy") != MIGRATION_POLICY
        or evidence.get("preCommitWriterContract") != PRE_COMMIT_WRITER_CONTRACT
        or evidence.get("candidateManifestSha256") != pointer.get("candidateSha256")
        or evidence.get("legacyRepair") is not (state.get("mode") == "repair")
        or evidence.get("priorReaderBindingSha256")
        != _prior_reader_binding_sha256(
            str(evidence.get("priorMigrationSetSha256") or ""),
            str(evidence.get("candidateMigrationSetSha256") or ""),
        )
    ):
        raise TransactionError("services cannot stop before migration compatibility is verified")


def _verify_live_migration_ledger(state: dict[str, Any]) -> None:
    _require_verified_migration_compatibility(state)
    evidence = state["databaseCompatibility"]
    initial = set(evidence.get("appliedMigrations") or [])
    prior = set(evidence.get("priorMigrations") or [])
    candidate = set(evidence.get("candidateMigrations") or [])
    declared_new = set(evidence.get("newMigrations") or [])
    runtime = Path(state["runtime"])
    prior_identity = evidence.get("databaseIdentity")
    if not isinstance(prior_identity, dict):
        raise TransactionError("migration compatibility evidence has no database identity")
    current_identity = _runtime_database_identity(runtime)
    if not _database_identity_matches_gate(prior_identity, current_identity):
        raise TransactionError("Runtime database identity changed after migration compatibility gate")
    if prior_identity.get("kind") == "missing" and current_identity.get("kind") == "file":
        evidence["databaseIdentity"] = current_identity
    current = set(_applied_migration_versions(runtime))
    if _runtime_database_identity(runtime) != current_identity:
        raise TransactionError("Runtime database identity changed while reading its migration ledger")
    if not initial.issubset(current):
        raise TransactionError("candidate removed entries from the live migration ledger")
    unexpected = current - candidate
    if unexpected:
        raise TransactionError(
            f"candidate wrote an undeclared live migration: {sorted(unexpected)[0]}"
        )
    incompatible = (current - prior) - declared_new
    if incompatible:
        raise TransactionError(
            f"candidate wrote a migration outside the prior-reader contract: {sorted(incompatible)[0]}"
        )


def capture_mutable(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "stopped" or state.get("mutableStateCaptured"):
        raise TransactionError("mutable-state capture requires exactly one completed service stop")
    _verify_critical_control_state(state)
    _verify_live_migration_ledger(state)
    runtime = Path(state["runtime"])
    paths = [
        ("settings", runtime / "config" / "settings.json"),
        ("runtime-manifest", runtime / "config" / "runtime.json"),
        ("location", Path(args.location).expanduser().absolute()),
        ("runtime-cli", Path(args.cli_shim).expanduser().absolute()),
        ("user-cli", Path(args.user_cli_shim).expanduser().absolute()),
        ("desktop-link", Path(args.desktop_link).expanduser().absolute()),
    ]
    if args.shell_profile:
        paths.append(("shell-profile", Path(args.shell_profile).expanduser().absolute()))
    for key, path in paths:
        _snapshot_path(state_path, state, key, path)
    _snapshot_database_files(state_path, state, runtime)
    state["databaseSnapshotPolicy"] = SQLITE_SNAPSHOT_POLICY
    state["mutableStateCaptured"] = True
    _save_state(state_path, state, event="mutable-state-captured")
    return 0


def stop_services(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "candidate-staged":
        raise TransactionError("service stop requires a staged candidate")
    if not state.get("sourceCandidateReady") or (
        state.get("mode") in {"upgrade", "repair"} and not state.get("venvCandidateReady")
    ):
        raise TransactionError("service stop requires all mode-specific candidates to be validated")
    _verify_critical_control_state(state)
    _verify_recorded_candidate_artifacts(state)
    _verify_live_migration_ledger(state)
    if state["platform"] == "Linux":
        units = state.get("systemdUnits") or []
        if not units:
            state["status"] = "stopped"
            _save_state(state_path, state, event="services-stopped")
            return 0
        _verify_prior_service_vector(state)
        state["status"] = "stopping"
        state["serviceStopInitiated"] = True
        _save_state(state_path, state, event="services-stopping")
        _maybe_test_fail("service-stop-initiated")
        ordered = sorted(units, key=lambda item: (not item["name"].endswith(".timer"), item["name"]))
        if state.get("mode") == "repair":
            # Isolate every persistently or runtime-enabled unit before the
            # first stop.  Otherwise a crash between stop and disable can let
            # the user manager immediately respawn an old release.
            for unit in ordered:
                if unit.get("enableState") not in {"enabled", "enabled-runtime"}:
                    continue
                enable_state, _active_state = _systemd_probe_state(state, unit["name"])
                if enable_state != unit["enableState"]:
                    raise TransactionError(
                        f"managed systemd unit enable state changed during repair isolation: {unit['name']}"
                    )
                unit["disabledByTransaction"] = True
                unit["enableRestoreMode"] = (
                    "runtime"
                    if unit["enableState"] == "enabled-runtime"
                    else "persistent"
                )
                _save_state(
                    state_path,
                    state,
                    event=f"systemd-unit-disable-for-repair-armed:{unit['name']}",
                )
                if unit.get("enableRestoreMode") == "runtime":
                    _run_systemctl(state, "disable", "--runtime", unit["name"])
                else:
                    _run_systemctl(state, "disable", unit["name"])
                _save_state(
                    state_path,
                    state,
                    event=f"systemd-unit-disabled-for-repair:{unit['name']}",
                )
            _maybe_test_fail("systemd-units-disabled-for-repair")
        for unit in ordered:
            if not unit["active"]:
                continue
            _run_systemctl(state, "stop", unit["name"])
            unit["stoppedByTransaction"] = True
            _save_state(state_path, state, event=f"systemd-unit-stopped:{unit['name']}")
        for unit in ordered:
            enable_state, active_state = _wait_for_systemd_active_state(
                state,
                unit["name"],
                expected_active=False,
            )
            expected_enable_state = (
                "disabled"
                if unit.get("disabledByTransaction") is True
                else unit["enableState"]
            )
            if enable_state != expected_enable_state:
                raise TransactionError(
                    f"managed systemd unit enable state changed during stop: {unit['name']}"
                )
            if active_state == "active":
                raise TransactionError(
                    f"managed systemd unit remained active after stop: {unit['name']}"
                )
            if not unit["active"] and active_state != unit["activeState"]:
                raise TransactionError(
                    f"inactive managed systemd unit state changed during stop: {unit['name']}"
                )
        state["status"] = "stopped"
        _save_state(state_path, state, event="services-stopped")
        return 0
    if state["platform"] != "Darwin":
        state["status"] = "stopped"
        _save_state(state_path, state, event="services-stopped")
        return 0
    state["status"] = "stopping"
    state["serviceStopInitiated"] = True
    _save_state(state_path, state, event="services-stopping")
    _maybe_test_fail("service-stop-initiated")
    priority = {"watchdog": 0, "dashboard": 1, "rag": 2, "scheduler-pipeline": 3, "scheduler-aggregation": 4}
    services = sorted(state["services"], key=lambda item: priority.get(item["kind"], 5))
    for service in services:
        if not service["loaded"]:
            continue
        _run_launchctl(
            state["launchctl"],
            "bootout",
            f"{state['domain']}/{service['label']}",
            allow_absent=False,
        )
        service["stoppedByTransaction"] = True
        _save_state(state_path, state, event=f"service-stopped:{service['kind']}")
    for service in services:
        loaded, _ = _wait_for_service_state(
            state["launchctl"],
            state["domain"],
            service["label"],
            expected_loaded=False,
        )
        if service["loaded"] and loaded:
            raise TransactionError(f"managed service remained loaded after stop: {service['label']}")
    state["status"] = "stopped"
    _save_state(state_path, state, event="services-stopped")
    return 0


def promote(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "stopped" or not state.get("mutableStateCaptured"):
        raise TransactionError("source promotion requires stopped services and durable mutable-state capture")
    _verify_critical_control_state(state)
    _verify_live_migration_ledger(state)
    _verify_recorded_candidate_artifacts(state)
    _verify_service_plist_bindings(state)
    state["status"] = "promoting"
    _save_state(state_path, state, event="promotion-started")
    _promote_pointer(state_path, state, "source")
    _maybe_test_fail("source-pointer-promoted")
    if state["mode"] in {"upgrade", "repair"}:
        _promote_pointer(state_path, state, "venv")
        _maybe_test_fail("venv-pointer-promoted")
    state["status"] = "promoted"
    _save_state(state_path, state, event="promotion-complete")
    return 0


def restore_services(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    _verify_critical_control_state(state)
    rollback_restore = bool(getattr(args, "rollback", False))
    if rollback_restore:
        if state.get("status") != "rolling-back":
            raise TransactionError("rollback service restore requires rolling-back state")
    else:
        if state.get("status") != "promoted" or not state.get("mutableStateCaptured"):
            raise TransactionError("candidate service restore requires completed promotion")
        _verify_live_migration_ledger(state)
        _verify_recorded_candidate_artifacts(state)
        _verify_service_plist_bindings(state)
    if state["platform"] == "Linux":
        units = state.get("systemdUnits") or []
        ordered = sorted(units, key=lambda item: (item["name"].endswith(".timer"), item["name"]))
        for unit in ordered:
            enable_state, active_state = _systemd_probe_state(state, unit["name"])
            allowed_enable_states = (
                {"disabled", unit["enableState"]}
                if unit.get("disabledByTransaction") is True
                else {unit["enableState"]}
            )
            if enable_state not in allowed_enable_states:
                raise TransactionError(
                    f"managed systemd unit enable state changed before restore: {unit['name']}"
                )
            if not unit["active"]:
                if active_state == "active":
                    raise TransactionError(
                        f"managed systemd unit changed concurrently from inactive to active: {unit['name']}"
                    )
                continue
            if unit.get("disabledByTransaction") is True and enable_state == "disabled":
                if unit.get("enableRestoreMode") == "runtime":
                    _run_systemctl(state, "enable", "--runtime", unit["name"])
                else:
                    _run_systemctl(state, "enable", unit["name"])
            if active_state != "active":
                _run_systemctl(state, "start", unit["name"])
        for unit in ordered:
            if unit.get("disabledByTransaction") is True and not unit["active"]:
                enable_state, _active_state = _systemd_probe_state(state, unit["name"])
                if enable_state == "disabled":
                    if unit.get("enableRestoreMode") == "runtime":
                        _run_systemctl(state, "enable", "--runtime", unit["name"])
                    else:
                        _run_systemctl(state, "enable", unit["name"])
        for unit in ordered:
            enable_state, active_state = _wait_for_systemd_active_state(
                state,
                unit["name"],
                expected_active=bool(unit["active"]),
            )
            if enable_state != unit["enableState"]:
                raise TransactionError(
                    f"managed systemd unit enable state was not preserved: {unit['name']}"
                )
            if (active_state == "active") != unit["active"]:
                raise TransactionError(
                    f"managed systemd unit active state was not restored: {unit['name']}"
                )
            if not unit["active"] and active_state != unit["activeState"]:
                raise TransactionError(
                    f"inactive managed systemd unit state was not restored: {unit['name']}"
                )
        state["status"] = "services-restored"
        _save_state(state_path, state, event="services-restored")
        return 0
    if state["platform"] != "Darwin":
        state["status"] = "services-restored"
        _save_state(state_path, state, event="services-restored")
        return 0
    priority = {"dashboard": 0, "rag": 1, "watchdog": 2, "scheduler-pipeline": 3, "scheduler-aggregation": 4}
    services = sorted(state["services"], key=lambda item: priority.get(item["kind"], 5))
    for service in services:
        label = service["label"]
        loaded_now, _ = _launch_state(state["launchctl"], state["domain"], label)
        if not service["loaded"]:
            if loaded_now:
                raise TransactionError(
                    f"managed service state changed concurrently from unloaded to loaded: {label}"
                )
            continue
        if loaded_now:
            _run_launchctl(state["launchctl"], "bootout", f"{state['domain']}/{label}", allow_absent=True)
            loaded_now, _ = _wait_for_service_state(
                state["launchctl"],
                state["domain"],
                label,
                expected_loaded=False,
            )
            if loaded_now:
                raise TransactionError(f"managed service remained loaded before restore: {label}")
        plist = Path(service["plistPath"])
        if not plist.is_file():
            raise TransactionError(f"managed service plist is missing during restore: {label}")
        _run_launchctl(state["launchctl"], "bootstrap", state["domain"], str(plist))
        if service["kind"] == "dashboard" and service["state"] in RUNNING_STATES:
            _run_launchctl(state["launchctl"], "kickstart", "-k", f"{state['domain']}/{label}")
    for service in services:
        requires_running = (
            service["loaded"]
            and service["kind"] in {"dashboard", "rag"}
            and service["state"] in RUNNING_STATES
        )
        loaded, launch_state = _wait_for_service_state(
            state["launchctl"],
            state["domain"],
            service["label"],
            expected_loaded=bool(service["loaded"]),
            require_running=requires_running,
        )
        if loaded != service["loaded"]:
            raise TransactionError(f"managed service loaded state was not restored: {service['label']}")
        if requires_running:
            if launch_state not in RUNNING_STATES:
                raise TransactionError(f"managed service running state was not restored: {service['label']}")
    _verify_service_health(state, require_candidate_commit=not rollback_restore)
    state["status"] = "services-restored"
    _save_state(state_path, state, event="services-restored")
    return 0


def verify(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "services-restored":
        raise TransactionError("candidate verify requires restored and healthy managed services")
    _verify_critical_control_state(state)
    source = Path(state["source"]["path"])
    candidate_source = Path(state["source"]["candidateTarget"])
    if not source.is_symlink() or source.resolve(strict=False) != candidate_source.resolve(strict=False):
        raise TransactionError("active source pointer does not match the staged candidate")
    if not (source / ".actanara-runtime-source.json").is_file():
        raise TransactionError("active source manifest is missing after promotion")
    _verify_recorded_candidate_artifacts(state)
    _verify_live_migration_ledger(state)
    _verify_service_plist_bindings(state)
    if state["mode"] in {"upgrade", "repair"}:
        venv = Path(state["venv"]["path"])
        candidate_venv = Path(state["venv"]["candidateTarget"])
        if not venv.is_symlink() or venv.resolve(strict=False) != candidate_venv.resolve(strict=False):
            raise TransactionError("active venv pointer does not match the staged candidate")
        if not (venv / "bin" / "python").is_file():
            raise TransactionError("active venv Python is missing after promotion")
    services_by_plist = {
        str(Path(service["plistPath"]).absolute()): service
        for service in state.get("services") or []
    }
    for item in state["files"]:
        if item["key"] == "database":
            _validate_sqlite_snapshot_record(item)
        elif item["key"] == "managed-plist":
            service = services_by_plist.get(str(item.get("path") or ""))
            if not _file_matches_snapshot(item) and not (
                isinstance(service, dict) and _service_plist_matches_normalized(service)
            ):
                raise TransactionError(
                    "protected update state changed unexpectedly: managed-plist"
                )
        elif not _file_matches_snapshot(item):
            raise TransactionError(f"protected update state changed unexpectedly: {item['key']}")
    state["status"] = "verified"
    _save_state(state_path, state, event="candidate-verified")
    return 0


def _service_stop_was_initiated(state: dict[str, Any]) -> bool:
    explicit = state.get("serviceStopInitiated")
    if isinstance(explicit, bool):
        return explicit
    if any(service.get("stoppedByTransaction") for service in state.get("services") or []):
        return True
    if any(unit.get("stoppedByTransaction") for unit in state.get("systemdUnits") or []):
        return True
    phase = str(state.get("phase") or "")
    if phase == "services-stopping" or phase.startswith("service-stopped:"):
        return True
    post_stop_prefixes = (
        "services-stopped",
        "mutable-state-captured",
        "promotion-",
        "prior-source-moved",
        "source-promotion-",
        "source-pointer-",
        "source-promoted",
        "prior-venv-moved",
        "venv-promotion-",
        "venv-prior-",
        "venv-pointer-",
        "venv-promoted",
        "services-restored",
        "candidate-verified",
        "committed",
    )
    return phase.startswith(post_stop_prefixes)


def _verify_prior_service_vector(state: dict[str, Any]) -> None:
    if state.get("platform") == "Linux":
        for unit in state.get("systemdUnits") or []:
            enable_state, active_state = _systemd_probe_state(state, unit["name"])
            if enable_state != unit["enableState"] or active_state != unit["activeState"]:
                raise TransactionError(
                    f"managed systemd unit state changed before the update stop boundary: {unit['name']}"
                )
        return
    for service in state.get("services") or []:
        loaded, launch_state = _launch_state(
            state["launchctl"],
            state["domain"],
            service["label"],
        )
        if loaded != service["loaded"]:
            raise TransactionError(
                f"managed service state changed before the update stop boundary: {service['label']}"
            )
        if (
            loaded
            and service["kind"] in {"dashboard", "rag"}
            and service["state"] in RUNNING_STATES
            and launch_state not in RUNNING_STATES
        ):
            raise TransactionError(
                f"managed service stopped running before the update stop boundary: {service['label']}"
            )


def _remove_candidate_artifact(
    state_path: Path,
    state: dict[str, Any],
    record: dict[str, Any],
) -> None:
    kind = str(record.get("kind") or "")
    path = Path(str(record.get("path") or ""))
    quarantine = state_path.parent / f".cleanup-{kind}"
    if record.get("reservationStarted"):
        if path.exists() or path.is_symlink():
            if not _artifact_marker_matches(path, record):
                raise TransactionError("partially reserved candidate artifact ownership is unproven")
            _record_artifact_identity(record, path)
            record["reservationAttemptNonce"] = None
            record["reservationStarted"] = False
            _save_state(state_path, state, event=f"candidate-artifact-reservation-recovered:{kind}")
        else:
            staging = _reservation_staging_path(state_path, record)
            if staging.exists() or staging.is_symlink():
                if _artifact_marker_matches(staging, record):
                    _rename_exclusive(staging, path)
                    _fsync_dir(staging.parent)
                    _fsync_dir(path.parent)
                    _record_artifact_identity(record, path)
                    record["reservationAttemptNonce"] = None
                    record["reservationStarted"] = False
                    _save_state(
                        state_path,
                        state,
                        event=f"candidate-artifact-reservation-recovered:{kind}",
                    )
                else:
                    _abandon_reservation_attempt(state_path, state, record)
                    state = _load_state(state_path)
                    record = _candidate_artifact_record(state, kind)
                    record["cleaned"] = True
                    _save_state(
                        state_path,
                        state,
                        event=f"candidate-artifact-reservation-aborted:{kind}",
                    )
                    return
            else:
                record["reservationAttemptNonce"] = None
                record["reservationStarted"] = False
                record["cleaned"] = True
                _save_state(state_path, state, event=f"candidate-artifact-reservation-aborted:{kind}")
                return
    if not record.get("created"):
        if path.exists() or path.is_symlink():
            raise TransactionError("unowned candidate artifact occupies a reserved path")
        if quarantine.exists() or quarantine.is_symlink():
            raise TransactionError("unowned candidate artifact occupies a cleanup path")
        return

    pointer_paths = [Path(state[name]["path"]) for name in ("source", "venv")]
    for pointer_path in pointer_paths:
        if pointer_path.exists() or pointer_path.is_symlink():
            if pointer_path.resolve(strict=False) == path.resolve(strict=False):
                raise TransactionError(
                    f"candidate artifact is still active through the {pointer_path.name} pointer"
                )

    cleanup_path = record.get("cleanupPath")
    if cleanup_path is not None and Path(str(cleanup_path)) != quarantine:
        raise TransactionError("candidate artifact cleanup journal escaped its transaction")
    require_marker = not bool(record.get("markerRemoved"))
    if cleanup_path is None:
        if quarantine.exists() or quarantine.is_symlink():
            raise TransactionError("candidate artifact cleanup path was preoccupied")
        if not path.exists() and not path.is_symlink():
            record["created"] = False
            record["cleaned"] = True
            _save_state(state_path, state, event=f"candidate-artifact-already-absent:{kind}")
            return
        _verify_artifact_ownership(record, require_marker=require_marker)
        record["cleanupPath"] = str(quarantine)
        _save_state(state_path, state, event=f"candidate-artifact-cleanup-started:{kind}")
        _maybe_test_fail(f"candidate-artifact-cleanup-authorized-{kind}")
        _rename_exclusive(path, quarantine)
        _fsync_dir(path.parent)

    if (
        not quarantine.exists()
        and not quarantine.is_symlink()
        and (path.exists() or path.is_symlink())
    ):
        _verify_artifact_ownership(record, require_marker=require_marker)
        _rename_exclusive(path, quarantine)
        _fsync_dir(path.parent)
    _maybe_test_fail(f"candidate-artifact-cleanup-moved-{kind}")

    if not quarantine.exists() and not quarantine.is_symlink():
        record["created"] = False
        record["cleaned"] = True
        record["cleanupPath"] = None
        _save_state(state_path, state, event=f"candidate-artifact-cleaned:{kind}")
        return
    _verify_artifact_ownership(
        record,
        require_marker=require_marker,
        path_override=quarantine,
    )
    shutil.rmtree(quarantine)
    _fsync_dir(quarantine.parent)
    record["created"] = False
    record["cleaned"] = True
    record["cleanupPath"] = None
    _save_state(state_path, state, event=f"candidate-artifact-cleaned:{kind}")


def _cleanup_candidate_artifacts(state_path: Path, state: dict[str, Any]) -> None:
    if state.get("legacySchemaVersion"):
        preserved = [
            str(item.get("kind") or "unknown")
            for item in state.get("candidateArtifacts") or []
            if isinstance(item, dict)
            and (
                Path(str(item.get("path") or "")).exists()
                or Path(str(item.get("path") or "")).is_symlink()
            )
        ]
        state["legacyPreservedCandidateArtifactKinds"] = sorted(preserved)
        _save_state(state_path, state, event="legacy-candidate-artifacts-preserved")
        return
    for item in reversed(state.get("candidateArtifacts") or []):
        _remove_candidate_artifact(state_path, state, item)


def cleanup_validation_runtime(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "candidate-staged":
        raise TransactionError("candidate validation cleanup requires a staged candidate")
    _verify_critical_control_state(state)
    _verify_live_migration_ledger(state)
    record = _candidate_artifact_record(state, "validation-runtime")
    _remove_candidate_artifact(state_path, state, record)
    _save_state(state_path, state, event="candidate-validation-runtime-cleaned")
    return 0


def _restore_repair_configuration_pending_snapshot(
    state: dict[str, Any],
    record: dict[str, Any],
) -> None:
    marker = _repair_configuration_pending_path(state)
    if Path(str(record.get("path") or "")) != marker:
        raise TransactionError("repair configuration marker snapshot escaped the Runtime")
    if record.get("kind") not in {"missing", "file"}:
        raise TransactionError("repair configuration marker snapshot has an unsafe type")
    if _file_matches_snapshot(record):
        return

    try:
        current, _metadata = _read_repair_configuration_pending(marker)
    except FileNotFoundError:
        current = None
    if current is not None and current != _repair_configuration_pending_payload(str(state["txId"])):
        raise TransactionError("repair configuration marker changed concurrently")

    if record["kind"] == "missing":
        if current is not None:
            marker.unlink()
            _fsync_dir(marker.parent)
        return

    backup = Path(str(record.get("backupPath") or ""))
    if (
        backup.is_symlink()
        or not backup.is_file()
        or _sha256(backup) != record.get("sha256")
    ):
        raise TransactionError("repair configuration marker backup is missing or changed")
    prior, _metadata = _read_repair_configuration_pending(backup)
    _atomic_bytes(marker, prior, mode=int(record["mode"]))
    if not _file_matches_snapshot(record):
        raise TransactionError("prior repair configuration marker was not restored")


def rollback_state(state_path: Path, state: dict[str, Any]) -> None:
    state = _stop_recorded_candidate_command(state_path, state)
    errors: list[str] = []
    service_stop_initiated = _service_stop_was_initiated(state)
    state["status"] = "rolling-back"
    _save_state(state_path, state, event="rollback-started")
    database_compatibility_safe = True
    if state["platform"] == "Darwin" and service_stop_initiated:
        priority = {"watchdog": 0, "scheduler-pipeline": 1, "scheduler-aggregation": 2, "dashboard": 3, "rag": 4}
        for service in sorted(state["services"], key=lambda item: priority.get(item["kind"], 5)):
            try:
                loaded, _ = _launch_state(state["launchctl"], state["domain"], service["label"])
                if loaded and not service["loaded"]:
                    errors.append(f"service-concurrent-load:{service['kind']}")
                    continue
                if loaded and service["loaded"]:
                    _run_launchctl(
                        state["launchctl"],
                        "bootout",
                        f"{state['domain']}/{service['label']}",
                        allow_absent=True,
                    )
            except Exception as exc:
                errors.append(f"service-bootout:{service['kind']}:{exc}")
    elif state["platform"] == "Linux" and service_stop_initiated:
        ordered = sorted(
            state.get("systemdUnits") or [],
            key=lambda item: (not item["name"].endswith(".timer"), item["name"]),
        )
        for unit in ordered:
            try:
                enable_state, active_state = _systemd_probe_state(state, unit["name"])
                allowed_enable_states = (
                    {"disabled", unit["enableState"]}
                    if unit.get("disabledByTransaction") is True
                    else {unit["enableState"]}
                )
                if enable_state not in allowed_enable_states:
                    errors.append(f"systemd-enable-concurrent-change:{unit['name']}")
                    continue
                if not unit["active"]:
                    if active_state != unit["activeState"]:
                        errors.append(f"systemd-active-concurrent-change:{unit['name']}")
                    continue
                if active_state != "inactive":
                    _run_systemctl(state, "stop", unit["name"])
                _enabled, stopped_state = _wait_for_systemd_active_state(
                    state,
                    unit["name"],
                    expected_active=False,
                )
                if stopped_state == "active":
                    errors.append(f"systemd-stop:{unit['name']}:unit remained active")
            except Exception as exc:
                errors.append(f"systemd-stop:{unit['name']}:{exc}")
    if service_stop_initiated:
        try:
            _verify_live_migration_ledger(state)
        except Exception as exc:
            database_compatibility_safe = False
            errors.append(f"database-compatibility:{exc}")
    if database_compatibility_safe:
        for name in ("venv", "source"):
            try:
                _restore_pointer(state_path, name, state[name])
                _save_state(state_path, state, event=f"rollback-{name}-restored")
            except Exception as exc:
                errors.append(f"pointer:{name}:{exc}")
        try:
            _cleanup_candidate_artifacts(state_path, state)
        except Exception as exc:
            errors.append(f"candidate-artifacts:{exc}")
    else:
        errors.append("pointers:preserved-because-database-compatibility-is-unproven")
    services_by_plist = {
        str(Path(service["plistPath"]).absolute()): service
        for service in state.get("services") or []
    }
    for item in reversed(state["files"]):
        try:
            if item["key"] == "database":
                # The online backup is durable evidence, not an automatic
                # rewind source.  Replacing live SQLite here would erase commits
                # made by a legitimate writer after capture and cannot safely
                # distinguish them from candidate writes.
                _validate_sqlite_snapshot_record(item)
            elif item["key"] == "repair-configuration-pending":
                _restore_repair_configuration_pending_snapshot(state, item)
            elif item["key"] == "managed-plist":
                service = services_by_plist.get(str(item.get("path") or ""))
                if _file_matches_snapshot(item):
                    continue
                if isinstance(service, dict) and _service_plist_matches_normalized(service):
                    _restore_normalized_service_plist(state, service)
                else:
                    errors.append(f"file-concurrent-change:{item['key']}")
            elif not _file_matches_snapshot(item):
                errors.append(f"file-concurrent-change:{item['key']}")
        except Exception as exc:
            errors.append(f"file:{item['key']}:{exc}")
    if state["platform"] in {"Darwin", "Linux"} and service_stop_initiated and not errors:
        try:
            # Reuse the normal state-restoration contract after exact plist restore.
            state["status"] = "rolling-back"
            _save_state(state_path, state, event="rollback-files-restored")
            namespace = argparse.Namespace(state=str(state_path), rollback=True)
            restore_services(namespace)
            state = _load_state(state_path)
        except Exception as exc:
            errors.append(f"services:{exc}")
    elif state["platform"] in {"Darwin", "Linux"} and service_stop_initiated:
        errors.append("services:not-restored-after-pointer-or-control-state-conflict")
    elif state["platform"] in {"Darwin", "Linux"}:
        try:
            _verify_prior_service_vector(state)
            _save_state(state_path, state, event="rollback-services-unchanged")
        except Exception as exc:
            errors.append(f"services-unchanged:{exc}")
    state["rollbackErrors"] = errors
    if errors:
        state["status"] = "rollback-failed"
        _save_state(state_path, state, event="rollback-failed")
        raise TransactionError("update rollback was incomplete; preserved journal requires operator review")
    state["status"] = "rolled-back"
    _save_state(state_path, state, event="rolled-back")
    _maybe_test_fail("rollback-journaled-before-lock-release")
    _release_lock(state)


def rollback(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") == "committed":
        return 0
    if state.get("status") == "rolled-back":
        _release_lock(state)
        return 0
    rollback_state(state_path, state)
    return 0


def commit(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("status") != "verified":
        raise TransactionError("update commit requires a verified candidate and restored prior service state")
    _verify_critical_control_state(state)
    _verify_recorded_candidate_artifacts(state)
    _verify_live_migration_ledger(state)
    _verify_service_plist_bindings(state)
    state["status"] = "committed"
    _save_state(state_path, state, event="committed")
    _maybe_test_fail("commit-journaled-before-lock-release")
    _release_lock(state)
    return 0


def _verify_repair_candidate_pointers(state: dict[str, Any]) -> None:
    for name in ("source", "venv"):
        pointer = Path(str(state[name]["path"]))
        candidate = Path(str(state[name]["candidateTarget"]))
        if (
            not pointer.is_symlink()
            or pointer.resolve(strict=False) != candidate.resolve(strict=False)
        ):
            raise TransactionError(
                f"repair {name} pointer no longer matches its staged candidate"
            )


def commit_repair(args: argparse.Namespace) -> int:
    """Commit rebuilt Runtime pointers while legacy services remain stopped.

    A repair may cross migration boundaries that the legacy reader cannot
    safely consume.  It therefore commits the validated source and venv before
    any candidate service is started.  The installer performs configuration,
    schema migration, service registration, and the final doctor afterwards.
    Durable Settings and SQLite snapshots captured in the transaction are kept
    for operator recovery.
    """

    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("mode") != "repair" or state.get("status") != "promoted":
        raise TransactionError("repair commit requires promoted repair candidates")
    service_stop_required = state.get("platform") == "Darwin" or (
        state.get("platform") == "Linux" and bool(state.get("systemdUnits"))
    )
    if not state.get("mutableStateCaptured") or (
        service_stop_required and not state.get("serviceStopInitiated")
    ):
        raise TransactionError("repair commit requires captured state and a completed service stop")
    _verify_critical_control_state(state)
    _verify_recorded_candidate_artifacts(state)
    _verify_live_migration_ledger(state)
    _verify_service_plist_bindings(state)

    _verify_repair_candidate_pointers(state)

    if state["platform"] == "Darwin":
        for service in state.get("services") or []:
            loaded, _ = _launch_state(state["launchctl"], state["domain"], service["label"])
            if loaded:
                raise TransactionError(
                    f"legacy managed service restarted before repair commit: {service['label']}"
                )
    elif state["platform"] == "Linux":
        for unit in state.get("systemdUnits") or []:
            enable_state, active_state = _systemd_probe_state(state, unit["name"])
            expected_enable_state = (
                "disabled"
                if unit.get("disabledByTransaction") is True
                else unit["enableState"]
            )
            if enable_state != expected_enable_state:
                raise TransactionError(
                    f"managed systemd unit enable state changed before repair commit: {unit['name']}"
                )
            if active_state == "active":
                raise TransactionError(
                    f"legacy managed systemd unit restarted before repair commit: {unit['name']}"
                )

    pending_records = [
        item
        for item in state.get("files") or []
        if item.get("key") == "repair-configuration-pending"
    ]
    if (
        len(pending_records) != 1
        or pending_records[0].get("kind") not in {"missing", "file"}
        or not _file_matches_snapshot(pending_records[0])
    ):
        raise TransactionError("repair configuration marker changed before repair commit")
    pending_path = _repair_configuration_pending_path(state)
    pending_payload = _repair_configuration_pending_payload(str(state["txId"]))
    state["repairConfigurationPending"] = {
        "path": str(pending_path),
        "txId": str(state["txId"]),
        "sha256": hashlib.sha256(pending_payload).hexdigest(),
        "device": None,
        "inode": None,
    }
    state["repairConfigurationComplete"] = False
    _save_state(state_path, state, event="repair-configuration-marker-planned")
    _atomic_bytes(pending_path, pending_payload, mode=0o600)
    written_payload, written_metadata = _read_repair_configuration_pending(pending_path)
    if written_payload != pending_payload:
        raise TransactionError("repair configuration marker does not match its transaction")
    state["repairConfigurationPending"]["device"] = written_metadata.st_dev
    state["repairConfigurationPending"]["inode"] = written_metadata.st_ino
    _save_state(state_path, state, event="repair-configuration-marker-created")

    state["repairBackupPath"] = str(state_path.parent / "backups")
    state["status"] = "committed"
    _save_state(state_path, state, event="repair-committed-services-stopped")
    _maybe_test_fail("repair-commit-journaled-before-lock-release")
    return 0


def _verify_live_repair_configuration_marker(
    state: dict[str, Any],
) -> tuple[Path, bytes, os.stat_result]:
    pending = state.get("repairConfigurationPending")
    expected_fields = {"path", "txId", "sha256", "device", "inode"}
    if (
        not isinstance(pending, dict)
        or set(pending) != expected_fields
        or pending.get("txId") != state.get("txId")
        or not isinstance(pending.get("device"), int)
        or not isinstance(pending.get("inode"), int)
    ):
        raise TransactionError("repair configuration marker binding is incomplete")
    marker = _repair_configuration_pending_path(state)
    expected_payload = _repair_configuration_pending_payload(str(state["txId"]))
    payload, metadata = _read_repair_configuration_pending(marker)
    if (
        payload != expected_payload
        or hashlib.sha256(payload).hexdigest() != pending.get("sha256")
        or metadata.st_dev != pending.get("device")
        or metadata.st_ino != pending.get("inode")
    ):
        raise TransactionError(
            "repair configuration marker does not belong to this transaction"
        )
    return marker, payload, metadata


def restore_repair_services(args: argparse.Namespace) -> int:
    """Converge retained Linux units to their exact pre-repair state vector."""

    state_path = Path(args.state)
    state = _load_state(state_path)
    if (
        state.get("mode") != "repair"
        or state.get("platform") != "Linux"
        or state.get("status") != "committed"
        or state.get("repairConfigurationComplete") is True
    ):
        raise TransactionError(
            "repair systemd restore requires a pending committed Linux repair"
        )
    _verify_repair_candidate_pointers(state)
    _verify_live_repair_configuration_marker(state)
    requested = list(args.unit)
    if len(requested) != len(set(requested)) or any(
        not SYSTEMD_UNIT_NAME_RE.fullmatch(name) for name in requested
    ):
        raise TransactionError("repair systemd restore unit inventory is invalid")
    by_name = {
        str(unit.get("name") or ""): unit
        for unit in state.get("systemdUnits") or []
        if isinstance(unit, dict)
    }
    if any(name not in by_name for name in requested):
        raise TransactionError(
            "repair systemd restore requested a unit outside the prior inventory"
        )
    selected = [by_name[name] for name in requested]
    for unit in selected:
        if (
            unit.get("enableState") not in {"enabled", "enabled-runtime", "disabled"}
            or unit.get("activeState") not in {"active", "inactive"}
        ):
            raise TransactionError(
                f"repair cannot exactly restore the prior systemd state: {unit['name']}"
            )
    planned = [
        {
            "name": unit["name"],
            "enableState": unit["enableState"],
            "activeState": unit["activeState"],
        }
        for unit in selected
    ]
    existing_plan = state.get("repairSystemdRestorePlan")
    if existing_plan is not None and existing_plan != planned:
        raise TransactionError("repair systemd restore plan changed while resuming")
    if existing_plan is None:
        state["repairSystemdRestorePlan"] = planned
        state["repairSystemdRestoreComplete"] = False
        _save_state(state_path, state, event="repair-systemd-restore-armed")
    _maybe_test_fail("repair-systemd-restore-armed")

    ordered = sorted(
        selected,
        key=lambda item: (str(item["name"]).endswith(".timer"), str(item["name"])),
    )
    for unit in ordered:
        name = str(unit["name"])
        desired_enable = str(unit["enableState"])
        # Clear both persistent and runtime links before rebuilding the exact
        # requested enable mode.  The operations are intentionally convergent
        # so a SIGKILL at any command boundary can be resumed safely.
        _run_systemctl(state, "disable", name)
        _run_systemctl(state, "disable", "--runtime", name)
        if desired_enable == "enabled":
            _run_systemctl(state, "enable", name)
        elif desired_enable == "enabled-runtime":
            _run_systemctl(state, "enable", "--runtime", name)
        if unit["activeState"] == "active":
            _run_systemctl(state, "start", name)
        else:
            _run_systemctl(state, "stop", name)
        _maybe_test_fail(f"repair-systemd-unit-restored:{name}")

    for unit in ordered:
        enable_state, active_state = _wait_for_systemd_active_state(
            state,
            str(unit["name"]),
            expected_active=unit["activeState"] == "active",
        )
        if (
            enable_state != unit["enableState"]
            or active_state != unit["activeState"]
        ):
            raise TransactionError(
                f"repair did not restore the exact systemd state: {unit['name']}"
            )
    state = _load_state(state_path)
    if state.get("repairSystemdRestorePlan") != planned:
        raise TransactionError("repair systemd restore plan changed before commit")
    state["repairSystemdRestoreComplete"] = True
    _save_state(state_path, state, event="repair-systemd-restore-complete")
    return 0


def complete_repair(args: argparse.Namespace) -> int:
    """Clear the durable retry marker after repair configuration succeeds."""

    state_path = Path(args.state)
    state = _load_state(state_path)
    if state.get("mode") != "repair" or state.get("status") != "committed":
        raise TransactionError("repair completion requires a committed repair transaction")
    pending = state.get("repairConfigurationPending")
    if (
        not isinstance(pending, dict)
        or set(pending) != {"path", "txId", "sha256", "device", "inode"}
        or pending.get("txId") != state.get("txId")
        or not isinstance(pending.get("device"), int)
        or not isinstance(pending.get("inode"), int)
    ):
        raise TransactionError("repair configuration marker binding is incomplete")
    marker = _repair_configuration_pending_path(state)
    expected_payload = _repair_configuration_pending_payload(str(state["txId"]))
    try:
        payload, metadata = _read_repair_configuration_pending(marker)
    except FileNotFoundError:
        if state.get("repairConfigurationComplete") is not True:
            raise TransactionError("repair configuration marker disappeared before completion")
        _verify_recorded_candidate_artifacts(state)
        _verify_repair_candidate_pointers(state)
        _release_lock(state)
        return 0
    if payload == expected_payload:
        _verify_live_repair_configuration_marker(state)
    else:
        raise TransactionError("repair configuration marker does not belong to this transaction")

    _verify_recorded_candidate_artifacts(state)
    _verify_repair_candidate_pointers(state)
    if state.get("repairConfigurationComplete") is not True:
        state["repairConfigurationComplete"] = True
        _save_state(state_path, state, event="repair-configuration-complete")
        _maybe_test_fail("repair-configuration-complete-before-marker-clear")
    payload, metadata = _read_repair_configuration_pending(marker)
    if (
        payload != expected_payload
        or metadata.st_dev != pending.get("device")
        or metadata.st_ino != pending.get("inode")
    ):
        raise TransactionError("repair configuration marker changed before completion")
    _verify_repair_candidate_pointers(state)
    marker.unlink()
    _fsync_dir(marker.parent)
    _maybe_test_fail("repair-marker-cleared-before-lock-release")
    _release_lock(state)
    return 0


def recover(args: argparse.Namespace) -> int:
    runtime_input = Path(args.runtime).expanduser().absolute()
    try:
        runtime = runtime_input.resolve(strict=True)
    except OSError as exc:
        raise TransactionError("recovery Runtime is unavailable") from exc
    for managed in (
        runtime / "app",
        runtime / "app" / "releases",
        runtime / "app" / "venvs",
        runtime / "app" / "update-transactions",
        runtime / "config",
        runtime / "data",
    ):
        _require_managed_directory(managed, runtime)
    lock = runtime / "app" / ".update-transaction.lock"
    if not lock.exists():
        return 0
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        state_path = Path(owner["journal"])
        owner_pid = int(owner.get("ownerPid") or 0)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise TransactionError("stale update lock has no recoverable owner journal") from exc
    if not _is_within(state_path, runtime / "app" / "update-transactions"):
        raise TransactionError("stale update lock journal escaped the selected Runtime")
    command_lock: int | None = None
    try:
        command_lock = _acquire_transaction_command_lock(state_path)
        state = _load_state(state_path)
        if Path(state["runtime"]).resolve(strict=False) != runtime.resolve(strict=False):
            raise TransactionError("stale update lock journal belongs to a different Runtime")
        repair_configuration_live = (
            state.get("mode") == "repair"
            and state.get("status") == "committed"
            and state.get("repairConfigurationComplete") is not True
        )
        if (
            state.get("mode") == "repair"
            and state.get("status") == "committed"
            and state.get("repairConfigurationComplete") is True
        ):
            complete_repair(argparse.Namespace(state=str(state_path)))
            return 0
        if state.get("status") in TERMINAL_STATUSES and not repair_configuration_live:
            _release_lock(state)
            return 0
        if state.get("legacySchemaVersion"):
            owner_is_active = _pid_alive(owner_pid)
        else:
            owner_is_active = _same_process(owner_pid, state.get("ownerProcessIdentity"))
        if owner_is_active:
            raise TransactionError("another update transaction process is still active")
        active_command_pid = int(state.get("activeCommandPid") or 0)
        active_command_identity = state.get("activeCommandProcessIdentity")
        if active_command_pid and _same_process(active_command_pid, active_command_identity):
            deadline = time.monotonic() + _candidate_child_term_timeout_seconds() + 2.0
            while (
                _same_process(active_command_pid, active_command_identity)
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if _same_process(active_command_pid, active_command_identity):
                raise TransactionError("an update transaction helper command is still active")
            state = _load_state(state_path)
        if state.get("status") in TERMINAL_STATUSES:
            # A committed repair is not operationally terminal until its
            # configuration marker is cleared.  Keep the durable owner link
            # in place so Settings and service mutations remain fail-closed
            # while the installer resumes the committed candidate.
            if repair_configuration_live:
                return 0
            _release_lock(state)
            return 0
        rollback_state(state_path, state)
        return 0
    finally:
        _release_transaction_command_lock(command_lock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    begin_parser = sub.add_parser("begin")
    begin_parser.add_argument("--runtime", required=True)
    begin_parser.add_argument("--home", required=True)
    begin_parser.add_argument("--source-pointer", required=True)
    begin_parser.add_argument("--venv-pointer", required=True)
    begin_parser.add_argument("--expected-settings-sha256")
    begin_parser.add_argument("--settings-only-profile-evidence", action="store_true")
    begin_parser.add_argument("--expected-active-venv-target")
    begin_parser.add_argument(
        "--expected-active-marker-status",
        choices=("missing", "trusted"),
    )
    begin_parser.add_argument("--expected-active-marker-sha256")
    begin_parser.add_argument("--mode", choices=("upgrade", "repair", "source-only"), required=True)
    begin_parser.add_argument("--tx-id", required=True)
    begin_parser.add_argument("--owner-pid", type=int, required=True)
    begin_parser.add_argument("--platform", required=True)
    begin_parser.add_argument("--launchctl", default="")
    begin_parser.add_argument("--systemctl", default="")
    begin_parser.add_argument("--systemd-unit", action="append", default=[])
    begin_parser.add_argument(
        "--expected-systemd-unit-sha256",
        action="append",
        default=[],
    )
    begin_parser.add_argument("--uid", required=True)
    begin_parser.set_defaults(func=begin)

    reserve = sub.add_parser("reserve-artifact")
    reserve.add_argument("--state", required=True)
    reserve.add_argument(
        "--kind",
        choices=("source-temp", "venv", "validation-runtime"),
        required=True,
    )
    reserve.set_defaults(func=reserve_candidate_artifact)

    promote_source = sub.add_parser("promote-source-artifact")
    promote_source.add_argument("--state", required=True)
    promote_source.set_defaults(func=promote_source_artifact)

    clean_source = sub.add_parser("clean-source-build-artifacts")
    clean_source.add_argument("--state", required=True)
    clean_source.set_defaults(func=clean_candidate_source_build_artifacts)

    record = sub.add_parser("record-candidate")
    record.add_argument("--state", required=True)
    record.add_argument("--kind", choices=("source", "venv"), required=True)
    record.add_argument("--candidate", required=True)
    record.set_defaults(func=record_candidate)

    migration_compatibility = sub.add_parser("verify-migration-compatibility")
    migration_compatibility.add_argument("--state", required=True)
    migration_compatibility.add_argument("--allow-legacy-repair", action="store_true")
    migration_compatibility.set_defaults(func=verify_migration_compatibility)

    cleanup_validation = sub.add_parser("cleanup-validation-runtime")
    cleanup_validation.add_argument("--state", required=True)
    cleanup_validation.set_defaults(func=cleanup_validation_runtime)

    capture = sub.add_parser("capture-mutable")
    capture.add_argument("--state", required=True)
    capture.add_argument("--location", required=True)
    capture.add_argument("--cli-shim", required=True)
    capture.add_argument("--user-cli-shim", required=True)
    capture.add_argument("--desktop-link", required=True)
    capture.add_argument("--shell-profile", default="")
    capture.set_defaults(func=capture_mutable)

    normalize_plists = sub.add_parser("normalize-service-plists")
    normalize_plists.add_argument("--state", required=True)
    normalize_plists.set_defaults(func=normalize_service_plists)

    for name, func in (
        ("stop", stop_services),
        ("promote", promote),
        ("restore-services", restore_services),
        ("verify", verify),
        ("commit", commit),
        ("commit-repair", commit_repair),
        ("complete-repair", complete_repair),
        ("rollback", rollback),
    ):
        command = sub.add_parser(name)
        command.add_argument("--state", required=True)
        command.set_defaults(func=func)

    restore_repair = sub.add_parser("restore-repair-services")
    restore_repair.add_argument("--state", required=True)
    restore_repair.add_argument("--unit", action="append", default=[])
    restore_repair.set_defaults(func=restore_repair_services)

    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("--runtime", required=True)
    recover_parser.set_defaults(func=recover)

    candidate_command = sub.add_parser("run-candidate-command")
    candidate_command.add_argument("--state", required=True)
    candidate_command.add_argument("--phase", required=True)
    candidate_command.add_argument("command", nargs=argparse.REMAINDER)
    candidate_command.set_defaults(func=run_candidate_command)

    candidate_child = sub.add_parser("run-candidate-child", help=argparse.SUPPRESS)
    candidate_child.add_argument("--gate-fd", type=int, required=True)
    candidate_child.add_argument("command", nargs=argparse.REMAINDER)
    candidate_child.set_defaults(func=run_candidate_child)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_path = Path(args.state) if getattr(args, "state", None) else None
    marked_active = False
    command_lock: int | None = None
    try:
        if state_path is not None:
            command_lock = _acquire_transaction_command_lock(state_path)
            _require_owner_caller(
                _load_state(state_path),
                command=str(args.command),
            )
            active_command = (
                f"run-candidate-command:{args.phase}"
                if args.func is run_candidate_command
                else str(args.command)
            )
            _set_active_command(state_path, active_command)
            marked_active = True
        return int(args.func(args) or 0)
    except TransactionError as exc:
        print(f"update transaction error: {exc}", file=sys.stderr)
        return 70
    finally:
        if marked_active and state_path is not None and state_path.is_file():
            try:
                _set_active_command(state_path, None)
            except TransactionError:
                pass
        _release_transaction_command_lock(command_lock)


if __name__ == "__main__":
    raise SystemExit(main())
