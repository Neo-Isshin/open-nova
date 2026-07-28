"""Local nova-RAG server process lifecycle helpers.

The nova-RAG subsystem owns its serving process under the selected Actanara runtime
state directory. This module does not build, promote or mutate indexes.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .rag_settings import RagSettings, resolve_rag_settings
from data_foundation.network import RAG_SERVER_NON_LOOPBACK_ISSUE_CODE, host_for_url, is_loopback_host
from data_foundation.source_identity import loaded_source_commit

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
SERVER_SCRIPT = Path(__file__).resolve().parent / "embedding_server.py"
BASE_SERVER_MODULES = ("fastapi", "uvicorn", "numpy", "pydantic")
LOCAL_EMBEDDING_MODULES = ("sentence_transformers",)
REQUIRED_SERVER_MODULES = LOCAL_EMBEDDING_MODULES + BASE_SERVER_MODULES
MODULE_IMPORT_PROBE_TIMEOUT_SECONDS = 120
RAG_HEALTH_SCHEMA_VERSION = 1
PROC_ROOT = Path("/proc")
_FULL_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_USE_LOADED_SOURCE_COMMIT = object()
_READY_HEALTH_STATUSES = {"ok", "healthy", "ready"}
_STARTING_HEALTH_STATUSES = {"booting", "starting", "loading", "cold-starting"}


class RagServerReadinessError(RuntimeError):
    """A managed nova-RAG server did not reach semantic readiness."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        status = str(result.get("status") or "not-ready")
        reason = str(result.get("reasonCode") or "rag-server-not-ready")
        super().__init__(f"nova-RAG readiness failed ({status}): {reason}")

try:
    from data_foundation.paths import load_paths
except ImportError:  # pragma: no cover - direct script fallback
    load_paths = None  # type: ignore


def probe_rag_server_health(
    settings: RagSettings | None = None,
    *,
    expected_source_commit: str | None | object = _USE_LOADED_SOURCE_COMMIT,
    timeout_seconds: float = 0.5,
) -> dict[str, Any]:
    """Probe and semantically validate the managed nova-RAG health document.

    HTTP 2xx alone is deliberately insufficient.  A ready response must be a
    JSON object loaded from the expected Runtime source and must describe the
    configured embedding provider/profile.  A valid ``booting`` document is a
    retryable cold-start state rather than a healthy response.
    """

    resolved = settings or resolve_rag_settings()
    expected_commit = _resolve_expected_source_commit(expected_source_commit)
    transport = _probe_health(resolved, timeout_seconds=timeout_seconds)
    reachable = transport.get("statusCode") is not None
    base: dict[str, Any] = {
        **transport,
        "schemaVersion": RAG_HEALTH_SCHEMA_VERSION,
        "healthy": False,
        "reachable": reachable,
        "ready": False,
        "status": "unavailable",
        "phase": "health-unavailable",
        "coldModel": False,
        "identityMatches": False,
        "reasonCode": "connection-unavailable",
        "expectedSourceCommit": expected_commit,
        "expectedProvider": resolved.embedding_provider,
        "expectedProviderId": resolved.embedding_provider_id,
        "expectedProfile": _expected_embedding_profile(resolved),
    }
    if not is_loopback_host(resolved.server_host):
        return {
            **base,
            "status": "blocked",
            "phase": "network-boundary",
            "reasonCode": RAG_SERVER_NON_LOOPBACK_ISSUE_CODE,
        }
    payload = transport.get("payload")
    if not isinstance(payload, dict):
        if reachable:
            return {
                **base,
                "status": "invalid",
                "phase": "health-document",
                "reasonCode": "invalid-json",
            }
        return base
    if "sourceCommit" not in payload:
        return {
            **base,
            "status": "invalid",
            "phase": "source-identity",
            "reasonCode": "source-commit-missing",
        }
    if payload.get("sourceCommit") != expected_commit:
        return {
            **base,
            "status": "mismatch",
            "phase": "source-identity",
            "reasonCode": "source-commit-mismatch",
            "actualSourceCommit": payload.get("sourceCommit"),
        }

    expected_profile = _expected_embedding_profile(resolved)
    if payload.get("provider") != resolved.embedding_provider:
        return {
            **base,
            "status": "mismatch",
            "phase": "provider-identity",
            "reasonCode": "provider-mismatch",
        }
    if payload.get("providerId") != resolved.embedding_provider_id:
        return {
            **base,
            "status": "mismatch",
            "phase": "provider-identity",
            "reasonCode": "provider-id-mismatch",
        }
    if payload.get("model") != resolved.embedding_model:
        return {
            **base,
            "status": "mismatch",
            "phase": "embedding-profile",
            "reasonCode": "embedding-model-mismatch",
        }
    if type(payload.get("dimension")) is not int or payload.get("dimension") != resolved.embedding_dimension:
        return {
            **base,
            "status": "mismatch",
            "phase": "embedding-profile",
            "reasonCode": "embedding-dimension-mismatch",
        }
    actual_profile = payload.get("embeddingProfile")
    if not isinstance(actual_profile, dict) or any(
        actual_profile.get(key) != value for key, value in expected_profile.items()
    ):
        return {
            **base,
            "status": "mismatch",
            "phase": "embedding-profile",
            "reasonCode": "embedding-profile-mismatch",
        }
    expected_profile_hash = _embedding_profile_hash(expected_profile)
    actual_profile_hash = payload.get("embeddingProfileHash")
    if actual_profile_hash is not None and actual_profile_hash != expected_profile_hash:
        return {
            **base,
            "status": "mismatch",
            "phase": "embedding-profile",
            "reasonCode": "embedding-profile-hash-mismatch",
        }

    status = str(payload.get("status") or "").strip().lower()
    if type(payload.get("providerLoaded")) is not bool:
        return {
            **base,
            "status": "invalid",
            "phase": "provider-state",
            "identityMatches": True,
            "reasonCode": "provider-loaded-state-missing",
        }
    provider_loaded = payload.get("providerLoaded") is True
    identity_base = {
        **base,
        "identityMatches": True,
        "actualSourceCommit": payload.get("sourceCommit"),
        "actualProfile": actual_profile,
    }
    if status in _STARTING_HEALTH_STATUSES or not provider_loaded:
        cold_model = resolved.embedding_provider == "local" and not provider_loaded
        return {
            **identity_base,
            "status": "starting",
            "phase": "cold-model-loading" if cold_model else "provider-loading",
            "coldModel": cold_model,
            "reasonCode": "embedding-provider-loading",
        }
    if status not in _READY_HEALTH_STATUSES:
        return {
            **identity_base,
            "status": "invalid",
            "phase": "health-document",
            "reasonCode": "health-status-invalid",
        }
    status_code = transport.get("statusCode")
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        return {
            **identity_base,
            "status": "invalid",
            "phase": "health-transport",
            "reasonCode": "health-http-status-invalid",
        }
    return {
        **identity_base,
        "healthy": True,
        "ready": True,
        "status": "ready",
        "phase": "ready",
        "reasonCode": None,
    }


