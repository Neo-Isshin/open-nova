"""Resolve the immutable commit behind the latest stable Actanara Release."""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
from typing import Any, Callable
from urllib import request
from urllib.parse import urlsplit


DEFAULT_RELEASE_API_URL = (
    "https://api.github.com/repos/Neo-Isshin/actanara/releases/latest"
)
DEFAULT_RELEASE_SOURCE_URL = "https://github.com/Neo-Isshin/actanara.git"
MAX_RELEASE_RESPONSE_BYTES = 1024 * 1024
FULL_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
SAFE_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
WITHDRAWN_MARKER_RE = re.compile(
    r"(?<![a-z0-9])withdrawn(?![a-z0-9])",
    re.IGNORECASE,
)


class ReleaseResolutionError(ValueError):
    """The stable Release could not be resolved without ambiguity."""


def _verified_urlopen(api_request: request.Request, *, timeout: int):
    """Open GitHub with the runtime CA bundle when one is available."""

    try:
        import certifi
    except ImportError:
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    return request.urlopen(api_request, timeout=timeout, context=context)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ReleaseResolutionError(
                f"latest Release response contains duplicate field: {key}"
            )
        payload[key] = value
    return payload


def parse_latest_release_payload(payload: bytes) -> str:
    """Validate the latest Release response and return its safe tag name."""

    if not payload or len(payload) > MAX_RELEASE_RESPONSE_BYTES:
        raise ReleaseResolutionError("latest Release response has an unsafe size")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        metadata = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseResolutionError("latest Release response is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise ReleaseResolutionError("latest Release response must be an object")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise ReleaseResolutionError("latest Release is not stable")
    if metadata.get("immutable") is not True:
        raise ReleaseResolutionError("latest Release is not immutable")
    release_name = metadata.get("name")
    if release_name is not None and not isinstance(release_name, str):
        raise ReleaseResolutionError("latest Release name is invalid")
    if isinstance(release_name, str) and WITHDRAWN_MARKER_RE.search(release_name):
        raise ReleaseResolutionError("latest Release was withdrawn")
    release_tag = metadata.get("tag_name")
    if not isinstance(release_tag, str) or not SAFE_TAG_RE.fullmatch(release_tag):
        raise ReleaseResolutionError("latest Release tag is invalid")
    if ".." in release_tag or "@{" in release_tag or release_tag.endswith("."):
        raise ReleaseResolutionError("latest Release tag is unsafe")
    return release_tag


def resolve_release_tag_rows(release_tag: str, output: str) -> str:
    """Resolve an annotated or lightweight tag from exact ls-remote rows."""

    direct_ref = f"refs/tags/{release_tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    rows: dict[str, str] = {}
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) != 2:
            raise ReleaseResolutionError("stable Release tag response is malformed")
        object_id, ref_name = fields
        if ref_name not in {direct_ref, peeled_ref}:
            raise ReleaseResolutionError("stable Release tag response contains an unexpected ref")
        if ref_name in rows:
            raise ReleaseResolutionError("stable Release tag response contains a duplicate ref")
        if not FULL_COMMIT_RE.fullmatch(object_id):
            raise ReleaseResolutionError("stable Release tag did not resolve to a full object ID")
        rows[ref_name] = object_id.casefold()
    if direct_ref not in rows:
        raise ReleaseResolutionError("stable Release tag is missing")
    return rows.get(peeled_ref, rows[direct_ref])


def _read_latest_release(
    api_url: str,
    *,
    opener: Callable[..., Any],
) -> bytes:
    api_request = request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "actanara-release-resolver",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with opener(api_request, timeout=30) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ReleaseResolutionError(
                    f"latest Release request returned HTTP {status}"
                )
            payload = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except ReleaseResolutionError:
        raise
    except Exception as exc:
        raise ReleaseResolutionError("latest Release could not be read") from exc
    if len(payload) > MAX_RELEASE_RESPONSE_BYTES:
        raise ReleaseResolutionError("latest Release response is too large")
    return payload


def _git_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for name in ("SYSTEMROOT", "WINDIR", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def resolve_latest_stable_commit(
    *,
    source_url: str = DEFAULT_RELEASE_SOURCE_URL,
    api_url: str = DEFAULT_RELEASE_API_URL,
    git_binary: str = "git",
    opener: Callable[..., Any] = _verified_urlopen,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Resolve the latest stable immutable Release to one full commit ID."""

    parsed_source = urlsplit(source_url)
    if (
        parsed_source.scheme != "https"
        or not parsed_source.hostname
        or parsed_source.username is not None
        or parsed_source.password is not None
        or parsed_source.query
        or parsed_source.fragment
    ):
        raise ReleaseResolutionError(
            "stable Release source must be an uncredentialed HTTPS URL"
        )
    payload = _read_latest_release(api_url, opener=opener)
    release_tag = parse_latest_release_payload(payload)
    direct_ref = f"refs/tags/{release_tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    try:
        with tempfile.TemporaryDirectory(prefix="actanara-release-resolver-") as git_cwd:
            environment = _git_environment()
            environment["GIT_CEILING_DIRECTORIES"] = git_cwd
            completed = runner(
                [
                    git_binary,
                    "-c",
                    "protocol.allow=never",
                    "-c",
                    "protocol.https.allow=always",
                    "-c",
                    "protocol.ext.allow=never",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "ls-remote",
                    "--tags",
                    source_url,
                    direct_ref,
                    peeled_ref,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=git_cwd,
                env=environment,
            )
    except Exception as exc:
        raise ReleaseResolutionError("stable Release tag could not be read") from exc
    if completed.returncode != 0:
        raise ReleaseResolutionError("stable Release tag could not be resolved")
    return resolve_release_tag_rows(release_tag, completed.stdout)
