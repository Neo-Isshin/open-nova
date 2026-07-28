"""Cross-process coordination for Runtime and user-service mutations."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RuntimeMutationBusy(RuntimeError):
    pass


class RuntimeMutationUnsafe(RuntimeError):
    pass


GUARD_NAME = ".runtime-mutation.guard"
TRANSACTION_LOCK_NAME = ".update-transaction.lock"
REPAIR_PENDING_NAME = ".repair-configuration-pending"
_OWNER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LOCAL_GUARDS = threading.local()


@contextmanager
def _file_guard(
    path: Path,
    *,
    blocking: bool,
    kind: str,
) -> Iterator[None]:
    key = str(path)
    held = getattr(_LOCAL_GUARDS, "held", None)
    if held is None:
        held = {}
        _LOCAL_GUARDS.held = held
    if key in held:
        held[key]["count"] += 1
        try:
            yield
        finally:
            held[key]["count"] -= 1
        return
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeMutationUnsafe("Runtime mutation guard is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeMutationUnsafe("Runtime mutation guard is unsafe")
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, operation)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeMutationBusy("another Runtime mutation is publishing") from exc
            raise RuntimeMutationUnsafe("Runtime mutation guard could not be locked") from exc
        held[key] = {
            "count": 1,
            "descriptor": descriptor,
            "kind": kind,
        }
        try:
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _namespace_guard_path() -> Path:
    parent = Path(tempfile.gettempdir()) / f"actanara-runtime-mutation-{os.getuid()}"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeMutationUnsafe(
            "Runtime mutation namespace directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or parent.is_symlink()
        or metadata.st_uid != os.getuid()
    ):
        raise RuntimeMutationUnsafe(
            "Runtime mutation namespace directory is unsafe"
        )
    parent.chmod(0o700)
    return parent / GUARD_NAME


@contextmanager
def runtime_mutation_guard(
    runtime: Path,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """Serialize every Runtime and per-user Linux publication namespace."""

    runtime = runtime.expanduser().absolute()
    app = runtime / "app"
    app.mkdir(parents=True, exist_ok=True, mode=0o700)
    if app.is_symlink() or not app.is_dir():
        raise RuntimeMutationUnsafe("Runtime app directory is unsafe")
    with _file_guard(
        _namespace_guard_path(),
        blocking=blocking,
        kind="namespace",
    ):
        with _file_guard(
            app / GUARD_NAME,
            blocking=blocking,
            kind="runtime",
        ):
            yield


def current_runtime_mutation_guard_fd() -> int | None:
    """Return the current thread's guard fd for safe child inheritance."""

    held = getattr(_LOCAL_GUARDS, "held", None)
    if not isinstance(held, dict):
        return None
    # Children inherit the user-namespace lock. It is stronger than a single
    # Runtime lock and keeps location, user shim, and systemd mutations
    # serialized even if the parent dies while the child is still applying.
    descriptors = {
        int(record.get("descriptor"))
        for record in held.values()
        if (
            isinstance(record, dict)
            and record.get("kind") == "namespace"
            and isinstance(record.get("descriptor"), int)
        )
    }
    return next(iter(descriptors)) if len(descriptors) == 1 else None


def _update_transaction_owner(runtime: Path) -> str | None:
    path = runtime / "app" / TRANSACTION_LOCK_NAME
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeMutationUnsafe("Runtime transaction lock is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 2
            or before.st_size <= 0
            or before.st_size > 4096
        ):
            raise RuntimeMutationUnsafe("Runtime transaction lock is unsafe")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise RuntimeMutationUnsafe("Runtime transaction lock changed while read")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeMutationUnsafe("Runtime transaction lock payload is invalid") from exc
    owner = payload.get("txId") if isinstance(payload, dict) else None
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise RuntimeMutationUnsafe("Runtime transaction lock owner is invalid")
    return owner


def _repair_configuration_owner(runtime: Path) -> str | None:
    path = runtime / "app" / REPAIR_PENDING_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeMutationUnsafe(
            "Runtime repair configuration marker is unreadable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 2
            or before.st_size > 129
        ):
            raise RuntimeMutationUnsafe(
                "Runtime repair configuration marker is unsafe"
            )
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise RuntimeMutationUnsafe(
                "Runtime repair configuration marker changed while read"
            )
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeMutationUnsafe(
            "Runtime repair configuration marker is invalid"
        ) from exc
    owner = value[:-1] if value.endswith("\n") else ""
    if not _OWNER_ID_RE.fullmatch(owner):
        raise RuntimeMutationUnsafe(
            "Runtime repair configuration marker owner is invalid"
        )
    return owner


def runtime_transaction_owner(runtime: Path) -> str | None:
    """Return the exact owner encoded by the active update/install lock."""

    return _update_transaction_owner(runtime.expanduser().absolute())


def durable_runtime_mutation_owner(runtime: Path) -> str | None:
    """Return the owner across the lock and committed-repair marker phases."""

    runtime = runtime.expanduser().absolute()
    update_owner = _update_transaction_owner(runtime)
    repair_owner = _repair_configuration_owner(runtime)
    if (
        update_owner is not None
        and repair_owner is not None
        and update_owner != repair_owner
    ):
        raise RuntimeMutationUnsafe(
            "Runtime transaction lock and repair marker owners disagree"
        )
    return update_owner or repair_owner


def require_runtime_mutation_owner(
    runtime: Path,
    *,
    owner_id: str | None,
) -> None:
    current = durable_runtime_mutation_owner(runtime)
    if current is not None and current != owner_id:
        raise RuntimeMutationBusy(
            "a Runtime install, update, or repair transaction is active"
        )
