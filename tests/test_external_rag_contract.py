import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))
sys.path.insert(0, str(ROOT / "src" / "agentic_rag"))

from dashboard.app.services import settings as dashboard_settings

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from dashboard.app.routers import settings as settings_router
    from starlette.requests import Request
else:  # pragma: no cover - exercised on lean test interpreters
    settings_router = None

SERVER_DEPS_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("fastapi", "uvicorn", "numpy"))
if SERVER_DEPS_AVAILABLE:
    from agentic_rag import embedding_server
else:  # pragma: no cover - exercised on lean test interpreters
    embedding_server = None


def _loopback_request() -> "Request":
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/rag/external/health",
            "raw_path": b"/api/rag/external/health",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:3036")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 3036),
        }
    )


class ExternalRagContractTests(unittest.TestCase):
    def assert_auxiliary_usage_prompt(self, contract: dict) -> None:
        prompt = contract["usagePrompt"]
        priority_tokens = (
            "current conversation",
            "host Agent Runtime's built-in or connected memory/history retrieval",
            "nova-RAG only when the preceding sources do not provide enough reliable information",
        )
        positions = [prompt.index(token) for token in priority_tokens]

        self.assertEqual(positions, sorted(positions))
        self.assertIn("user-provided material", prompt)
        self.assertIn("local authoritative files", prompt)
        self.assertIn("user explicitly asks to query nova-RAG", prompt)
        self.assertIn("evidence rather than authority", prompt)
        for eager_wording in (
            "use nova-RAG before answering",
            "always use nova-RAG",
            "search nova-RAG first",
        ):
            self.assertNotIn(eager_wording.lower(), prompt.lower())

    def test_external_contract_lists_contract_endpoint_and_stable_agentic_fields(self):
        from app.services.settings import rag_external_agent_contract

        with patch("app.services.settings.get_rag_status", return_value={"provider": {}}):
            contract = rag_external_agent_contract()

        self.assertIn("GET /api/rag/external/contract", contract["allowedEndpoints"])
        self.assert_auxiliary_usage_prompt(contract)
        for field in ("quality", "retrievalController", "agentic", "externalAgentContract"):
            self.assertIn(field, contract["searchResponse"]["includes"])

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "nova-RAG server dependencies are unavailable")
    def test_rag_health_publishes_frozen_loaded_source_commit(self):
        assert embedding_server is not None
        commit = "2" * 64
        index_state = {
            "path": None,
            "source": "unavailable",
            "ready": False,
            "reason": "fixture",
        }
        with (
            patch.object(embedding_server, "_LOADED_SOURCE_COMMIT", commit),
            patch.object(embedding_server, "get_emb_matrix", return_value=(None, [], [])),
            patch.object(embedding_server, "_current_index_state", return_value=index_state),
            patch.object(embedding_server, "_embedding_provider_ready", return_value=False),
            patch.object(embedding_server, "_read_internal_token", return_value=""),
        ):
            payload = asyncio.run(embedding_server.health())

        self.assertEqual(payload["status"], "booting")
        self.assertEqual(payload["sourceCommit"], commit)
        self.assertNotIn(str(ROOT), payload.values())

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard router dependencies are unavailable")
    def test_contract_health_and_search_envelopes_publish_auxiliary_usage_prompt(self):
        assert settings_router is not None
        status = {"searchAvailable": True, "provider": {}}
        with (
            patch.object(settings_router.settings, "get_rag_status", return_value=status),
            patch.object(
                settings_router.settings,
                "rag_search",
                return_value={"available": True, "results": []},
            ),
        ):
            contract = asyncio.run(settings_router.api_rag_external_contract(_loopback_request()))
            health = asyncio.run(settings_router.api_rag_external_health(_loopback_request(), probe=False))
            search = asyncio.run(
                settings_router.api_rag_external_search(
                    _loopback_request(),
                    {"query": "prior work"},
                )
            )

        envelope_contracts = (
            contract,
            health["externalAgentContract"],
            search["externalAgentContract"],
        )
        self.assertEqual(len({item["usagePrompt"] for item in envelope_contracts}), 1)
        for envelope_contract in envelope_contracts:
            self.assert_auxiliary_usage_prompt(envelope_contract)

    def test_external_agent_contract_docs_align_with_generated_skill(self):
        contract = (ROOT / "docs" / "rag-external-agent-contract.md").read_text(encoding="utf-8")
        skill_service = (ROOT / "src" / "dashboard" / "app" / "services" / "external_rag_skill_registration.py").read_text(
            encoding="utf-8"
        )
        normalized_contract = " ".join(contract.split())
        normalized_skill_service = " ".join(skill_service.split())

        self.assertIn("GET  /api/rag/external/contract", contract)
        self.assertIn("GET /api/rag/external/contract", skill_service)
        for token in ("queryPlan", "citationPack", "eventAggregation", "answerSynthesis", "quality", "retrievalController"):
            self.assertIn(token, contract)
            self.assertIn(token, skill_service)
        for token in ("sourceSets", "lifecycle", "workType", "current-state"):
            self.assertIn(token, contract)
            self.assertIn(token, skill_service)
        for token in (
            "current conversation",
            "user-provided material",
            "local authoritative files",
            "host Agent Runtime",
            "only when the preceding sources do not provide enough reliable information",
            "user explicitly asks",
            "evidence rather than authority",
        ):
            self.assertIn(token, normalized_contract)
            self.assertIn(token, normalized_skill_service)
        self.assertNotIn("Use nova-RAG before answering", contract)
        self.assertIn("callers must not localize those values", contract)
        for token in (
            "multi-pass",
            "quality.needsMoreEvidence",
            "metaDiscussionTop",
            "retry-with-meta-discussion-suppressed",
            "authoritative-source-pass",
            "Exact pass",
            "Rewrite pass",
            "Filtered pass",
            "available=false",
            "remainingBudgetMs",
            "running_after_timeout",
        ):
            self.assertIn(token, contract)
            self.assertIn(token, skill_service)

    def assert_evidence_schema(self, payload: dict, *, status: str = "unavailable") -> None:
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertFalse(payload["available"])
        self.assertIn("queryPlan", payload)
        self.assertEqual(payload["queryPlan"]["schemaVersion"], 2)
        self.assertIn("citationPack", payload)
        self.assertIn("eventAggregation", payload)
        self.assertEqual(payload["eventAggregation"]["schemaVersion"], 2)
        self.assertEqual(payload["eventAggregation"]["status"], status)
        self.assertIn("answerSynthesis", payload)
        self.assertEqual(payload["answerSynthesis"]["status"], status)
        self.assertIn("quality", payload)
        self.assertEqual(payload["quality"]["schemaVersion"], 1)
        self.assertTrue(payload["quality"]["needsMoreEvidence"])
        self.assertIn("retrievalController", payload)
        self.assertEqual(payload["retrievalController"]["schemaVersion"], 1)
        self.assertTrue(payload["retrievalController"]["serverSide"])
        self.assertEqual(payload["agentic"]["schemaVersion"], 2)
        self.assertTrue(payload["agentic"]["evidenceFieldsStable"])
        self.assertTrue(payload["agentic"]["serverSideMultiPass"])
        self.assertTrue(payload["agentic"]["serverSideQualityGate"])
        self.assertTrue(payload["agentic"]["serverSideEventAggregation"])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard router dependencies are unavailable")
    def test_dashboard_external_search_value_error_returns_stable_schema(self):
        assert settings_router is not None
        response = asyncio.run(settings_router.api_rag_external_search(_loopback_request(), {}))
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assert_evidence_schema(payload)
        self.assertTrue(payload["externalAgentContract"]["readOnly"])
        self.assertEqual(payload["queryPlan"]["query"], "")

    def test_dashboard_rag_search_transport_error_returns_stable_schema(self):
        status = {
            "searchAvailable": True,
            "settings": {"server_health_path": "/health"},
            "server": {"url": "http://127.0.0.1:9999/health"},
        }
        with (
            patch.object(dashboard_settings, "get_rag_status", return_value=status),
            patch.object(dashboard_settings.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")),
        ):
            payload = dashboard_settings.rag_search({"query": "hello", "topK": 3})

        self.assert_evidence_schema(payload)
        self.assertEqual(payload["queryPlan"]["query"], "hello")
        self.assertEqual(payload["queryPlan"]["topK"], 3)
        self.assertIn("rag-server-unavailable", payload["reason"])

    def test_dashboard_facade_forwards_only_remaining_capped_budget(self):
        captured = {}
        status = {
            "searchAvailable": True,
            "settings": {"server_health_path": "/health"},
            "server": {"url": "http://127.0.0.1:3037/health"},
        }
        settings = types.SimpleNamespace(retrieval_top_k=8, retrieval_latency_budget_seconds=120.0)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"available":true,"results":[]}'

        def urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return Response()

        with (
            patch.object(dashboard_settings, "resolve_rag_settings", return_value=settings),
            patch.object(dashboard_settings, "get_rag_status", return_value=status),
            patch.object(dashboard_settings.urllib.request, "urlopen", side_effect=urlopen),
        ):
            payload = dashboard_settings.rag_search(
                {
                    "query": "bounded",
                    "remainingBudgetMs": 500,
                    "latencyBudgetMs": 120_000,
                }
            )

        self.assertTrue(payload["available"])
        self.assertGreaterEqual(captured["payload"]["latency_budget_ms"], 100)
        self.assertLessEqual(captured["payload"]["latency_budget_ms"], 450)
        self.assertLessEqual(captured["timeout"], 0.5)
        self.assertEqual(payload["facadeBudget"]["totalBudgetMs"], 500)
        self.assertEqual(payload["facadeBudget"]["serverBudgetCapMs"], 60_000)

    def test_dashboard_facade_exhausted_budget_does_not_probe_or_call_server(self):
        settings = types.SimpleNamespace(retrieval_top_k=8, retrieval_latency_budget_seconds=60.0)
        with (
            patch.object(dashboard_settings, "resolve_rag_settings", return_value=settings),
            patch.object(dashboard_settings, "get_rag_status") as status,
            patch.object(dashboard_settings.urllib.request, "urlopen") as urlopen,
        ):
            payload = dashboard_settings.rag_search({"query": "bounded", "remainingBudgetMs": 0})

        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "rag-search-budget-exhausted")
        self.assertEqual(payload["facadeBudget"]["remainingBudgetMs"], 0)
        status.assert_not_called()
        urlopen.assert_not_called()

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_provider_not_ready_returns_stable_schema(self):
        assert embedding_server is not None
        original = embedding_server.embedding_provider
        embedding_server.embedding_provider = None
        try:
            response = asyncio.run(embedding_server.search(embedding_server.SearchRequest(query="hello", top_k=2)))
        finally:
            embedding_server.embedding_provider = original
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 503)
        self.assert_evidence_schema(payload)
        self.assertEqual(payload["queryPlan"]["topK"], 2)

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_rejects_unbounded_top_k_and_clamps_core_fallback(self):
        assert embedding_server is not None
        for invalid in (-1, 0, 21, 1_000_000):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                embedding_server.SearchRequest(query="hello", top_k=invalid)

        self.assertEqual(embedding_server.SearchRequest(query="hello", top_k=1).top_k, 1)
        self.assertEqual(embedding_server.SearchRequest(query="hello", top_k=20).top_k, 20)
        unchecked = embedding_server.SearchRequest.model_construct(query="hello", top_k=1_000_000)
        with patch.object(embedding_server, "get_emb_matrix", return_value=(None, None, None)):
            payload = embedding_server.perform_search(unchecked)
        self.assertEqual(payload["topK"], 20)
        self.assertEqual(payload["queryPlan"]["topK"], 20)

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_baseline_timeout_returns_redacted_envelope(self):
        assert embedding_server is not None
        matrix = embedding_server.np.array([[1.0, 0.0]], dtype=embedding_server.np.float32)
        request = embedding_server.SearchRequest(query="hello", top_k=2, latency_budget_ms=100)
        with (
            patch.object(embedding_server, "get_emb_matrix", return_value=(matrix, ["r1"], [{"id": "r1"}])),
            patch.object(
                embedding_server,
                "_encode_queries",
                side_effect=TimeoutError("synthetic api_key must never escape"),
            ),
        ):
            payload = embedding_server.perform_search(request)

        self.assert_evidence_schema(payload)
        self.assertEqual(payload["reason"], "search-timeout")
        self.assertEqual(payload["error"], "timeout")
        self.assertEqual(payload["retrievalController"]["timeoutStage"], "baseline-embedding")
        self.assertNotIn("api_key", json.dumps(payload))

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_optional_timeout_preserves_baseline_results(self):
        assert embedding_server is not None
        matrix = embedding_server.np.array([[1.0, 0.0]], dtype=embedding_server.np.float32)
        weak = {
            "results": [{"id": "baseline", "text": "baseline evidence"}],
            "quality": {"status": "weak", "needsMoreEvidence": True},
            "retrievalController": {"serverSide": True},
        }
        with (
            patch.object(embedding_server, "get_emb_matrix", return_value=(matrix, ["r1"], [{"id": "r1"}])),
            patch.object(embedding_server, "build_query_plan", return_value={"query": "hello"}),
            patch.object(
                embedding_server,
                "build_retrieval_passes",
                return_value=[
                    {"id": "baseline-hybrid", "query": "hello"},
                    {"id": "rewrite", "query": "hello detail"},
                ],
            ),
            patch.object(
                embedding_server,
                "_encode_queries",
                side_effect=[[[1.0, 0.0]], TimeoutError("synthetic secret=must-not-escape")],
            ),
            patch.object(
                embedding_server,
                "_rank_retrieval_pass",
                return_value={"id": "baseline-hybrid", "ranked": weak},
            ),
            patch.object(embedding_server, "_fuse_search_passes", return_value=weak),
        ):
            payload = embedding_server.perform_search(embedding_server.SearchRequest(query="hello", top_k=2))

        self.assertEqual(payload["results"][0]["id"], "baseline")
        self.assertEqual(payload["retrievalController"]["stopReason"], "optional-embedding-timeout")
        self.assertTrue(payload["retrievalController"]["degraded"])
        self.assertNotIn("must-not-escape", json.dumps(payload))

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_cancelled_search_holds_capacity_until_worker_exits(self):
        assert embedding_server is not None

        async def exercise():
            started = threading.Event()
            unblock = threading.Event()
            semaphore = asyncio.Semaphore(1)

            def blocking_search(_request):
                started.set()
                unblock.wait(timeout=2)
                return {"available": True}

            try:
                with (
                    patch.object(embedding_server, "_embedding_provider_configured", return_value=True),
                    patch.object(embedding_server, "_search_semaphore", semaphore),
                    patch.object(embedding_server, "perform_search", new=blocking_search),
                ):
                    task = asyncio.create_task(
                        embedding_server.search(embedding_server.SearchRequest(query="hello"))
                    )
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    self.assertTrue(semaphore.locked())
                    task.cancel()
                    response = await task
                    payload = json.loads(response.body.decode("utf-8"))
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(payload["reason"], "search-cancelled")
                    self.assertEqual(payload["workerTelemetry"]["workerState"], "running_after_cancel")
                    self.assertFalse(payload["workerTelemetry"]["hardCancelled"])
                    self.assertTrue(payload["workerTelemetry"]["capacityPermitHeld"])
                    self.assertTrue(semaphore.locked())
                    unblock.set()
                    await asyncio.wait_for(semaphore.acquire(), timeout=1)
                    semaphore.release()
            finally:
                unblock.set()

        asyncio.run(exercise())

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_timed_out_sync_worker_returns_promptly_and_holds_capacity_until_real_exit(self):
        assert embedding_server is not None

        async def exercise():
            started = threading.Event()
            unblock = threading.Event()
            semaphore = asyncio.Semaphore(1)

            def blocking_search(_request):
                started.set()
                unblock.wait(timeout=2)
                return {"available": True, "late": "must-not-mutate-timeout-envelope"}

            try:
                with (
                    patch.object(embedding_server, "_embedding_provider_configured", return_value=True),
                    patch.object(embedding_server, "_search_semaphore", semaphore),
                    patch.object(embedding_server, "perform_search", new=blocking_search),
                ):
                    before = embedding_server.time.monotonic()
                    response = await embedding_server.search(
                        embedding_server.SearchRequest(query="hello", latency_budget_ms=100)
                    )
                    elapsed = embedding_server.time.monotonic() - before
                    payload = json.loads(response.body.decode("utf-8"))
                    self.assertTrue(started.is_set())
                    self.assertLess(elapsed, 0.5)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(payload["reason"], "search-timeout")
                    self.assertEqual(payload["workerTelemetry"]["workerState"], "running_after_timeout")
                    self.assertTrue(payload["workerTelemetry"]["capacityPermitHeld"])
                    self.assertNotIn("late", payload)
                    self.assertTrue(semaphore.locked())
                    unblock.set()
                    await asyncio.wait_for(semaphore.acquire(), timeout=1)
                    semaphore.release()
                    self.assertNotIn("late", payload)
            finally:
                unblock.set()

        asyncio.run(exercise())

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_server_caps_budget_and_rejects_non_loopback_clients(self):
        assert embedding_server is not None
        request = embedding_server.SearchRequest(query="hello", latency_budget_ms=120_000)
        with patch.object(embedding_server.rag_config, "SEARCH_LATENCY_BUDGET_SECONDS", 120):
            self.assertEqual(embedding_server._search_budget_seconds(request), 60.0)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("203.0.113.10", 4567),
            "server": ("127.0.0.1", 3037),
        }
        downstream_called = False

        async def call_next(_request):
            nonlocal downstream_called
            downstream_called = True
            return embedding_server.JSONResponse({"ok": True})

        response = asyncio.run(
            embedding_server.enforce_loopback_client(embedding_server.Request(scope), call_next)
        )
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(payload["error"], "rag-server-non-loopback")
        self.assertFalse(downstream_called)

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_internal_encode_route_requires_private_runtime_token(self):
        assert embedding_server is not None
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "internal-token"
            token_path.write_text("candidate-internal-token\n", encoding="utf-8")
            token_path.chmod(0o600)
            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/encode",
                "raw_path": b"/encode",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 4567),
                "server": ("127.0.0.1", 3037),
            }
            called = []

            async def call_next(_request):
                called.append(True)
                return embedding_server.JSONResponse({"ok": True})

            with patch.dict(os.environ, {"NOVA_RAG_INTERNAL_TOKEN_FILE": str(token_path)}, clear=False):
                denied = asyncio.run(
                    embedding_server.enforce_loopback_client(embedding_server.Request(scope), call_next)
                )
                authorized_scope = dict(scope)
                authorized_scope["headers"] = [
                    (b"x-actanara-rag-internal-token", b"candidate-internal-token")
                ]
                accepted = asyncio.run(
                    embedding_server.enforce_loopback_client(
                        embedding_server.Request(authorized_scope),
                        call_next,
                    )
                )

        denied_payload = json.loads(denied.body.decode("utf-8"))
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied_payload["error"], "rag-internal-authorization-required")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(called, [True])

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_no_index_returns_stable_schema(self):
        assert embedding_server is not None
        request = embedding_server.SearchRequest(query="hello", top_k=4)
        with patch.object(embedding_server, "get_emb_matrix", return_value=(None, None, None)):
            payload = embedding_server.perform_search(request)

        self.assert_evidence_schema(payload)
        self.assertEqual(payload["reason"], "active-v2-index-not-loaded")
        self.assertEqual(payload["queryPlan"]["topK"], 4)

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_runs_bounded_internal_requery_passes(self):
        assert embedding_server is not None
        calls = []

        class FakeProvider:
            ready = True

            def encode_query(self, query):
                calls.append(query)
                return [1.0, 0.0]

        chunks = [
            {
                "id": "meta",
                "text": "nova-RAG 真实索引下召回质量差：2096 端口为什么不可用 Top-1000 benchmark needsMoreEvidence。",
                "date": "2026-07-07",
                "layer": "technical",
                "sourceSet": "filtered-dialogue-daily",
            },
            {
                "id": "fact",
                "text": "2096 端口不可用，因为目标环境 firewall DROP 该端口。",
                "date": "2026-05-04",
                "layer": "technical",
                "sourceSet": "lessons",
                "governance": {"lifecycle": "canonical", "authorityRank": 95, "provenanceScore": 1.0},
                "sourceId": "lesson:2096",
            },
        ]
        m_norm = embedding_server.np.array([[1.0, 0.0], [0.98, 0.2]], dtype=embedding_server.np.float32)
        m_norm = m_norm / (embedding_server.np.linalg.norm(m_norm, axis=1, keepdims=True) + 1e-9)
        original_provider = embedding_server.embedding_provider
        embedding_server.embedding_provider = FakeProvider()
        try:
            with patch.object(embedding_server, "get_emb_matrix", return_value=(m_norm, ["meta", "fact"], chunks)):
                payload = embedding_server.perform_search(
                    embedding_server.SearchRequest(query="2096 端口为什么不可用？", top_k=2)
                )
        finally:
            embedding_server.embedding_provider = original_provider

        self.assertEqual(len(calls), 1)
        self.assertTrue(payload["retrievalController"]["serverSide"])
        self.assertEqual(payload["retrievalController"]["passesRun"][0], "baseline-hybrid")
        self.assertEqual(payload["retrievalController"]["stopReason"], "quality-strong-after-baseline")
        self.assertEqual(len(payload["retrievalController"]["passTelemetry"]), 1)
        self.assertIn("server-side-requery", payload["queryPlan"]["stages"])

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_direct_rag_server_batches_optional_embeddings_for_weak_baseline(self):
        assert embedding_server is not None
        batches = []

        class FakeProvider:
            ready = True

            def encode_query(self, _query):
                return [1.0, 0.0]

            def encode(self, queries, show_progress_bar=False):
                batches.append(list(queries))
                return [[1.0, 0.0] for _query in queries]

        chunks = [{"id": "generic", "text": "unrelated generic memory", "sourceSet": "filtered-dialogue-daily"}]
        m_norm = embedding_server.np.array([[1.0, 0.0]], dtype=embedding_server.np.float32)
        original_provider = embedding_server.embedding_provider
        embedding_server.embedding_provider = FakeProvider()
        try:
            with patch.object(embedding_server, "get_emb_matrix", return_value=(m_norm, ["generic"], chunks)):
                payload = embedding_server.perform_search(
                    embedding_server.SearchRequest(query="2096 端口为什么不可用？", top_k=2)
                )
        finally:
            embedding_server.embedding_provider = original_provider

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 1)
        self.assertGreaterEqual(len(batches[1]), 1)
        self.assertEqual(payload["retrievalController"]["executionPolicy"], "baseline-first-adaptive-bounded")
        self.assertIn("latencyBudgetMs", payload["retrievalController"])
        self.assertEqual(payload["retrievalController"]["chunkCount"], 1)
        self.assertFalse(payload["retrievalController"]["slowQuery"])
        self.assertEqual(
            [item["batch"] for item in payload["retrievalController"]["embeddingTelemetry"]],
            ["baseline", "optional-passes"],
        )

    @unittest.skipUnless(SERVER_DEPS_AVAILABLE, "direct RAG server dependencies are unavailable")
    def test_unit_chunk_count_telemetry_is_not_the_candidate_85k_profile(self):
        assert embedding_server is not None

        class FakeProvider:
            ready = True

            def encode(self, queries, **_kwargs):
                return [[1.0, 0.0] for _query in queries]

        class LargeIndexView(list):
            def __len__(self):
                return 85000

        strong = {
            "results": [{"id": "r1"}],
            "quality": {"status": "strong"},
            "retrievalController": {"serverSide": True, "passesRun": ["baseline-hybrid"]},
        }
        original_provider = embedding_server.embedding_provider
        embedding_server.embedding_provider = FakeProvider()
        try:
            with (
                patch.object(
                    embedding_server,
                    "get_emb_matrix",
                    return_value=(embedding_server.np.array([[1.0, 0.0]]), ["r1"], LargeIndexView([{}])),
                ),
                patch.object(embedding_server, "_rank_retrieval_pass", return_value={"id": "baseline-hybrid", "ranked": strong}),
                patch.object(embedding_server, "_fuse_search_passes", return_value=strong),
            ):
                payload = embedding_server.perform_search(embedding_server.SearchRequest(query="large index"))
        finally:
            embedding_server.embedding_provider = original_provider

        self.assertEqual(payload["retrievalController"]["chunkCount"], 85000)
        self.assertEqual(payload["retrievalController"]["stopReason"], "quality-strong-after-baseline")

    def test_external_rag_normalizers_upgrade_nested_schema_versions(self):
        payload = dashboard_settings.normalize_external_rag_search_response(
            {
                "available": True,
                "results": [{"id": "r1"}],
                "queryPlan": {"schemaVersion": 1, "query": "hello"},
                "eventAggregation": {"schemaVersion": 1, "status": "ready"},
                "quality": {"schemaVersion": 99, "status": "strong"},
                "retrievalController": {"schemaVersion": 99, "serverSide": True},
                "agentic": {"schemaVersion": 1, "evidenceFieldsStable": True},
            },
            query="hello",
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["queryPlan"]["schemaVersion"], 2)
        self.assertEqual(payload["eventAggregation"]["schemaVersion"], 2)
        self.assertEqual(payload["quality"]["schemaVersion"], 1)
        self.assertEqual(payload["retrievalController"]["schemaVersion"], 1)
        self.assertEqual(payload["agentic"]["schemaVersion"], 2)
        self.assertTrue(payload["agentic"]["serverSideMultiPass"])


if __name__ == "__main__":
    unittest.main()
