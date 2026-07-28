"""Crash-recoverable Settings/runtime-manifest transaction boundary.

The journal intentionally contains hashes, stable resource identifiers, phases,
and transaction-owned secret references only. File preimages and staged payloads
live in private transaction files and never contain a secret value because raw
provider keys are moved to the secret store before Settings bytes are built.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .paths import RuntimePaths
from .secret_store import delete_secret, store_secret


SETTINGS_TRANSACTION_SCHEMA_VERSION = 1
SETTINGS_TRANSACTION_TERMINAL_STATUSES = {"committed", "compensated"}
MISSING_RESOURCE_HASH = "missing"
_LOCAL_TRANSACTION_LOCKS = threading.local()


@dataclass(frozen=True)
class SettingsTransactionPlan:
    settings_bytes: bytes
    manifest_bytes: bytes
    secret_writes: tuple[tuple[dict[str, str], str], ...] = ()
    garbage_collection_candidates: tuple[dict[str, str], ...] = ()


class SettingsTransactionError(ValueError):
    """Public-safe transaction failure with an additive structured summary."""

    def __init__(self, summary: dict, *, cause_type: str = "transaction-error") -> None:
        self.summary = summary
        transaction_id = str(summary.get("id") or "unknown")
        phase = str(summary.get("phase") or "unknown")
        status = str(summary.get("status") or "failed")
        compensation = summary.get("compensation") if isinstance(summary.get("compensation"), dict) else {}
        compensation_status = str(compensation.get("status") or "unknown")
        super().__init__(
            f"settings transaction {transaction_id} {status} during {phase}; "
            f"compensation={compensation_status}; cause={cause_type}"
        )


class _SettingsTransactionConflict(RuntimeError):
    pass


def settings_transaction_checkpoint(phase: str, transaction_id: str) -> None:
    """No-op production checkpoint patched by crash-window tests."""


@contextmanager
def settings_mutation_barrier(
    paths: RuntimePaths,
    *,
    pretransaction_side_effects: Callable[[], None] | None = None,
) -> Iterator[list[dict]]:
    """Serialize a Settings mutation and recover older durable journals first."""

    with _transaction_lock(paths):
        recovery = _recover_settings_transactions_locked(paths)
        blocked = next(
            (
                item
                for item in recovery
                if item.get("status") in {"conflict", "compensation-incomplete"}
            ),
            None,
        )
        if blocked is not None:
            raise SettingsTransactionError(
                {
                    "id": blocked.get("id"),
                    "status": "recovery-blocked",
                    "phase": "stale-recovery",
                    "conflict": blocked.get("status") == "conflict",
                    "compensation": blocked.get("compensation")
                    or {"status": blocked.get("status")},
                },
                cause_type="stale-transaction",
            )
        # Coupled systemd journals depend on the Settings transaction outcome.
        # Resolve Settings first: an active journal restores Settings-before,
        # while a terminal commit leaves Settings-after authoritative.
        if pretransaction_side_effects is not None:
            pretransaction_side_effects()
        yield recovery


def execute_settings_transaction(
    paths: RuntimePaths,
    prepare: Callable[[str, bytes | None, bytes | None], SettingsTransactionPlan],
    *,
    verify: Callable[[], None] | None = None,
    pretransaction_side_effects: Callable[[], None] | None = None,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None] | None = None,
    postcommit_side_effects: Callable[[dict], None] | None = None,
    apply_side_effects: Callable[[], Callable[[], None] | None] | None = None,
) -> dict:
    """Prepare, CAS-commit, verify, and finalize one Settings bundle."""

    with settings_mutation_barrier(
        paths,
        pretransaction_side_effects=pretransaction_side_effects,
    ) as recovery:
        settings_path = paths.config_dir / "settings.json"
        manifest_path = paths.config_dir / "runtime.json"
        settings_before = _read_optional_bytes(settings_path)
        manifest_before = _read_optional_bytes(manifest_path)
        transaction_id = uuid.uuid4().hex
        plan = prepare(transaction_id, settings_before, manifest_before)
        if not isinstance(plan, SettingsTransactionPlan):
            raise TypeError("settings transaction prepare callback returned an invalid plan")

        transaction_dir = _transaction_root(paths) / transaction_id
        transaction_dir.mkdir(parents=True, mode=0o700)
        os.chmod(transaction_dir, 0o700)
        resources = {
            "settings": _resource_journal(settings_before, plan.settings_bytes),
            "runtimeManifest": _resource_journal(manifest_before, plan.manifest_bytes),
        }
        owned_secret_refs = [
            _normalized_secret_ref(ref)
            for ref, _ in plan.secret_writes
        ]
        journal = {
            "schemaVersion": SETTINGS_TRANSACTION_SCHEMA_VERSION,
            "id": transaction_id,
            "status": "active",
            "phase": "journal-created",
            "resources": resources,
            "ownedSecretRefs": owned_secret_refs,
            "garbageCollectionCandidateIds": [
                _secret_ref_id(ref)
                for ref in plan.garbage_collection_candidates
            ],
        }
        try:
            _write_journal(transaction_dir, journal)
        except Exception as error:
            raise SettingsTransactionError(
                {
                    "id": transaction_id,
                    "status": "failed",
                    "phase": "journal-create",
                    "conflict": False,
                    "compensation": {
                        "status": "not-required",
                        "settings": "unchanged",
                        "runtimeManifest": "unchanged",
                        "secretCleanup": "not-required",
                        "conflictResourceIds": [],
                    },
                },
                cause_type=type(error).__name__,
            ) from None

        cleanup_side_effects: list[Callable[[], None]] = []
        transaction_context = {
            "id": transaction_id,
            "settingsBeforeHash": resources["settings"]["beforeHash"],
            "settingsAfterHash": resources["settings"]["afterHash"],
            "runtimeManifestBeforeHash": resources["runtimeManifest"]["beforeHash"],
            "runtimeManifestAfterHash": resources["runtimeManifest"]["afterHash"],
        }
        success_summary: dict | None = None
        try:
            settings_transaction_checkpoint("after-journal-created", transaction_id)

            _write_snapshot(transaction_dir / "settings.before", settings_before)
            _write_snapshot(transaction_dir / "settings.after", plan.settings_bytes)
            _write_snapshot(transaction_dir / "manifest.before", manifest_before)
            _write_snapshot(transaction_dir / "manifest.after", plan.manifest_bytes)
            _advance_journal(transaction_dir, journal, "files-staged")
            settings_transaction_checkpoint("after-files-staged", transaction_id)

            for ref, value in plan.secret_writes:
                _store_secret_for_paths(paths, ref, value)
            _advance_journal(transaction_dir, journal, "secrets-created")
            settings_transaction_checkpoint("after-secrets-created", transaction_id)

            if precommit_side_effects is not None:
                cleanup = precommit_side_effects(transaction_context)
                if cleanup is not None:
                    cleanup_side_effects.append(cleanup)
            _advance_journal(transaction_dir, journal, "precommit-side-effects-applied")
            settings_transaction_checkpoint("after-precommit-side-effects", transaction_id)

            settings_transaction_checkpoint("before-settings-commit", transaction_id)
            _cas_replace(settings_path, resources["settings"]["beforeHash"], plan.settings_bytes)
            _advance_journal(transaction_dir, journal, "settings-committed")
            settings_transaction_checkpoint("after-settings-commit", transaction_id)

            settings_transaction_checkpoint("before-runtime-manifest-commit", transaction_id)
            if resources["runtimeManifest"]["changed"]:
                _cas_replace(
                    manifest_path,
                    resources["runtimeManifest"]["beforeHash"],
                    plan.manifest_bytes,
                )
            _advance_journal(transaction_dir, journal, "runtime-manifest-committed")
            settings_transaction_checkpoint("after-runtime-manifest-commit", transaction_id)

            if verify is not None:
                verify()
            _advance_journal(transaction_dir, journal, "verified")
            settings_transaction_checkpoint("after-verified", transaction_id)

            if apply_side_effects is not None:
                cleanup = apply_side_effects()
                if cleanup is not None:
                    cleanup_side_effects.append(cleanup)
            _advance_journal(transaction_dir, journal, "side-effects-applied")
            settings_transaction_checkpoint("before-finalize", transaction_id)

            journal["status"] = "committed"
            _advance_journal(transaction_dir, journal, "committed")
            settings_transaction_checkpoint("after-finalize", transaction_id)
            success_summary = _success_summary(journal, recovery)
        except Exception as error:
            phase = str(journal.get("phase") or "unknown")
            try:
                compensation = _compensate_transaction_locked(paths, transaction_dir, journal)
            except Exception:
                compensation = {
                    "status": "compensation-incomplete",
                    "settings": "unknown",
                    "runtimeManifest": "unknown",
                    "secretCleanup": "unknown",
                    "conflictResourceIds": [],
                }
            for cleanup in reversed(cleanup_side_effects):
                try:
                    cleanup()
                except Exception:
                    compensation["sideEffects"] = "cleanup-failed"
                    if compensation.get("status") == "compensated":
                        compensation["status"] = "compensation-incomplete"
            status = "conflict" if compensation.get("status") == "conflict" else "failed"
            summary = {
                "id": transaction_id,
                "status": status,
                "phase": phase,
                "conflict": status == "conflict",
                "compensation": compensation,
            }
            raise SettingsTransactionError(summary, cause_type=type(error).__name__) from None
        if postcommit_side_effects is not None:
            # Settings-after is durable and must never be compensated from
            # this point. Keep the Settings lock held while coupled external
            # journals acknowledge the same postcondition, preventing a newer
            # Settings commit from invalidating their before/after CAS.
            postcommit_side_effects(transaction_context)
        if success_summary is None:
            raise RuntimeError("settings transaction completed without a summary")
        return success_summary


def recover_settings_transactions(paths: RuntimePaths) -> list[dict]:
    """Recover stale non-terminal Settings transactions idempotently."""

    with _transaction_lock(paths):
        return _recover_settings_transactions_locked(paths)


def _recover_settings_transactions_locked(paths: RuntimePaths) -> list[dict]:
    root = _transaction_root(paths)
    if not root.exists():
        return []
    results: list[dict] = []
    for transaction_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        journal = _read_journal(transaction_dir)
        if not journal:
            entries = list(transaction_dir.iterdir())
            if not entries or all(entry.name.startswith(".journal.json.") for entry in entries):
                for entry in entries:
                    try:
                        entry.unlink()
                    except OSError:
                        pass
                try:
                    transaction_dir.rmdir()
                except OSError:
                    pass
                continue
            results.append(
                {
                    "id": transaction_dir.name,
                    "status": "conflict",
                    "phase": "journal-unreadable",
                    "compensation": {
                        "status": "conflict",
                        "settings": "not-overwritten",
                        "runtimeManifest": "not-overwritten",
                        "secretCleanup": "not-attempted",
                        "conflictResourceIds": ["journal"],
                    },
                }
            )
            continue
        if journal.get("status") in SETTINGS_TRANSACTION_TERMINAL_STATUSES:
            continue
        compensation = _compensate_transaction_locked(paths, transaction_dir, journal)
        results.append(
            {
                "id": journal.get("id") or transaction_dir.name,
                "status": compensation.get("status"),
                "phase": journal.get("phase"),
                "compensation": compensation,
            }
        )
    return results


def _compensate_transaction_locked(
    paths: RuntimePaths,
    transaction_dir: Path,
    journal: dict,
) -> dict:
    resources = journal.get("resources") if isinstance(journal.get("resources"), dict) else {}
    resource_paths = {
        "settings": paths.config_dir / "settings.json",
        "runtimeManifest": paths.config_dir / "runtime.json",
    }
    conflicts: list[str] = []
    for resource_id, resource_path in resource_paths.items():
        metadata = resources.get(resource_id) if isinstance(resources.get(resource_id), dict) else {}
        if not metadata.get("changed"):
            continue
        current_hash = _resource_hash(resource_path)
        if current_hash not in {metadata.get("beforeHash"), metadata.get("afterHash")}:
            conflicts.append(resource_id)
    if conflicts:
        journal["status"] = "conflict"
        journal["conflictResourceIds"] = conflicts
        _advance_journal(transaction_dir, journal, "compensation-conflict")
        return {
            "status": "conflict",
            "settings": "not-overwritten",
            "runtimeManifest": "not-overwritten",
            "secretCleanup": "not-attempted",
            "conflictResourceIds": conflicts,
        }

    restored: dict[str, str] = {}
    for resource_id, resource_path, snapshot_name in (
        ("runtimeManifest", resource_paths["runtimeManifest"], "manifest.before"),
        ("settings", resource_paths["settings"], "settings.before"),
    ):
        metadata = resources.get(resource_id) if isinstance(resources.get(resource_id), dict) else {}
        if not metadata.get("changed"):
            restored[resource_id] = "unchanged"
            continue
        current_hash = _resource_hash(resource_path)
        if current_hash == metadata.get("beforeHash"):
            restored[resource_id] = "already-before"
            continue
        before = _read_snapshot(transaction_dir / snapshot_name, bool(metadata.get("beforeExists")))
        _replace_optional(resource_path, before)
        restored[resource_id] = "restored"

    cleanup_status = "not-required"
    cleanup_failed = False
    for raw_ref in journal.get("ownedSecretRefs") or []:
        ref = _normalized_secret_ref(raw_ref)
        if not _secret_ref_owned_by_transaction(
            paths,
            ref,
            str(journal.get("id") or transaction_dir.name),
        ):
            cleanup_failed = True
            continue
        if _secret_ref_referenced(paths, ref):
            cleanup_status = "retained-referenced"
            continue
        try:
            _delete_secret_for_paths(paths, ref)
            cleanup_status = "deleted-or-absent"
        except Exception:
            cleanup_failed = True
    status = "compensation-incomplete" if cleanup_failed else "compensated"
    journal["status"] = status
    _advance_journal(transaction_dir, journal, status)
    return {
        "status": status,
        "settings": restored.get("settings", "unchanged"),
        "runtimeManifest": restored.get("runtimeManifest", "unchanged"),
        "secretCleanup": "failed" if cleanup_failed else cleanup_status,
        "conflictResourceIds": [],
    }


def _secret_ref_referenced(paths: RuntimePaths, ref: dict[str, str]) -> bool:
    for path in (paths.config_dir / "settings.json", paths.config_dir / "runtime.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if _contains_secret_ref(value, ref):
            return True
    return False


def _store_secret_for_paths(paths: RuntimePaths, ref: dict[str, str], value: str) -> dict[str, str]:
    if str(ref.get("backend") or "") == "runtime-file":
        return store_secret(ref, value, runtime_home=paths.home)
    return store_secret(ref, value)


def _delete_secret_for_paths(paths: RuntimePaths, ref: dict[str, str]) -> bool:
    if str(ref.get("backend") or "") == "runtime-file":
        return delete_secret(ref, runtime_home=paths.home)
    return delete_secret(ref)


def _contains_secret_ref(value: object, ref: dict[str, str]) -> bool:
    if isinstance(value, dict):
        if all(str(value.get(key) or "") == ref[key] for key in ("backend", "service", "account")):
            return True
        return any(_contains_secret_ref(item, ref) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret_ref(item, ref) for item in value)
    return False


def _secret_ref_owned_by_transaction(
    paths: RuntimePaths,
    ref: dict[str, str],
    transaction_id: str,
) -> bool:
    runtime_id = hashlib.sha256(str(paths.home).encode("utf-8")).hexdigest()[:12]
    pattern = rf"settings-tx-{runtime_id}-[a-f0-9]{{12}}-{re.escape(transaction_id)}"
    return (
        bool(transaction_id)
        and ref["service"] == "actanara"
        and re.fullmatch(pattern, ref["account"]) is not None
    )


def _normalized_secret_ref(ref: object) -> dict[str, str]:
    value = ref if isinstance(ref, dict) else getattr(ref, "as_dict", lambda: {})()
    if not isinstance(value, dict):
        value = {}
    return {
        "backend": str(value.get("backend") or ""),
        "service": str(value.get("service") or "actanara"),
        "account": str(value.get("account") or ""),
    }


def _secret_ref_id(ref: object) -> str:
    normalized = _normalized_secret_ref(ref)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _success_summary(journal: dict, recovery: list[dict]) -> dict:
    return {
        "id": journal["id"],
        "status": "committed",
        "phase": "committed",
        "conflict": False,
        "compensation": {"status": "not-required"},
        "recoveredTransactions": [item.get("id") for item in recovery],
        "garbageCollectionCandidateIds": list(journal.get("garbageCollectionCandidateIds") or []),
    }


def _resource_journal(before: bytes | None, after: bytes) -> dict:
    return {
        "beforeExists": before is not None,
        "beforeHash": _bytes_hash(before),
        "afterHash": _bytes_hash(after),
        "changed": before != after,
    }


def _cas_replace(path: Path, expected_hash: str, content: bytes) -> None:
    if _resource_hash(path) != expected_hash:
        raise _SettingsTransactionConflict("settings transaction resource changed concurrently")
    _atomic_replace_bytes(path, content)


def _replace_optional(path: Path, content: bytes | None) -> None:
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)
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
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_snapshot(path: Path, content: bytes | None) -> None:
    if content is None:
        return
    _atomic_replace_bytes(path, content)


def _read_snapshot(path: Path, exists: bool) -> bytes | None:
    if not exists:
        return None
    return path.read_bytes()


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _resource_hash(path: Path) -> str:
    return _bytes_hash(_read_optional_bytes(path))


def _bytes_hash(content: bytes | None) -> str:
    return MISSING_RESOURCE_HASH if content is None else hashlib.sha256(content).hexdigest()


def _transaction_root(paths: RuntimePaths) -> Path:
    return paths.state_dir / "settings-transactions"


@contextmanager
def _transaction_lock(paths: RuntimePaths) -> Iterator[None]:
    root = _transaction_root(paths)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / ".lock"
    key = str(lock_path.absolute())
    held = getattr(_LOCAL_TRANSACTION_LOCKS, "held", None)
    if held is None:
        held = {}
        _LOCAL_TRANSACTION_LOCKS.held = held
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_journal(transaction_dir: Path, journal: dict) -> None:
    content = (json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_replace_bytes(transaction_dir / "journal.json", content)


def _advance_journal(transaction_dir: Path, journal: dict, phase: str) -> None:
    journal["phase"] = phase
    _write_journal(transaction_dir, journal)


def _read_journal(transaction_dir: Path) -> dict:
    try:
        value = json.loads((transaction_dir / "journal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
