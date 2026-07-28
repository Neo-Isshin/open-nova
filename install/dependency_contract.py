#!/usr/bin/env python3
"""Strict runtime dependency contracts for Actanara installs and updates.

This helper is intentionally stdlib-only.  It is safe to run before a Runtime
venv exists and exposes a JSON-only CLI for installer orchestration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


PRODUCT = "actanara"
LOCK_SCHEMA_VERSION = 1
FINGERPRINT_SCHEMA_VERSION = 1
MARKER_SCHEMA_VERSION = 1
WHEELHOUSE_SCHEMA_VERSION = 1
FINGERPRINT_ALGORITHM = "actanara-runtime-dependencies-v1"
MARKER_NAME = ".actanara-dependencies.json"
WHEELHOUSE_MANIFEST_NAME = ".actanara-wheelhouse.json"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_PYPROJECT_BYTES = 2 * 1024 * 1024
MAX_SUBPROCESS_JSON_BYTES = 16 * 1024 * 1024
MAX_PIP_ERROR_SUMMARY_CHARS = 1600
MAX_PIP_DIAGNOSTIC_STREAM_BYTES = 512 * 1024
HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
SAFE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}\Z")
PYTHON_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\Z")
MACOS_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}\Z")
DIRECT_DEPENDENCY_NAME_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
WHEEL_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,239}\.whl\Z")
PYPI_ARTIFACT_HOST = "files.pythonhosted.org"
PYTORCH_CPU_ARTIFACT_HOSTS = frozenset(
    {"download.pytorch.org", "download-r2.pytorch.org"}
)
SENSITIVE_ENVIRONMENT_NAME_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|CREDENTIAL|AUTHORIZATION)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|credential|authorization)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
AUTHORIZATION_VALUE_RE = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)([^\s]+)"
)
HTTP_URL_RE = re.compile(r"(?i)https?://[^\s<>'\"\]\[()]+")

LOCK_ROOT_FIELDS = {
    "schemaVersion",
    "product",
    "artifactPolicy",
    "resolver",
    "profiles",
    "environments",
}
LOCK_ARTIFACT_POLICY_FIELDS = {
    "hashAlgorithm",
    "hashesRequired",
    "sourceBuildsAllowed",
    "wheelsOnly",
}
LOCK_RESOLVER_FIELDS = {"name", "reportSchemaVersion", "version"}
LOCK_PROFILE_FIELDS = {"directRequirements", "packages"}
LOCK_ENVIRONMENT_IDENTITY_FIELDS = {
    "implementation",
    "pythonMajorMinor",
    "abi",
    "platformFamily",
    "architecture",
    "minimumMacOS",
}
LOCK_ENVIRONMENT_FIELDS = LOCK_ENVIRONMENT_IDENTITY_FIELDS | {
    "supportedProfiles",
    "profilePackages",
    "packages",
}
LOCK_PACKAGE_FIELDS = {"name", "version", "filename", "sha256", "url"}
PROBE_FIELDS = {
    "implementation",
    "pythonMajorMinor",
    "abi",
    "platformFamily",
    "architecture",
    "macOSVersion",
    "environmentId",
}
MARKER_FIELDS = {
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
MARKER_DISTRIBUTION_FIELDS = {"name", "version", "hashes"}
WHEELHOUSE_FIELDS = {
    "schemaVersion",
    "product",
    "dependencyFingerprint",
    "lockSha256",
    "environmentId",
    "profiles",
    "files",
}
WHEELHOUSE_FILE_FIELDS = {"filename", "sha256"}


class ContractError(RuntimeError):
    """A stable, non-secret, operator-facing dependency contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContractSelection:
    lock_path: Path
    lock_sha256: str
    environment_probe: dict[str, Any]
    lock_environment: dict[str, Any]
    environment_id: str
    profiles: tuple[str, ...]
    direct_dependencies: tuple[dict[str, Any], ...]
    distributions: tuple[dict[str, Any], ...]
    fingerprint_payload: dict[str, Any]
    fingerprint: str

    def marker_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": MARKER_SCHEMA_VERSION,
            "product": PRODUCT,
            "fingerprintAlgorithm": FINGERPRINT_ALGORITHM,
            "dependencyFingerprint": self.fingerprint,
            "lockSha256": self.lock_sha256,
            "environmentId": self.environment_id,
            "lockEnvironment": dict(self.lock_environment),
            "profiles": list(self.profiles),
            "directDependencies": [dict(item) for item in self.direct_dependencies],
            "distributions": [
                {
                    "name": item["name"],
                    "version": item["version"],
                    "hashes": list(item["hashes"]),
                }
                for item in self.distributions
            ],
        }


def _error(code: str, message: str) -> ContractError:
    return ContractError(code, message)


def _require_exact_fields(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _error("invalid-schema", f"{label} has an invalid exact schema")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_bytes(raw: bytes, *, label: str) -> Any:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise _error("invalid-json", f"{label} is empty or exceeds the size limit")
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _error("invalid-json", f"{label} is not strict JSON") from exc


def _read_regular_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    require_private_owner: bool = False,
    required_mode: int | None = None,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _error("missing-file", f"{label} is missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _error("unsafe-file", f"{label} must be a regular non-symlink file") from exc
        raise _error("unreadable-file", f"{label} cannot be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise _error("unsafe-file", f"{label} must be a regular non-symlink file")
        if details.st_nlink != 1:
            raise _error("unsafe-file", f"{label} must not be hard linked")
        if details.st_size <= 0 or details.st_size > maximum_bytes:
            raise _error("unsafe-file", f"{label} is empty or exceeds the size limit")
        if require_private_owner:
            if details.st_uid != os.getuid():
                raise _error("unsafe-owner", f"{label} is not owned by the current user")
            if stat.S_IMODE(details.st_mode) & 0o022:
                raise _error("unsafe-permissions", f"{label} is group/world writable")
        if required_mode is not None and stat.S_IMODE(details.st_mode) != required_mode:
            raise _error("unsafe-permissions", f"{label} must have {required_mode:04o} permissions")
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) != details.st_size or any(getattr(final, key) != getattr(details, key) for key in identity):
            raise _error("unsafe-file", f"{label} changed while it was read")
        return raw
    except OSError as exc:
        raise _error("unreadable-file", f"{label} cannot be read") from exc
    finally:
        os.close(descriptor)


def _read_strict_json_file(
    path: Path,
    *,
    label: str,
    require_private_owner: bool = False,
    required_mode: int | None = None,
) -> tuple[Any, bytes]:
    raw = _read_regular_file(
        path,
        label=label,
        maximum_bytes=MAX_JSON_BYTES,
        require_private_owner=require_private_owner,
        required_mode=required_mode,
    )
    return _strict_json_bytes(raw, label=label), raw


def runtime_dependency_profiles(
    runtime: Path | str,
    *,
    allow_untrusted_active_venv: bool = False,
    allow_legacy_settings: bool = False,
) -> dict[str, Any]:
    """Read the Runtime-owned settings that select dependency profiles.

    This deliberately returns only non-secret booleans/profile identifiers. It
    never turns profile inheritance into a Settings mutation request.
    """

    home = _lexical_absolute(runtime)
    config_directory = home / "config"
    settings_path = config_directory / "settings.json"
    _validate_non_symlink_chain(config_directory, allow_missing_tail=False)
    for directory, label in (
        (home, "Runtime home"),
        (config_directory, "Runtime config directory"),
    ):
        try:
            details = directory.lstat()
        except OSError as exc:
            raise _error("settings-profile-untrusted", f"{label} is unavailable") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _error("settings-profile-untrusted", f"{label} is not a regular directory")
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o022:
            raise _error("settings-profile-untrusted", f"{label} has unsafe ownership or permissions")

    settings, settings_raw = _read_strict_json_file(
        settings_path,
        label="Runtime Settings",
        require_private_owner=True,
    )
    if not isinstance(settings, dict):
        raise _error("settings-profile-untrusted", "Runtime Settings must be a JSON object")
    if allow_legacy_settings:
        schema_version = settings.get("schemaVersion")
        if schema_version is not None and not (
            type(schema_version) is int and schema_version in {0, 1}
        ):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings use an unsupported schema version",
            )
    features = settings.get("features")
    rag = settings.get("rag")
    if allow_legacy_settings and "features" not in settings:
        features = {}
    if allow_legacy_settings and "rag" not in settings:
        rag = {}
    if not isinstance(features, dict) or not isinstance(rag, dict):
        raise _error(
            "settings-profile-untrusted",
            "Runtime Settings do not contain a trustworthy RAG dependency profile",
        )
    feature_enabled = features.get("rag") if "rag" in features else None
    rag_enabled_value = rag.get("enabled") if "enabled" in rag else None
    if allow_legacy_settings:
        if (
            (feature_enabled is not None and type(feature_enabled) is not bool)
            or (rag_enabled_value is not None and type(rag_enabled_value) is not bool)
        ):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings contain an ambiguous RAG dependency profile",
            )
        # Pre-GitHub Settings may contain a stale top-level feature mirror.
        # The Runtime itself treats rag.enabled as the explicit value and uses
        # features.rag only as its default, so repair must preserve that same
        # precedence before synchronizing the mirror during migration.
        if type(rag_enabled_value) is bool:
            rag_enabled = rag_enabled_value
        elif type(feature_enabled) is bool:
            rag_enabled = feature_enabled
        else:
            rag_enabled = False
    else:
        if type(feature_enabled) is not bool or type(rag_enabled_value) is not bool:
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings contain an ambiguous RAG dependency profile",
            )
        rag_enabled = rag_enabled_value
    if (
        not allow_legacy_settings
        and type(feature_enabled) is bool
        and feature_enabled != rag_enabled
    ):
        raise _error(
            "settings-profile-untrusted",
            "Runtime Settings contain conflicting RAG dependency profile flags",
        )

    profiles = ["dashboard"]
    embedding_mode: str | None = None
    if rag_enabled:
        embedding = rag.get("embedding")
        if allow_legacy_settings and not isinstance(embedding, dict) and (
            "embedding" not in rag or embedding is None
        ):
            embedding = {}
        if not isinstance(embedding, dict):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings do not identify a supported RAG embedding dependency profile",
            )
        mode = embedding.get("mode")
        provider = embedding.get("provider")
        supported_modes = ("local", "cloud")
        if mode is not None:
            if mode not in supported_modes:
                raise _error(
                    "settings-profile-untrusted",
                    "Runtime Settings do not identify a supported RAG embedding dependency profile",
                )
            embedding_mode = str(mode)
        elif allow_legacy_settings:
            normalized_provider = str(provider or "").strip()
            if normalized_provider in supported_modes:
                embedding_mode = normalized_provider
            elif normalized_provider:
                embedding_mode = "cloud"
            else:
                embedding_mode = "local"
        else:
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings do not identify a supported RAG embedding dependency profile",
            )
        profiles.append("rag-server")
        if embedding_mode == "local":
            profiles.append("rag-local")

    # Settings are authoritative for feature-dependent profiles.  The active
    # immutable dependency marker is authoritative for operational profiles
    # that Settings intentionally do not model (currently ``dev-test``).
    # Missing markers identify a legacy Runtime and conservatively select no
    # unprovable operational profile; malformed markers fail closed.
    active_venv: Path | None
    try:
        active_venv, _ = _managed_active_venv(home)
    except ContractError as exc:
        if not allow_untrusted_active_venv:
            raise _error(
                "settings-profile-untrusted",
                "active Runtime venv cannot provide trustworthy dependency profile evidence",
            ) from exc
        active_venv = None
    marker_path = active_venv / MARKER_NAME if active_venv is not None else None
    marker_status = "missing" if active_venv is not None else "unavailable"
    marker_sha256: str | None = None
    if marker_path is not None and (marker_path.exists() or marker_path.is_symlink()):
        try:
            marker_payload, marker_raw = _read_strict_json_file(
                marker_path,
                label="dependency marker",
                require_private_owner=True,
                required_mode=0o444,
            )
            marker = _validate_marker_payload(marker_payload)
            marker_profiles = set(marker["profiles"])
            supported_profiles = {"dashboard", "rag-server", "rag-local", "dev-test"}
            if (
                "dashboard" not in marker_profiles
                or not marker_profiles.issubset(supported_profiles)
                or ("rag-local" in marker_profiles and "rag-server" not in marker_profiles)
            ):
                raise _error(
                    "settings-profile-untrusted",
                    "active dependency marker contains an unsupported profile selection",
                )
            if "dev-test" in marker_profiles:
                profiles.append("dev-test")
            marker_status = "trusted"
            marker_sha256 = _sha256_bytes(marker_raw)
        except ContractError:
            if not allow_untrusted_active_venv:
                raise
            active_venv = None
            marker_status = "unavailable"
            marker_sha256 = None
    return {
        "schemaVersion": 1,
        "status": "ok",
        "profiles": sorted(profiles),
        "rag": {"enabled": rag_enabled, "embeddingMode": embedding_mode},
        "evidence": {
            "settingsSha256": _sha256_bytes(settings_raw),
            "activeVenvTarget": str(
                active_venv.resolve(strict=True)
                if active_venv is not None
                else home / ".venv"
            ),
            "activeMarkerStatus": marker_status,
            "activeMarkerSha256": marker_sha256,
        },
    }


