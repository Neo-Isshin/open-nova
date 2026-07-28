#!/usr/bin/env python3
"""Build deterministic, privacy-safe Actanara release artifacts.

The builder reads a clean Git working tree, copies tracked source into a private
temporary directory for Python package construction, and writes only beneath the
explicit output directory.  No source-root path is serialized into release
metadata.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Iterable, Sequence
import zipfile


SCHEMA_VERSION = 1
AGGREGATE_ALGORITHM = (
    "sha256(path NUL mode NUL size NUL sha256 LF), sorted by UTF-8 relative path"
)
PAYLOAD_ROOT_FILES = frozenset({"LICENSE", "MANIFEST.in", "config.py", "pyproject.toml"})
PAYLOAD_DIRECTORIES = frozenset({"advanced", "install", "src"})
STABLE_INSTALL_SOURCE = "install/setup.sh"
STABLE_INSTALL_ASSET = "install.sh"
STABLE_INSTALL_REF_ANCHOR = b'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"'
ALLOWED_GIT_MODES = frozenset({"100644", "100755", "120000"})
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]*)?$")
FULL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
EXPECTED_RELEASE_TOOLCHAIN = {
    "build": "1.5.1",
    "packaging": "26.2",
    "pyproject-hooks": "1.2.0",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
}
BUILD_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TMPDIR",
        "WINDIR",
    }
)
PRIVATE_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"/Users/(?!example(?:/|$)|you(?:/|$)|\{)[^/\s\"']+(?:/[^\s\"']*)?"),
    re.compile(r"/home/(?!example(?:/|$)|user(?:/|$)|\{)[^/\s\"']+(?:/[^\s\"']*)?"),
    re.compile(r"/private/tmp/[^\s\"']+"),
    re.compile(r"/var/folders/[^\s\"']+"),
    re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!example[\\/]|you[\\/])[^\\/\s\"']+"),
)
SECRET_HEADER_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    re.compile(r"(?im)^\s*Authorization\s*:\s*(?:Bearer|Basic)\s+\S+"),
    re.compile(r"(?im)^\s*X-Api-Key\s*:\s*\S+"),
)

FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "cache",
        "coverage",
        "dist",
        "htmlcov",
        "logs",
        "node_modules",
        "playwright-report",
        "runtime",
        "test-results",
        "tests",
        "wheelhouse",
    }
)
FORBIDDEN_FILE_NAMES = frozenset({".coverage", "coverage.xml"})
FORBIDDEN_SUFFIXES = (
    ".db",
    ".egg-info",
    ".log",
    ".pyc",
    ".pyo",
    ".shm",
    ".sqlite",
    ".sqlite3",
    ".wal",
    "-shm",
    "-wal",
)


class ReleaseBuildError(RuntimeError):
    """Raised when an immutable release precondition is not satisfied."""


@dataclass(frozen=True)
class SourceEntry:
    path: str
    mode: str
    size: int
    sha256: str
    symlink_broken: bool = False

    def manifest_line(self) -> str:
        return f"{self.path}\t{self.mode}\t{self.size}\t{self.sha256}"


@dataclass(frozen=True)
class FrozenSource:
    root: Path
    commit: str
    version: str
    entries: tuple[SourceEntry, ...]


def _run_git(root: Path, *arguments: str, text: bool = True) -> str | bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=False,
        capture_output=True,
        text=text,
        env=environment,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", errors="replace")
        raise ReleaseBuildError(f"git {' '.join(arguments)} failed: {stderr.strip()}")
    return completed.stdout


def _validate_relative_path(relative: str) -> PurePosixPath:
    if not relative or "\0" in relative or "\n" in relative or "\r" in relative or "\t" in relative:
        raise ReleaseBuildError(f"manifest-incompatible source path: {relative!r}")
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or relative.startswith("/"):
        raise ReleaseBuildError(f"unsafe source path: {relative!r}")
    if WINDOWS_ABSOLUTE_RE.match(relative):
        raise ReleaseBuildError(f"unsafe Windows-absolute source path: {relative!r}")
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReleaseBuildError(f"source path is not valid UTF-8: {relative!r}") from exc
    return candidate


def _entry_payload(path: Path, mode: str) -> tuple[bytes, bool]:
    details = path.lstat()
    if mode == "120000":
        if not stat.S_ISLNK(details.st_mode):
            raise ReleaseBuildError(f"Git mode and filesystem type differ: {path.name}")
        target = os.readlink(path)
        return os.fsencode(target), not path.exists()
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseBuildError(f"unsupported tracked filesystem node: {path.name}")
    executable = bool(details.st_mode & 0o111)
    if executable != (mode == "100755"):
        raise ReleaseBuildError(f"Git mode and filesystem mode differ: {path.name}")
    return path.read_bytes(), False


def _parse_git_index(raw: bytes) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, _object_id, stage = metadata.decode("ascii").split(" ")
            relative = os.fsdecode(raw_path)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseBuildError("could not parse Git index record") from exc
        _validate_relative_path(relative)
        if stage != "0":
            raise ReleaseBuildError(f"unmerged Git index entry: {relative}")
        if mode not in ALLOWED_GIT_MODES:
            raise ReleaseBuildError(f"unsupported Git mode {mode}: {relative}")
        records.append((relative, mode))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    return records


def read_project_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = payload["project"]["version"]
    except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseBuildError("pyproject.toml must define project.version") from exc
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseBuildError(f"unsupported project.version: {version!r}")
    return version


def inspect_frozen_git_source(
    source_root: Path,
    *,
    expected_commit: str | None = None,
    expected_version: str | None = None,
) -> FrozenSource:
    root = source_root.expanduser().resolve(strict=True)
    top_level = Path(str(_run_git(root, "rev-parse", "--show-toplevel")).strip()).resolve(
        strict=True
    )
    if top_level != root:
        raise ReleaseBuildError("--source-root must be the Git repository root")

    status_payload = str(_run_git(root, "status", "--porcelain=v1", "--untracked-files=all"))
    if status_payload:
        changed_paths = [line[3:] if len(line) > 3 else line for line in status_payload.splitlines()]
        preview = ", ".join(changed_paths[:5])
        suffix = " …" if len(changed_paths) > 5 else ""
        raise ReleaseBuildError(f"source Git tree is not clean ({preview}{suffix})")

    commit = str(_run_git(root, "rev-parse", "HEAD")).strip().lower()
    if not FULL_COMMIT_RE.fullmatch(commit):
        raise ReleaseBuildError("source HEAD did not resolve to a full commit")
    if expected_commit is not None and commit != expected_commit.lower():
        raise ReleaseBuildError(f"source commit mismatch: expected {expected_commit}, got {commit}")

    version = read_project_version(root)
    if expected_version is not None and version != expected_version:
        raise ReleaseBuildError(f"source version mismatch: expected {expected_version}, got {version}")

    raw_index = _run_git(root, "ls-files", "--stage", "-z", text=False)
    assert isinstance(raw_index, bytes)
    entries: list[SourceEntry] = []
    for relative, mode in _parse_git_index(raw_index):
        payload, broken = _entry_payload(root / relative, mode)
        if mode == "120000" and not _safe_relative_symlink(relative, os.fsdecode(payload)):
            raise ReleaseBuildError(f"unsafe tracked symlink target: {relative}")
        entries.append(
            SourceEntry(
                path=relative,
                mode=mode,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                symlink_broken=broken,
            )
        )
    if not entries:
        raise ReleaseBuildError("source Git tree contains no tracked files")
    broken = [entry.path for entry in entries if entry.symlink_broken]
    if broken:
        raise ReleaseBuildError("broken tracked symlink(s): " + ", ".join(broken))
    return FrozenSource(root=root, commit=commit, version=version, entries=tuple(entries))


def aggregate_sha256(entries: Iterable[SourceEntry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.path.encode("utf-8")):
        digest.update(
            f"{entry.path}\0{entry.mode}\0{entry.size}\0{entry.sha256}\n".encode("utf-8")
        )
    return digest.hexdigest()


def inspect_source_file_set(source_root: Path) -> tuple[SourceEntry, ...]:
    """Snapshot every worktree file, including ignored files, but not Git metadata."""

    root = source_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ReleaseBuildError("source root is not a directory")
    entries: list[SourceEntry] = []
    for current, raw_directories, raw_files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        raw_directories.sort(key=lambda item: os.fsencode(item))
        raw_files.sort(key=lambda item: os.fsencode(item))
        if directory == root:
            raw_directories[:] = [name for name in raw_directories if name != ".git"]
            raw_files = [name for name in raw_files if name != ".git"]

        symlink_directories: list[str] = []
        retained_directories: list[str] = []
        for name in raw_directories:
            candidate = directory / name
            if candidate.is_symlink():
                symlink_directories.append(name)
            else:
                retained_directories.append(name)
        raw_directories[:] = retained_directories

        for name in [*symlink_directories, *raw_files]:
            path = directory / name
            relative = path.relative_to(root).as_posix()
            _validate_relative_path(relative)
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode):
                mode = "120000"
                payload = os.fsencode(os.readlink(path))
                broken = not path.exists()
            elif stat.S_ISREG(details.st_mode):
                mode = "100755" if details.st_mode & 0o111 else "100644"
                payload = path.read_bytes()
                broken = False
            else:
                raise ReleaseBuildError(f"unsupported source filesystem node: {relative}")
            entries.append(
                SourceEntry(
                    path=relative,
                    mode=mode,
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    symlink_broken=broken,
                )
            )
    entries.sort(key=lambda item: item.path.encode("utf-8"))
    return tuple(entries)


def is_runtime_payload_path(relative: str) -> bool:
    first = relative.split("/", 1)[0]
    return relative in PAYLOAD_ROOT_FILES or first in PAYLOAD_DIRECTORIES


def _payload_path_violation(relative: str) -> str | None:
    path = _validate_relative_path(relative)
    lowered_parts = tuple(part.lower() for part in path.parts)
    for part in lowered_parts:
        if part in FORBIDDEN_DIRECTORY_NAMES or part.endswith(".egg-info"):
            return f"forbidden directory component {part!r}"
    name = lowered_parts[-1]
    if name in FORBIDDEN_FILE_NAMES:
        return f"forbidden generated file {name!r}"
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "runtime/generated file suffix"
    return None


def _safe_relative_symlink(entry_path: str, target: str) -> bool:
    if not target or PurePosixPath(target).is_absolute() or WINDOWS_ABSOLUTE_RE.match(target):
        return False
    depth = len(PurePosixPath(entry_path).parent.parts)
    for component in PurePosixPath(target).parts:
        if component in {"", "."}:
            continue
        if component == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def select_runtime_payload(source: FrozenSource) -> tuple[SourceEntry, ...]:
    selected: list[SourceEntry] = []
    violations: list[str] = []
    for entry in source.entries:
        if not is_runtime_payload_path(entry.path):
            continue
        reason = _payload_path_violation(entry.path)
        if reason:
            violations.append(f"{entry.path}: {reason}")
            continue
        if entry.mode == "120000":
            target = os.readlink(source.root / entry.path)
            if not _safe_relative_symlink(entry.path, target):
                violations.append(f"{entry.path}: unsafe symlink target")
                continue
        selected.append(entry)
    if violations:
        raise ReleaseBuildError("Runtime payload boundary violation(s): " + "; ".join(violations))
    if not selected:
        raise ReleaseBuildError("Runtime payload allowlist selected no files")
    return tuple(selected)


def write_manifest(path: Path, entries: Sequence[SourceEntry]) -> None:
    lines = ["path\tmode\tsize\tsha256"]
    lines.extend(entry.manifest_line() for entry in sorted(entries, key=lambda item: item.path.encode("utf-8")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> tuple[SourceEntry, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "path\tmode\tsize\tsha256":
        raise ReleaseBuildError("unexpected manifest header")
    entries: list[SourceEntry] = []
    seen: set[str] = set()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 4:
            raise ReleaseBuildError("malformed manifest row")
        relative, mode, raw_size, digest = fields
        _validate_relative_path(relative)
        if relative in seen:
            raise ReleaseBuildError(f"duplicate manifest path: {relative}")
        if mode not in ALLOWED_GIT_MODES:
            raise ReleaseBuildError(f"unsupported manifest mode {mode}: {relative}")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise ReleaseBuildError(f"invalid manifest size: {relative}") from exc
        if size < 0 or not HEX_SHA256_RE.fullmatch(digest):
            raise ReleaseBuildError(f"invalid manifest metadata: {relative}")
        entries.append(SourceEntry(relative, mode, size, digest))
        seen.add(relative)
    expected_order = sorted(entries, key=lambda item: item.path.encode("utf-8"))
    if entries != expected_order:
        raise ReleaseBuildError("manifest paths are not sorted")
    return tuple(entries)


def _verify_entry(root: Path, entry: SourceEntry) -> bytes:
    payload, broken = _entry_payload(root / entry.path, entry.mode)
    if broken:
        raise ReleaseBuildError(f"source symlink became broken: {entry.path}")
    if len(payload) != entry.size or hashlib.sha256(payload).hexdigest() != entry.sha256:
        raise ReleaseBuildError(f"source changed after freeze: {entry.path}")
    return payload


def _normalized_tar_info(
    name: str,
    *,
    mode: int,
    mtime: int,
    size: int = 0,
    kind: bytes = tarfile.REGTYPE,
    linkname: str = "",
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = size
    info.mtime = mtime
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.linkname = linkname
    return info


def build_runtime_archive(
    source_root: Path,
    entries: Sequence[SourceEntry],
    output: Path,
    *,
    prefix: str,
    source_date_epoch: int,
) -> None:
    _validate_relative_path(prefix)
    if "/" in prefix:
        raise ReleaseBuildError("Runtime archive prefix must be one path component")
    directories: set[str] = set()
    for entry in entries:
        parent = PurePosixPath(entry.path).parent
        while str(parent) not in {"", "."}:
            directories.add(parent.as_posix())
            parent = parent.parent

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_date_epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(
                    _normalized_tar_info(
                        prefix + "/", mode=0o755, mtime=source_date_epoch, kind=tarfile.DIRTYPE
                    )
                )
                for directory in sorted(directories, key=lambda item: item.encode("utf-8")):
                    archive.addfile(
                        _normalized_tar_info(
                            f"{prefix}/{directory}/",
                            mode=0o755,
                            mtime=source_date_epoch,
                            kind=tarfile.DIRTYPE,
                        )
                    )
                for entry in sorted(entries, key=lambda item: item.path.encode("utf-8")):
                    payload = _verify_entry(source_root, entry)
                    name = f"{prefix}/{entry.path}"
                    if entry.mode == "120000":
                        target = os.fsdecode(payload)
                        if not _safe_relative_symlink(entry.path, target):
                            raise ReleaseBuildError(f"unsafe Runtime symlink: {entry.path}")
                        archive.addfile(
                            _normalized_tar_info(
                                name,
                                mode=0o777,
                                mtime=source_date_epoch,
                                kind=tarfile.SYMTYPE,
                                linkname=target,
                            )
                        )
                    else:
                        archive.addfile(
                            _normalized_tar_info(
                                name,
                                mode=0o755 if entry.mode == "100755" else 0o644,
                                mtime=source_date_epoch,
                                size=len(payload),
                            ),
                            io.BytesIO(payload),
                        )


def _copy_frozen_source(
    source: FrozenSource,
    destination: Path,
    *,
    source_date_epoch: int,
) -> None:
    directories = {destination}
    for entry in source.entries:
        target = destination / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        directories.update(target.parents)
        payload = _verify_entry(source.root, entry)
        if entry.mode == "120000":
            os.symlink(os.fsdecode(payload), target)
            os.utime(target, (source_date_epoch, source_date_epoch), follow_symlinks=False)
        else:
            target.write_bytes(payload)
            target.chmod(0o755 if entry.mode == "100755" else 0o644)
            os.utime(target, (source_date_epoch, source_date_epoch))
    for directory in sorted(
        (path for path in directories if path == destination or destination in path.parents),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        os.utime(directory, (source_date_epoch, source_date_epoch))


def _package_member_violation(relative: str, *, sdist_wrapper: bool = False) -> str | None:
    candidate = relative.rstrip("/")
    if not candidate:
        return None
    _validate_relative_path(candidate)
    parts = PurePosixPath(candidate).parts
    if any(part.endswith(".dist-info") or part.endswith(".egg-info") for part in parts):
        return None
    logical_parts = parts[1:] if sdist_wrapper else parts
    if not logical_parts:
        return None
    if logical_parts[0].lower() == "tools":
        return "release tooling is public-source-only"
    if len(logical_parts) == 1 and logical_parts[0].lower() == "requirements-release.txt":
        return "release toolchain requirements are public-source-only"
    return _payload_path_violation(PurePosixPath(*logical_parts).as_posix())


def validate_python_package_contents(wheel: Path, sdist: Path) -> None:
    violations: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            relative = info.filename
            try:
                reason = _package_member_violation(relative)
            except ReleaseBuildError:
                reason = "unsafe archive path"
            if reason:
                violations.append(f"{wheel.name}:{relative}: {reason}")
    with tarfile.open(sdist, mode="r:gz") as archive:
        for info in archive.getmembers():
            relative = info.name
            try:
                reason = _package_member_violation(relative, sdist_wrapper=True)
            except ReleaseBuildError:
                reason = "unsafe archive path"
            if reason:
                violations.append(f"{sdist.name}:{relative}: {reason}")
            if info.issym() and not _safe_relative_symlink(relative, info.linkname):
                violations.append(f"{sdist.name}:{relative}: unsafe symlink")
    if violations:
        raise ReleaseBuildError("Python package boundary violation(s): " + "; ".join(violations))


def _decode_archive_text(payload: bytes) -> str | None:
    if b"\0" in payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    control_count = sum(
        1 for character in text if ord(character) < 32 and character not in "\n\r\t\f\b"
    )
    if control_count > max(4, len(text) // 100):
        return None
    return text


def _archive_text_privacy_violations(
    descriptor: str,
    payload: bytes,
    *,
    forbidden_paths: Sequence[str],
) -> list[str]:
    text = _decode_archive_text(payload)
    if text is None:
        return []
    violations: list[str] = []
    for forbidden in forbidden_paths:
        if forbidden and forbidden in text:
            violations.append(f"{descriptor}: serialized private build/source path")
            break
    if any(pattern.search(text) for pattern in PRIVATE_ABSOLUTE_PATH_PATTERNS):
        violations.append(f"{descriptor}: private absolute path")
    if any(pattern.search(text) for pattern in SECRET_HEADER_PATTERNS):
        violations.append(f"{descriptor}: secret-bearing header")
    return violations


def validate_python_package_privacy(
    wheel: Path,
    sdist: Path,
    *,
    forbidden_paths: Sequence[str] = (),
) -> None:
    violations: list[str] = []
    normalized_forbidden = tuple(
        dict.fromkeys(os.fspath(Path(value).expanduser().absolute()) for value in forbidden_paths if value)
    )
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            violations.extend(
                _archive_text_privacy_violations(
                    f"{wheel.name}:{info.filename}",
                    archive.read(info),
                    forbidden_paths=normalized_forbidden,
                )
            )
    with tarfile.open(sdist, mode="r:gz") as archive:
        for info in archive.getmembers():
            if not info.isfile():
                continue
            extracted = archive.extractfile(info)
            if extracted is None:
                raise ReleaseBuildError(f"could not read sdist privacy-scan member: {info.name}")
            violations.extend(
                _archive_text_privacy_violations(
                    f"{sdist.name}:{info.name}",
                    extracted.read(),
                    forbidden_paths=normalized_forbidden,
                )
            )
    if violations:
        raise ReleaseBuildError("Python package privacy violation(s): " + "; ".join(violations))


def _normalized_zip_datetime(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def normalize_wheel(
    source: Path,
    output: Path,
    *,
    source_date_epoch: int,
) -> None:
    """Repack a built wheel with deterministic container metadata."""

    records: list[tuple[str, bytes, int, bool]] = []
    seen: set[str] = set()
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            relative = info.filename
            candidate = relative.rstrip("/")
            if not candidate:
                continue
            _validate_relative_path(candidate)
            if relative in seen:
                raise ReleaseBuildError(f"duplicate wheel member: {relative}")
            seen.add(relative)
            mode = (info.external_attr >> 16) & 0o777
            records.append((relative, archive.read(info), mode, info.is_dir()))

    fixed_datetime = _normalized_zip_datetime(source_date_epoch)
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative, payload, original_mode, directory in sorted(
            records, key=lambda item: item[0].encode("utf-8")
        ):
            info = zipfile.ZipInfo(relative, date_time=fixed_datetime)
            info.create_system = 3
            info.extra = b""
            info.comment = b""
            if directory:
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                archive.writestr(info, b"")
            else:
                mode = 0o755 if original_mode & 0o111 else 0o644
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def normalize_sdist(
    source: Path,
    output: Path,
    *,
    source_date_epoch: int,
) -> None:
    """Repack a built source distribution with deterministic tar/gzip metadata."""

    records: list[tuple[tarfile.TarInfo, bytes | None]] = []
    seen: set[str] = set()
    with tarfile.open(source, mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = member.name.rstrip("/")
            if not relative:
                continue
            _validate_relative_path(relative)
            if member.name in seen:
                raise ReleaseBuildError(f"duplicate sdist member: {member.name}")
            seen.add(member.name)
            if member.isdir() or member.issym():
                payload = None
            elif member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ReleaseBuildError(f"could not read sdist member: {member.name}")
                payload = extracted.read()
            else:
                raise ReleaseBuildError(f"unsupported sdist member type: {member.name}")
            records.append((member, payload))

    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_date_epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for member, payload in sorted(records, key=lambda item: item[0].name.encode("utf-8")):
                    if member.isdir():
                        normalized = _normalized_tar_info(
                            member.name.rstrip("/") + "/",
                            mode=0o755,
                            mtime=source_date_epoch,
                            kind=tarfile.DIRTYPE,
                        )
                        archive.addfile(normalized)
                    elif member.issym():
                        if not _safe_relative_symlink(member.name, member.linkname):
                            raise ReleaseBuildError(f"unsafe sdist symlink: {member.name}")
                        normalized = _normalized_tar_info(
                            member.name,
                            mode=0o777,
                            mtime=source_date_epoch,
                            kind=tarfile.SYMTYPE,
                            linkname=member.linkname,
                        )
                        archive.addfile(normalized)
                    else:
                        assert payload is not None
                        normalized = _normalized_tar_info(
                            member.name,
                            mode=0o755 if member.mode & 0o111 else 0o644,
                            mtime=source_date_epoch,
                            size=len(payload),
                        )
                        archive.addfile(normalized, io.BytesIO(payload))


def _build_subprocess_environment(source_date_epoch: int | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in BUILD_ENVIRONMENT_ALLOWLIST and value
    }
    environment.setdefault("PATH", os.defpath)
    if not any(key in environment for key in ("LC_ALL", "LC_CTYPE", "LANG")):
        environment["LC_ALL"] = "C"
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TZ": "UTC",
        }
    )
    if source_date_epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
    return environment


def validate_release_toolchain(python: str) -> dict[str, str]:
    locked_distributions = json.dumps(tuple(EXPECTED_RELEASE_TOOLCHAIN))
    probe = f"""