def inspect_rag_server_port(
    settings: RagSettings | None = None,
    *,
    state: dict[str, Any] | None = None,
    proc_root: Path = PROC_ROOT,
) -> dict[str, Any]:
    """Return Linux listener ownership evidence for the configured RAG port."""

    resolved = settings or resolve_rag_settings()
    recorded = state if isinstance(state, dict) else _read_json(_state_path(resolved))
    base: dict[str, Any] = {
        "host": resolved.server_host,
        "port": resolved.server_port,
        "listening": False,
        "pids": [],
        "managed": False,
        "managedPid": None,
        "inspectable": False,
        "basis": "unsupported-platform",
    }
    if not _is_linux():
        return base
    inodes, inspectable = _linux_listener_socket_inodes(resolved.server_port, proc_root=proc_root)
    pids = _linux_socket_owner_pids(inodes, proc_root=proc_root) if inodes else []
    pid = _optional_int(recorded.get("pid"))
    managed = bool(pid and pid in pids and _state_process_running(recorded, proc_root=proc_root))
    return {
        **base,
        "listening": bool(inodes),
        "pids": pids,
        "managed": managed,
        "managedPid": pid if managed else None,
        "inspectable": inspectable,
        "basis": "linux-proc-socket-owner" if inspectable else "linux-proc-unavailable",
    }


def probe_rag_server_readiness(
    settings: RagSettings | None = None,
    *,
    expected_source_commit: str | None | object = _USE_LOADED_SOURCE_COMMIT,
    timeout_seconds: float = 0.5,
) -> dict[str, Any]:
    """Combine semantic health, process identity, and listener ownership."""

    resolved = settings or resolve_rag_settings()
    state = _read_json(_state_path(resolved))
    pid = _optional_int(state.get("pid"))
    process_exists = _pid_running(pid) if pid else False
    process_running = _state_process_running(state) if pid else False
    health = probe_rag_server_health(
        resolved,
        expected_source_commit=expected_source_commit,
        timeout_seconds=timeout_seconds,
    )
    listener = dict(inspect_rag_server_port(resolved, state=state))
    if health.get("reachable") or health.get("statusCode") is not None:
        listener["listening"] = True
    if health.get("identityMatches") and (
        listener.get("managed") or not listener.get("inspectable")
    ):
        # Semantic identity is a fallback only when process ownership cannot
        # be inspected. It must never override positive /proc evidence that
        # the listening socket belongs to a different process.
        already_managed = bool(listener.get("managed"))
        listener["managed"] = True
        if not already_managed:
            listener["basis"] = "semantic-health-fallback"
    listener["conflict"] = bool(listener.get("listening") and not listener.get("managed"))

    base: dict[str, Any] = {
        "schemaVersion": RAG_HEALTH_SCHEMA_VERSION,
        "ready": False,
        "status": "stopped",
        "phase": "not-running",
        "reasonCode": "rag-server-not-running",
        "coldModel": False,
        "retryable": True,
        "rollbackRequired": False,
        "health": health,
        "listener": listener,
        "process": {
            "pid": pid,
            "recorded": pid is not None,
            "exists": process_exists,
            "running": process_running,
            "identityMatches": process_running if pid else None,
        },
    }
    if listener.get("conflict"):
        return {
            **base,
            "status": "port-conflict",
            "phase": "listener-ownership",
            "reasonCode": "rag-port-owned-by-external-process",
            "retryable": False,
            "rollbackRequired": True,
        }
    if health.get("ready"):
        return {
            **base,
            "ready": True,
            "status": "ready",
            "phase": "ready",
            "reasonCode": None,
            "retryable": False,
        }
    if process_exists and not process_running:
        return {
            **base,
            "status": "failed",
            "phase": "process-identity",
            "reasonCode": "pid-identity-mismatch",
            "retryable": False,
            "rollbackRequired": True,
        }
    health_status = str(health.get("status") or "")
    if health_status in {"mismatch", "invalid", "blocked"} and (process_running or listener.get("managed")):
        return {
            **base,
            "status": "failed",
            "phase": health.get("phase") or "health-document",
            "reasonCode": health.get("reasonCode") or "rag-health-invalid",
            "retryable": False,
            "rollbackRequired": True,
        }
    if process_running or (listener.get("managed") and health_status == "starting"):
        cold_model = bool(
            health.get("coldModel")
            or (resolved.embedding_provider == "local" and not health.get("ready"))
        )
        return {
            **base,
            "status": "starting",
            "phase": health.get("phase") if health_status == "starting" else (
                "cold-model-loading" if cold_model else "server-starting"
            ),
            "reasonCode": health.get("reasonCode") or "rag-server-starting",
            "coldModel": cold_model,
        }
    if pid and state.get("startedAt") and not state.get("stoppedAt"):
        return {
            **base,
            "status": "failed",
            "phase": "process-exited",
            "reasonCode": "model-process-exited",
            "coldModel": resolved.embedding_provider == "local",
            "retryable": False,
            "rollbackRequired": True,
        }
    return base


