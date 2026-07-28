"""Supported external tool catalog and path rediscovery helpers."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .external_tool_definitions import TOOL_CATALOG, fields_for_tool_home
from .paths import RuntimePaths
from .settings import default_external_tool_settings, read_settings, resolve_external_tool_paths, write_operator_settings


def supported_external_tool_catalog() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "tools": [
            {"id": tool_id, **definition}
            for tool_id, definition in TOOL_CATALOG.items()
        ],
    }


def detect_external_tools(
    paths: RuntimePaths | None = None,
    *,
    user_home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return authoritative local-presence status for every supported tool.

    A configured/default home directory alone is not evidence that a tool is
    installed. Presence requires at least one tool-specific marker, configured
    primary path, detection candidate, explicit binary candidate, or executable
    discoverable on ``PATH``. This distinction is especially important for
    Antigravity, whose default home is shared with Gemini CLI.

    ``which`` is injectable so isolated callers and tests do not inherit the
    host process's executable inventory.
    """

    isolated_home = user_home is not None
    selected_home = (user_home or Path.home()).expanduser().absolute()
    configured = _configured_paths_for_detection(
        paths,
        user_home=selected_home if isolated_home else None,
    )
    which_executable = which or shutil.which
    presence: dict[str, dict[str, Any]] = {}
    detected_tool_keys: list[str] = []

    for tool_id, definition in TOOL_CATALOG.items():
        configured_records = _configured_records_for_tool(configured, tool_id)
        configured_homes = _dedupe_paths(
            value
            for _instance_id, values in configured_records
            for value in _path_values(values.get("home"))
        )
        candidate_homes = _dedupe_paths(
            [
                *configured_homes,
                *_expand_home_candidates(selected_home, definition),
            ]
        )
        evidence: list[dict[str, str]] = []
        evidence_keys: set[tuple[str, ...]] = set()

        def add_evidence(kind: str, path: Path | str, **metadata: str) -> None:
            normalized_path = _absolute_path(path)
            record = {
                "kind": kind,
                "path": str(normalized_path),
                **{
                    key: str(value)
                    for key, value in metadata.items()
                    if str(value or "").strip()
                },
            }
            key = tuple(f"{name}={record[name]}" for name in sorted(record))
            if key not in evidence_keys:
                evidence_keys.add(key)
                evidence.append(record)

        for home in candidate_homes:
            for marker in definition.get("homeMarkers") or []:
                marker_path = home / str(marker)
                if _path_exists(marker_path):
                    add_evidence(
                        "home-marker",
                        marker_path,
                        home=str(home),
                        marker=str(marker),
                    )

        detection_fields = tuple(
            str(field)
            for field in definition.get("detectionFields") or ()
        )
        default_binary_candidates = {
            str(path)
            for path in _path_values(
                fields_for_tool_home(
                    tool_id,
                    selected_home,
                    user_home=selected_home,
                ).get("binaryCandidates")
            )
        }
        for instance_id, values in configured_records:
            for field in detection_fields:
                for configured_path in _path_values(values.get(field)):
                    if _path_exists(configured_path):
                        add_evidence(
                            "configured-path",
                            configured_path,
                            field=field,
                            instanceId=instance_id,
                        )
            for binary_path in _path_values(values.get("binaryCandidates")):
                if (
                    isolated_home
                    and str(binary_path) in default_binary_candidates
                    and not _path_is_within(binary_path, selected_home)
                ):
                    # An explicit user_home is an isolation boundary for tests
                    # and sandboxed probes. System-wide catalog defaults are
                    # still considered during normal production detection.
                    continue
                if _path_is_file(binary_path):
                    add_evidence(
                        "binary-candidate",
                        binary_path,
                        field="binaryCandidates",
                        instanceId=instance_id,
                    )

        for candidate_path in _expanded_detection_candidates(
            selected_home,
            definition,
        ):
            if _path_exists(candidate_path):
                add_evidence("detection-candidate", candidate_path)

        for binary_name in definition.get("binaryNames") or []:
            normalized_name = str(binary_name or "").strip()
            if not normalized_name:
                continue
            try:
                executable = which_executable(normalized_name)
            except (OSError, TypeError, ValueError):
                executable = None
            if executable:
                add_evidence(
                    "path-binary",
                    executable,
                    binary=normalized_name,
                )

        detected = bool(evidence)
        if detected:
            detected_tool_keys.append(tool_id)
        primary_home = configured_homes[0] if configured_homes else (
            candidate_homes[0] if candidate_homes else None
        )
        detail = {
            "id": tool_id,
            "name": str(definition.get("name") or tool_id),
            "emoji": str(definition.get("emoji") or ""),
            "detected": detected,
            "status": "detected" if detected else "missing",
            "configuredHome": str(primary_home) if primary_home is not None else "",
            "configuredHomes": [str(home) for home in configured_homes],
            "detectedBy": list(dict.fromkeys(item["kind"] for item in evidence)),
            "evidence": evidence,
        }
        presence[tool_id] = detail

    # ``detectedToolIds`` and ``tools`` are compatibility aliases for early
    # callers. Dashboard code should prefer detectedToolKeys/toolPresence.
    return {
        "schemaVersion": 1,
        "checkedAt": datetime.now().astimezone().isoformat(),
        "detectedToolKeys": detected_tool_keys,
        "detectedToolIds": list(detected_tool_keys),
        "toolPresence": presence,
        "tools": presence,
    }


