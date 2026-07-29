import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.routers import settings as settings_router
from app.services import settings as dashboard_settings
from app.services.dashboard_security import (
    is_loopback_external_request,
    is_session_exempt_path,
)


def _request(
    *,
    client: str = "127.0.0.1",
    host: str = "127.0.0.1:3036",
    forwarded_for: str | None = None,
) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/memory/external/health",
            "raw_path": b"/api/memory/external/health",
            "query_string": b"",
            "headers": headers,
            "client": (client, 50000),
            "server": ("127.0.0.1", 3036),
        }
    )


class DashboardMemoryApiTests(unittest.TestCase):
    def test_contract_exposes_auto_local_and_rag_modes(self):
        contract = dashboard_settings.memory_external_agent_contract()

        self.assertTrue(contract["readOnly"])
        self.assertEqual(contract["security"]["anonymousAccess"], "loopback-only")
        self.assertEqual(set(contract["modes"]), {"auto", "local", "rag"})
        self.assertIn("POST /api/memory/external/search", contract["allowedEndpoints"])
        self.assertIn("backend", contract["searchResponse"]["includes"])
        self.assertIn("capabilities", contract["searchResponse"]["includes"])
        self.assertEqual(contract["legacyStrictRagFacade"], "/api/rag/external")
        self.assertEqual(
            contract["searchRequest"]["budgetPolicy"]["singleRequestHardLimitMs"],
            90_000,
        )
        for field in ("remainingBudgetMs", "budgetCall", "budgetMaxCalls"):
            self.assertIn(field, contract["searchRequest"]["optionalFields"])

    def test_memory_search_forwards_mode_caller_and_merged_filters(self):
        returned = {
            "available": True,
            "results": [],
            "backend": {"kind": "local-fts", "semantic": False},
            "capabilities": {"lexical": True, "semantic": False},
        }
        selected_paths = object()
        with (
            patch.object(dashboard_settings, "load_paths", return_value=selected_paths),
            patch.object(
                dashboard_settings,
                "resolve_dashboard_settings",
                return_value={"port": 4040},
            ),
            patch.object(
                dashboard_settings.external_agent_memory,
                "search_memory",
                return_value=returned,
            ) as search,
        ):
            payload = dashboard_settings.memory_external_search(
                {
                    "query": "earlier decision",
                    "topK": 7,
                    "mode": "local",
                    "caller": "codex",
                    "filters": {"project": "actanara"},
                    "sourceSets": ["lessons"],
                }
            )

        search.assert_called_once_with(
            "earlier decision",
            top_k=7,
            filters={"project": "actanara", "sourceSets": ["lessons"]},
            mode="local",
            caller="codex",
            paths=selected_paths,
            dashboard_url="http://127.0.0.1:4040",
            budget=None,
            budget_call=None,
            budget_max_calls=None,
        )
        self.assertEqual(payload["backend"]["kind"], "local-fts")
        self.assertTrue(payload["capabilities"]["lexical"])
        self.assertEqual(payload["requestedMode"], "local")
        self.assertIn("externalAgentContract", payload)

    def test_memory_search_rejects_invalid_mode_and_filter_shape(self):
        with self.assertRaisesRegex(ValueError, "mode must be"):
            dashboard_settings.memory_external_search({"query": "x", "mode": "hybrid"})
        with self.assertRaisesRegex(ValueError, "filters must be an object"):
            dashboard_settings.memory_external_search({"query": "x", "filters": []})

    def test_memory_search_forwards_bounded_remaining_budget_and_call_metadata(self):
        selected_paths = object()
        with (
            patch.object(dashboard_settings, "load_paths", return_value=selected_paths),
            patch.object(
                dashboard_settings,
                "resolve_dashboard_settings",
                return_value={"port": 4040},
            ),
            patch.object(
                dashboard_settings.external_agent_memory,
                "search_memory",
                return_value={"available": True, "results": []},
            ) as search,
        ):
            dashboard_settings.memory_external_search(
                {
                    "query": "bounded recall",
                    "mode": "rag",
                    "remainingBudgetMs": 120_000,
                    "budgetCall": 2,
                    "budgetMaxCalls": 3,
                }
            )

        forwarded = search.call_args.kwargs
        self.assertEqual(forwarded["budget_call"], 2)
        self.assertEqual(forwarded["budget_max_calls"], 3)
        telemetry = forwarded["budget"].telemetry()
        self.assertEqual(telemetry["totalBudgetMs"], 90_000)
        self.assertEqual(telemetry["maxCalls"], 1)

    def test_memory_search_rejects_invalid_budget_fields(self):
        invalid_payloads = (
            ({"remainingBudgetMs": 0}, "remainingBudgetMs"),
            ({"remainingBudgetMs": 1.5}, "remainingBudgetMs"),
            ({"remainingBudgetMs": True}, "remainingBudgetMs"),
            ({"budgetCall": 1}, "provided together"),
            ({"budgetMaxCalls": 3}, "provided together"),
            ({"budgetCall": 4, "budgetMaxCalls": 3}, "budgetCall"),
            ({"budgetCall": 1, "budgetMaxCalls": 4}, "budgetMaxCalls"),
        )
        for extra, message in invalid_payloads:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                dashboard_settings.memory_external_search({"query": "x", **extra})

    def test_memory_external_route_rejects_invalid_budget_before_search(self):
        with patch.object(
            settings_router.settings,
            "memory_external_search",
            side_effect=ValueError("remainingBudgetMs must be a positive integer"),
        ) as search:
            response = asyncio.run(
                settings_router.api_memory_external_search(
                    _request(),
                    {"query": "x", "remainingBudgetMs": 0},
                )
            )

        self.assertEqual(response.status_code, 400)
        search.assert_called_once()

    def test_status_selects_ready_local_backend_when_rag_is_unavailable(self):
        local = {
            "available": True,
            "ready": True,
            "status": "ready",
            "backend": {"kind": "local-fts", "semantic": False, "indexPath": "/tmp/memory.sqlite3"},
            "capabilities": {"lexical": True, "semantic": False, "fts5": True},
            "documentCount": 12,
            "sourceCount": 3,
        }
        configured = {
            "enabled": True,
            "backendPolicy": "auto",
            "local": {"enabled": True},
        }
        with (
            patch.object(dashboard_settings, "load_paths", return_value=object()),
            patch.object(dashboard_settings, "resolve_memory_search_settings", return_value=configured),
            patch.object(dashboard_settings, "local_memory_status", return_value=local),
            patch.object(
                dashboard_settings,
                "get_rag_status",
                return_value={"searchAvailable": False, "provider": {}},
            ),
        ):
            status = dashboard_settings.get_memory_status(probe_server=False)

        self.assertTrue(status["available"])
        self.assertEqual(status["backend"]["kind"], "local-fts")
        self.assertFalse(status["backend"]["semantic"])
        self.assertEqual(status["backends"]["local"]["documentCount"], 12)
        self.assertTrue(status["actions"]["sync"])

    def test_local_sync_returns_refreshed_status(self):
        with (
            patch.object(dashboard_settings, "load_paths", return_value=object()),
            patch.object(
                dashboard_settings,
                "sync_local_memory_index",
                return_value={"status": "ready", "changedSources": 2},
            ) as sync,
            patch.object(
                dashboard_settings,
                "get_memory_status",
                return_value={"available": True, "backend": {"kind": "local-fts"}},
            ),
        ):
            payload = dashboard_settings.sync_memory_local_index()

        sync.assert_called_once()
        self.assertEqual(payload["action"], "sync")
        self.assertEqual(payload["result"]["changedSources"], 2)
        self.assertEqual(payload["memoryStatus"]["backend"]["kind"], "local-fts")

    def test_local_rebuild_uses_recoverable_sidecar_rebuilder(self):
        with (
            patch.object(dashboard_settings, "load_paths", return_value=object()),
            patch.object(
                dashboard_settings,
                "rebuild_local_memory_index",
                return_value={"status": "ready", "rebuilt": True},
            ) as rebuild,
            patch.object(dashboard_settings, "sync_local_memory_index") as sync,
            patch.object(
                dashboard_settings,
                "get_memory_status",
                return_value={"available": True, "backend": {"kind": "local-fts"}},
            ),
        ):
            payload = dashboard_settings.sync_memory_local_index(rebuild=True)

        rebuild.assert_called_once()
        sync.assert_not_called()
        self.assertEqual(payload["action"], "rebuild")
        self.assertTrue(payload["result"]["rebuilt"])

    def test_interactive_memory_status_probes_rag_by_default(self):
        with patch.object(
            settings_router.settings,
            "get_memory_status",
            return_value={"available": True, "backend": {"kind": "agentic-rag"}},
        ) as status:
            payload = asyncio.run(settings_router.api_memory_status())

        self.assertEqual(payload["backend"]["kind"], "agentic-rag")
        status.assert_called_once_with(probe_server=True)

    def test_generic_memory_skill_apply_requires_and_forwards_explicit_tools(self):
        with patch.object(
            settings_router.settings,
            "rag_external_skill_registration",
            return_value={"status": "planned", "selectedTools": ["codex"]},
        ) as register:
            missing = asyncio.run(
                settings_router.api_memory_external_skill_registration(
                    {"dryRun": False, "confirmationText": "INSTALL ACTANARA MEMORY SKILL"}
                )
            )
            selected = asyncio.run(
                settings_router.api_memory_external_skill_registration(
                    {
                        "dryRun": True,
                        "tools": ["codex"],
                    }
                )
            )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(selected["selectedTools"], ["codex"])
        register.assert_called_once_with({"dryRun": True, "tools": ["codex"]})

    def test_external_endpoints_allow_only_loopback_peer_and_host(self):
        self.assertTrue(is_session_exempt_path("/api/memory/external/search"))
        self.assertFalse(is_session_exempt_path("/api/memory/external-unsafe"))
        self.assertFalse(is_session_exempt_path("/api/memory/search"))
        self.assertTrue(is_loopback_external_request("127.0.0.1", "localhost:3036"))
        self.assertFalse(is_loopback_external_request("203.0.113.8", "127.0.0.1:3036"))
        self.assertFalse(is_loopback_external_request("127.0.0.1", "memory.example.test"))
        self.assertFalse(
            is_loopback_external_request(
                "127.0.0.1",
                "127.0.0.1:3036",
                "203.0.113.8",
            )
        )

        with patch.object(
            settings_router.settings,
            "memory_external_health",
            return_value={"available": True, "backend": {"kind": "local-fts"}},
        ) as health:
            local = asyncio.run(
                settings_router.api_memory_external_health(_request(), probe=False)
            )
            remote = asyncio.run(
                settings_router.api_memory_external_health(
                    _request(client="203.0.113.8"),
                    probe=False,
                )
            )
            proxied = asyncio.run(
                settings_router.api_memory_external_health(
                    _request(host="memory.example.test"),
                    probe=False,
                )
            )
            forwarded = asyncio.run(
                settings_router.api_memory_external_health(
                    _request(forwarded_for="203.0.113.8"),
                    probe=False,
                )
            )

        self.assertEqual(local["backend"]["kind"], "local-fts")
        self.assertEqual(remote.status_code, 403)
        self.assertEqual(proxied.status_code, 403)
        self.assertEqual(forwarded.status_code, 403)
        self.assertEqual(health.call_count, 1)
        self.assertEqual(
            json.loads(remote.body.decode("utf-8"))["error"],
            "memory-external-loopback-required",
        )

    def test_legacy_rag_external_routes_remain_present_and_loopback_only(self):
        source = (
            ROOT / "src" / "dashboard" / "app" / "routers" / "settings.py"
        ).read_text(encoding="utf-8")

        for route in (
            '@router.get("/rag/external/health")',
            '@router.get("/rag/external/contract")',
            '@router.post("/rag/external/search")',
        ):
            self.assertIn(route, source)

        remote_request = _request(client="203.0.113.8")
        forwarded_request = _request(forwarded_for="203.0.113.8")
        with (
            patch.object(settings_router.settings, "get_rag_status") as health,
            patch.object(settings_router.settings, "rag_stats") as stats,
            patch.object(settings_router.settings, "rag_external_agent_contract") as contract,
            patch.object(settings_router.settings, "rag_search") as search,
        ):
            responses = (
                asyncio.run(settings_router.api_rag_external_health(remote_request)),
                asyncio.run(settings_router.api_rag_external_stats(remote_request)),
                asyncio.run(settings_router.api_rag_external_contract(remote_request)),
                asyncio.run(
                    settings_router.api_rag_external_search(
                        remote_request,
                        {"query": "private memory"},
                    )
                ),
                asyncio.run(settings_router.api_rag_external_health(forwarded_request)),
            )

        self.assertTrue(all(response.status_code == 403 for response in responses))
        health.assert_not_called()
        stats.assert_not_called()
        contract.assert_not_called()
        search.assert_not_called()

    def test_dashboard_ui_uses_authenticated_auto_search_and_local_controls(self):
        source = (
            ROOT / "src" / "dashboard" / "app" / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("fetch('/api/memory/search'", source)
        self.assertNotIn("fetch('/api/rag/search'", source)
        self.assertIn("mode: 'auto'", source)
        self.assertIn("fetch('/api/memory/status?probe=true')", source)
        self.assertIn("syncLocalMemoryIndex(false)", source)
        self.assertIn("backend.kind", source)
        self.assertIn("semantic=", source)


if __name__ == "__main__":
    unittest.main()