def migrate_legacy_runtime_settings(
    runtime: Path | str,
    *,
    scheduler_enabled: bool | None = None,
    dashboard_enabled: bool | None = None,
    dashboard_server_enabled: bool | None = None,
    rag_server_enabled: bool | None = None,
) -> dict[str, Any]:
    """Complete trusted pre-GitHub RAG profile fields without replacing choices.

    The repair path uses this narrow mutation after dependency selection.  It
    never fills defaults or rewrites an already-current Settings file.
    """

    profile = runtime_dependency_profiles(
        runtime,
        allow_untrusted_active_venv=True,
        allow_legacy_settings=True,
    )
    home = _lexical_absolute(runtime)
    settings_path = home / "config" / "settings.json"
    settings, raw = _read_strict_json_file(
        settings_path,
        label="Runtime Settings",
        require_private_owner=True,
    )
    if not isinstance(settings, dict):
        raise _error("settings-profile-untrusted", "Runtime Settings must be a JSON object")
    if _sha256_bytes(raw) != profile["evidence"]["settingsSha256"]:
        raise _error("settings-profile-untrusted", "Runtime Settings changed during repair")

    schema_version = settings.get("schemaVersion")
    changed = False
    if schema_version != 1:
        if schema_version is not None and not (
            type(schema_version) is int and schema_version == 0
        ):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings use an unsupported schema version",
            )
        settings["schemaVersion"] = 1
        changed = True

    features = settings.get("features")
    if not isinstance(features, dict):
        features = {}
        settings["features"] = features
        changed = True
    rag = settings.get("rag")
    if not isinstance(rag, dict):
        rag = {}
        settings["rag"] = rag
        changed = True
    enabled = profile["rag"]["enabled"] is True
    if features.get("rag") is not enabled:
        features["rag"] = enabled
        changed = True
    if "enabled" not in rag:
        rag["enabled"] = enabled
        changed = True

    if enabled:
        embedding = rag.get("embedding")
        if not isinstance(embedding, dict):
            embedding = {}
            rag["embedding"] = embedding
            changed = True
        mode = profile["rag"]["embeddingMode"]
        if mode not in {"local", "cloud"}:
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings do not identify a supported legacy RAG profile",
            )
        if embedding.get("mode") is None:
            embedding["mode"] = mode
            changed = True
        if embedding.get("providerId") is None:
            legacy_provider = str(embedding.get("provider") or "").strip()
            embedding["providerId"] = (
                legacy_provider
                if legacy_provider and legacy_provider not in {"local", "cloud"}
                else mode
            )
            changed = True

    service_defaults = (
        ("scheduler", scheduler_enabled),
        ("dashboard", dashboard_enabled),
        ("dashboard server", dashboard_server_enabled),
        ("RAG server", rag_server_enabled),
    )
    if any(value is not None and type(value) is not bool for _label, value in service_defaults):
        raise _error(
            "settings-profile-untrusted",
            "Legacy service defaults must be booleans",
        )

    if scheduler_enabled is not None:
        schedule = settings.get("schedule")
        if schedule is None:
            schedule = {}
            settings["schedule"] = schedule
            changed = True
        if not isinstance(schedule, dict):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings contain an invalid scheduler section",
            )
        if "enabled" not in schedule:
            schedule["enabled"] = scheduler_enabled
            changed = True

    if dashboard_enabled is not None or dashboard_server_enabled is not None:
        dashboard = settings.get("dashboard")
        if dashboard is None:
            dashboard = {}
            settings["dashboard"] = dashboard
            changed = True
        if not isinstance(dashboard, dict):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings contain an invalid Dashboard section",
            )
        if dashboard_enabled is not None and "dashboard" not in features:
            features["dashboard"] = dashboard_enabled
            changed = True
        if dashboard_server_enabled is not None:
            server = dashboard.get("server")
            if server is None:
                server = {}
                dashboard["server"] = server
                changed = True
            if not isinstance(server, dict):
                raise _error(
                    "settings-profile-untrusted",
                    "Runtime Settings contain an invalid Dashboard server section",
                )
            if "enabled" not in server:
                server["enabled"] = dashboard_server_enabled
                changed = True

    if rag_server_enabled is not None:
        server = rag.get("server")
        if server is None:
            server = {}
            rag["server"] = server
            changed = True
        if not isinstance(server, dict):
            raise _error(
                "settings-profile-untrusted",
                "Runtime Settings contain an invalid RAG server section",
            )
        if "enabled" not in server:
            server["enabled"] = rag_server_enabled
            changed = True

    if not changed:
        return {
            "schemaVersion": 1,
            "status": "unchanged",
            "settingsMigrated": False,
        }

    if features.get("rag") is not enabled or rag.get("enabled") is not enabled:
        raise _error(
            "settings-profile-untrusted",
            "Runtime Settings changed inconsistently during legacy migration",
        )
    details = settings_path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise _error(
            "settings-profile-untrusted",
            "Runtime Settings changed to an unsafe file during repair",
        )
    identity = tuple(
        getattr(details, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )
    payload = (
        json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write(
        settings_path,
        payload,
        mode=stat.S_IMODE(details.st_mode),
        replace=True,
        expected_identity=identity,
    )
    return {
        "schemaVersion": 1,
        "status": "migrated",
        "settingsMigrated": True,
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _error("untrusted-cache", "cached wheel cannot be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise _error("untrusted-cache", "cached wheel has unsafe file metadata")
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        final = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if total != details.st_size or any(getattr(final, key) != getattr(details, key) for key in identity):
            raise _error("untrusted-cache", "cached wheel changed while it was hashed")
        return digest.hexdigest()
    except OSError as exc:
        raise _error("untrusted-cache", "cached wheel cannot be read") from exc
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(
    path: Path,
    raw: bytes,
    *,
    mode: int,
    replace: bool,
    expected_identity: tuple[int, ...] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if replace:
            if expected_identity is not None:
                try:
                    current = path.lstat()
                except OSError as exc:
                    raise _error("unsafe-file", f"file changed before replacement: {path.name}") from exc
                current_identity = tuple(
                    getattr(current, field)
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_nlink",
                        "st_uid",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                )
                if current_identity != expected_identity:
                    raise _error("unsafe-file", f"file changed before replacement: {path.name}")
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise _error("immutable-file-exists", f"immutable file already exists: {path.name}") from exc
            temporary.unlink()
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def canonical_distribution_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise _error("invalid-distribution", "dependency distribution name is invalid")
    return re.sub(r"[-_.]+", "-", value).lower()


def _dependency_name(value: str) -> str:
    match = DIRECT_DEPENDENCY_NAME_RE.match(value)
    if not match:
        raise _error("invalid-dependency", "direct dependency has no valid distribution name")
    return canonical_distribution_name(match.group(1))


def normalize_direct_dependency(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value or "\n" in value or "\r" in value:
        raise _error("invalid-dependency", "direct dependency must be one non-empty line")
    raw = value.strip()
    if any(token in raw for token in (";", "@", "[", "]")):
        raise _error(
            "invalid-dependency",
            "runtime direct dependencies must use simple name/specifier contracts",
        )
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(.*)", raw)
    if not match:
        raise _error("invalid-dependency", "direct dependency has an invalid name")
    name = canonical_distribution_name(match.group(1))
    specifier = re.sub(r"\s+", "", match.group(2))
    if not specifier:
        return name
    parts = specifier.split(",")
    if any(
        not part or not re.fullmatch(r"(?:===|==|!=|~=|<=|>=|<|>)[^,]+", part)
        for part in parts
    ):
        raise _error("invalid-dependency", "runtime dependency specifier is unsupported")
    normalized = name + ",".join(sorted(parts))
    if len(normalized) > 1024:
        raise _error("invalid-dependency", "direct dependency exceeds the size limit")
    return normalized


def normalize_profiles(values: Iterable[str]) -> tuple[str, ...]:
    profiles: set[str] = set()
    for raw in values:
        for item in str(raw).split(","):
            profile = item.strip().lower()
            if not profile:
                continue
            if not SAFE_ID_RE.fullmatch(profile):
                raise _error("invalid-profile", "dependency profile id is invalid")
            profiles.add(profile)
    if not profiles:
        raise _error("invalid-profile", "at least one dependency profile is required")
    return tuple(sorted(profiles))


def normalize_architecture(value: str) -> str:
    architecture = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "x86_64": "x86_64",
    }
    architecture = aliases.get(architecture, architecture)
    if not SAFE_ID_RE.fullmatch(architecture):
        raise _error("unsupported-environment", "runtime architecture is unsupported")
    return architecture


def platform_family(system: str) -> str:
    families = {"darwin": "macos", "linux": "linux", "windows": "windows"}
    family = families.get(str(system or "").strip().lower())
    if family is None:
        raise _error("unsupported-environment", "runtime platform family is unsupported")
    return family


def _environment_id(environment: dict[str, Any]) -> str:
    raw = "-".join(
        (
            str(environment["implementation"]),
            str(environment["pythonMajorMinor"]),
            str(environment["abi"]),
            str(environment["platformFamily"]),
            str(environment["architecture"]),
        )
    ).lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-.")
    if not value or len(value) > 128:
        raise _error("unsupported-environment", "runtime environment id is invalid")
    return value


def _validate_probe(value: Any) -> dict[str, Any]:
    probe = _require_exact_fields(value, PROBE_FIELDS, label="environment probe")
    for key in ("implementation", "abi", "platformFamily", "architecture", "environmentId"):
        if not isinstance(probe[key], str) or not SAFE_ID_RE.fullmatch(probe[key].lower()):
            raise _error("invalid-environment-probe", f"environment probe field is invalid: {key}")
    if not isinstance(probe["pythonMajorMinor"], str) or not PYTHON_VERSION_RE.fullmatch(probe["pythonMajorMinor"]):
        raise _error("invalid-environment-probe", "environment probe Python version is invalid")
    if probe["platformFamily"] == "macos":
        if not isinstance(probe["macOSVersion"], str) or not MACOS_VERSION_RE.fullmatch(probe["macOSVersion"]):
            raise _error("invalid-environment-probe", "macOS version could not be determined")
    elif probe["macOSVersion"] is not None:
        raise _error("invalid-environment-probe", "non-macOS probe contains a macOS version")
    computed = _environment_id(probe)
    if probe["environmentId"] != computed:
        raise _error("invalid-environment-probe", "environment probe id does not match its fields")
    return dict(probe)


ENVIRONMENT_PROBE_SCRIPT = r'''
import json
import platform
import re
import sys
import sysconfig

def family(system):
    value = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(system.lower())
    if value is None:
        raise SystemExit(7)
    return value

def architecture(value):
    raw = value.strip().lower().replace(" ", "_")
    return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64", "x86-64": "x86_64"}.get(raw, raw)

implementation = platform.python_implementation().strip().lower()
major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
abi = str(sysconfig.get_config_var("SOABI") or getattr(sys.implementation, "cache_tag", "") or "unknown").lower()
platform_family = family(platform.system())
arch = architecture(platform.machine())
macos_version = (platform.mac_ver()[0] or None) if platform_family == "macos" else None
parts = (implementation, major_minor, abi, platform_family, arch)
environment_id = re.sub(r"[^a-z0-9._-]+", "-", "-".join(parts).lower()).strip("-.")
print(json.dumps({
    "implementation": implementation,
    "pythonMajorMinor": major_minor,
    "abi": abi,
    "platformFamily": platform_family,
    "architecture": arch,
    "macOSVersion": macos_version,
    "environmentId": environment_id,
}, sort_keys=True, separators=(",", ":")))
'''


def _run_json_python(python: Path, script: str, *, code: str) -> Any:
    if not python.exists() or not python.is_file() or not os.access(python, os.X_OK):
        raise _error(code, "target Python executable is unavailable")
    environment = dict(os.environ)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", script],
            text=False,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error(code, "target Python probe failed") from exc
    if result.returncode != 0 or len(result.stdout) > MAX_SUBPROCESS_JSON_BYTES:
        raise _error(code, "target Python probe returned an invalid result")
    return _strict_json_bytes(result.stdout, label="target Python probe output")


def probe_environment(python: Path | str = sys.executable) -> dict[str, Any]:
    value = _run_json_python(Path(python), ENVIRONMENT_PROBE_SCRIPT, code="environment-probe-failed")
    return _validate_probe(value)


def current_environment_probe() -> dict[str, Any]:
    family = platform_family(platform.system())
    implementation = platform.python_implementation().strip().lower()
    abi = str(sysconfig.get_config_var("SOABI") or getattr(sys.implementation, "cache_tag", "") or "unknown").lower()
    value: dict[str, Any] = {
        "implementation": implementation,
        "pythonMajorMinor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "abi": abi,
        "platformFamily": family,
        "architecture": normalize_architecture(platform.machine()),
        "macOSVersion": (platform.mac_ver()[0] or None) if family == "macos" else None,
    }
    value["environmentId"] = _environment_id(value)
    return _validate_probe(value)


def lock_environment_from_probe(
    probe: dict[str, Any],
    *,
    minimum_macos: str | None = None,
) -> dict[str, Any]:
    checked = _validate_probe(probe)
    if checked["platformFamily"] == "macos":
        minimum = minimum_macos or checked["macOSVersion"]
        if not isinstance(minimum, str) or not MACOS_VERSION_RE.fullmatch(minimum):
            raise _error("invalid-lock-environment", "minimumMacOS is invalid")
    else:
        if minimum_macos is not None:
            raise _error("invalid-lock-environment", "minimumMacOS is only valid for macOS")
        minimum = None
    return {
        "implementation": checked["implementation"],
        "pythonMajorMinor": checked["pythonMajorMinor"],
        "abi": checked["abi"],
        "platformFamily": checked["platformFamily"],
        "architecture": checked["architecture"],
        "minimumMacOS": minimum,
    }


def _macos_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not MACOS_VERSION_RE.fullmatch(value):
        raise _error("invalid-lock-environment", "macOS version is invalid")
    parts = [int(item) for item in value.split(".")]
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _read_pyproject_contract(path: Path) -> dict[str, tuple[str, ...]]:
    raw = _read_regular_file(path, label="pyproject.toml", maximum_bytes=MAX_PYPROJECT_BYTES)
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _error("invalid-pyproject", "pyproject.toml is invalid") from exc
    project = payload.get("project")
    if not isinstance(project, dict) or project.get("name") != PRODUCT:
        raise _error("invalid-pyproject", "pyproject.toml does not describe actanara")
    base = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    if not isinstance(base, list) or not isinstance(optional, dict):
        raise _error("invalid-pyproject", "pyproject dependency tables are invalid")
    if base:
        raise _error(
            "invalid-pyproject",
            "project.dependencies must remain empty because the runtime lock is profile-scoped",
        )
    result: dict[str, tuple[str, ...]] = {}
    for profile, raw_dependencies in optional.items():
        if not isinstance(profile, str) or not SAFE_ID_RE.fullmatch(profile.lower()):
            raise _error("invalid-pyproject", "pyproject dependency profile id is invalid")
        if not isinstance(raw_dependencies, list) or any(not isinstance(item, str) for item in raw_dependencies):
            raise _error("invalid-pyproject", "pyproject dependency profile must be a string array")
        normalized = tuple(sorted(normalize_direct_dependency(item) for item in raw_dependencies))
        if len(set(normalized)) != len(normalized):
            raise _error("invalid-pyproject", "pyproject dependency profile contains duplicates")
        result[profile.lower()] = normalized
    return result


def _validate_lock_environment(value: Any) -> dict[str, Any]:
    environment = _require_exact_fields(
        value,
        LOCK_ENVIRONMENT_IDENTITY_FIELDS,
        label="lock environment identity",
    )
    for key in ("implementation", "abi", "platformFamily", "architecture"):
        if not isinstance(environment[key], str) or not SAFE_ID_RE.fullmatch(environment[key].lower()):
            raise _error("invalid-lock", f"lock environment field is invalid: {key}")
    if not isinstance(environment["pythonMajorMinor"], str) or not PYTHON_VERSION_RE.fullmatch(environment["pythonMajorMinor"]):
        raise _error("invalid-lock", "lock Python major/minor is invalid")
    if environment["platformFamily"] == "macos":
        minimum = environment["minimumMacOS"]
        if not isinstance(minimum, str) or not MACOS_VERSION_RE.fullmatch(minimum):
            raise _error("invalid-lock", "macOS lock requires minimumMacOS")
    elif environment["minimumMacOS"] is not None:
        raise _error("invalid-lock", "minimumMacOS must be null for non-macOS locks")
    return dict(environment)


def _parse_lock(path: Path) -> tuple[dict[str, Any], bytes]:
    payload, raw = _read_strict_json_file(path, label="runtime dependency lock")
    root = _require_exact_fields(payload, LOCK_ROOT_FIELDS, label="runtime dependency lock")
    if (
        type(root["schemaVersion"]) is not int
        or root["schemaVersion"] != LOCK_SCHEMA_VERSION
        or root["product"] != PRODUCT
    ):
        raise _error("invalid-lock", "runtime dependency lock version/product is unsupported")
    policy = _require_exact_fields(
        root["artifactPolicy"],
        LOCK_ARTIFACT_POLICY_FIELDS,
        label="runtime lock artifact policy",
    )
    if not (
        policy["hashAlgorithm"] == "sha256"
        and policy["hashesRequired"] is True
        and policy["sourceBuildsAllowed"] is False
        and policy["wheelsOnly"] is True
    ):
        raise _error("invalid-lock", "runtime lock artifact policy is unsupported")
    resolver = _require_exact_fields(
        root["resolver"], LOCK_RESOLVER_FIELDS, label="runtime lock resolver"
    )
    if (
        resolver["name"] != "pip"
        or resolver["reportSchemaVersion"] != "1"
        or not isinstance(resolver["version"], str)
        or not SAFE_VERSION_RE.fullmatch(resolver["version"])
    ):
        raise _error("invalid-lock", "runtime lock resolver evidence is invalid")

    raw_profiles = root["profiles"]
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise _error("invalid-lock", "runtime dependency lock has no profiles")
    profiles: dict[str, dict[str, list[str]]] = {}
    for profile_name, raw_contract in raw_profiles.items():
        if (
            not isinstance(profile_name, str)
            or profile_name != profile_name.lower()
            or not SAFE_ID_RE.fullmatch(profile_name)
        ):
            raise _error("invalid-lock", "runtime lock profile id is invalid")
        contract = _require_exact_fields(
            raw_contract, LOCK_PROFILE_FIELDS, label="runtime lock profile"
        )
        direct = contract["directRequirements"]
        audit_packages = contract["packages"]
        if not isinstance(direct, list) or any(not isinstance(item, str) for item in direct):
            raise _error("invalid-lock", "runtime lock direct requirements must be a string array")
        normalized_direct = [normalize_direct_dependency(item) for item in direct]
        if normalized_direct != sorted(set(normalized_direct)) or direct != normalized_direct:
            raise _error(
                "invalid-lock",
                "runtime lock direct requirements must be canonical, sorted, and unique",
            )
        if (
            not isinstance(audit_packages, list)
            or not audit_packages
            or any(not isinstance(item, str) for item in audit_packages)
        ):
            raise _error("invalid-lock", "runtime lock profile audit packages are invalid")
        normalized_audit = [canonical_distribution_name(item) for item in audit_packages]
        if normalized_audit != sorted(set(normalized_audit)) or audit_packages != normalized_audit:
            raise _error(
                "invalid-lock",
                "runtime lock profile audit packages must be canonical, sorted, and unique",
            )
        if not {_dependency_name(item) for item in normalized_direct}.issubset(
            set(normalized_audit)
        ):
            raise _error("invalid-lock", "runtime lock profile audit omits a direct requirement")
        profiles[profile_name] = {
            "directRequirements": normalized_direct,
            "packages": normalized_audit,
        }

    raw_environments = root["environments"]
    if not isinstance(raw_environments, dict) or not raw_environments:
        raise _error("invalid-lock", "runtime dependency lock has no environments")
    environments: dict[str, dict[str, Any]] = {}
    for environment_id, raw_environment in raw_environments.items():
        if (
            not isinstance(environment_id, str)
            or environment_id != environment_id.lower()
            or not SAFE_ID_RE.fullmatch(environment_id)
        ):
            raise _error("invalid-lock", "runtime lock environment id is invalid")
        environment = _require_exact_fields(
            raw_environment, LOCK_ENVIRONMENT_FIELDS, label="runtime lock environment"
        )
        identity = _validate_lock_environment(
            {key: environment[key] for key in LOCK_ENVIRONMENT_IDENTITY_FIELDS}
        )
        supported = environment["supportedProfiles"]
        if (
            not isinstance(supported, list)
            or not supported
            or any(not isinstance(item, str) for item in supported)
            or supported != sorted(set(supported))
            or any(item not in profiles for item in supported)
        ):
            raise _error("invalid-lock", "runtime lock supportedProfiles is invalid")

        raw_packages = environment["packages"]
        if not isinstance(raw_packages, list) or not raw_packages:
            raise _error("invalid-lock", "runtime lock environment has no packages")
        packages: dict[str, dict[str, str]] = {}
        filenames: set[str] = set()
        package_order: list[str] = []
        for raw_package in raw_packages:
            package = _require_exact_fields(
                raw_package, LOCK_PACKAGE_FIELDS, label="runtime lock package"
            )
            name = canonical_distribution_name(package["name"])
            if name != package["name"] or name in packages:
                raise _error("invalid-lock", "runtime lock package name is non-canonical or duplicated")
            version = package["version"]
            filename = package["filename"]
            digest = package["sha256"]
            url = package["url"]
            if not isinstance(version, str) or not SAFE_VERSION_RE.fullmatch(version):
                raise _error("invalid-lock", "runtime lock package version is invalid")
            if (
                not isinstance(filename, str)
                or not WHEEL_FILENAME_RE.fullmatch(filename)
                or filename in filenames
            ):
                raise _error("invalid-lock", "runtime lock wheel filename is unsafe or duplicated")
            if not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
                raise _error("invalid-lock", "runtime lock wheel SHA-256 is invalid")
            if not isinstance(url, str):
                raise _error("invalid-lock", "runtime lock wheel URL is invalid")
            try:
                parsed_url = urlsplit(url)
                port = parsed_url.port
            except ValueError as exc:
                raise _error("invalid-lock", "runtime lock wheel URL is invalid") from exc
            url_filename = unquote(parsed_url.path.rsplit("/", 1)[-1])
            trusted_pytorch_cpu = (
                name == "torch"
                and parsed_url.hostname in PYTORCH_CPU_ARTIFACT_HOSTS
                and parsed_url.path.startswith("/whl/cpu/")
            )
            if (
                parsed_url.scheme != "https"
                or not (
                    parsed_url.hostname == PYPI_ARTIFACT_HOST
                    or trusted_pytorch_cpu
                )
                or parsed_url.username is not None
                or parsed_url.password is not None
                or port is not None
                or parsed_url.query
                or parsed_url.fragment
                or url_filename != filename
            ):
                raise _error(
                    "invalid-lock",
                    "runtime lock wheel URL must be an approved exact HTTPS artifact",
                )
            filenames.add(filename)
            package_order.append(name)
            packages[name] = {
                "name": name,
                "version": version,
                "filename": filename,
                "sha256": digest,
                "url": url,
            }
        if package_order != sorted(package_order):
            raise _error("invalid-lock", "runtime lock environment packages are not sorted")

        raw_profile_packages = environment["profilePackages"]
        if not isinstance(raw_profile_packages, dict) or set(raw_profile_packages) != set(supported):
            raise _error(
                "invalid-lock",
                "runtime lock profilePackages must exactly cover supportedProfiles",
            )
        profile_packages: dict[str, list[str]] = {}
        referenced: set[str] = set()
        for profile_name in supported:
            raw_closure = raw_profile_packages[profile_name]
            if (
                not isinstance(raw_closure, list)
                or not raw_closure
                or any(not isinstance(item, str) for item in raw_closure)
            ):
                raise _error("invalid-lock", "runtime lock profile package closure is invalid")
            closure = [canonical_distribution_name(item) for item in raw_closure]
            if closure != sorted(set(closure)) or raw_closure != closure:
                raise _error(
                    "invalid-lock",
                    "runtime lock profile package closure must be canonical, sorted, and unique",
                )
            if any(item not in packages for item in closure):
                raise _error("invalid-lock", "runtime lock profile references an unknown package")
            direct_names = {
                _dependency_name(item)
                for item in profiles[profile_name]["directRequirements"]
            }
            if not direct_names.issubset(set(closure)):
                raise _error("invalid-lock", "runtime lock profile closure omits a direct requirement")
            referenced.update(closure)
            profile_packages[profile_name] = closure
        if set(packages) != referenced:
            raise _error("invalid-lock", "runtime lock environment contains unreferenced packages")
        environments[environment_id] = {
            "environmentId": environment_id,
            "environment": identity,
            "supportedProfiles": list(supported),
            "profilePackages": profile_packages,
            "packages": packages,
        }
    return {
        "artifactPolicy": dict(policy),
        "resolver": dict(resolver),
        "profiles": profiles,
        "environments": environments,
    }, raw


def _target_matches_probe(target: dict[str, Any], probe: dict[str, Any]) -> bool:
    environment = target["environment"]
    for key in ("implementation", "pythonMajorMinor", "abi", "platformFamily", "architecture"):
        if environment[key] != probe[key]:
            return False
    return True


def _select_target(lock: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    matches = [
        target
        for target in lock["environments"].values()
        if _target_matches_probe(target, probe)
    ]
    if len(matches) != 1:
        raise _error("unsupported-environment", "runtime lock has no unique target for this Python/platform/architecture")
    target = matches[0]
    minimum = target["environment"]["minimumMacOS"]
    if probe["platformFamily"] == "macos" and _macos_tuple(probe["macOSVersion"]) < _macos_tuple(minimum):
        raise _error(
            "unsupported-macos",
            f"current macOS is below the locked minimum {minimum}",
        )
    return target


def load_contract_selection(
    lock_path: Path | str,
    pyproject_path: Path | str,
    profiles: Iterable[str],
    *,
    python: Path | str = sys.executable,
    environment_probe: dict[str, Any] | None = None,
) -> ContractSelection:
    lock_file = Path(lock_path).expanduser().absolute()
    pyproject_file = Path(pyproject_path).expanduser().absolute()
    lock, raw_lock = _parse_lock(lock_file)
    pyproject = _read_pyproject_contract(pyproject_file)
    if set(pyproject) != set(lock["profiles"]):
        raise _error(
            "stale-lock",
            "runtime lock profiles do not exactly match pyproject optional dependencies",
        )
    for profile_name in sorted(pyproject):
        if tuple(lock["profiles"][profile_name]["directRequirements"]) != pyproject[profile_name]:
            raise _error(
                "stale-lock",
                f"runtime lock direct dependencies do not match pyproject profile: {profile_name}",
            )
    probe = _validate_probe(environment_probe) if environment_probe is not None else probe_environment(python)
    target = _select_target(lock, probe)
    selected_profiles = normalize_profiles(profiles)
    profile_records: list[dict[str, Any]] = []
    selected_distribution_names: set[str] = set()
    for profile in selected_profiles:
        expected = pyproject.get(profile)
        locked = lock["profiles"].get(profile)
        closure = target["profilePackages"].get(profile)
        if expected is None or locked is None or closure is None:
            raise _error("unsupported-profile", f"runtime lock does not support dependency profile: {profile}")
        selected_distribution_names.update(closure)
        profile_records.append(
            {
                "profile": profile,
                "requirements": list(expected),
            }
        )
    selected_distributions: list[dict[str, Any]] = []
    for name in sorted(selected_distribution_names):
        distribution = target["packages"][name]
        selected_distributions.append(
            {
                "name": name,
                "version": distribution["version"],
                "hashes": [f"sha256:{distribution['sha256']}"],
                "artifacts": [
                    {
                        "filename": distribution["filename"],
                        "sha256": distribution["sha256"],
                        "url": distribution["url"],
                    }
                ],
            }
        )
    runtime_environment = {
        key: target["environment"][key]
        for key in ("implementation", "pythonMajorMinor", "abi", "platformFamily", "architecture")
    }
    runtime_environment["environmentId"] = target["environmentId"]
    lock_sha = _sha256_bytes(raw_lock)
    fingerprint_payload = {
        "schemaVersion": FINGERPRINT_SCHEMA_VERSION,
        "algorithm": FINGERPRINT_ALGORITHM,
        "runtimeEnvironment": runtime_environment,
        "lockEnvironment": dict(target["environment"]),
        "profiles": list(selected_profiles),
        "directDependencies": profile_records,
        "runtimeLockSha256": lock_sha,
        "resolvedDistributions": [
            {
                "name": item["name"],
                "version": item["version"],
                "hashes": list(item["hashes"]),
            }
            for item in selected_distributions
        ],
    }
    fingerprint = _sha256_bytes(_canonical_json_bytes(fingerprint_payload))
    return ContractSelection(
        lock_path=lock_file,
        lock_sha256=lock_sha,
        environment_probe=probe,
        lock_environment=dict(target["environment"]),
        environment_id=target["environmentId"],
        profiles=selected_profiles,
        direct_dependencies=tuple(profile_records),
        distributions=tuple(selected_distributions),
        fingerprint_payload=fingerprint_payload,
        fingerprint=fingerprint,
    )


def hashed_requirements(selection: ContractSelection) -> str:
    lines: list[str] = []
    for distribution in selection.distributions:
        hashes = " ".join(f"--hash={item}" for item in distribution["hashes"])
        lines.append(f"{distribution['name']}=={distribution['version']} {hashes}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def exact_download_requirements(selection: ContractSelection) -> str:
    lines: list[str] = []
    for distribution in selection.distributions:
        artifacts = distribution["artifacts"]
        if len(artifacts) != 1:
            raise _error("invalid-lock", "each locked distribution must have one exact artifact")
        artifact = artifacts[0]
        lines.append(
            f"{distribution['name']} @ {artifact['url']} --hash=sha256:{artifact['sha256']}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def write_hashed_requirements(path: Path, selection: ContractSelection) -> dict[str, Any]:
    raw = hashed_requirements(selection).encode("utf-8")
    _atomic_write(path, raw, mode=0o444, replace=True)
    return {
        "path": str(path),
        "sha256": _sha256_bytes(raw),
        "distributionCount": len(selection.distributions),
    }


def _validate_marker_payload(value: Any) -> dict[str, Any]:
    marker = _require_exact_fields(value, MARKER_FIELDS, label="dependency marker")
    if (
        type(marker["schemaVersion"]) is not int
        or marker["schemaVersion"] != MARKER_SCHEMA_VERSION
        or marker["product"] != PRODUCT
        or marker["fingerprintAlgorithm"] != FINGERPRINT_ALGORITHM
    ):
        raise _error("invalid-marker", "dependency marker version/product is unsupported")
    for key in ("dependencyFingerprint", "lockSha256"):
        if not isinstance(marker[key], str) or not HEX_SHA256_RE.fullmatch(marker[key]):
            raise _error("invalid-marker", f"dependency marker field is invalid: {key}")
    if not isinstance(marker["environmentId"], str) or not SAFE_ID_RE.fullmatch(marker["environmentId"]):
        raise _error("invalid-marker", "dependency marker environmentId is invalid")
    environment = _validate_lock_environment(marker["lockEnvironment"])
    profiles = marker["profiles"]
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(not isinstance(profile, str) or not SAFE_ID_RE.fullmatch(profile) for profile in profiles)
        or tuple(profiles) != tuple(sorted(set(profiles)))
    ):
        raise _error("invalid-marker", "dependency marker profiles are invalid")
    direct = marker["directDependencies"]
    if not isinstance(direct, list):
        raise _error("invalid-marker", "dependency marker direct dependency records are invalid")
    seen_profiles: list[str] = []
    for item in direct:
        record = _require_exact_fields(item, {"profile", "requirements"}, label="marker direct dependency record")
        if record["profile"] not in profiles or record["profile"] in seen_profiles:
            raise _error("invalid-marker", "dependency marker direct dependency profile is invalid")
        requirements = record["requirements"]
        if not isinstance(requirements, list) or requirements != sorted(requirements):
            raise _error("invalid-marker", "dependency marker requirements are invalid")
        if any(normalize_direct_dependency(item_value) != item_value for item_value in requirements):
            raise _error("invalid-marker", "dependency marker requirement is non-canonical")
        seen_profiles.append(record["profile"])
    if seen_profiles != profiles:
        raise _error("invalid-marker", "dependency marker does not cover every selected profile")
    distributions = marker["distributions"]
    if not isinstance(distributions, list):
        raise _error("invalid-marker", "dependency marker distributions are invalid")
    seen_names: list[str] = []
    for item in distributions:
        record = _require_exact_fields(item, MARKER_DISTRIBUTION_FIELDS, label="marker distribution")
        name = canonical_distribution_name(record["name"])
        if name != record["name"] or name in seen_names:
            raise _error("invalid-marker", "dependency marker distribution name is invalid")
        if not isinstance(record["version"], str) or not SAFE_VERSION_RE.fullmatch(record["version"]):
            raise _error("invalid-marker", "dependency marker distribution version is invalid")
        hashes = record["hashes"]
        if (
            not isinstance(hashes, list)
            or not hashes
            or hashes != sorted(set(hashes))
            or any(not isinstance(item_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item_hash) for item_hash in hashes)
        ):
            raise _error("invalid-marker", "dependency marker distribution hashes are invalid")
        seen_names.append(name)
    if seen_names != sorted(seen_names):
        raise _error("invalid-marker", "dependency marker distributions are not sorted")
    fingerprint_payload = {
        "schemaVersion": FINGERPRINT_SCHEMA_VERSION,
        "algorithm": FINGERPRINT_ALGORITHM,
        "runtimeEnvironment": {
            key: marker["lockEnvironment"][key]
            for key in ("implementation", "pythonMajorMinor", "abi", "platformFamily", "architecture")
        }
        | {"environmentId": marker["environmentId"]},
        "lockEnvironment": dict(marker["lockEnvironment"]),
        "profiles": list(marker["profiles"]),
        "directDependencies": [dict(item) for item in marker["directDependencies"]],
        "runtimeLockSha256": marker["lockSha256"],
        "resolvedDistributions": [dict(item) for item in marker["distributions"]],
    }
    computed = _sha256_bytes(_canonical_json_bytes(fingerprint_payload))
    if marker["dependencyFingerprint"] != computed:
        raise _error("invalid-marker", "dependency marker fingerprint does not match its content")
    return marker


def read_dependency_marker(venv: Path | str) -> dict[str, Any]:
    root = Path(venv).expanduser().absolute()
    marker_path = root / MARKER_NAME
    payload, _ = _read_strict_json_file(
        marker_path,
        label="dependency marker",
        require_private_owner=True,
        required_mode=0o444,
    )
    return _validate_marker_payload(payload)


def verify_dependency_marker(venv: Path | str, selection: ContractSelection) -> dict[str, Any]:
    marker = read_dependency_marker(venv)
    expected = selection.marker_payload()
    if marker != expected:
        raise _error("marker-mismatch", "dependency marker does not match the requested runtime contract")
    return marker


def write_dependency_marker(
    venv: Path | str,
    selection: ContractSelection,
    *,
    live_python: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(venv).expanduser().absolute()
    try:
        details = root.lstat()
    except OSError as exc:
        raise _error("invalid-venv", "candidate venv is unavailable") from exc
    if root.is_symlink() or not stat.S_ISDIR(details.st_mode):
        raise _error("invalid-venv", "dependency marker must be written to a concrete venv generation")
    python = Path(live_python) if live_python is not None else root / "bin" / "python"
    validate_live_distributions(python, selection)
    path = root / MARKER_NAME
    payload = selection.marker_payload()
    raw = _canonical_json_bytes(payload) + b"\n"
    if path.exists() or path.is_symlink():
        existing = read_dependency_marker(root)
        if existing != payload:
            raise _error("immutable-marker-conflict", "existing dependency marker has different content")
        return {"status": "verified-existing", "path": str(path), **payload}
    _atomic_write(path, raw, mode=0o444, replace=False)
    verified = verify_dependency_marker(root, selection)
    return {"status": "written", "path": str(path), **verified}


LIVE_DISTRIBUTIONS_SCRIPT = r'''
import importlib.metadata
import json

records = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    version = distribution.version
    if isinstance(name, str) and isinstance(version, str):
        records.append({"name": name, "version": version})
print(json.dumps({"distributions": records}, sort_keys=True, separators=(",", ":")))
'''


def _live_distribution_map(python: Path | str) -> dict[str, str]:
    payload = _run_json_python(Path(python), LIVE_DISTRIBUTIONS_SCRIPT, code="distribution-probe-failed")
    root = _require_exact_fields(payload, {"distributions"}, label="distribution probe")
    records = root["distributions"]
    if not isinstance(records, list):
        raise _error("distribution-probe-failed", "distribution probe result is invalid")
    result: dict[str, str] = {}
    for item in records:
        record = _require_exact_fields(item, {"name", "version"}, label="live distribution")
        name = canonical_distribution_name(record["name"])
        version = record["version"]
        if not isinstance(version, str) or not SAFE_VERSION_RE.fullmatch(version):
            raise _error("distribution-probe-failed", "live distribution version is invalid")
        if name in result and result[name] != version:
            raise _error("distribution-probe-failed", "target venv has ambiguous distribution metadata")
        result[name] = version
    return result


def validate_live_distributions(python: Path | str, selection: ContractSelection) -> dict[str, Any]:
    live = _live_distribution_map(python)
    missing: list[str] = []
    mismatched: list[dict[str, str]] = []
    for distribution in selection.distributions:
        name = distribution["name"]
        actual = live.get(name)
        if actual is None:
            missing.append(name)
        elif actual != distribution["version"]:
            mismatched.append(
                {
                    "name": name,
                    "expected": distribution["version"],
                    "actual": actual,
                }
            )
    if missing or mismatched:
        raise _error("live-distributions-mismatch", "target venv distributions do not match the runtime lock")
    return {
        "status": "valid",
        "python": str(Path(python)),
        "verified": len(selection.distributions),
        "missing": [],
        "mismatched": [],
    }


def _lexical_absolute(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _path_chain(path: Path) -> list[Path]:
    absolute = _lexical_absolute(path)
    return list(reversed((absolute, *absolute.parents)))


def _validate_non_symlink_chain(path: Path, *, allow_missing_tail: bool) -> None:
    missing = False
    for component in _path_chain(path):
        try:
            details = component.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise _error("unsafe-path", "cache directory chain cannot be inspected") from exc
        if missing and not allow_missing_tail:
            raise _error("unsafe-path", "cache directory chain is incomplete")
        if stat.S_ISLNK(details.st_mode):
            raise _error("unsafe-path", "cache directory chain contains a symlink")
        if not stat.S_ISDIR(details.st_mode):
            raise _error("unsafe-path", "cache directory chain contains a non-directory")
        owner_allowed = details.st_uid in {0, os.getuid()}
        mode = stat.S_IMODE(details.st_mode)
        sticky_system_parent = details.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if not owner_allowed or ((mode & 0o022) and not sticky_system_parent):
            raise _error("unsafe-path", "cache directory ancestor has unsafe ownership or permissions")


def _validate_private_directory(path: Path, *, label: str) -> None:
    _validate_non_symlink_chain(path, allow_missing_tail=False)
    details = path.lstat()
    if details.st_uid != os.getuid():
        raise _error("unsafe-owner", f"{label} is not owned by the current user")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise _error("unsafe-permissions", f"{label} must have 0700 permissions")


def ensure_secure_cache_root(cache_root: Path | str) -> Path:
    root = _lexical_absolute(cache_root)
    _validate_non_symlink_chain(root.parent, allow_missing_tail=True)
    if not root.exists() and not root.is_symlink():
        try:
            root.mkdir(parents=True, mode=0o700)
            os.chmod(root, 0o700)
        except OSError as exc:
            raise _error("cache-create-failed", "dependency cache root could not be created") from exc
    _validate_private_directory(root, label="dependency cache root")
    return root


def wheelhouse_path(cache_root: Path | str, selection: ContractSelection) -> Path:
    return _lexical_absolute(cache_root) / selection.fingerprint


def _allowed_artifacts(selection: ContractSelection) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    by_filename: dict[str, dict[str, str]] = {}
    owner: dict[str, str] = {}
    for distribution in selection.distributions:
        for artifact in distribution["artifacts"]:
            by_filename[artifact["filename"]] = dict(artifact)
            owner[artifact["filename"]] = distribution["name"]
    return by_filename, owner


def _wheelhouse_manifest_payload(
    selection: ContractSelection,
    files: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schemaVersion": WHEELHOUSE_SCHEMA_VERSION,
        "product": PRODUCT,
        "dependencyFingerprint": selection.fingerprint,
        "lockSha256": selection.lock_sha256,
        "environmentId": selection.environment_id,
        "profiles": list(selection.profiles),
        "files": files,
    }


def write_wheelhouse_manifest(wheelhouse: Path | str, selection: ContractSelection) -> dict[str, Any]:
    root = _lexical_absolute(wheelhouse)
    _validate_private_directory(root, label="dependency wheelhouse")
    allowed, owners = _allowed_artifacts(selection)
    files: list[dict[str, str]] = []
    covered: set[str] = set()
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == WHEELHOUSE_MANIFEST_NAME:
            continue
        details = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise _error("untrusted-cache", "dependency wheelhouse contains an unsafe entry")
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o022:
            raise _error("untrusted-cache", "dependency wheel has unsafe ownership or permissions")
        locked = allowed.get(path.name)
        if locked is None:
            raise _error("untrusted-cache", "dependency wheelhouse contains an unlocked artifact")
        digest = _sha256_file(path)
        if digest != locked["sha256"]:
            raise _error("untrusted-cache", "dependency wheel hash does not match the runtime lock")
        files.append({"filename": path.name, "sha256": digest})
        covered.add(owners[path.name])
    expected_distributions = {item["name"] for item in selection.distributions}
    if covered != expected_distributions:
        raise _error("cache-miss", "dependency wheelhouse does not cover every locked distribution")
    payload = _wheelhouse_manifest_payload(selection, files)
    manifest = root / WHEELHOUSE_MANIFEST_NAME
    _atomic_write(manifest, _canonical_json_bytes(payload) + b"\n", mode=0o444, replace=True)
    return verify_wheelhouse(root, selection)


def _validate_wheelhouse_manifest(value: Any, selection: ContractSelection) -> dict[str, Any]:
    manifest = _require_exact_fields(value, WHEELHOUSE_FIELDS, label="wheelhouse manifest")
    if type(manifest["schemaVersion"]) is not int:
        raise _error("untrusted-cache", "wheelhouse manifest schema version is invalid")
    expected_header = {
        "schemaVersion": WHEELHOUSE_SCHEMA_VERSION,
        "product": PRODUCT,
        "dependencyFingerprint": selection.fingerprint,
        "lockSha256": selection.lock_sha256,
        "environmentId": selection.environment_id,
        "profiles": list(selection.profiles),
    }
    if any(manifest.get(key) != expected for key, expected in expected_header.items()):
        raise _error("untrusted-cache", "wheelhouse manifest does not match the dependency contract")
    files = manifest["files"]
    if not isinstance(files, list):
        raise _error("untrusted-cache", "wheelhouse manifest file inventory is invalid")
    names: list[str] = []
    for item in files:
        record = _require_exact_fields(item, WHEELHOUSE_FILE_FIELDS, label="wheelhouse file record")
        if not isinstance(record["filename"], str) or not WHEEL_FILENAME_RE.fullmatch(record["filename"]):
            raise _error("untrusted-cache", "wheelhouse manifest filename is unsafe")
        if not isinstance(record["sha256"], str) or not HEX_SHA256_RE.fullmatch(record["sha256"]):
            raise _error("untrusted-cache", "wheelhouse manifest hash is invalid")
        names.append(record["filename"])
    if names != sorted(set(names)):
        raise _error("untrusted-cache", "wheelhouse manifest inventory is not sorted and unique")
    return manifest


def verify_wheelhouse(wheelhouse: Path | str, selection: ContractSelection) -> dict[str, Any]:
    root = _lexical_absolute(wheelhouse)
    _validate_private_directory(root, label="dependency wheelhouse")
    manifest_path = root / WHEELHOUSE_MANIFEST_NAME
    payload, _ = _read_strict_json_file(
        manifest_path,
        label="wheelhouse manifest",
        require_private_owner=True,
        required_mode=0o444,
    )
    manifest = _validate_wheelhouse_manifest(payload, selection)
    records = {item["filename"]: item["sha256"] for item in manifest["files"]}
    actual_names: list[str] = []
    allowed, owners = _allowed_artifacts(selection)
    covered: set[str] = set()
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == WHEELHOUSE_MANIFEST_NAME:
            continue
        actual_names.append(path.name)
        details = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise _error("untrusted-cache", "dependency wheelhouse contains an unsafe entry")
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o022:
            raise _error("untrusted-cache", "dependency wheel has unsafe ownership or permissions")
        locked = allowed.get(path.name)
        if locked is None or records.get(path.name) != locked["sha256"]:
            raise _error("untrusted-cache", "dependency wheelhouse contains an unlocked artifact")
        if _sha256_file(path) != locked["sha256"]:
            raise _error("untrusted-cache", "dependency wheel hash verification failed")
        covered.add(owners[path.name])
    if actual_names != list(records):
        raise _error("untrusted-cache", "wheelhouse file inventory differs from its manifest")
    expected_distributions = {item["name"] for item in selection.distributions}
    if covered != expected_distributions:
        raise _error("cache-miss", "dependency wheelhouse does not cover every locked distribution")
    return {
        "status": "hit",
        "usable": True,
        "path": str(root),
        "dependencyFingerprint": selection.fingerprint,
        "files": len(actual_names),
        "distributions": len(covered),
    }


def dependency_cache_status(cache_root: Path | str, selection: ContractSelection) -> dict[str, Any]:
    root = _lexical_absolute(cache_root)
    _validate_non_symlink_chain(root.parent, allow_missing_tail=True)
    wheelhouse = wheelhouse_path(root, selection)
    if not root.exists() and not root.is_symlink():
        return {"status": "miss", "usable": False, "path": str(wheelhouse), "reason": "cache-root-missing"}
    _validate_private_directory(root, label="dependency cache root")
    if not wheelhouse.exists() and not wheelhouse.is_symlink():
        return {"status": "miss", "usable": False, "path": str(wheelhouse), "reason": "wheelhouse-missing"}
    return verify_wheelhouse(wheelhouse, selection)


def _validate_managed_runtime_directory(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _error("active-venv-untrusted", f"{label} is unavailable") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise _error("active-venv-untrusted", f"{label} must be a regular non-symlink directory")
    if details.st_uid != os.getuid():
        raise _error("active-venv-untrusted", f"{label} is not owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise _error("active-venv-untrusted", f"{label} is group/world writable")


def _managed_active_venv(runtime: Path | str) -> tuple[Path, Path]:
    home = _lexical_absolute(runtime)
    app = home / "app"
    pointer = home / ".venv"
    store = app / "venvs"
    try:
        for path, label in (
            (home, "Runtime home"),
            (app, "Runtime app directory"),
            (store, "Runtime venv store"),
        ):
            _validate_managed_runtime_directory(path, label=label)
        pointer_details = pointer.lstat()
        if not stat.S_ISLNK(pointer_details.st_mode) or pointer_details.st_uid != os.getuid():
            raise ValueError
        raw = Path(os.readlink(pointer))
        if raw.is_absolute() or raw.parts[:2] != ("app", "venvs") or len(raw.parts) != 3:
            raise ValueError
        if any(part in {"", ".", ".."} for part in raw.parts):
            raise ValueError
        target = pointer.parent / raw
        if target.parent != store:
            raise ValueError
        _validate_managed_runtime_directory(target, label="Runtime venv generation")
        if target.resolve(strict=True).parent != store.resolve(strict=True):
            raise ValueError
        bin_directory = target / "bin"
        _validate_managed_runtime_directory(bin_directory, label="Runtime venv bin directory")
        python = bin_directory / "python"
        python_entry = python.lstat()
        if stat.S_ISLNK(python_entry.st_mode):
            if python_entry.st_uid != os.getuid():
                raise ValueError
        elif stat.S_ISREG(python_entry.st_mode):
            if python_entry.st_uid != os.getuid() or stat.S_IMODE(python_entry.st_mode) & 0o022:
                raise ValueError
        else:
            raise ValueError
        resolved_python = python.resolve(strict=True)
        resolved_details = resolved_python.stat(follow_symlinks=False)
        if (
            resolved_python.is_symlink()
            or not stat.S_ISREG(resolved_details.st_mode)
            or resolved_details.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(resolved_details.st_mode) & 0o022
            or not os.access(python, os.X_OK)
        ):
            raise ValueError
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        raise _error("active-venv-untrusted", "active Runtime venv pointer is missing or unsafe") from exc
    return target, python


def select_plan_python(
    runtime: Path | str,
    *,
    mode: str,
    fallback_python: Path | str | None,
) -> tuple[Path, str]:
    """Select a plan interpreter without trusting an unchecked Runtime path."""

    if mode not in {"auto", "explicit-source-only", "force-rebuild"}:
        raise _error("invalid-mode", "update dependency mode is invalid")
    explicit_fallback = fallback_python is not None
    if explicit_fallback:
        return Path(fallback_python), "explicit-python"
    try:
        _, active_python = _managed_active_venv(runtime)
    except ContractError:
        raise _error(
            "missing-rebuild-python",
            "active Runtime venv is untrusted and no explicit rebuild Python was provided",
        )
    return active_python, "managed-active-venv-python"


def inspect_active_venv(runtime: Path | str) -> dict[str, Any]:
    try:
        target, python = _managed_active_venv(runtime)
    except ContractError as exc:
        return {
            "status": "untrusted",
            "reason": exc.code,
            "markerStatus": "unavailable",
        }
    marker = target / MARKER_NAME
    if not marker.exists() and not marker.is_symlink():
        return {
            "status": "managed",
            "reason": None,
            "markerStatus": "missing",
            "venvTarget": str(target),
            "python": str(python),
        }
    try:
        payload = read_dependency_marker(target)
    except ContractError as exc:
        return {
            "status": "managed",
            "reason": exc.code,
            "markerStatus": "untrusted",
            "venvTarget": str(target),
            "python": str(python),
        }
    return {
        "status": "managed",
        "reason": None,
        "markerStatus": "trusted",
        "venvTarget": str(target),
        "python": str(python),
        "marker": payload,
    }


def plan_update(
    runtime: Path | str,
    selection: ContractSelection,
    *,
    mode: str,
    offline: bool,
    cache_root: Path | str,
) -> tuple[dict[str, Any], int]:
    if mode not in {"auto", "explicit-source-only", "force-rebuild"}:
        raise _error("invalid-mode", "update dependency mode is invalid")
    active = inspect_active_venv(runtime)
    reuse = False
    reason = "forced-rebuild" if mode == "force-rebuild" else ""
    if mode != "force-rebuild":
        if active["status"] != "managed":
            reason = "active-venv-untrusted"
        elif active["markerStatus"] == "missing":
            reason = "legacy-runtime-no-dependency-marker"
        elif active["markerStatus"] != "trusted":
            reason = "active-dependency-marker-untrusted"
        elif active["marker"] != selection.marker_payload():
            reason = "dependency-fingerprint-changed"
        else:
            try:
                validate_live_distributions(active["python"], selection)
            except ContractError:
                reason = "active-distributions-untrusted"
            else:
                reuse = True
                reason = "dependency-fingerprint-match"
    if mode == "explicit-source-only" and not reuse:
        raise _error("source-only-incompatible", f"source-only update is not compatible: {reason}")
    if reuse:
        return (
            {
                "schemaVersion": 1,
                "status": "ready",
                "updateMode": "reuse-existing-venv",
                "reason": reason,
                "dependencyFingerprint": selection.fingerprint,
                "reusesRuntimeVenv": True,
                "plannedDependenciesInstalled": False,
                "offline": bool(offline),
                "cacheUsed": False,
                "cache": {"status": "not-required", "usable": False},
                "activeVenvTarget": active.get("venvTarget"),
                "failBeforeServiceStop": False,
            },
            0,
        )
    try:
        cache = dependency_cache_status(cache_root, selection)
    except ContractError as exc:
        return (
            {
                "schemaVersion": 1,
                "status": "blocked",
                "updateMode": "rebuild-candidate-venv",
                "reason": "dependency-cache-untrusted",
                "dependencyFingerprint": selection.fingerprint,
                "reusesRuntimeVenv": False,
                "plannedDependenciesInstalled": False,
                "offline": bool(offline),
                "cacheUsed": False,
                "cache": {"status": "untrusted", "usable": False, "errorCode": exc.code},
                "failBeforeServiceStop": True,
            },
            3,
        )
    if offline and cache["status"] != "hit":
        return (
            {
                "schemaVersion": 1,
                "status": "blocked",
                "updateMode": "rebuild-candidate-venv",
                "reason": "offline-cache-miss",
                "dependencyFingerprint": selection.fingerprint,
                "reusesRuntimeVenv": False,
                "plannedDependenciesInstalled": False,
                "offline": True,
                "cacheUsed": False,
                "cache": cache,
                "failBeforeServiceStop": True,
            },
            3,
        )
    return (
        {
            "schemaVersion": 1,
            "status": "ready",
            "updateMode": "rebuild-candidate-venv",
            "reason": reason,
            "dependencyFingerprint": selection.fingerprint,
            "reusesRuntimeVenv": False,
            "plannedDependenciesInstalled": True,
            "offline": bool(offline),
            "cacheUsed": cache["status"] == "hit",
            "cache": cache,
            "failBeforeServiceStop": False,
        },
        0,
    )


def _pip_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PIP_") and key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _bounded_diagnostic_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
    if len(raw) <= MAX_PIP_DIAGNOSTIC_STREAM_BYTES:
        return raw
    half = MAX_PIP_DIAGNOSTIC_STREAM_BYTES // 2
    omitted = len(raw) - (half * 2)
    marker = f"\n... [{omitted} diagnostic bytes omitted] ...\n".encode("ascii")
    return raw[:half] + marker + raw[-half:]


def _redact_diagnostic_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if not host:
            return "[redacted-url]"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        netloc = f"[redacted]@{host}" if parsed.username is not None else host
        query = urlencode(
            [(key, "[redacted]") for key, _value in parse_qsl(parsed.query, keep_blank_values=True)]
        )
        fragment = "[redacted]" if parsed.fragment else ""
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
    except (TypeError, ValueError):
        return "[redacted-url]"


def _sanitize_pip_diagnostic(value: bytes | str | None) -> str:
    raw = _bounded_diagnostic_bytes(value)
    text = raw.decode("utf-8", errors="replace").replace("\x00", "�")
    text = HTTP_URL_RE.sub(_redact_diagnostic_url, text)
    text = AUTHORIZATION_VALUE_RE.sub(r"\1[redacted]", text)
    text = SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
    sensitive_values = sorted(
        {
            str(value)
            for key, value in os.environ.items()
            if SENSITIVE_ENVIRONMENT_NAME_RE.search(key)
            and isinstance(value, str)
            and len(value) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in sensitive_values:
        text = text.replace(secret, "[redacted]")
    return text


def _write_pip_diagnostic(
    path: Path | str | None,
    *,
    command: Sequence[str],
    returncode: int | str,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
) -> Path | None:
    if path is None:
        return None
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command_text = _sanitize_pip_diagnostic(
        json.dumps([str(item) for item in command], ensure_ascii=False)
    )
    payload = (
        f"=== isolated pip failure {datetime.now().astimezone().isoformat()} ===\n"
        f"returncode: {returncode}\n"
        f"command: {command_text}\n"
        "--- stdout ---\n"
        f"{_sanitize_pip_diagnostic(stdout)}\n"
        "--- stderr ---\n"
        f"{_sanitize_pip_diagnostic(stderr)}\n"
    ).encode("utf-8", errors="replace")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise OSError("dependency diagnostic log is not a private regular file")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("dependency diagnostic log write did not make progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def _try_write_pip_diagnostic(
    path: Path | str | None,
    **details: Any,
) -> Path | None:
    try:
        return _write_pip_diagnostic(path, **details)
    except OSError:
        return None


def _pip_failure_message(
    *,
    returncode: int | str,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
    diagnostic_log: Path | None,
) -> str:
    detail = _sanitize_pip_diagnostic(stderr).strip()
    if not detail:
        detail = _sanitize_pip_diagnostic(stdout).strip()
    detail = re.sub(r"\s+", " ", detail)
    prefix = f"isolated pip command failed with exit code {returncode}"
    suffix = f" (diagnostic log: {diagnostic_log})" if diagnostic_log is not None else ""
    available = MAX_PIP_ERROR_SUMMARY_CHARS - len(prefix) - len(suffix) - 2
    if detail and available > 1:
        if len(detail) > available:
            detail = detail[: max(1, available - 1)].rstrip() + "…"
        return f"{prefix}: {detail}{suffix}"
    return (prefix + suffix)[:MAX_PIP_ERROR_SUMMARY_CHARS]


def _run_pip(
    command: Sequence[str],
    *,
    timeout: int,
    code: str,
    diagnostic_log: Path | str | None = None,
) -> None:
    child_options: dict[str, Any] = {}
    if sys.platform == "linux":
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
            list(command),
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout,
            env=_pip_environment(),
            **child_options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout = getattr(exc, "stdout", None)
        stderr = getattr(exc, "stderr", None)
        written = _try_write_pip_diagnostic(
            diagnostic_log,
            command=command,
            returncode="not-started" if isinstance(exc, OSError) else "timeout",
            stdout=stdout,
            stderr=stderr,
        )
        message = _pip_failure_message(
            returncode="not-started" if isinstance(exc, OSError) else "timeout",
            stdout=stdout,
            stderr=stderr,
            diagnostic_log=written,
        )
        raise _error(code, message) from exc
    if result.returncode != 0:
        written = _try_write_pip_diagnostic(
            diagnostic_log,
            command=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        raise _error(
            code,
            _pip_failure_message(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                diagnostic_log=written,
            ),
        )


def materialize_dependency_cache(
    cache_root: Path | str,
    selection: ContractSelection,
    *,
    python: Path | str,
    offline: bool = False,
    timeout: int = 900,
    diagnostic_log: Path | str | None = None,
) -> dict[str, Any]:
    if offline:
        status = dependency_cache_status(cache_root, selection)
        if status["status"] != "hit":
            raise _error(
                "offline-cache-miss",
                "offline dependency materialization requires an existing trusted wheelhouse",
            )
        return {**status, "materialized": False, "cacheUsed": True, "offline": True}
    root = ensure_secure_cache_root(cache_root)
    destination = wheelhouse_path(root, selection)
    if destination.exists() or destination.is_symlink():
        status = verify_wheelhouse(destination, selection)
        return {**status, "materialized": False, "cacheUsed": True}
    staging = Path(tempfile.mkdtemp(prefix=".candidate-wheelhouse-", dir=root))
    os.chmod(staging, 0o700)
    requirement_path = root / f".requirements-{os.getpid()}-{os.urandom(4).hex()}.txt"
    try:
        raw_requirements = exact_download_requirements(selection).encode("utf-8")
        _atomic_write(requirement_path, raw_requirements, mode=0o400, replace=False)
        command = [
            str(Path(python)),
            "-I",
            "-B",
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--dest",
            str(staging),
            "--requirement",
            str(requirement_path),
        ]
        _run_pip(
            command,
            timeout=timeout,
            code="cache-materialization-failed",
            diagnostic_log=diagnostic_log,
        )
        for path in staging.iterdir():
            if path.is_file() and not path.is_symlink():
                os.chmod(path, 0o400)
        write_wheelhouse_manifest(staging, selection)
        try:
            os.rename(staging, destination)
            _fsync_directory(root)
        except FileExistsError:
            shutil.rmtree(staging)
            status = verify_wheelhouse(destination, selection)
            return {**status, "materialized": False, "cacheUsed": True}
        status = verify_wheelhouse(destination, selection)
        return {**status, "materialized": True, "cacheUsed": False}
    finally:
        try:
            requirement_path.unlink()
        except FileNotFoundError:
            pass
        if staging.exists():
            shutil.rmtree(staging)


def install_locked_dependencies(
    cache_root: Path | str,
    selection: ContractSelection,
    *,
    venv_python: Path | str,
    timeout: int = 900,
    diagnostic_log: Path | str | None = None,
) -> dict[str, Any]:
    status = dependency_cache_status(cache_root, selection)
    if status["status"] != "hit":
        raise _error("offline-cache-miss", "trusted dependency wheelhouse is unavailable")
    wheelhouse = Path(status["path"])
    with tempfile.TemporaryDirectory(prefix="actanara-locked-install-") as temporary:
        requirement_path = Path(temporary) / "requirements.txt"
        requirement_path.write_text(hashed_requirements(selection), encoding="utf-8")
        command = [
            str(Path(venv_python)),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(requirement_path),
        ]
        _run_pip(
            command,
            timeout=timeout,
            code="locked-install-failed",
            diagnostic_log=diagnostic_log,
        )
    validation = validate_live_distributions(venv_python, selection)
    return {
        "status": "installed",
        "dependencyFingerprint": selection.fingerprint,
        "dependenciesInstalled": True,
        "cacheUsed": True,
        "wheelhouse": str(wheelhouse),
        "verifiedDistributions": validation["verified"],
    }


def _selection_from_args(args: argparse.Namespace, *, python: Path | str | None = None) -> ContractSelection:
    return load_contract_selection(
        args.lock,
        args.pyproject,
        args.profile or (),
        python=python or args.python,
    )


def _add_selection_arguments(parser: argparse.ArgumentParser, *, include_python: bool = True) -> None:
    parser.add_argument("--lock", required=True, help="Path to install/runtime-dependencies.lock.json")
    parser.add_argument("--pyproject", required=True, help="Path to the candidate pyproject.toml")
    parser.add_argument("--profile", action="append", default=[], help="Enabled extra/profile; repeat or comma-separate")
    if include_python:
        parser.add_argument("--python", default=sys.executable, help="Python used for environment selection")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    probe = subcommands.add_parser("probe-environment")
    probe.add_argument("--python", default=sys.executable)

    runtime_profiles = subcommands.add_parser("runtime-profiles")
    runtime_profiles.add_argument("--runtime", required=True)
    runtime_profiles.add_argument("--allow-untrusted-active-venv", action="store_true")
    runtime_profiles.add_argument("--allow-legacy-settings", action="store_true")

    migrate_settings = subcommands.add_parser("migrate-legacy-settings")
    migrate_settings.add_argument("--runtime", required=True)
    for option in (
        "scheduler-enabled",
        "dashboard-enabled",
        "dashboard-server-enabled",
        "rag-server-enabled",
    ):
        migrate_settings.add_argument(f"--{option}", choices=("0", "1"))

    fingerprint = subcommands.add_parser("fingerprint")
    _add_selection_arguments(fingerprint)

    requirements = subcommands.add_parser("requirements")
    _add_selection_arguments(requirements)
    requirements.add_argument("--output")

    plan = subcommands.add_parser("plan")
    _add_selection_arguments(plan, include_python=False)
    plan.add_argument("--runtime", required=True)
    plan.add_argument("--cache-root", required=True)
    plan.add_argument(
        "--python",
        default=None,
        help="Explicit target interpreter; when omitted, use the validated active Runtime venv Python",
    )
    plan.add_argument(
        "--mode",
        choices=("auto", "explicit-source-only", "force-rebuild"),
        default="auto",
    )
    plan.add_argument("--offline", action="store_true")

    write_marker = subcommands.add_parser("write-marker")
    _add_selection_arguments(write_marker)
    write_marker.add_argument("--venv", required=True)

    verify_marker = subcommands.add_parser("verify-marker")
    _add_selection_arguments(verify_marker)
    verify_marker.add_argument("--venv", required=True)

    validate_live = subcommands.add_parser("validate-live")
    _add_selection_arguments(validate_live, include_python=False)
    validate_live.add_argument("--venv-python", required=True)

    cache_status = subcommands.add_parser("cache-status")
    _add_selection_arguments(cache_status)
    cache_status.add_argument("--cache-root", required=True)

    materialize = subcommands.add_parser("materialize-cache")
    _add_selection_arguments(materialize)
    materialize.add_argument("--cache-root", required=True)
    materialize.add_argument("--offline", action="store_true")
    materialize.add_argument("--timeout", type=int, default=900)

    install = subcommands.add_parser("install")
    _add_selection_arguments(install, include_python=False)
    install.add_argument("--cache-root", required=True)
    install.add_argument("--venv-python", required=True)
    install.add_argument("--timeout", type=int, default=900)

    return parser


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "probe-environment":
        return {"schemaVersion": 1, "status": "ok", "environment": probe_environment(args.python)}, 0
    if args.command == "runtime-profiles":
        return runtime_dependency_profiles(
            args.runtime,
            allow_untrusted_active_venv=args.allow_untrusted_active_venv,
            allow_legacy_settings=args.allow_legacy_settings,
        ), 0
    if args.command == "migrate-legacy-settings":
        optional_bool = lambda value: None if value is None else value == "1"
        return migrate_legacy_runtime_settings(
            args.runtime,
            scheduler_enabled=optional_bool(args.scheduler_enabled),
            dashboard_enabled=optional_bool(args.dashboard_enabled),
            dashboard_server_enabled=optional_bool(args.dashboard_server_enabled),
            rag_server_enabled=optional_bool(args.rag_server_enabled),
        ), 0
    if args.command == "fingerprint":
        selection = _selection_from_args(args)
        return {
            "schemaVersion": 1,
            "status": "ok",
            "dependencyFingerprint": selection.fingerprint,
            "lockSha256": selection.lock_sha256,
            "environmentId": selection.environment_id,
            "environment": selection.environment_probe,
            "lockEnvironment": selection.lock_environment,
            "profiles": list(selection.profiles),
            "directDependencies": list(selection.direct_dependencies),
            "distributions": [
                {"name": item["name"], "version": item["version"], "hashes": item["hashes"]}
                for item in selection.distributions
            ],
        }, 0
    if args.command == "requirements":
        selection = _selection_from_args(args)
        content = hashed_requirements(selection)
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "status": "ok",
            "dependencyFingerprint": selection.fingerprint,
            "requirements": content,
            "sha256": _sha256_bytes(content.encode("utf-8")),
        }
        if args.output:
            payload["output"] = write_hashed_requirements(Path(args.output), selection)
        return payload, 0
    if args.command == "plan":
        selected_python, selection_reason = select_plan_python(
            args.runtime,
            mode=args.mode,
            fallback_python=args.python,
        )
        selection = _selection_from_args(args, python=selected_python)
        payload, returncode = plan_update(
            args.runtime,
            selection,
            mode=args.mode,
            offline=args.offline,
            cache_root=args.cache_root,
        )
        payload["selectedPython"] = str(selected_python)
        payload["pythonSelectionReason"] = selection_reason
        return payload, returncode
    if args.command == "write-marker":
        selection = _selection_from_args(args)
        return write_dependency_marker(args.venv, selection), 0
    if args.command == "verify-marker":
        selection = _selection_from_args(args)
        marker = verify_dependency_marker(args.venv, selection)
        return {"schemaVersion": 1, "status": "valid", "marker": marker}, 0
    if args.command == "validate-live":
        selection = _selection_from_args(args, python=args.venv_python)
        return validate_live_distributions(args.venv_python, selection), 0
    if args.command == "cache-status":
        selection = _selection_from_args(args)
        return dependency_cache_status(args.cache_root, selection), 0
    if args.command == "materialize-cache":
        if args.timeout <= 0:
            raise _error("invalid-timeout", "timeout must be positive")
        selection = _selection_from_args(args)
        return materialize_dependency_cache(
            args.cache_root,
            selection,
            python=args.python,
            offline=args.offline,
            timeout=args.timeout,
        ), 0
    if args.command == "install":
        if args.timeout <= 0:
            raise _error("invalid-timeout", "timeout must be positive")
        selection = _selection_from_args(args, python=args.venv_python)
        return install_locked_dependencies(
            args.cache_root,
            selection,
            venv_python=args.venv_python,
            timeout=args.timeout,
        ), 0
    raise _error("invalid-command", "unknown dependency contract command")


def _write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload, returncode = _dispatch(args)
        _write_json(sys.stdout, payload)
        return returncode
    except ContractError as exc:
        _write_json(
            sys.stderr,
            {
                "schemaVersion": 1,
                "status": "error",
                "error": {"code": exc.code, "message": exc.message},
            },
        )
        return 2
    except Exception:
        _write_json(
            sys.stderr,
            {
                "schemaVersion": 1,
                "status": "error",
                "error": {
                    "code": "internal-error",
                    "message": "dependency contract helper failed closed",
                },
            },
        )
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
