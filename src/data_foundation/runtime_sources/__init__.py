"""Shared local-runtime parsing contracts.

The parsers in this package are deliberately read-only.  They normalize local
runtime state for Foundation, the diary collector, and Dashboard projections
without reading credential stores or mutating the source runtimes.
"""

from .base import (
    DialogueRecord,
    SessionRecord,
    UsageRecord,
    connect_sqlite_read_only,
)
from .antigravity import AntigravityRuntime
from .cursor import CursorRuntime
from .opencode import OpenCodeRuntime

__all__ = [
    "AntigravityRuntime",
    "CursorRuntime",
    "DialogueRecord",
    "OpenCodeRuntime",
    "SessionRecord",
    "UsageRecord",
    "connect_sqlite_read_only",
]