def detected_external_tool_ids(
    paths: RuntimePaths | None = None,
    *,
    user_home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Return detected catalog IDs in stable catalog order."""

    result = detect_external_tools(
        paths,
        user_home=user_home,
        which=which,
    )
    return tuple(result["detectedToolKeys"])


def rediscover_external_tools(paths: RuntimePaths | None = None) -> dict[str, Any]:
    user_home = Path.home()
    configured = resolve_external_tool_paths(paths)
    candidates = _candidate_homes(user_home)
    discoveries = []
    updates: dict[str, dict[str, str]] = {}
    for tool_id, definition in TOOL_CATALOG.items():
        current_home = _path_str((configured.get(tool_id) or {}).get("home"))
        homes = [
            home
            for home in candidates.get(tool_id, [])
            if _matches_tool(home, definition, user_home=user_home)
        ]
        for home in homes:
            same_home = bool(current_home) and _same_path(home, Path(current_home))
            if same_home:
                status = "unchanged"
            elif current_home and tool_id != "openclaw":
                status = "changed"
            else:
                status = "new"
            instance_id = tool_id
            if status == "new" and tool_id == "openclaw" and current_home:
                instance_id = _next_instance_id("openclaw", configured)
            update = _fields_for_home(tool_id, home)
            discoveries.append(
                {
                    "tool": tool_id,
                    "instanceId": instance_id,
                    "name": definition["name"],
                    "path": str(home),
                    "configuredPath": current_home,
                    "status": status,
                    "update": update,
                }
            )
            if status in {"new", "changed"}:
                updates[instance_id] = update
    return {
        "schemaVersion": 1,
        "checkedAt": datetime.now().astimezone().isoformat(),
        "catalog": supported_external_tool_catalog(),
        "discoveries": discoveries,
        "suggestedUpdates": updates,
        "summary": {
            "detected": len(discoveries),
            "new": sum(1 for item in discoveries if item["status"] == "new"),
            "changed": sum(1 for item in discoveries if item["status"] == "changed"),
        },
    }


def add_external_tool_instance(tool_id: str, home_path: str, paths: RuntimePaths | None = None, *, instance_id: str | None = None) -> dict[str, Any]:
    if tool_id not in TOOL_CATALOG:
        raise ValueError(f"unsupported external tool: {tool_id}")
    home = Path(home_path).expanduser().absolute()
    if not home.exists() or not home.is_dir():
        raise ValueError("tool home path must be an existing directory")
    settings = read_settings(paths)
    external = settings.get("externalTools") if isinstance(settings.get("externalTools"), dict) else {}
    target_id = str(instance_id or tool_id).strip() or tool_id
    if target_id in external and _path_str((external.get(target_id) or {}).get("home")) and not _same_path(home, Path(_path_str(external[target_id]["home"]))):
        target_id = _next_instance_id(tool_id, external)
    update = {target_id: _fields_for_home(tool_id, home)}
    updated = write_operator_settings({"externalTools": update}, paths)
    return {"added": target_id, "tool": tool_id, "path": str(home), "externalTools": updated.get("externalTools", {})}


def _candidate_homes(home: Path) -> dict[str, list[Path]]:
    candidates: dict[str, list[Path]] = {}
    for tool_id, definition in TOOL_CATALOG.items():
        expanded = _expand_home_candidates(home, definition)
        existing = _existing_dirs(expanded)
        if not existing and _matches_detection_candidates(home, definition):
            canonical = _canonical_home_candidate(home, definition)
            if canonical is not None:
                existing.append(canonical)
        candidates[tool_id] = existing
    return candidates


def _configured_records_for_tool(
    configured: dict[str, dict[str, Any]],
    tool_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (instance_id, values)
        for instance_id, values in configured.items()
        if isinstance(values, dict)
        and (
            instance_id == tool_id
            or instance_id.startswith(f"{tool_id}-")
        )
    ]


def _configured_paths_for_detection(
    paths: RuntimePaths | None,
    *,
    user_home: Path | None,
) -> dict[str, dict[str, Any]]:
    configured = {
        tool_id: dict(values)
        for tool_id, values in resolve_external_tool_paths(paths).items()
        if isinstance(values, dict)
    }
    if user_home is None:
        return configured

    process_defaults = default_external_tool_settings()
    isolated_defaults = default_external_tool_settings(user_home)
    for tool_id, values in configured.items():
        if tool_id not in TOOL_CATALOG:
            continue
        process_values = process_defaults.get(tool_id) or {}
        isolated_values = isolated_defaults.get(tool_id) or {}
        for field, isolated_value in isolated_values.items():
            if field not in process_values:
                continue
            if _same_path_setting(values.get(field), process_values[field]):
                values[field] = isolated_value
    return configured


def _same_path_setting(left: Any, right: Any) -> bool:
    left_paths = [str(path) for path in _path_values(left)]
    right_paths = [str(path) for path in _path_values(right)]
    return left_paths == right_paths


def _path_values(value: Any) -> list[Path]:
    if isinstance(value, (list, tuple)):
        return [
            _absolute_path(item)
            for item in value
            if _path_str(item)
        ]
    if isinstance(value, (str, Path)) and _path_str(value):
        return [_absolute_path(value)]
    return []


def _absolute_path(value: Path | str) -> Path:
    return Path(str(value)).expanduser().absolute()


def _dedupe_paths(values) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = _absolute_path(value)
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _path_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _expanded_detection_candidates(
    home: Path,
    definition: dict[str, Any],
) -> list[Path]:
    context = _candidate_context(home)
    return _dedupe_paths(
        Path(str(pattern).format(**context)).expanduser()
        for pattern in definition.get("detectionCandidates") or []
    )


def _expand_home_candidates(home: Path, definition: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    environment_name = str(definition.get("homeEnvironment") or "")
    environment_value = str(os.getenv(environment_name) or "").strip() if environment_name else ""
    if environment_value:
        candidates.append(Path(environment_value).expanduser())
    context = _candidate_context(home)
    for pattern in definition.get("homeCandidates") or []:
        raw = str(pattern).format(**context)
        if raw.startswith("~/"):
            relative = raw[2:]
            if any(char in relative for char in "*?["):
                candidates.extend(home.glob(relative))
            else:
                candidates.append(home / relative)
        else:
            candidate = Path(raw).expanduser()
            if any(char in raw for char in "*?["):
                candidates.extend(candidate.parent.glob(candidate.name))
            else:
                candidates.append(candidate)
    return candidates


def _existing_dirs(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        expanded = path.expanduser().absolute()
        marker = str(expanded)
        if marker not in seen and expanded.is_dir():
            seen.add(marker)
            result.append(expanded)
    return result


def _matches_tool(
    home: Path,
    definition: dict[str, Any],
    *,
    user_home: Path | None = None,
) -> bool:
    return any(
        (home / marker).exists()
        for marker in definition.get("homeMarkers") or []
    ) or _matches_detection_candidates(user_home or Path.home(), definition)


def _matches_detection_candidates(home: Path, definition: dict[str, Any]) -> bool:
    context = _candidate_context(home)
    return any(
        Path(str(pattern).format(**context)).expanduser().exists()
        for pattern in definition.get("detectionCandidates") or []
    )


def _canonical_home_candidate(
    home: Path,
    definition: dict[str, Any],
) -> Path | None:
    patterns = definition.get("homeCandidates") or []
    if not patterns:
        return None
    raw = str(patterns[0]).format(**_candidate_context(home))
    if any(char in raw for char in "*?["):
        return None
    if raw.startswith("~/"):
        return (home / raw[2:]).expanduser().absolute()
    return Path(raw).expanduser().absolute()


def _candidate_context(home: Path) -> dict[str, str]:
    xdg_data_home = Path(
        os.getenv("XDG_DATA_HOME") or home / ".local" / "share"
    ).expanduser()
    xdg_config_home = Path(
        os.getenv("XDG_CONFIG_HOME") or home / ".config"
    ).expanduser()
    return {
        "userHome": str(home.expanduser().absolute()),
        "xdgDataHome": str(xdg_data_home.absolute()),
        "xdgConfigHome": str(xdg_config_home.absolute()),
    }


def _fields_for_home(tool_id: str, home: Path) -> dict[str, str]:
    return fields_for_tool_home(tool_id, home)


def _path_str(value: Any) -> str:
    return str(value or "").strip()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return left.expanduser().absolute() == right.expanduser().absolute()


def _next_instance_id(base: str, configured: dict[str, Any]) -> str:
    idx = 2
    while f"{base}-{idx}" in configured:
        idx += 1
    return f"{base}-{idx}"
