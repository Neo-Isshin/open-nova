"""Non-RAG asset observations produced by shadow jobs."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .db import connect
from .paths import RuntimePaths
from .settings import default_external_tool_path, external_tool_path


def _directory_metrics(path: Path, *, suffix: str | None = None) -> tuple[int, float]:
    count = size = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file() and (suffix is None or item.name.endswith(suffix)):
                count += 1
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    return count, round(size / 1024 / 1024, 3)


def _tool_home(tool: str, key: str, fallback: Path) -> Path:
    try:
        return external_tool_path(tool, key)
    except Exception:
        return fallback


def _paths_metrics(paths: list[Path], *, suffix: str | None = None) -> tuple[int, float]:
    count = 0
    size_mb = 0.0
    for path in paths:
        current_count, current_size = _directory_metrics(path, suffix=suffix)
        count += current_count
        size_mb += current_size
    return count, round(size_mb, 3)


def _observation_roots(
    *,
    openclaw_root: Path,
    legacy_root: Path,
    tool_homes: dict[str, Path],
    workspace_root: Path,
) -> tuple[tuple[str, str, int, float, dict], ...]:
    agent_memory = [directory / "memory" for directory in (openclaw_root / "agents").iterdir()] if (openclaw_root / "agents").exists() else []
    memory_roots = agent_memory + [openclaw_root / "memory"]
    memory_count, memory_size = _paths_metrics(memory_roots)
    daily_notes = 0
    diary_notes = 0
    global_memory = openclaw_root / "memory"
    if global_memory.exists():
        for item in global_memory.rglob("*.md"):
            if "diary" in item.name.lower():
                diary_notes += 1
            elif re.match(r"\d{4}-\d{2}-\d{2}", item.name):
                daily_notes += 1

    rows: list[tuple[str, str, int, float, dict]] = [
        (
            "memory",
            "openclaw_memory",
            memory_count,
            memory_size,
            {"roots": [str(root) for root in memory_roots], "diaryCount": diary_notes, "dailyNoteCount": daily_notes},
        )
    ]
    storage_roots = {
        "legacy_diary_outputs": list(legacy_root.glob("diary-*")),
        "legacy_daily_archive": [legacy_root / "__diary_daily"],
        "legacy_history_archive": [legacy_root / "_archive"],
    }
    for key, roots in storage_roots.items():
        count, size = _paths_metrics(roots)
        rows.append(("storage", key, count, size, {"roots": [str(root) for root in roots]}))
    for key, root in tool_homes.items():
        count, size = _directory_metrics(root)
        rows.append(("storage", f"tool_home:{key}", count, size, {"root": str(root)}))

    skill_roots = [
        openclaw_root / "skills",
        openclaw_root / "workspace" / "skills",
        _mapped_tool_home(tool_homes, "claude-code", "claudeCode", default_external_tool_path("claudeCode", "home")) / "skills",
        _mapped_tool_home(tool_homes, "codex", "codex", default_external_tool_path("codex", "home")) / "skills",
        _mapped_tool_home(tool_homes, "gemini-cli", "geminiCli", default_external_tool_path("geminiCli", "home")) / "skills",
        _mapped_tool_home(tool_homes, "hermes", "hermes", default_external_tool_path("hermes", "home")) / "skills",
        workspace_root / ".claude" / "skills",
        workspace_root / ".codex" / "skills",
        workspace_root / ".gemini" / "skills",
    ]
    skill_files = {
        str(item)
        for root in skill_roots
        if root.exists()
        for item in tuple(root.rglob("SKILL.md")) + tuple(root.rglob("DESCRIPTION.md"))
    }
    skill_size = round(sum(Path(item).stat().st_size for item in skill_files) / 1024 / 1024, 3)
    rows.append(("skills", "skill_inventory", len(skill_files), skill_size, {"roots": [str(root) for root in skill_roots]}))

    configs = [
        openclaw_root / "openclaw.json",
        _mapped_tool_home(tool_homes, "claude-code", "claudeCode", default_external_tool_path("claudeCode", "home")) / "settings.json",
        _mapped_tool_home(tool_homes, "codex", "codex", default_external_tool_path("codex", "home")) / "config.toml",
        _mapped_tool_home(tool_homes, "gemini-cli", "geminiCli", default_external_tool_path("geminiCli", "home")) / "settings.json",
        _mapped_tool_home(tool_homes, "hermes", "hermes", default_external_tool_path("hermes", "home")) / "config.yaml",
        _tool_home("opencode", "configPath", default_external_tool_path("opencode", "configPath")),
        _tool_home("cursor", "configPath", default_external_tool_path("cursor", "configPath")),
    ]
    present_configs = [item for item in configs if item.exists()]
    rows.append(
        (
            "tool_config",
            "configured_tools",
            len(present_configs),
            round(sum(item.stat().st_size for item in present_configs) / 1024 / 1024, 3),
            {"paths": [str(item) for item in configs], "present": [str(item) for item in present_configs]},
        )
    )
    return tuple(rows)


def observe_non_rag_assets(
    paths: RuntimePaths,
    business_date: date,
    run_id: int,
    *,
    openclaw_agents: Path | None = None,
    legacy_archive: Path | None = None,
    openclaw_root: Path | None = None,
    workspace_root: Path | None = None,
    tool_homes: dict[str, Path] | None = None,
) -> None:
    legacy_root = paths.diary_dir
    if legacy_archive is not None:
        legacy_root = legacy_archive.parent
    oc_root = openclaw_root or _tool_home("openclaw", "home", default_external_tool_path("openclaw", "home"))
    if openclaw_agents is not None:
        oc_root = openclaw_agents.parent
    homes = tool_homes or {
        "openclaw": oc_root,
        "claude-code": _tool_home("claudeCode", "home", default_external_tool_path("claudeCode", "home")),
        "gemini-cli": _tool_home("geminiCli", "home", default_external_tool_path("geminiCli", "home")),
        "codex": _tool_home("codex", "home", default_external_tool_path("codex", "home")),
        "hermes": _tool_home("hermes", "home", default_external_tool_path("hermes", "home")),
        "opencode": _tool_home("opencode", "home", default_external_tool_path("opencode", "home")),
        "antigravity": _tool_home("antigravity", "home", default_external_tool_path("antigravity", "home")),
        "cursor": _tool_home("cursor", "home", default_external_tool_path("cursor", "home")),
    }
    rows = _observation_roots(
        openclaw_root=oc_root,
        legacy_root=legacy_root,
        tool_homes=homes,
        workspace_root=workspace_root or Path(config.WORKSPACE_DIR),
    )
    timestamp = datetime.now().astimezone().isoformat()
    with connect(paths) as connection:
        for asset_type, asset_key, count, size_mb, details in rows:
            connection.execute(
                """
                INSERT INTO asset_observations(
                    observed_at, business_date, asset_type, asset_key, count_value,
                    size_mb, status, details_json, ingestion_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    business_date.isoformat(),
                    asset_type,
                    asset_key,
                    count,
                    size_mb,
                    "observed",
                    json.dumps({**details, "ragExcluded": True}, sort_keys=True),
                    run_id,
                ),
            )


def _mapped_tool_home(tool_homes: dict[str, Path], label: str, settings_tool: str, fallback: Path) -> Path:
    return tool_homes.get(label) or _tool_home(settings_tool, "home", fallback)
