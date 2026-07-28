"""Adapter contracts and registration support."""

from .base import Cursor, NormalizedEvent, SourceArtifact, ToolAdapter
from .registry import RegisteredTool, ToolRegistry
from .usage import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    CronAdapter,
    CursorRuntimeAdapter,
    GeminiCliAdapter,
    HermesAdapter,
    LocalRuntimeAdapter,
    OpenCodeAdapter,
    OpenClawAdapter,
    default_usage_adapters,
)

__all__ = [
    "AntigravityAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CronAdapter",
    "Cursor",
    "CursorRuntimeAdapter",
    "GeminiCliAdapter",
    "HermesAdapter",
    "LocalRuntimeAdapter",
    "NormalizedEvent",
    "OpenCodeAdapter",
    "OpenClawAdapter",
    "RegisteredTool",
    "SourceArtifact",
    "ToolAdapter",
    "ToolRegistry",
    "default_usage_adapters",
]
