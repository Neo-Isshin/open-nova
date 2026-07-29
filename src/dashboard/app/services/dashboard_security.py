"""Dashboard browser security helpers for local-session auth and CSRF."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from data_foundation.paths import load_paths
from data_foundation.settings import resolve_dashboard_settings


DASHBOARD_SESSION_COOKIE = "actanara_dashboard_session"
DASHBOARD_CSRF_COOKIE = "actanara_dashboard_csrf"
DASHBOARD_CSRF_HEADER = "x-actanara-csrf"
DASHBOARD_SESSION_TTL_SECONDS = 12 * 60 * 60
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_PROTECTED_PREFIXES = ("/api", "/events")
_SESSION_EXEMPT_PREFIXES = ("/api/rag/external", "/api/memory/external")
_BOOTSTRAP_PATHS = {"/", "/dashboard", "/tasks"}
_BOOTSTRAP_PREFIXES = ("/static", "/diary-data")


@dataclass
class DashboardSession:
    csrf_token: str
    expires_at: float


class DashboardSessionStore:
    def __init__(self, ttl_seconds: int = DASHBOARD_SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, DashboardSession] = {}

    def create(self) -> tuple[str, str]:
        self._purge_expired()
        session_id = secrets.token_urlsafe(32)
        csrf_value = secrets.token_urlsafe(32)
        self._sessions[session_id] = DashboardSession(
            csrf_token=csrf_value,
            expires_at=time.time() + self.ttl_seconds,
        )
        return session_id, csrf_value

    def validate(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        session = self._sessions.get(str(session_id))
        if session is None:
            return False
        if session.expires_at < time.time():
            self._sessions.pop(str(session_id), None)
            return False
        return True

    def csrf_value(self, session_id: str | None) -> str | None:
        if not self.validate(session_id):
            return None
        return self._sessions[str(session_id)].csrf_token

    def validate_csrf(
        self,
        session_id: str | None,
        header_value: str | None,
        cookie_value: str | None,
    ) -> bool:
        expected = self.csrf_value(session_id)
        if not expected or not header_value or not cookie_value:
            return False
        return secrets.compare_digest(expected, str(header_value)) and secrets.compare_digest(expected, str(cookie_value))

    def _purge_expired(self) -> None:
        now = time.time()
        for session_id, session in list(self._sessions.items()):
            if session.expires_at < now:
                self._sessions.pop(session_id, None)


def dashboard_security_config(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = settings or _load_dashboard_settings()
    port = _positive_int(resolved.get("port"), 3036)
    public_base_url = str(resolved.get("publicBaseUrl") or f"http://127.0.0.1:{port}").strip().rstrip("/")
    configured_origins = [
        str(origin).strip()
        for origin in (resolved.get("allowedOrigins") or [])
        if str(origin or "").strip()
    ]
    origins: list[str] = []
    for origin in [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
        _origin_from_url(public_base_url),
        *configured_origins,
    ]:
        normalized = normalize_origin(origin)
        if normalized and normalized not in origins:
            origins.append(normalized)
    hostnames = {_hostname_from_origin(origin) for origin in origins}
    hostnames = {host for host in hostnames if host}
    host = str(resolved.get("host") or "").strip()
    if _is_loopback_hostname(host):
        hostnames.add(host.lower())
    return {
        "allowedOrigins": origins,
        "allowedHosts": sorted(hostnames | {"127.0.0.1", "localhost", "::1"}),
        "publicBaseUrl": public_base_url,
    }


def normalize_origin(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    default_port = 443 if parsed.scheme == "https" else 80
    port_part = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{port_part}"


def is_origin_allowed(origin: str | None, settings: dict[str, Any] | None = None) -> bool:
    if not origin:
        return True
    normalized = normalize_origin(origin)
    if not normalized:
        return False
    return normalized in set(dashboard_security_config(settings)["allowedOrigins"])


def is_host_allowed(host_header: str | None, settings: dict[str, Any] | None = None) -> bool:
    hostname = _hostname_from_host_header(host_header)
    if not hostname:
        return False
    if _is_loopback_hostname(hostname):
        return True
    allowed_hosts = {str(host).lower() for host in dashboard_security_config(settings)["allowedHosts"]}
    return hostname.lower() in allowed_hosts


def is_protected_path(path: str) -> bool:
    clean = str(path or "")
    return clean.startswith(_PROTECTED_PREFIXES)


def is_session_exempt_path(path: str) -> bool:
    clean = str(path or "")
    return any(
        clean == prefix or clean.startswith(prefix + "/")
        for prefix in _SESSION_EXEMPT_PREFIXES
    )


def is_loopback_external_request(
    client_host: str | None,
    host_header: str | None,
    forwarded_client: str | None = None,
) -> bool:
    """Require both the network peer and requested Host to be loopback.

    The peer check blocks direct remote calls.  The Host check also prevents a
    public reverse proxy on the same machine from turning the anonymous local
    facade into a remote endpoint.
    """
    if str(forwarded_client or "").strip():
        return False
    return _is_loopback_hostname(client_host) and _is_loopback_hostname(
        _hostname_from_host_header(host_header)
    )


def should_bootstrap_session(path: str, method: str) -> bool:
    if str(method or "").upper() not in SAFE_METHODS:
        return False
    clean = str(path or "")
    return clean in _BOOTSTRAP_PATHS or clean.startswith(_BOOTSTRAP_PREFIXES)


def set_dashboard_session_cookies(response: Any, session_id: str, csrf_value: str, *, secure: bool = False) -> None:
    common = {
        "max_age": DASHBOARD_SESSION_TTL_SECONDS,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
    }
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        session_id,
        httponly=True,
        **common,
    )
    response.set_cookie(
        DASHBOARD_CSRF_COOKIE,
        csrf_value,
        httponly=False,
        **common,
    )
    response.headers["X-Actanara-CSRF"] = csrf_value


def request_uses_secure_cookie(headers: dict[str, str] | Any, scheme: str = "http") -> bool:
    forwarded_proto = ""
    try:
        forwarded_proto = str(headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    except Exception:
        forwarded_proto = ""
    return str(scheme or "").lower() == "https" or forwarded_proto == "https"


def _load_dashboard_settings() -> dict[str, Any]:
    try:
        return resolve_dashboard_settings(load_paths())
    except Exception:
        return {}


def _origin_from_url(value: str) -> str | None:
    return normalize_origin(value)


def _hostname_from_origin(origin: str) -> str | None:
    parsed = urlparse(origin)
    return parsed.hostname.lower() if parsed.hostname else None


def _hostname_from_host_header(host_header: str | None) -> str | None:
    text = str(host_header or "").strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        return text[1:end].lower() if end > 0 else None
    return text.rsplit(":", 1)[0].lower()


def _is_loopback_hostname(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default
