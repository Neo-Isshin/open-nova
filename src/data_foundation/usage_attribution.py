"""Shared display policy for Agent / Workspace usage buckets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .workspace_attribution import (
    attribute_workspace_path,
    canonical_workspace_name,
    infer_workspace_from_text,
    source_session_workspace_attribution,
    workspace_display_name,
    workspace_usage_display_allowed,
)

WORKSPACE_USAGE_MIN_TOKENS = 10_000_000

TOOL_EMOJI = {
    "OpenClaw": "🦞",
    "Claude Code": "✳️",
    "Gemini CLI": "✨",
    "Codex": "🤖",
    "Hermes": "⚕️",
    "OpenCode": "🐙",
    "Antigravity": "🛡️",
    "Cursor": "🖱️",
}

CONTAINER_WORKSPACE_NAMES = {
    "default",
    "dev",
    "external",
    "general",
    "home",
    "ssd",
    "unattributed",
    "unknown",
}


@dataclass(frozen=True)
class UsageGroupResolution:
    group: str
    confidence: str
    source: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def resolve_usage_group(
    tool_key: str | None = None,
    tool_name: str | None = None,
    *,
    raw_path: str | None = None,
    cwd: str | None = None,
    initial_cwd: str | None = None,
    metadata: dict[str, Any] | None = None,
    fallback: str | None = None,
) -> UsageGroupResolution:
    meta = metadata if isinstance(metadata, dict) else {}
    normalized_tool = _normalize_tool_key(tool_key or tool_name)
    raw_path = str(raw_path or meta.get("path") or "")
    cwd = str(cwd or meta.get("cwd") or "")
    initial_cwd = str(initial_cwd or meta.get("initial_cwd") or "")

    source_override = source_session_workspace_attribution(raw_path)
    if source_override is not None:
        return UsageGroupResolution(
            canonical_workspace_name(source_override.display_name),
            source_override.confidence,
            source_override.evidence,
            source_override.root_path,
        )

    if normalized_tool == "openclaw":
        agent = _openclaw_agent_from_path(raw_path)
        if agent:
            return UsageGroupResolution(agent, "high", "openclaw-agent-path", raw_path)

    deferred_resolution = None
    for source, candidate in (("cwd", cwd), ("initial-cwd", initial_cwd)):
        resolved = _resolve_workspace_path(candidate, source=source)
        if resolved is not None:
            resolved = _canonical_resolution(resolved)
            if normalized_tool in {"codex", "claude-code"} and (
                resolved.confidence != "high" or _is_container_resolution(resolved)
            ):
                deferred_resolution = deferred_resolution or resolved
                continue
            return resolved

    if normalized_tool == "codex":
        resolved = _resolve_transcript_workspace(raw_path, marker=".codex", source="codex-transcript-path")
        if resolved is not None:
            return resolved

    if normalized_tool == "claude-code":
        resolved = _resolve_transcript_workspace(raw_path, marker=".claude", source="claude-transcript-path")
        if resolved is not None:
            return resolved
        encoded = _path_segment_after(raw_path, ".claude/projects")
        if encoded:
            decoded = _decode_claude_project_segment(encoded)
            resolved = _resolve_workspace_path(str(decoded) if decoded else "", source="claude-project-path")
            if resolved is not None:
                resolved = _canonical_resolution(resolved)
                if resolved.confidence != "high" or _is_container_resolution(resolved):
                    deferred_resolution = deferred_resolution or resolved
                else:
                    return resolved
            if deferred_resolution is not None:
                return deferred_resolution
            if encoded:
                return _canonical_resolution(UsageGroupResolution(_workspace_label(encoded), "medium", "claude-project-segment", encoded))

    if normalized_tool == "gemini-cli":
        resolved = _resolve_transcript_workspace(raw_path, marker=".gemini", source="gemini-transcript-path")
        if resolved is not None:
            return resolved

    if deferred_resolution is not None:
        return deferred_resolution

    if fallback:
        return _canonical_resolution(UsageGroupResolution(str(fallback), "fallback", "fallback", str(fallback)))
    return UsageGroupResolution("", "none", "none", "")


def usage_group_display_allowed(group: str | None, tool_name: str = "") -> bool:
    normalized = str(group or "").strip()
    if not normalized:
        return False
    # Hermes is a single-agent system.  Its stable logical agent intentionally
    # has the same display name as the tool, unlike workspace container rows.
    if normalized.lower() == "hermes" and str(tool_name or "").strip().lower() == "hermes":
        return True
    if normalized == str(tool_name or "").strip():
        return False
    if normalized.lower() in CONTAINER_WORKSPACE_NAMES:
        return False
    if normalized.lower().endswith(" unattributed"):
        return False
    return workspace_usage_display_allowed(normalized)


def _normalize_tool_key(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "claude code": "claude-code",
        "claude-code": "claude-code",
        "gemini cli": "gemini-cli",
        "gemini-cli": "gemini-cli",
        "openclaw": "openclaw",
        "codex": "codex",
        "hermes": "hermes",
        "opencode": "opencode",
        "open code": "opencode",
        "antigravity": "antigravity",
        "cursor": "cursor",
    }
    return aliases.get(normalized, normalized)


def _resolve_workspace_path(raw_path: str, *, source: str) -> UsageGroupResolution | None:
    if not raw_path:
        return None
    attribution = attribute_workspace_path(raw_path)
    if _is_volume_root(raw_path) and (attribution is None or attribution.confidence != "high"):
        return UsageGroupResolution("external", "low", source, raw_path)
    if attribution is not None:
        return UsageGroupResolution(canonical_workspace_name(attribution.display_name), attribution.confidence, source, attribution.evidence)
    return UsageGroupResolution(canonical_workspace_name(workspace_display_name(raw_path)), "low", source, raw_path)


def _is_volume_root(raw_path: str) -> bool:
    """Return whether a path is a macOS volume container, not a project root."""
    parts = Path(raw_path).expanduser().parts
    return len(parts) == 3 and parts[0] == "/" and parts[1] == "Volumes"


def _canonical_resolution(resolution: UsageGroupResolution) -> UsageGroupResolution:
    return UsageGroupResolution(
        canonical_workspace_name(resolution.group),
        resolution.confidence,
        resolution.source,
        resolution.evidence,
    )


def _is_container_resolution(resolution: UsageGroupResolution) -> bool:
    return str(resolution.group or "").strip().lower() in CONTAINER_WORKSPACE_NAMES


def _resolve_transcript_workspace(raw_path: str, *, marker: str, source: str) -> UsageGroupResolution | None:
    if not raw_path:
        return None
    attribution = _transcript_workspace_tuple(raw_path, marker)
    if attribution is None:
        return None
    return UsageGroupResolution(canonical_workspace_name(attribution[0]), "high", source, attribution[1])


@lru_cache(maxsize=8192)
def _transcript_workspace_tuple(raw_path: str, marker: str) -> tuple[str, str] | None:
    path = Path(raw_path).expanduser()
    if not path.is_file() or marker not in path.parts:
        return None
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            if size <= 8_000_000:
                text = handle.read()
            else:
                head = handle.read(4_000_000)
                handle.seek(max(0, size - 2_000_000))
                text = head + "\n" + handle.read(2_000_000)
    except OSError:
        return None
    attribution = infer_workspace_from_text(text)
    if attribution is None or attribution.confidence != "high":
        return None
    return (canonical_workspace_name(attribution.display_name), attribution.root_path)


def _openclaw_agent_from_path(raw_path: str) -> str:
    parts = Path(raw_path).parts
    for index, part in enumerate(parts):
        if part == "agents" and index + 1 < len(parts):
            agent = parts[index + 1]
            if agent and agent not in {"sessions", "workspace"}:
                return agent
    return ""


def _path_segment_after(raw_path: str, marker: str) -> str:
    marker_parts = tuple(part for part in marker.split("/") if part)
    parts = Path(raw_path).parts
    width = len(marker_parts)
    for index in range(0, len(parts) - width):
        if tuple(parts[index : index + width]) == marker_parts and index + width < len(parts):
            return parts[index + width]
    return ""


def _decode_claude_project_segment(encoded_name: str) -> Path | None:
    if not encoded_name.startswith("-"):
        return None
    candidate = Path("/" + encoded_name.lstrip("-").replace("-", "/"))
    return candidate if candidate.is_absolute() else None


def _workspace_label(encoded_name: str) -> str:
    parts = [part for part in encoded_name.split("-") if part]
    return parts[-1] if parts else encoded_name