import importlib.metadata
import json
import platform

payload = {{
    "python": platform.python_version(),
    "pythonImplementation": platform.python_implementation(),
}}
for distribution in {locked_distributions}:
    payload[distribution] = importlib.metadata.version(distribution)
print(json.dumps(payload, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [python, "-B", "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=_build_subprocess_environment(),
        )
    except OSError as exc:
        raise ReleaseBuildError(f"could not execute release Python: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip().splitlines()
        tail = " | ".join(diagnostic[-5:])
        raise ReleaseBuildError(f"could not inspect locked release toolchain: {tail}")
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise ReleaseBuildError("release toolchain probe did not return JSON") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str)
        for key in ("python", "pythonImplementation", *EXPECTED_RELEASE_TOOLCHAIN)
    ):
        raise ReleaseBuildError("release toolchain probe returned an invalid contract")
    mismatches = [
        f"{name}=={payload.get(name)} (required {required})"
        for name, required in EXPECTED_RELEASE_TOOLCHAIN.items()
        if payload.get(name) != required
    ]
    if mismatches:
        raise ReleaseBuildError("release toolchain version mismatch: " + "; ".join(mismatches))
    return {key: str(value) for key, value in payload.items()}


def build_python_packages(
    source: FrozenSource,
    output_directory: Path,
    *,
    source_date_epoch: int,
    python: str,
) -> tuple[Path, Path]:
    with tempfile.TemporaryDirectory(prefix="actanara-release-build-") as temporary:
        temporary_root = Path(temporary)
        build_root = temporary_root / "source"
        package_output = temporary_root / "dist"
        build_root.mkdir()
        package_output.mkdir()
        _copy_frozen_source(source, build_root, source_date_epoch=source_date_epoch)
        source_before_subprocess = inspect_source_file_set(source.root)
        environment = _build_subprocess_environment(source_date_epoch)
        completed = subprocess.run(
            [
                python,
                "-B",
                "-m",
                "build",
                "--no-isolation",
                "--sdist",
                "--wheel",
                "--outdir",
                os.fspath(package_output),
            ],
            cwd=build_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        source_after_subprocess = inspect_source_file_set(source.root)
        if source_after_subprocess != source_before_subprocess:
            raise ReleaseBuildError(
                "source full file set (including ignored files) changed during Python build subprocess"
            )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout).strip().splitlines()
            tail = " | ".join(diagnostic[-5:])
            raise ReleaseBuildError(f"python -m build failed: {tail}")
        wheels = sorted(package_output.glob("*.whl"))
        sdists = sorted(package_output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ReleaseBuildError("python -m build must produce exactly one wheel and one sdist")
        expected_fragment = source.version.replace("-", "_")
        if expected_fragment not in wheels[0].name or source.version not in sdists[0].name:
            raise ReleaseBuildError("Python package filenames do not match pyproject version")
        wheel = output_directory / wheels[0].name
        sdist = output_directory / sdists[0].name
        normalize_wheel(wheels[0], wheel, source_date_epoch=source_date_epoch)
        normalize_sdist(sdists[0], sdist, source_date_epoch=source_date_epoch)
    validate_python_package_contents(wheel, sdist)
    validate_python_package_privacy(
        wheel,
        sdist,
        forbidden_paths=(
            os.fspath(source.root),
            os.fspath(output_directory),
            os.fspath(temporary_root),
            os.fspath(build_root),
            os.fspath(package_output),
        ),
    )
    return wheel, sdist


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_stable_install_asset(
    source: FrozenSource,
    output_directory: Path,
    *,
    source_date_epoch: int,
) -> Path:
    """Publish a cross-platform setup entrypoint pinned to this release."""

    matches = [item for item in source.entries if item.path == STABLE_INSTALL_SOURCE]
    if len(matches) != 1:
        raise ReleaseBuildError(f"release source is missing {STABLE_INSTALL_SOURCE}")
    entry = matches[0]
    if entry.mode != "100755" or entry.symlink_broken:
        raise ReleaseBuildError("stable install bootstrap must be a regular executable file")
    payload = _verify_entry(source.root, entry)
    if not payload.startswith(b"#!/bin/sh\n") or b"\0" in payload:
        raise ReleaseBuildError("stable install entrypoint has an invalid executable format")
    if (
        b"if true; then\n" not in payload
        or not payload.endswith(b"fi\n")
        or b"select_platform_adapter" not in payload
        or b'ADAPTER_PATH="install/bootstrap.sh"' not in payload
        or b'ADAPTER_PATH="install/bootstrap-linux.sh"' not in payload
        or b"download_exact_adapter" not in payload
    ):
        raise ReleaseBuildError(
            "stable install entrypoint is missing its cross-platform hosted-stream contract"
        )
    if payload.count(STABLE_INSTALL_REF_ANCHOR) != 1:
        raise ReleaseBuildError(
            "stable install entrypoint must contain exactly one release-ref anchor"
        )
    pinned_ref = (
        f'SOURCE_REF="${{ACTANARA_INSTALL_REF:-{source.commit}}}"'.encode("ascii")
    )
    payload = payload.replace(STABLE_INSTALL_REF_ANCHOR, pinned_ref, 1)
    target = output_directory / STABLE_INSTALL_ASSET
    target.write_bytes(payload)
    target.chmod(0o755)
    os.utime(target, (source_date_epoch, source_date_epoch))
    return target


def _source_date_epoch(value: str | None) -> int:
    raw = value if value is not None else os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        raise ReleaseBuildError("set SOURCE_DATE_EPOCH or pass --source-date-epoch")
    try:
        epoch = int(raw)
    except ValueError as exc:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 315532800:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH must be on or after 1980-01-01")
    return epoch


def _assert_output_is_outside_source(source_root: Path, output_directory: Path) -> Path:
    output = output_directory.expanduser().resolve()
    try:
        output.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ReleaseBuildError("--output-dir must be outside the source repository")
    if output.exists() and any(output.iterdir()):
        raise ReleaseBuildError("--output-dir must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_release(
    source_root: Path,
    output_directory: Path,
    *,
    source_date_epoch: int,
    expected_commit: str | None = None,
    expected_version: str | None = None,
    python: str = sys.executable,
) -> dict[str, object]:
    source_date_epoch = _source_date_epoch(str(source_date_epoch))
    resolved_source_root = source_root.expanduser().resolve(strict=True)
    source_file_set_before = inspect_source_file_set(resolved_source_root)
    source_file_set_aggregate = aggregate_sha256(source_file_set_before)
    toolchain = validate_release_toolchain(python)
    source = inspect_frozen_git_source(
        resolved_source_root,
        expected_commit=expected_commit,
        expected_version=expected_version,
    )
    if inspect_source_file_set(source.root) != source_file_set_before:
        raise ReleaseBuildError(
            "source full file set (including ignored files) changed during startup validation"
        )
    output = _assert_output_is_outside_source(source.root, output_directory)
    payload = select_runtime_payload(source)
    source_aggregate = aggregate_sha256(source.entries)
    payload_aggregate = aggregate_sha256(payload)

    source_manifest = output / "public-source-manifest.tsv"
    payload_manifest = output / "runtime-payload-manifest.tsv"
    summary_path = output / "manifest-summary.json"
    write_manifest(source_manifest, source.entries)
    write_manifest(payload_manifest, payload)
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "aggregateAlgorithm": AGGREGATE_ALGORITHM,
        "version": source.version,
        "sourceGitCommit": source.commit,
        "publicSource": {
            "fileCount": len(source.entries),
            "aggregateSha256": source_aggregate,
        },
        "sourceFileSetIncludingIgnored": {
            "fileCount": len(source_file_set_before),
            "aggregateSha256": source_file_set_aggregate,
            "gitMetadataExcluded": True,
        },
        "runtimePayload": {
            "fileCount": len(payload),
            "aggregateSha256": payload_aggregate,
        },
        "publicSourceOnlyFileCount": len(source.entries) - len(payload),
        "brokenSymlinks": [],
    }
    _write_json(summary_path, summary)

    runtime_archive = output / f"actanara-{source.version}-runtime.tar.gz"
    build_runtime_archive(
        source.root,
        payload,
        runtime_archive,
        prefix=f"actanara-{source.version}",
        source_date_epoch=source_date_epoch,
    )
    wheel, sdist = build_python_packages(
        source,
        output,
        source_date_epoch=source_date_epoch,
        python=python,
    )
    stable_install_asset = build_stable_install_asset(
        source,
        output,
        source_date_epoch=source_date_epoch,
    )

    artifact_paths = [
        source_manifest,
        payload_manifest,
        summary_path,
        runtime_archive,
        wheel,
        sdist,
        stable_install_asset,
    ]
    provenance_path = output / "release-provenance.json"
    provenance = {
        "schemaVersion": SCHEMA_VERSION,
        "product": "actanara",
        "version": source.version,
        "sourceGitCommit": source.commit,
        "sourceDateEpoch": source_date_epoch,
        "normalizedTimestamp": datetime.fromtimestamp(
            source_date_epoch, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "publicSourceAggregateSha256": source_aggregate,
        "runtimePayloadAggregateSha256": payload_aggregate,
        "sourceFileSetIncludingIgnored": {
            "fileCount": len(source_file_set_before),
            "aggregateSha256": source_file_set_aggregate,
            "gitMetadataExcluded": True,
            "verifiedUnchangedAfterBuild": True,
        },
        "toolchain": toolchain,
        "pythonPackageContainerMetadataNormalized": True,
        "artifacts": [
            {"name": path.name, "sha256": _sha256_file(path), "size": path.stat().st_size}
            for path in sorted(artifact_paths, key=lambda item: item.name.encode("utf-8"))
        ],
    }
    _write_json(provenance_path, provenance)

    checksummed = sorted([*artifact_paths, provenance_path], key=lambda item: item.name.encode("utf-8"))
    checksums_path = output / "SHA256SUMS"
    checksums_path.write_text(
        "".join(f"{_sha256_file(path)}  {path.name}\n" for path in checksummed),
        encoding="utf-8",
    )

    final_source = inspect_frozen_git_source(
        source.root,
        expected_commit=source.commit,
        expected_version=source.version,
    )
    if final_source.entries != source.entries:
        raise ReleaseBuildError("source tree changed during release construction")
    source_file_set_after = inspect_source_file_set(source.root)
    if source_file_set_after != source_file_set_before:
        raise ReleaseBuildError(
            "source full file set (including ignored files) changed during release construction"
        )
    if aggregate_sha256(source_file_set_after) != source_file_set_aggregate:
        raise ReleaseBuildError("source full file-set aggregate changed during release construction")

    return {
        "status": "built",
        "version": source.version,
        "sourceGitCommit": source.commit,
        "publicSourceAggregateSha256": source_aggregate,
        "runtimePayloadAggregateSha256": payload_aggregate,
        "sourceFileSetIncludingIgnoredAggregateSha256": source_file_set_aggregate,
        "toolchain": toolchain,
        "outputFiles": [path.name for path in [*checksummed, checksums_path]],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-date-epoch")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-version")
    parser.add_argument("--python", default=sys.executable)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = build_release(
            arguments.source_root,
            arguments.output_dir,
            source_date_epoch=_source_date_epoch(arguments.source_date_epoch),
            expected_commit=arguments.expected_commit,
            expected_version=arguments.expected_version,
            python=arguments.python,
        )
    except (OSError, ReleaseBuildError) as exc:
        print(f"release build blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
