"""Fail-closed Tailscale status and Dashboard Serve controls.

This module never installs Tailscale, authenticates a tailnet, or configures
Funnel.  The only mutation it can issue is a fixed HTTPS Serve mapping from
the tailnet to the loopback-only Dashboard.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


SCHEMA_VERSION = 1
DEFAULT_DASHBOARD_PORT = 3036
SERVE_HTTPS_PORT = 443
ENABLE_CONFIRMATION = "ENABLE ACTANARA TAILNET SERVE"
DISABLE_CONFIRMATION = "DISABLE ACTANARA TAILNET SERVE"
COMMAND_TIMEOUT_SECONDS = 8

Runner = Callable[..., subprocess.CompletedProcess[str]]


class TailscaleError(RuntimeError):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TailscalePolicyError(TailscaleError):
    """The requested change would cross an Actanara security boundary."""


class TailscaleCommandError(TailscaleError):
    """A fixed argv Tailscale command failed."""


def tailscale_status(
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return bounded status without changing Tailscale state."""

    command_runner = runner or subprocess.run
    binary = (which or shutil.which)("tailscale")
    dashboard_target = _dashboard_target()
    result = _empty_status(installed=bool(binary), binary=binary, dashboard_target=dashboard_target)
    if not binary:
        result["errors"].append(_error("tailscale-not-installed", "Tailscale CLI is not installed."))
        return result

    try:
        raw_status = _run_json(command_runner, [binary, "status", "--json"])
    except TailscaleCommandError as exc:
        if _looks_logged_out(exc):
            result["loginState"] = "logged-out"
            result["errors"].append(_error("tailscale-not-logged-in", "Tailscale is installed but not logged in."))
        else:
            result["loginState"] = "error"
            result["errors"].append(_error(exc.code, str(exc)))
        return result

    backend_state = str(raw_status.get("BackendState") or raw_status.get("backendState") or "").strip()
    result["backendState"] = backend_state or "unknown"
    result["loginState"] = _login_state(backend_state, raw_status)
    result["loggedIn"] = result["loginState"] in {"connected", "disconnected"}
    result["connected"] = result["loginState"] == "connected"

    ipv4, ipv6 = _status_ips(raw_status)
    if result["loggedIn"]:
        if not ipv4:
            ipv4 = _run_ip(command_runner, binary, "-4", result["errors"])
        if not ipv6:
            ipv6 = _run_ip(command_runner, binary, "-6", result["errors"])
    result["ips"] = {"ipv4": ipv4, "ipv6": ipv6}

    dns_name, magic_dns_suffix = _magic_dns(raw_status)
    result["dns"] = {
        "magicDnsEnabled": bool(dns_name and magic_dns_suffix),
        "name": dns_name,
        "suffix": magic_dns_suffix,
        "origin": f"https://{dns_name}" if dns_name else None,
    }

    self_status = raw_status.get("Self") if isinstance(raw_status.get("Self"), dict) else {}
    self_online = self_status.get("Online")
    result["reachable"] = bool(result["connected"] and (self_online is not False) and (ipv4 or ipv6))
    result["reachability"] = {
        "nodeReachable": result["reachable"],
        "basis": "tailscale-status-self-online-and-ip",
        "httpServeProbed": False,
    }

    if result["connected"]:
        try:
            raw_serve = _run_json(command_runner, [binary, "serve", "status", "--json"])
            result["serve"] = _serve_status(raw_serve, dashboard_target=dashboard_target)
        except TailscaleCommandError as exc:
            result["serve"]["supported"] = False
            result["serve"]["statusError"] = exc.code
            result["errors"].append(_error(exc.code, str(exc)))

    result["canEnableServe"] = bool(
        result["connected"]
        and result["serve"]["supported"]
        and not result["serve"]["enabled"]
    )
    result["canDisableServe"] = bool(result["serve"]["exclusiveManaged"])
    return result