def wait_for_rag_server_readiness(
    settings: RagSettings | None = None,
    *,
    expected_source_commit: str | None | object = _USE_LOADED_SOURCE_COMMIT,
    timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 0.2,
    cancel_event: Any = None,
    timeout_is_failure: bool = True,
) -> dict[str, Any]:
    """Wait for semantic readiness while preserving cold-start information."""

    resolved = settings or resolve_rag_settings()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest: dict[str, Any] = {}
    while True:
        if _cancel_requested(cancel_event):
            return {
                **latest,
                "schemaVersion": RAG_HEALTH_SCHEMA_VERSION,
                "ready": False,
                "status": "canceled",
                "phase": "canceled",
                "reasonCode": "rag-start-canceled",
                "retryable": False,
                "rollbackRequired": True,
            }
        latest = probe_rag_server_readiness(
            resolved,
            expected_source_commit=expected_source_commit,
            timeout_seconds=min(0.5, max(0.05, deadline - time.monotonic())),
        )
        if latest.get("ready") or latest.get("status") in {"failed", "port-conflict", "blocked"}:
            return latest
        if time.monotonic() >= deadline:
            if not timeout_is_failure:
                return latest
            return {
                **latest,
                "ready": False,
                "status": "timeout",
                "phase": latest.get("phase") or "readiness-timeout",
                "reasonCode": "rag-readiness-timeout",
                "retryable": False,
                "rollbackRequired": True,
            }
        if poll_interval_seconds > 0:
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def require_rag_server_readiness(
    settings: RagSettings | None = None,
    *,
    expected_source_commit: str | None | object = _USE_LOADED_SOURCE_COMMIT,
    timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 0.2,
    cancel_event: Any = None,
    rollback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Require readiness and optionally execute a caller-owned rollback hook."""

    result = wait_for_rag_server_readiness(
        settings,
        expected_source_commit=expected_source_commit,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        cancel_event=cancel_event,
    )
    if result.get("ready"):
        return result
    if rollback is not None:
        rollback(result)
    raise RagServerReadinessError(result)


def read_server_process_state(
    settings: RagSettings | None = None,
    *,
    probe_health: bool = False,
    timeout_seconds: float = 0.5,
) -> dict[str, Any]:
    resolved = settings or resolve_rag_settings()
    state_path = _state_path(resolved)
    log_path = _log_path(resolved)
    state = _read_json(state_path)
    pid = _optional_int(state.get("pid"))
    if probe_health and _is_linux():
        health = probe_rag_server_health(resolved, timeout_seconds=timeout_seconds)
    else:
        health = _probe_health(resolved, timeout_seconds=timeout_seconds) if probe_health else None
    running = (_state_process_running(state) if pid else False) or bool(
        health and (health.get("ready") if _is_linux() else health.get("healthy"))
    )
    internal_token_path = rag_internal_token_path(resolved)
    internal_token_ready = bool(read_rag_internal_token(resolved))
    result = {
        "enabled": resolved.server_enabled,
        "statePath": str(state_path),
        "logPath": str(log_path),
        "pid": pid,
        "running": running,
        "status": _process_status(resolved, running, health),
        "health": health,
        "startedAt": state.get("startedAt"),
        "stoppedAt": state.get("stoppedAt"),
        "error": state.get("error"),
        "python": state.get("python"),
        "command": state.get("command"),
        "host": resolved.server_host,
        "port": resolved.server_port,
        "internalAuthorization": {
            "status": "ready" if internal_token_ready else "missing",
            "tokenFile": str(internal_token_path),
            "tokenPresent": internal_token_ready,
            "secretExposed": False,
        },
    }
    if _is_linux():
        result["processIdentity"] = state.get("processIdentity")
        result["processGroupId"] = state.get("processGroupId")
    return result


def start_rag_server(
    settings: RagSettings | None = None,
    *,
    requested_by: str = "dashboard",
    wait_timeout_seconds: float = 2.0,
    cancel_event: Any = None,
) -> dict[str, Any]:
    resolved = settings or resolve_rag_settings()
    state_path = _state_path(resolved)
    log_path = _log_path(resolved)
    if _cancel_requested(cancel_event):
        return _canceled_start_result(resolved)
    if not is_loopback_host(resolved.server_host):
        lifecycle = read_server_process_state(resolved, probe_health=False)
        return {
            "accepted": False,
            "status": "blocked",
            "reason": "nova-RAG direct server is restricted to loopback in macOS v1.",
            "issueCode": RAG_SERVER_NON_LOOPBACK_ISSUE_CODE,
            "lifecycle": lifecycle,
        }
    if _is_linux():
        readiness = probe_rag_server_readiness(resolved)
        existing = read_server_process_state(resolved, probe_health=False)
        if readiness.get("ready"):
            return {
                "accepted": True,
                "status": "already-running",
                "reason": "nova-RAG search server health endpoint is already healthy.",
                "readiness": readiness,
                "lifecycle": existing,
            }
        if readiness.get("status") == "port-conflict":
            return {
                "accepted": False,
                "status": "port-conflict",
                "reason": "The configured nova-RAG port is owned by an external listener.",
                "readiness": readiness,
                "lifecycle": existing,
            }
        if readiness.get("status") == "failed" and readiness.get("listener", {}).get("managed"):
            return {
                "accepted": False,
                "status": "managed-listener-unhealthy",
                "reason": "The managed nova-RAG listener does not match the expected Runtime or embedding profile.",
                "readiness": readiness,
                "lifecycle": existing,
            }
        if readiness.get("status") == "starting":
            return {
                "accepted": True,
                "status": "running-unhealthy",
                "reason": "nova-RAG search server process is running but health is not ready yet.",
                "readiness": readiness,
                "lifecycle": existing,
            }
    else:
        existing = read_server_process_state(resolved, probe_health=True)
        if existing["health"] and existing["health"].get("healthy"):
            return {
                "accepted": True,
                "status": "already-running",
                "reason": "nova-RAG search server health endpoint is already healthy.",
                "lifecycle": existing,
            }
        if existing["running"]:
            return {
                "accepted": True,
                "status": "running-unhealthy",
                "reason": "nova-RAG search server process is running but health is not ready yet.",
                "lifecycle": existing,
            }
    if not resolved.server_enabled:
        return {
            "accepted": False,
            "status": "disabled",
            "reason": "RAG serving is disabled because the RAG product is disabled.",
            "lifecycle": existing,
        }
    if not resolved.enabled or resolved.mode == "disabled":
        return {
            "accepted": False,
            "status": "rag-disabled",
            "reason": "nova-RAG subsystem is disabled in runtime settings.",
            "lifecycle": existing,
        }

    required_modules = _required_server_modules(resolved)
    select_kwargs: dict[str, Any] = {"required_modules": required_modules}
    if cancel_event is not None:
        select_kwargs["cancel_event"] = cancel_event
    python = _select_server_python(**select_kwargs)
    if _cancel_requested(cancel_event):
        return _canceled_start_result(resolved)
    if python is None:
        result = {
            "accepted": False,
            "status": "missing-runtime",
            "reason": "The Actanara runtime venv is missing the local nova-RAG server dependencies.",
            "requiredModules": list(required_modules),
            "requiredInstallGroup": "rag-local" if resolved.embedding_provider == "local" else "rag-server",
            "installHint": (
                "Initialize nova-RAG from Dashboard to install the optional rag-local dependencies in the background, "
                "or run the installer with --enable-rag."
                if resolved.embedding_provider == "local"
                else "Repair the Actanara rag-server runtime dependencies before starting nova-RAG."
            ),
            "lifecycle": existing,
        }
        _write_json_atomic(state_path, {**_read_json(state_path), "error": result["reason"], "updatedAt": _now()})
        return result

    command = [python, str(SERVER_SCRIPT)]
    token_path = _rotate_internal_token(resolved)
    env = _server_env(resolved)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {
        "schemaVersion": 2 if _is_linux() else 1,
        "pid": process.pid,
        "python": python,
        "command": command,
        "cwd": str(ROOT),
        "host": resolved.server_host,
        "port": resolved.server_port,
        "mode": resolved.mode,
        "indexPath": _active_index_path_hint(resolved),
        "startedAt": _now(),
        "requestedBy": requested_by,
        "stoppedAt": None,
        "error": None,
        "internalTokenFile": str(token_path),
    }
    if _is_linux():
        identity = _read_linux_process_identity(process.pid)
        if identity is not None:
            state["processIdentity"] = identity
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = None
        if process_group_id is not None:
            state["processGroupId"] = process_group_id
    _write_json_atomic(state_path, state)
    if _is_linux():
        readiness = wait_for_rag_server_readiness(
            resolved,
            timeout_seconds=wait_timeout_seconds,
            cancel_event=cancel_event,
            timeout_is_failure=False,
        )
        if readiness.get("status") == "canceled":
            stop_rag_server(
                resolved,
                requested_by=requested_by,
                wait_timeout_seconds=min(2.0, max(0.0, wait_timeout_seconds)),
            )
            return {
                "accepted": False,
                "status": "canceled",
                "reason": "nova-RAG search server start was canceled.",
                "readiness": readiness,
                "lifecycle": read_server_process_state(resolved, probe_health=False),
            }
        if readiness.get("status") == "failed":
            _record_start_failure(resolved, readiness)
            return {
                "accepted": False,
                "status": "start-failed",
                "reason": "nova-RAG model process exited before becoming ready.",
                "readiness": readiness,
                "lifecycle": read_server_process_state(resolved, probe_health=False),
            }
        return {
            "accepted": True,
            "status": "running" if readiness.get("ready") else "starting",
            "reason": "nova-RAG search server process started.",
            "readiness": readiness,
            "lifecycle": read_server_process_state(resolved, probe_health=bool(readiness.get("ready"))),
        }
    lifecycle = _wait_for_health(resolved, timeout_seconds=wait_timeout_seconds)
    return {
        "accepted": True,
        "status": "running" if lifecycle.get("health", {}).get("healthy") else "starting",
        "reason": "nova-RAG search server process started.",
        "lifecycle": read_server_process_state(resolved, probe_health=True),
    }


def stop_rag_server(
    settings: RagSettings | None = None,
    *,
    requested_by: str = "dashboard",
    wait_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    resolved = settings or resolve_rag_settings()
    if _is_linux():
        return _stop_rag_server_linux(
            resolved,
            requested_by=requested_by,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    state_path = _state_path(resolved)
    state = _read_json(state_path)
    pid = _optional_int(state.get("pid"))
    lifecycle = read_server_process_state(resolved, probe_health=False)
    if not pid or not lifecycle["running"]:
        updated = {**state, "stoppedAt": _now(), "requestedBy": requested_by}
        _write_json_atomic(state_path, updated)
        return {
            "accepted": True,
            "status": "not-running",
            "reason": "No nova-RAG search server process recorded as running.",
            "lifecycle": read_server_process_state(resolved, probe_health=False),
        }
    if not _state_matches_rag_server(state):
        return {
            "accepted": False,
            "status": "refused",
            "reason": "Recorded process does not match the nova-RAG search server command; refusing to terminate it.",
            "lifecycle": lifecycle,
        }

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        _write_json_atomic(state_path, {**state, "error": str(exc), "updatedAt": _now()})
        return {
            "accepted": False,
            "status": "stop-failed",
            "reason": str(exc),
            "lifecycle": read_server_process_state(resolved, probe_health=False),
        }

    deadline = time.time() + max(0.1, wait_timeout_seconds)
    while time.time() < deadline:
        if not _pid_running(pid):
            break
        time.sleep(0.1)
    stopped = not _pid_running(pid)
    _write_json_atomic(
        state_path,
        {
            **state,
            "stoppedAt": _now(),
            "requestedBy": requested_by,
            "status": "stopped" if stopped else "stopping",
        },
    )
    return {
        "accepted": True,
        "status": "stopped" if stopped else "stopping",
        "reason": "nova-RAG search server stop requested.",
        "lifecycle": read_server_process_state(resolved, probe_health=False),
    }


def _stop_rag_server_linux(
    settings: RagSettings,
    *,
    requested_by: str,
    wait_timeout_seconds: float,
) -> dict[str, Any]:
    state_path = _state_path(settings)
    state = _read_json(state_path)
    pid = _optional_int(state.get("pid"))
    process_exists = _pid_running(pid) if pid else False
    process_running = _state_process_running(state) if pid else False
    if pid and process_exists and not process_running:
        return {
            "accepted": False,
            "status": "refused",
            "reason": "Recorded PID now belongs to a different process; refusing to terminate it.",
            "lifecycle": read_server_process_state(settings, probe_health=False),
        }
    if not pid or not process_running:
        updated = {
            **state,
            "stoppedAt": _now(),
            "requestedBy": requested_by,
            "status": "stopped",
        }
        _write_json_atomic(state_path, updated)
        return {
            "accepted": True,
            "status": "not-running",
            "reason": "No nova-RAG search server process recorded as running.",
            "forced": False,
            "lifecycle": read_server_process_state(settings, probe_health=False),
        }
    if not _state_matches_rag_server(state):
        return {
            "accepted": False,
            "status": "refused",
            "reason": "Recorded process identity does not match the nova-RAG search server; refusing to terminate it.",
            "lifecycle": read_server_process_state(settings, probe_health=False),
        }

    try:
        _signal_rag_process(state, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        _write_json_atomic(state_path, {**state, "error": str(exc), "updatedAt": _now()})
        return {
            "accepted": False,
            "status": "stop-failed",
            "reason": str(exc),
            "forced": False,
            "lifecycle": read_server_process_state(settings, probe_health=False),
        }

    deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
    while time.monotonic() < deadline and _state_process_running(state):
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    forced = _state_process_running(state)
    if forced:
        try:
            _signal_rag_process(state, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            _write_json_atomic(state_path, {**state, "error": str(exc), "updatedAt": _now()})
            return {
                "accepted": False,
                "status": "stop-failed",
                "reason": str(exc),
                "forced": True,
                "lifecycle": read_server_process_state(settings, probe_health=False),
            }
        for _attempt in range(10):
            if not _state_process_running(state):
                break
            time.sleep(0.05)
    stopped = not _state_process_running(state)
    updated = {
        **state,
        "stoppedAt": _now(),
        "requestedBy": requested_by,
        "status": "stopped" if stopped else "stopping",
        "forced": forced,
    }
    _write_json_atomic(state_path, updated)
    return {
        "accepted": True,
        "status": "stopped" if stopped else "stopping",
        "reason": "nova-RAG search server stop requested.",
        "forced": forced,
        "lifecycle": read_server_process_state(settings, probe_health=False),
    }


def _signal_rag_process(state: dict[str, Any], signum: int) -> None:
    pid = _optional_int(state.get("pid"))
    if not pid:
        raise ProcessLookupError
    # Revalidate the recorded starttime/exe/cmdline immediately before every
    # signal, including escalation, so an exited child cannot turn into a
    # signal against a recycled PID.
    if not _state_process_running(state):
        raise ProcessLookupError
    recorded_group = _optional_int(state.get("processGroupId"))
    try:
        live_group = os.getpgid(pid)
    except OSError:
        live_group = None
    if recorded_group == pid and live_group == recorded_group:
        os.killpg(recorded_group, signum)
        return
    os.kill(pid, signum)


def _state_path(settings: RagSettings | None = None) -> Path:
    return _runtime_state_dir(settings) / "rag" / "server-state.json"


def _log_path(settings: RagSettings | None = None) -> Path:
    return _runtime_state_dir(settings) / "logs" / "rag-server.log"


def rag_internal_token_path(settings: RagSettings | None = None) -> Path:
    return _runtime_state_dir(settings) / "rag" / "internal-token"


def read_rag_internal_token(settings: RagSettings | None = None) -> str:
    path = rag_internal_token_path(settings)
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rotate_internal_token(settings: RagSettings) -> Path:
    path = rag_internal_token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    internal_credential = secrets.token_urlsafe(32)
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(internal_credential + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        os.chmod(path, 0o600)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
    return path


def _runtime_state_dir(settings: RagSettings | None = None) -> Path:
    if settings is not None:
        home = _home_from_v2_store(settings.v2_store_path)
        if home is not None:
            return home / "state"
    if load_paths is None:
        return Path(os.getenv("ACTANARA_HOME", str(ROOT))) / "state"
    return load_paths().state_dir


def _home_from_v2_store(v2_store_path: Path) -> Path | None:
    path = Path(v2_store_path).expanduser().absolute()
    try:
        if path.name == "v2" and path.parent.name == "rag" and path.parent.parent.name == "reserved":
            return path.parent.parent.parent
    except IndexError:
        return None
    return None


def _required_server_modules(settings: RagSettings) -> tuple[str, ...]:
    if settings.embedding_provider == "local":
        return REQUIRED_SERVER_MODULES
    return BASE_SERVER_MODULES


def _select_server_python(
    *,
    required_modules: tuple[str, ...] = REQUIRED_SERVER_MODULES,
    cancel_event: Any = None,
) -> str | None:
    candidates = [
        os.getenv("NOVA_RAG_SERVER_PYTHON"),
        sys.executable,
        _runtime_venv_python(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = str(Path(candidate).expanduser())
        if path in seen or not Path(path).exists():
            continue
        seen.add(path)
        probe_kwargs: dict[str, Any] = {"required_modules": required_modules}
        if cancel_event is not None:
            probe_kwargs["cancel_event"] = cancel_event
        if _python_has_modules(path, **probe_kwargs):
            return path
        if _cancel_requested(cancel_event):
            return None
    return None


def _python_has_modules(
    python: str,
    *,
    required_modules: tuple[str, ...] = REQUIRED_SERVER_MODULES,
    cancel_event: Any = None,
) -> bool:
    code = "import sys; assert sys.version_info >= (3, 10); " + "; ".join(
        f"import {module}" for module in required_modules
    )
    try:
        try:
            timeout_seconds = int(
                os.getenv(
                    "NOVA_RAG_MODULE_IMPORT_PROBE_TIMEOUT_SECONDS",
                    str(MODULE_IMPORT_PROBE_TIMEOUT_SECONDS),
                )
            )
        except (TypeError, ValueError):
            timeout_seconds = MODULE_IMPORT_PROBE_TIMEOUT_SECONDS
        command = [python, "-c", code]
        bounded_timeout = max(15, timeout_seconds)
        if cancel_event is None:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=bounded_timeout,
                check=False,
            )
        else:
            return _run_cancellable_module_probe(
                command,
                timeout_seconds=bounded_timeout,
                cancel_event=cancel_event,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _run_cancellable_module_probe(
    command: list[str],
    *,
    timeout_seconds: float,
    cancel_event: Any,
) -> bool:
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if _cancel_requested(cancel_event) or time.monotonic() >= deadline:
                _terminate_probe_process(process)
                return False
            time.sleep(0.05)
        return process.returncode == 0
    except OSError:
        if process is not None:
            _terminate_probe_process(process)
        return False


def _terminate_probe_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if _is_linux():
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=0.5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if _is_linux():
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _runtime_venv_python() -> str | None:
    try:
        home = load_paths().home if load_paths is not None else Path(os.getenv("ACTANARA_HOME", ""))
    except Exception:
        home = Path(os.getenv("ACTANARA_HOME", ""))
    if not home:
        return None
    path = Path(home).expanduser() / ".venv" / "bin" / "python"
    return str(path) if path.exists() else None


def _server_env(settings: RagSettings) -> dict[str, str]:
    if not is_loopback_host(settings.server_host):
        raise ValueError("nova-RAG server environment refuses a non-loopback host")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pythonpath = [
        str(ROOT),
        str(SRC_ROOT),
        str(SRC_ROOT / "agentic_rag"),
        env.get("PYTHONPATH", ""),
    ]
    env["PYTHONPATH"] = os.pathsep.join([item for item in pythonpath if item])
    if "ACTANARA_HOME" not in env and load_paths is not None:
        env["ACTANARA_HOME"] = str(load_paths().home)
    env.update(
        {
            "NOVA_RAG_ENABLED": "true" if settings.enabled else "false",
            "NOVA_RAG_MODE": settings.mode,
            "NOVA_RAG_SERVER_ENABLED": "true" if settings.server_enabled else "false",
            "NOVA_RAG_SERVER_HOST": settings.server_host,
            "NOVA_RAG_SERVER_PORT": str(settings.server_port),
            "NOVA_RAG_SERVER_HEALTH_PATH": settings.server_health_path,
            "NOVA_RAG_INTERNAL_TOKEN_FILE": str(rag_internal_token_path(settings)),
            "NOVA_RAG_EMBEDDING_MODEL": settings.embedding_model,
            "NOVA_RAG_EMBEDDING_DIMENSION": str(settings.embedding_dimension),
            "NOVA_RAG_V2_STORE": str(settings.v2_store_path),
            "NOVA_RAG_LEGACY_INDEX": str(settings.legacy_index_path),
        }
    )
    return env


def _probe_health(settings: RagSettings, *, timeout_seconds: float) -> dict[str, Any]:
    url = f"http://{host_for_url(settings.server_host)}:{settings.server_port}{settings.server_health_path}"
    result: dict[str, Any] = {
        "url": url,
        "healthy": False,
        "statusCode": None,
        "error": None,
        "payload": None,
    }
    if not is_loopback_host(settings.server_host):
        result["error"] = RAG_SERVER_NON_LOOPBACK_ISSUE_CODE
        return result
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            result["statusCode"] = response.status
            result["healthy"] = 200 <= response.status < 300
            try:
                payload = json.loads(response.read().decode("utf-8"))
                result["payload"] = payload if isinstance(payload, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                result["payload"] = None
    except (OSError, urllib.error.URLError) as exc:
        result["error"] = exc.__class__.__name__
    return result


def _wait_for_health(settings: RagSettings, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + max(0.0, timeout_seconds)
    health = _probe_health(settings, timeout_seconds=0.5)
    while not health["healthy"] and time.time() < deadline:
        time.sleep(0.2)
        health = _probe_health(settings, timeout_seconds=0.5)
    return {"health": health}


def _process_status(settings: RagSettings, running: bool, health: dict[str, Any] | None) -> str:
    if not settings.server_enabled:
        return "disabled"
    if health and health.get("healthy"):
        return "healthy"
    if health and health.get("status") == "starting":
        return "starting"
    if running:
        return "running"
    return "stopped"


def _pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        # The process is not our child (for example, launchd owns it); fall
        # through to the portable existence probe below.
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _state_process_running(
    state: dict[str, Any],
    *,
    proc_root: Path = PROC_ROOT,
) -> bool:
    pid = _optional_int(state.get("pid"))
    if not pid or state.get("status") == "stopped":
        return False
    if not _is_linux():
        return _pid_running(pid)
    if not _pid_running(pid):
        return False
    live = _read_linux_process_identity(pid, proc_root=proc_root)
    if live is None:
        return False
    recorded = state.get("processIdentity")
    if isinstance(recorded, dict):
        return _same_linux_process_identity(recorded, live)
    return _legacy_state_matches_live_process(state, live)


def _state_matches_rag_server(state: dict[str, Any]) -> bool:
    command = state.get("command")
    if not isinstance(command, list):
        return False
    command_text = " ".join(str(item) for item in command)
    static_match = str(SERVER_SCRIPT) in command_text and str(ROOT) == str(state.get("cwd") or ROOT)
    if not static_match:
        return False
    if not _is_linux():
        return True
    return _state_process_running(state)


def _read_linux_process_identity(
    pid: int,
    *,
    proc_root: Path = PROC_ROOT,
) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    process_root = proc_root / str(pid)
    try:
        stat_text = (process_root / "stat").read_text(encoding="utf-8")
        closing = stat_text.rfind(")")
        if closing < 0:
            return None
        fields = stat_text[closing + 1 :].strip().split()
        # fields starts at proc stat field 3 (state); starttime is field 22.
        if len(fields) <= 19:
            return None
        start_time_ticks = int(fields[19])
        exe = os.readlink(process_root / "exe")
        raw_cmdline = (process_root / "cmdline").read_bytes()
        cmdline = [item.decode("utf-8", errors="surrogateescape") for item in raw_cmdline.split(b"\0") if item]
        if not exe or not cmdline:
            return None
        try:
            cwd = os.readlink(process_root / "cwd")
        except OSError:
            cwd = None
    except (OSError, ValueError, UnicodeError):
        return None
    return {
        "schemaVersion": 1,
        "pid": pid,
        "startTimeTicks": start_time_ticks,
        "exe": exe,
        "cmdline": cmdline,
        "cwd": cwd,
    }


def _same_linux_process_identity(recorded: dict[str, Any], live: dict[str, Any]) -> bool:
    recorded_pid = _optional_int(recorded.get("pid"))
    recorded_start = _optional_int(recorded.get("startTimeTicks"))
    recorded_exe = recorded.get("exe")
    recorded_cmdline = recorded.get("cmdline")
    return bool(
        recorded_pid
        and recorded_start is not None
        and isinstance(recorded_exe, str)
        and isinstance(recorded_cmdline, list)
        and recorded_pid == live.get("pid")
        and recorded_start == live.get("startTimeTicks")
        and recorded_exe == live.get("exe")
        and [str(item) for item in recorded_cmdline] == live.get("cmdline")
    )


def _legacy_state_matches_live_process(state: dict[str, Any], live: dict[str, Any]) -> bool:
    command = state.get("command")
    if not isinstance(command, list) or not command:
        return False
    expected = [str(item) for item in command]
    if expected != live.get("cmdline"):
        return False
    expected_exe = _resolved_executable(expected[0])
    live_exe = _resolved_executable(str(live.get("exe") or ""))
    if not expected_exe or expected_exe != live_exe:
        return False
    expected_cwd = str(state.get("cwd") or "")
    live_cwd = live.get("cwd")
    return not expected_cwd or live_cwd == expected_cwd


def _resolved_executable(value: str) -> str:
    if not value:
        return ""
    try:
        return str(Path(value).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return value


def _linux_listener_socket_inodes(port: int, *, proc_root: Path) -> tuple[set[str], bool]:
    inodes: set[str] = set()
    inspected = False
    for name in ("tcp", "tcp6"):
        path = proc_root / "net" / name
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except (FileNotFoundError, PermissionError, OSError, UnicodeError):
            continue
        inspected = True
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == port:
                inodes.add(fields[9])
    return inodes, inspected


def _linux_socket_owner_pids(inodes: set[str], *, proc_root: Path) -> list[int]:
    if not inodes:
        return []
    socket_targets = {f"socket:[{inode}]" for inode in inodes}
    owners: list[int] = []
    try:
        candidates = list(proc_root.iterdir())
    except OSError:
        return owners
    for process_root in candidates:
        if not process_root.name.isdigit():
            continue
        try:
            descriptors = (process_root / "fd").iterdir()
            owns_socket = any(os.readlink(descriptor) in socket_targets for descriptor in descriptors)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if owns_socket:
            owners.append(int(process_root.name))
    return sorted(owners)


def _active_index_path_hint(settings: RagSettings) -> str | None:
    try:
        from .rag_active_source import resolve_active_rag_index

        active = resolve_active_rag_index(settings)
        return str(active.index_path) if active.index_path else None
    except Exception:
        return None


def _resolve_expected_source_commit(value: str | None | object) -> str | None:
    if value is _USE_LOADED_SOURCE_COMMIT:
        return loaded_source_commit(__file__)
    if value is None:
        return None
    text = str(value).strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(text):
        raise ValueError("expected_source_commit must be a full git commit id or None")
    return text


def _expected_embedding_profile(settings: RagSettings) -> dict[str, Any]:
    return {
        "mode": settings.embedding_provider,
        "providerId": settings.embedding_provider_id,
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
    }


def _embedding_profile_hash(profile: dict[str, Any]) -> str:
    encoded = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cancel_requested(cancel_event: Any) -> bool:
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel_event):
        return bool(cancel_event())
    return bool(cancel_event)


def _canceled_start_result(settings: RagSettings) -> dict[str, Any]:
    return {
        "accepted": False,
        "status": "canceled",
        "reason": "nova-RAG search server start was canceled.",
        "readiness": {
            "schemaVersion": RAG_HEALTH_SCHEMA_VERSION,
            "ready": False,
            "status": "canceled",
            "phase": "canceled",
            "reasonCode": "rag-start-canceled",
            "retryable": False,
            "rollbackRequired": True,
        },
        "lifecycle": read_server_process_state(settings, probe_health=False),
    }


def _record_start_failure(settings: RagSettings, readiness: dict[str, Any]) -> None:
    state_path = _state_path(settings)
    state = _read_json(state_path)
    _write_json_atomic(
        state_path,
        {
            **state,
            "status": "failed",
            "error": readiness.get("reasonCode") or "nova-RAG failed before readiness",
            "updatedAt": _now(),
        },
    )


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().astimezone().isoformat()
