"""Actanara business day handling with settings-backed timezone resolution."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
DEFAULT_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_BUSINESS_DAY_START_HOUR = 4
SCHEDULER_TIMEZONE_MISMATCH_ISSUE_CODE = "scheduler-timezone-mismatch"
SCHEDULER_SYSTEM_TIMEZONE_UNKNOWN_ISSUE_CODE = "scheduler-system-timezone-unknown"
_SYSTEM_ZONEINFO_DIRECTORY_NAMES = frozenset({"zoneinfo", "zoneinfo.default"})


def _valid_timezone_name(value: object) -> str | None:
    name = str(value or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return None


def _timezone_name_from_zoneinfo_path(path: Path) -> str | None:
    """Extract a validated IANA name from a resolved system zoneinfo path."""
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] not in _SYSTEM_ZONEINFO_DIRECTORY_NAMES:
            continue
        if index + 1 >= len(parts):
            return None
        return _valid_timezone_name("/".join(parts[index + 1 :]))
    return None


def detect_system_timezone(default: str = DEFAULT_TIMEZONE) -> str:
    """Best-effort local IANA timezone detection for new runtime defaults."""
    env_tz = _valid_timezone_name(os.getenv("TZ"))
    if env_tz:
        return env_tz
    try:
        localtime = Path("/etc/localtime")
        target = localtime.resolve()
        detected = _timezone_name_from_zoneinfo_path(target)
        if detected:
            return detected
    except Exception:
        pass
    return _valid_timezone_name(default) or DEFAULT_TIMEZONE


def detect_system_timezone_authority() -> str | None:
    """Read the host timezone authority without honoring process TZ overrides."""
    try:
        target = Path("/etc/localtime").resolve(strict=True)
        return _timezone_name_from_zoneinfo_path(target)
    except OSError:
        return None


def resolve_timezone_name(
    paths: Any | None = None,
    *,
    settings: dict[str, Any] | None = None,
    group: str = "general",
    default: str = DEFAULT_TIMEZONE,
) -> str:
    """Resolve the configured local timezone without mutating settings."""
    candidates: list[Any] = [os.getenv("TARGET_TIMEZONE")]
    if settings is not None:
        selected = settings.get(group) if isinstance(settings.get(group), dict) else {}
        general = settings.get("general") if isinstance(settings.get("general"), dict) else {}
        schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
        candidates.extend([selected.get("timezone"), general.get("timezone"), schedule.get("timezone")])
    elif group == "schedule":
        try:
            from .settings import read_settings

            runtime_settings = (
                read_settings(paths, persist_defaults=False)
                if paths is not None
                else read_settings(persist_defaults=False)
            )
            schedule = runtime_settings.get("schedule") if isinstance(runtime_settings.get("schedule"), dict) else {}
            general = runtime_settings.get("general") if isinstance(runtime_settings.get("general"), dict) else {}
            candidates.extend([schedule.get("timezone"), general.get("timezone")])
        except Exception:
            pass
    else:
        try:
            from .settings import resolve_general_settings

            candidates.append(resolve_general_settings(paths).get("timezone"))
        except Exception:
            pass
    candidates.append(default)
    for value in candidates:
        name = _valid_timezone_name(value)
        if name:
            return name
    return DEFAULT_TIMEZONE


def resolve_timezone(
    paths: Any | None = None,
    *,
    settings: dict[str, Any] | None = None,
    group: str = "general",
) -> ZoneInfo:
    return ZoneInfo(resolve_timezone_name(paths, settings=settings, group=group))


def business_now(paths: Any | None = None, *, group: str = "general") -> datetime:
    return datetime.now(resolve_timezone(paths, group=group))


def parse_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1_000_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OSError):
        return None


def business_date_for(
    occurred_at: datetime,
    *,
    paths: Any | None = None,
    tz: ZoneInfo | None = None,
    start_hour: int = DEFAULT_BUSINESS_DAY_START_HOUR,
) -> date:
    local = occurred_at.astimezone(tz or resolve_timezone(paths))
    return (local - timedelta(hours=start_hour)).date()


def business_today(paths: Any | None = None, *, group: str = "general") -> date:
    return business_date_for(business_now(paths, group=group), paths=paths)


def business_window(
    target: date,
    *,
    paths: Any | None = None,
    tz: ZoneInfo | None = None,
    start_hour: int = DEFAULT_BUSINESS_DAY_START_HOUR,
) -> tuple[datetime, datetime]:
    start = datetime.combine(target, time(hour=start_hour), tzinfo=tz or resolve_timezone(paths))
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