def set_dashboard_serve(
    enabled: bool,
    payload: dict[str, Any] | None = None,
    *,
    observed_status: dict[str, Any] | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Apply only Actanara's fixed, tailnet-only Dashboard Serve mapping."""

    supplied = str((payload or {}).get("confirmationText") or "")
    required = ENABLE_CONFIRMATION if enabled else DISABLE_CONFIRMATION
    if supplied != required:
        raise TailscalePolicyError("confirmation-mismatch", "The Tailscale Serve confirmation text did not match.")

    command_runner = runner or subprocess.run
    binary = (which or shutil.which)("tailscale")
    dashboard_target = _dashboard_target()
    if not binary:
        raise TailscalePolicyError("tailscale-not-installed", "Tailscale CLI is not installed; install it manually first.")
    current = observed_status or tailscale_status(runner=command_runner, which=lambda _name: binary)
    if not current.get("connected"):
        raise TailscalePolicyError("tailscale-not-connected", "Tailscale must be logged in and connected before Serve can be changed.")

    serve = current.get("serve") if isinstance(current.get("serve"), dict) else {}
    if not serve.get("supported", True):
        raise TailscalePolicyError("tailscale-serve-unavailable", "This Tailscale CLI does not provide Serve status safely.")

    if enabled:
        if serve.get("exclusiveManaged"):
            return _action_result(
                enabled=True,
                changed=False,
                command=None,
                dashboard_target=dashboard_target,
            )
        if serve.get("enabled"):
            raise TailscalePolicyError(
                "tailscale-serve-conflict",
                "Existing Tailscale Serve configuration is not owned exclusively by Actanara; it was preserved.",
            )
        argv = [binary, "serve", "--yes", "--bg", f"--https={SERVE_HTTPS_PORT}", dashboard_target]
    else:
        if not serve.get("enabled"):
            return _action_result(
                enabled=False,
                changed=False,
                command=None,
                dashboard_target=dashboard_target,
            )
        if not serve.get("exclusiveManaged"):
            raise TailscalePolicyError(
                "tailscale-serve-not-owned",
                "Actanara will not remove Tailscale Serve configuration that it cannot identify as its exclusive mapping.",
            )
        argv = [binary, "serve", f"--https={SERVE_HTTPS_PORT}", "off"]

    _run(command_runner, argv)
    return _action_result(
        enabled=enabled,
        changed=True,
        command=argv[1:],
        dashboard_target=dashboard_target,
    )


def dashboard_access_status(status: dict[str, Any], allowed_origins: Iterable[str]) -> dict[str, Any]:
    """Describe whether the detected MagicDNS origin passes Dashboard policy."""

    origin = str(((status.get("dns") or {}).get("origin") or "")).rstrip("/")
    normalized = {str(item).rstrip("/") for item in allowed_origins if str(item or "").strip()}
    allowed = bool(origin and origin in normalized)
    return {
        "origin": origin or None,
        "originAllowed": allowed,
        "ready": bool(status.get("connected") and origin and allowed),
        "requiredBeforeEnable": True,
    }


def _empty_status(
    *,
    installed: bool,
    binary: str | None,
    dashboard_target: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "tailnet-only",
        "installed": installed,
        "binaryPath": binary,
        "dashboardTarget": dashboard_target,
        "loginState": "not-installed" if not installed else "unknown",
        "loggedIn": False,
        "connected": False,
        "backendState": "not-installed" if not installed else "unknown",
        "reachable": False,
        "reachability": {
            "nodeReachable": False,
            "basis": "tailscale-status-self-online-and-ip",
            "httpServeProbed": False,
        },
        "ips": {"ipv4": None, "ipv6": None},
        "dns": {"magicDnsEnabled": False, "name": None, "suffix": None, "origin": None},
        "serve": {
            "scope": "tailnet-only",
            "supported": installed,
            "enabled": False,
            "managed": False,
            "exclusiveManaged": False,
            "target": None,
            "listeners": [],
            "conflict": False,
            "exposesNovaRag": False,
        },
        "funnel": {
            "scope": "public-internet",
            "available": False,
            "enabled": False,
            "risk": "high",
            "reason": "disabled-by-policy",
        },
        "canEnableServe": False,
        "canDisableServe": False,
        "errors": [],
    }


def _run(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(
            argv,
            text=True,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TailscaleCommandError("tailscale-command-timeout", "Tailscale command timed out.") from exc
    except OSError as exc:
        raise TailscaleCommandError("tailscale-command-unavailable", f"Tailscale command could not run: {_safe_error(exc)}") from exc
    if completed.returncode != 0:
        detail = _safe_error(completed.stderr or completed.stdout or "unknown error")
        raise TailscaleCommandError("tailscale-command-failed", f"Tailscale command failed: {detail}")
    return completed


def _run_json(runner: Runner, argv: list[str]) -> dict[str, Any]:
    completed = _run(runner, argv)
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TailscaleCommandError("tailscale-invalid-json", "Tailscale returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise TailscaleCommandError("tailscale-invalid-json", "Tailscale returned an unexpected JSON document.")
    return value


def _run_ip(runner: Runner, binary: str, family: str, errors: list[dict[str, str]]) -> str | None:
    try:
        value = _run(runner, [binary, "ip", family]).stdout.strip().splitlines()
        return value[0].strip() if value else None
    except TailscaleCommandError as exc:
        errors.append(_error(exc.code, str(exc)))
        return None


def _login_state(backend_state: str, status: dict[str, Any]) -> str:
    normalized = backend_state.lower()
    if normalized == "running":
        return "connected"
    if normalized in {"needslogin", "nologgedinuser", "loginrequired"} or status.get("AuthURL"):
        return "logged-out"
    if normalized in {"stopped", "starting", "needsmachineauth"}:
        return "disconnected"
    return "unknown"


def _status_ips(status: dict[str, Any]) -> tuple[str | None, str | None]:
    self_status = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    raw = status.get("TailscaleIPs") or self_status.get("TailscaleIPs") or []
    if isinstance(raw, str):
        raw = [raw]
    ipv4 = next((str(item) for item in raw if isinstance(item, str) and ":" not in item), None)
    ipv6 = next((str(item) for item in raw if isinstance(item, str) and ":" in item), None)
    return ipv4, ipv6


def _magic_dns(status: dict[str, Any]) -> tuple[str | None, str | None]:
    self_status = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    tailnet = status.get("CurrentTailnet") if isinstance(status.get("CurrentTailnet"), dict) else {}
    dns_name = str(self_status.get("DNSName") or status.get("DNSName") or "").strip().rstrip(".") or None
    suffix = str(
        tailnet.get("MagicDNSSuffix")
        or status.get("MagicDNSSuffix")
        or (dns_name.split(".", 1)[1] if dns_name and "." in dns_name else "")
    ).strip().rstrip(".") or None
    return dns_name, suffix


def _serve_status(raw: dict[str, Any], *, dashboard_target: str) -> dict[str, Any]:
    endpoints: list[dict[str, str]] = []
    web = raw.get("Web") if isinstance(raw.get("Web"), dict) else {}
    for listener, config in web.items():
        handlers = config.get("Handlers") if isinstance(config, dict) and isinstance(config.get("Handlers"), dict) else {}
        for path, handler in handlers.items():
            if not isinstance(handler, dict):
                continue
            target = handler.get("Proxy") or handler.get("proxy")
            if isinstance(target, str):
                endpoints.append({"listener": str(listener), "path": str(path), "target": target.rstrip("/")})

    if not endpoints:
        endpoints.extend(_generic_proxy_endpoints(raw))
    managed = [item for item in endpoints if _same_target(item.get("target"), dashboard_target)]
    exclusive = len(endpoints) == 1 and len(managed) == 1 and _is_https_443_listener(managed[0].get("listener"))
    exposes_rag = any(_target_port(item.get("target")) == 3037 for item in endpoints)
    enabled = bool(endpoints or _has_unparsed_serve_configuration(raw))
    return {
        "scope": "tailnet-only",
        "supported": True,
        "enabled": enabled,
        "managed": bool(managed),
        "exclusiveManaged": exclusive,
        "target": managed[0]["target"] if managed else None,
        "listeners": endpoints,
        "conflict": bool(enabled and not exclusive),
        "exposesNovaRag": exposes_rag,
    }


def _generic_proxy_endpoints(value: Any, trail: tuple[str, ...] = ()) -> list[dict[str, str]]:
    endpoints: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_trail = (*trail, str(key))
            if str(key).lower() in {"proxy", "target", "backend"} and isinstance(item, str) and item.startswith(("http://", "https://")):
                endpoints.append({"listener": "/".join(trail) or "unknown", "path": "/", "target": item.rstrip("/")})
            else:
                endpoints.extend(_generic_proxy_endpoints(item, next_trail))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            endpoints.extend(_generic_proxy_endpoints(item, (*trail, str(index))))
    return endpoints


def _has_unparsed_serve_configuration(raw: dict[str, Any]) -> bool:
    web = raw.get("Web") if isinstance(raw.get("Web"), dict) else {}
    tcp = raw.get("TCP") if isinstance(raw.get("TCP"), dict) else {}
    if tcp:
        return True
    for config in web.values():
        if not isinstance(config, dict):
            if config:
                return True
            continue
        handlers = config.get("Handlers")
        if isinstance(handlers, dict) and handlers:
            return True
        if any(value for key, value in config.items() if key != "Handlers"):
            return True
    ignored = {"Web", "TCP", "AllowFunnel", "Version"}
    return any(value for key, value in raw.items() if key not in ignored)


def _is_https_443_listener(listener: str | None) -> bool:
    text = str(listener or "").lower()
    return text == "443" or text.endswith(":443") or "https=443" in text or "https/443" in text


def _same_target(left: str | None, right: str | None) -> bool:
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")


def _target_port(value: str | None) -> int | None:
    try:
        parsed = urlparse(str(value or ""))
        return parsed.port
    except ValueError:
        return None


def _action_result(
    *,
    enabled: bool,
    changed: bool,
    command: list[str] | None,
    dashboard_target: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "tailnet-only",
        "ok": True,
        "changed": changed,
        "serveEnabled": enabled,
        "target": dashboard_target or _dashboard_target(),
        "command": command,
        "funnel": {"available": False, "enabled": False, "reason": "disabled-by-policy"},
    }


def _configured_dashboard_port() -> int:
    from data_foundation.settings import resolve_dashboard_settings

    try:
        value = resolve_dashboard_settings().get("port")
    except Exception:
        return DEFAULT_DASHBOARD_PORT
    if type(value) is not int or not 1 <= value <= 65535:
        return DEFAULT_DASHBOARD_PORT
    return value


def _dashboard_target() -> str:
    return f"http://127.0.0.1:{_configured_dashboard_port()}"


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _safe_error(value: Any) -> str:
    return " ".join(str(value).strip().split())[:300]


def _looks_logged_out(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("logged out", "not logged in", "needs login", "login required"))
