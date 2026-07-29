"""Read-only external-agent memory search helpers."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .cli_output import friendly_name, render_cli


DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3036"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 65.0
SKILL_TOTAL_BUDGET_SECONDS = 90.0
SKILL_MAX_SEARCH_CALLS = 3


class ExternalSearchBudget:
    """Monotonic, process-local budget shared by an agent's recall calls."""

    def __init__(
        self,
        *,
        total_seconds: float = SKILL_TOTAL_BUDGET_SECONDS,
        max_calls: int = SKILL_MAX_SEARCH_CALLS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total_seconds = max(float(total_seconds), 0.001)
        self.max_calls = max(1, min(int(max_calls), SKILL_MAX_SEARCH_CALLS))
        self._clock = clock
        self._started = clock()
        self._calls_used = 0
        self._lock = threading.Lock()

    def reserve_call(self) -> dict[str, Any] | None:
        with self._lock:
            remaining = self.total_seconds - (self._clock() - self._started)
            if self._calls_used >= self.max_calls or remaining <= 0:
                return None
            self._calls_used += 1
            return {
                "call": self._calls_used,
                "maxCalls": self.max_calls,
                "totalBudgetMs": int(round(self.total_seconds * 1000)),
                "remainingBudgetMs": max(1, int(remaining * 1000)),
            }

    def telemetry(self) -> dict[str, Any]:
        with self._lock:
            remaining = max(0.0, self.total_seconds - (self._clock() - self._started))
            return {
                "callsUsed": self._calls_used,
                "maxCalls": self.max_calls,
                "totalBudgetMs": int(round(self.total_seconds * 1000)),
                "remainingBudgetMs": int(remaining * 1000),
                "exhausted": self._calls_used >= self.max_calls or remaining <= 0,
            }


def search_memory(
    query: str,
    *,
    top_k: int = 5,
    dashboard_url: str | None = None,
    timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    filters: dict[str, Any] | None = None,
    budget: ExternalSearchBudget | None = None,
    mode: str = "rag",
    caller: str | None = None,
    paths: Any | None = None,
    budget_call: int | None = None,
    budget_max_calls: int | None = None,
) -> dict[str, Any]:
    """Search memory through the selected backend.

    ``auto`` prefers an enabled nova-RAG service and falls back to the local
    lexical index when that service is disabled or unavailable. ``rag`` is a
    strict compatibility path and never falls back. ``local`` bypasses the
    Dashboard and RAG service entirely.
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query is required")
    selected_mode = str(mode or "rag").strip().lower()
    if selected_mode not in {"auto", "rag", "local"}:
        raise ValueError("mode must be one of: auto, rag, local")
    bounded_top_k = max(1, min(int(top_k or 5), 20))

    if selected_mode == "local":
        return _search_local_memory(
            normalized_query,
            top_k=bounded_top_k,
            filters=filters,
            caller=caller,
            paths=paths,
        )

    if selected_mode == "rag":
        return _annotate_rag_response(
            _search_rag_memory(
                normalized_query,
                top_k=bounded_top_k,
                dashboard_url=dashboard_url,
                timeout_seconds=timeout_seconds,
                filters=filters,
                budget=budget,
                caller=caller,
                budget_call=budget_call,
                budget_max_calls=budget_max_calls,
            )
        )

    memory_enabled, backend_policy = _memory_backend_policy(paths)
    if not memory_enabled:
        return _search_local_memory(
            normalized_query,
            top_k=bounded_top_k,
            filters=filters,
            caller=caller,
            paths=paths,
        )
    if backend_policy == "local":
        return _search_local_memory(
            normalized_query,
            top_k=bounded_top_k,
            filters=filters,
            caller=caller,
            paths=paths,
        )
    if backend_policy == "rag":
        return _annotate_rag_response(
            _search_rag_memory(
                normalized_query,
                top_k=bounded_top_k,
                dashboard_url=dashboard_url,
                timeout_seconds=timeout_seconds,
                filters=filters,
                budget=budget,
                caller=caller,
                budget_call=budget_call,
                budget_max_calls=budget_max_calls,
            )
        )

    rag_enabled, disabled_reason = _rag_enabled(paths)
    if rag_enabled:
        rag_result = _annotate_rag_response(
            _search_rag_memory(
                normalized_query,
                top_k=bounded_top_k,
                dashboard_url=dashboard_url,
                timeout_seconds=timeout_seconds,
                filters=filters,
                budget=budget,
                caller=caller,
                budget_call=budget_call,
                budget_max_calls=budget_max_calls,
            )
        )
        if rag_result.get("available", True):
            return rag_result
        fallback_from = str(rag_result.get("reason") or "rag-external-unavailable")
    else:
        fallback_from = disabled_reason or "nova-rag-disabled"

    local_result = _search_local_memory(
        normalized_query,
        top_k=bounded_top_k,
        filters=filters,
        caller=caller,
        paths=paths,
    )
    return _annotate_fallback(local_result, fallback_from=fallback_from)


def _search_rag_memory(
    query: str,
    *,
    top_k: int = 5,
    dashboard_url: str | None = None,
    timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    filters: dict[str, Any] | None = None,
    budget: ExternalSearchBudget | None = None,
    caller: str | None = None,
    budget_call: int | None = None,
    budget_max_calls: int | None = None,
) -> dict[str, Any]:
    """Search nova-RAG memory through the external read-only facade."""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query is required")
    shared_budget = budget or ExternalSearchBudget()
    reservation = shared_budget.reserve_call()
    if reservation is None:
        result = normalize_memory_response(
            {
                "available": False,
                "reason": "rag-external-budget-exhausted",
                "error": "search budget exhausted",
                "results": [],
            },
            query=normalized_query,
            top_k=max(1, min(int(top_k or 5), 20)),
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    payload: dict[str, Any] = {
        "query": normalized_query,
        "topK": max(1, min(int(top_k or 5), 20)),
        "remainingBudgetMs": reservation["remainingBudgetMs"],
        "budgetCall": budget_call if budget_call is not None else reservation["call"],
        "budgetMaxCalls": (
            budget_max_calls
            if budget_max_calls is not None
            else reservation["maxCalls"]
        ),
    }
    if filters:
        payload.update({key: value for key, value in filters.items() if value not in (None, "", [])})
    if str(caller or "").strip():
        payload["caller"] = str(caller).strip()
    base_url = str(dashboard_url or _active_runtime_dashboard_url()).rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/api/rag/external/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        call_timeout = max(
            0.001,
            min(float(timeout_seconds), float(reservation["remainingBudgetMs"]) / 1000.0),
        )
        with urllib.request.urlopen(request, timeout=call_timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        result = normalize_memory_response(
            {
                "available": False,
                "reason": f"rag-external-http-{exc.code}",
                "error": detail,
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    except UnicodeError as exc:
        result = normalize_memory_response(
            {
                "available": False,
                "reason": "rag-external-invalid-encoding",
                "error": str(exc),
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    except (TimeoutError, urllib.error.URLError) as exc:
        detail = getattr(exc, "reason", exc)
        result = normalize_memory_response(
            {
                "available": False,
                "reason": f"rag-external-unavailable:{exc.__class__.__name__}",
                "error": str(detail),
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    try:
        parsed = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        result = normalize_memory_response(
            {
                "available": False,
                "reason": "rag-external-invalid-json",
                "error": str(exc),
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    if not isinstance(parsed, dict):
        result = normalize_memory_response(
            {
                "available": False,
                "reason": "rag-external-invalid-schema",
                "error": "RAG external search returned a non-object JSON payload",
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    if type(parsed.get("available")) is not bool or not isinstance(parsed.get("results"), list):
        result = normalize_memory_response(
            {
                "available": False,
                "reason": "rag-external-invalid-schema",
                "error": "RAG external search returned invalid available/results fields",
                "results": [],
            },
            query=normalized_query,
            top_k=payload["topK"],
        )
        result["budgetTelemetry"] = shared_budget.telemetry()
        return result
    result = normalize_memory_response(parsed, query=normalized_query, top_k=payload["topK"])
    result["budgetTelemetry"] = shared_budget.telemetry()
    return result


def _rag_enabled(paths: Any | None) -> tuple[bool, str | None]:
    try:
        from agentic_rag.rag_settings import rag_product_disabled_reason, resolve_rag_settings

        settings = resolve_rag_settings(paths)
        reason = rag_product_disabled_reason(settings)
        return reason is None, reason
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, f"nova-rag-settings-unavailable:{exc.__class__.__name__}"


def _memory_backend_policy(paths: Any | None) -> tuple[bool, str]:
    try:
        from .settings import resolve_memory_search_settings

        settings = resolve_memory_search_settings(paths)
        policy = str(settings.get("backendPolicy") or "auto")
        return settings.get("enabled") is True, policy if policy in {"auto", "rag", "local"} else "auto"
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return True, "auto"


def _search_local_memory(
    query: str,
    *,
    top_k: int,
    filters: dict[str, Any] | None,
    caller: str | None,
    paths: Any | None,
) -> dict[str, Any]:
    try:
        from .local_memory_search import search_local_memory

        result = search_local_memory(
            query,
            top_k=top_k,
            filters=filters,
            caller=caller,
            paths=paths,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        result = {
            "available": False,
            "reason": f"local-memory-unavailable:{exc.__class__.__name__}",
            "error": str(exc),
            "results": [],
            "backend": {
                "kind": "unavailable",
                "semantic": False,
                "degraded": True,
                "fallbackFrom": None,
            },
            "capabilities": {
                "lexical": False,
                "semantic": False,
                "metadataFilters": False,
                "citations": False,
            },
        }
    normalized = normalize_memory_response(result, query=query, top_k=top_k)
    backend = normalized.get("backend") if isinstance(normalized.get("backend"), dict) else {}
    backend = {
        "kind": "local-fts" if normalized.get("available", True) else "unavailable",
        "semantic": False,
        "degraded": True,
        "fallbackFrom": None,
        **backend,
    }
    normalized["backend"] = backend
    capabilities = (
        normalized.get("capabilities")
        if isinstance(normalized.get("capabilities"), dict)
        else {}
    )
    normalized["capabilities"] = {
        "lexical": bool(normalized.get("available", True)),
        "semantic": False,
        "metadataFilters": bool(normalized.get("available", True)),
        "citations": bool(normalized.get("available", True)),
        **capabilities,
    }
    normalized["fallbackFrom"] = backend.get("fallbackFrom")
    return normalized


def _annotate_rag_response(result: dict[str, Any]) -> dict[str, Any]:
    annotated = normalize_memory_response(result)
    backend = annotated.get("backend") if isinstance(annotated.get("backend"), dict) else {}
    annotated["backend"] = {
        **backend,
        "kind": "agentic-rag",
        "semantic": True,
        "degraded": not bool(annotated.get("available", True)),
        "fallbackFrom": None,
    }
    capabilities = (
        annotated.get("capabilities")
        if isinstance(annotated.get("capabilities"), dict)
        else {}
    )
    annotated["capabilities"] = {
        **capabilities,
        "lexical": True,
        "semantic": True,
        "metadataFilters": True,
        "citations": True,
        "agenticPlanning": True,
        "multiPass": True,
    }
    annotated.setdefault("retrievalMode", "semantic")
    annotated["fallbackFrom"] = None
    return annotated


def _annotate_fallback(
    result: dict[str, Any],
    *,
    fallback_from: str,
) -> dict[str, Any]:
    annotated = normalize_memory_response(result)
    backend = annotated.get("backend") if isinstance(annotated.get("backend"), dict) else {}
    annotated["backend"] = {
        **backend,
        "degraded": True,
        "fallbackFrom": fallback_from,
    }
    annotated["fallbackFrom"] = fallback_from
    return annotated


def normalize_memory_response(result: dict[str, Any] | None, *, query: str = "", top_k: int = 5) -> dict[str, Any]:
    """Preserve the external-agent evidence schema for CLI consumers."""
    response = dict(result) if isinstance(result, dict) else {"results": []}
    results = response.get("results") if isinstance(response.get("results"), list) else []
    available = response.get("available") if type(response.get("available")) is bool else False
    reason = str(response.get("reason") or "")
    response.setdefault("schemaVersion", 2)
    response["available"] = available
    response["results"] = results
    response.setdefault(
        "queryPlan",
        {
            "schemaVersion": 2,
            "query": str(response.get("query") or query or ""),
            "topK": int(response.get("topK") or top_k or 5),
            "stages": [],
            "subQueries": [str(response.get("query") or query or "")] if str(response.get("query") or query or "").strip() else [],
            "explicitFilters": {},
            "status": "unavailable" if not available else "ready",
        },
    )
    response.setdefault("citationPack", [])
    response.setdefault(
        "eventAggregation",
        {
            "schemaVersion": 2,
            "status": "unavailable" if not available else "no-events" if not results else "not-computed",
            "eventCount": 0,
            "events": [],
            "timeline": [],
            "mostSevereEvent": None,
            "resolutionCitations": [],
            "reason": reason or None,
        },
    )
    response.setdefault(
        "answerSynthesis",
        {
            "status": "unavailable" if not available else "no-results" if not results else "ready",
            "method": "extractive",
            "summary": "",
            "citationIds": [],
            "reason": reason or None,
        },
    )
    response.setdefault(
        "quality",
        {
            "schemaVersion": 1,
            "status": "insufficient" if not available else "weak" if not results else "not-computed",
            "needsMoreEvidence": True if not available or not results else None,
            "resultCount": len(results),
            "keyTerms": [],
            "coveredTerms": [],
            "missingTerms": [],
            "coverage": 0.0 if not results else None,
            "flags": {},
            "recommendations": ["retry-when-rag-available"] if not available else [],
        },
    )
    response.setdefault(
        "retrievalController",
        {
            "schemaVersion": 1,
            "mode": "bounded-deterministic-multi-pass",
            "serverSide": True,
            "executionPolicy": "not reported by backend",
            "passesRun": ["quality-gate"],
            "passes": [
                {
                    "id": "quality-gate",
                    "status": "insufficient" if not available else "weak" if not results else "not-computed",
                    "needsMoreEvidence": True if not available or not results else None,
                }
            ],
            "qualityStatus": "insufficient" if not available else "weak" if not results else "not-computed",
            "needsMoreEvidence": True if not available or not results else None,
        },
    )
    response.setdefault(
        "agentic",
        {
            "schemaVersion": 2,
            "evidenceFieldsStable": True,
            "serverSidePlanning": True,
            "serverSideMultiPass": True,
            "serverSideQualityGate": True,
            "serverSideEventAggregation": True,
            "llmGenerated": False,
        },
    )
    agentic = response.get("agentic")
    if isinstance(agentic, dict):
        agentic.setdefault("serverSideMultiPass", True)
        agentic.setdefault("serverSideQualityGate", True)
    for key in ("queryPlan", "eventAggregation", "agentic"):
        section = response.get(key)
        if isinstance(section, dict):
            section["schemaVersion"] = 2
    for key in ("quality", "retrievalController"):
        section = response.get(key)
        if isinstance(section, dict):
            section["schemaVersion"] = 1
    return response


def _active_runtime_dashboard_url() -> str:
    """Resolve the active Dashboard's loopback URL for anonymous local access."""
    try:
        from .paths import load_paths
        from .settings import resolve_dashboard_settings

        dashboard = resolve_dashboard_settings(load_paths())
        port = int(dashboard.get("port") or 3036)
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return DEFAULT_DASHBOARD_URL


def compact_memory_results(result: dict[str, Any], *, max_results: int = 5) -> str:
    """Render a compact product-facing text view for CLI consumers."""
    if not result.get("available", True):
        return render_cli(
            "Memory search",
            fields=(("Status", "Unavailable"), ("Reason", _friendly_memory_reason(result.get("reason")))),
            next_steps=("actanara doctor --rag",),
        ).rstrip()
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    if not rows:
        return render_cli("Memory search", fields=(("Status", "No matches"),)).rstrip()
    lines: list[str] = []
    for index, row in enumerate(rows[:max_results], start=1):
        if not isinstance(row, dict):
            continue
        source = row.get("sourceSet") or row.get("source") or "unknown-source"
        date = row.get("date") or row.get("createdAt") or ""
        score = row.get("score")
        prefix = f"{index}. {friendly_name(source, fallback='Memory')}"
        if date:
            prefix += f" · {date}"
        if score is not None:
            prefix += f" · relevance {score}"
        text = row.get("textPreview") or row.get("text") or row.get("content") or ""
        entry = prefix
        if text:
            entry += f"\n   {str(text).strip()}"
        lines.append(entry)
    if not lines:
        return render_cli("Memory search", fields=(("Status", "No matches"),)).rstrip()
    return render_cli(
        "Memory search",
        fields=(("Status", "Ready"), ("Results", len(lines))),
        sections=(("Matches", lines),),
    ).rstrip()


def _friendly_memory_reason(value: object) -> str:
    reason = str(value or "").strip().lower()
    if "budget" in reason or "timeout" in reason:
        return "The search took too long"
    if "encoding" in reason or "schema" in reason or "response" in reason:
        return "The memory service returned an unreadable response"
    if "unavailable" in reason or "server" in reason or "connection" in reason:
        return "The memory service is not responding"
    return "The memory service could not complete the search"
