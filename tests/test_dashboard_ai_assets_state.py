import asyncio
import importlib.machinery
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))


try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    class _RouterStub:
        def get(self, *_args, **_kwargs):
            return lambda function: function

        def post(self, *_args, **_kwargs):
            return lambda function: function

        def put(self, *_args, **_kwargs):
            return lambda function: function

    class _BackgroundTasksStub:
        def add_task(self, *_args, **_kwargs):
            return None

    class _ResponseStub(dict):
        def __init__(self, content=None, status_code=200, **kwargs):
            super().__init__(content or {})
            self.status_code = status_code
            self.kwargs = kwargs

    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.__spec__ = importlib.machinery.ModuleSpec("fastapi", loader=None)
    fastapi_stub.APIRouter = lambda: _RouterStub()
    fastapi_stub.BackgroundTasks = _BackgroundTasksStub
    fastapi_stub.Request = object
    responses_stub = types.ModuleType("fastapi.responses")
    responses_stub.__spec__ = importlib.machinery.ModuleSpec("fastapi.responses", loader=None)
    responses_stub.JSONResponse = _ResponseStub
    responses_stub.StreamingResponse = _ResponseStub
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub


from app.routers import ai_assets as ai_assets_router
from app.services import ai_assets
from app.services.dashboard_state import attach_dashboard_state


class DashboardAiAssetsStateTests(unittest.TestCase):
    def setUp(self):
        self._cache_before = ai_assets._cache
        ai_assets._cache = {"data": None, "ts": 0}
        self.addCleanup(setattr, ai_assets, "_cache", self._cache_before)

    @staticmethod
    def _paths(root: Path):
        return SimpleNamespace(
            home=root / "Actanara",
            db_path=root / "Actanara" / "foundation.sqlite3",
            diary_dir=root / "Diary",
        )

    @staticmethod
    def _snapshot(payload: dict) -> dict:
        return {
            "payload": payload,
            "projectionType": "foundation-ai-assets-non-rag-v2",
            "generatedAt": "2026-07-11T12:00:00+00:00",
            "sourceRunId": 7,
            "status": "ready",
        }

    def test_missing_snapshot_is_explicit_empty_with_refresh_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_workspace_dir", return_value=Path(tmp)),
                patch("data_foundation.snapshots.read_dashboard_snapshot", return_value=None),
            ):
                result = ai_assets._get_ai_assets_foundation()

        self.assertEqual(result["dashboardState"]["status"], "empty")
        self.assertEqual(result["dashboardState"]["sourceErrors"], [])
        self.assertNotIn("error", result)
        self.assertEqual(result["activeDayCount"], 0)
        freshness = result["dataFreshness"]["aiAssets"]
        self.assertEqual(freshness["source"], "snapshot-missing")
        self.assertEqual(freshness["status"], "snapshot_missing")
        self.assertTrue(freshness["refreshRequired"])

    def test_sqlite_and_json_read_failures_are_stable_redacted_errors(self):
        marker = "secret-token=do-not-leak /Users/operator/private.sqlite3"
        failures = (
            sqlite3.OperationalError(marker),
            json.JSONDecodeError("invalid snapshot JSON", marker, 0),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                paths = self._paths(Path(tmp))
                with (
                    patch.object(ai_assets, "load_paths", return_value=paths),
                    patch.object(ai_assets, "_workspace_dir", return_value=Path(tmp)),
                    patch("data_foundation.snapshots.read_dashboard_snapshot", side_effect=failure),
                    patch.object(ai_assets.logger, "exception") as logged,
                ):
                    result = ai_assets._get_ai_assets_foundation()

                logged.assert_called_once()
                self.assertEqual(result["dashboardState"]["status"], "error")
                self.assertEqual(result["error"], "ai-assets-snapshot error")
                self.assertEqual(result["activeDayCount"], 0)
                self.assertEqual(result["dataFreshness"]["aiAssets"]["status"], "source_error")
                self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))

    def test_invalid_snapshot_payload_shape_is_error_not_missing(self):
        marker = "/Users/operator/private-invalid-payload.json"
        snapshot = self._snapshot({})
        snapshot["payload"] = [marker]
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_workspace_dir", return_value=Path(tmp)),
                patch("data_foundation.snapshots.read_dashboard_snapshot", return_value=snapshot),
                patch.object(ai_assets.logger, "exception"),
            ):
                result = ai_assets._get_ai_assets_foundation()

        self.assertEqual(result["dashboardState"]["status"], "error")
        self.assertEqual(result["dataFreshness"]["aiAssets"]["status"], "source_error")
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))

    def test_usage_cache_errors_degrade_but_preserve_snapshot_and_active_days(self):
        payload = {
            "tools": [{"name": "Codex", "allTimeTokens": 42}],
            "totalTokens": 42,
            "activeDayCount": 17,
            "usageCache": {"sources": 4, "cached": 3, "errors": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_workspace_dir", return_value=Path(tmp)),
                patch.object(
                    ai_assets,
                    "_current_external_tool_detection",
                    return_value={"detectedToolKeys": ["codex"]},
                ),
                patch("data_foundation.snapshots.read_dashboard_snapshot", return_value=self._snapshot(payload)),
            ):
                result = ai_assets._get_ai_assets_foundation()

        self.assertEqual(result["dashboardState"]["status"], "degraded")
        self.assertEqual(
            result["dashboardState"]["sourceErrors"],
            [
                {
                    "source": "ai-assets-usage-cache",
                    "code": "incremental-source-read-failed",
                    "retryable": True,
                }
            ],
        )
        self.assertEqual(result["tools"], payload["tools"])
        self.assertEqual(result["totalTokens"], 42)
        self.assertEqual(result["activeDayCount"], 17)
        self.assertTrue(result["degraded"])

    def test_snapshot_reader_uses_current_detection_to_remove_stale_runtime_cards(self):
        stale_presence = {
            "detectedToolKeys": ["codex", "cursor"],
            "toolPresence": {
                "codex": {"id": "codex", "detected": True},
                "cursor": {"id": "cursor", "detected": True},
            },
        }
        current_presence = {
            "detectedToolKeys": ["codex"],
            "toolPresence": {
                "codex": {"id": "codex", "detected": True},
                "cursor": {"id": "cursor", "detected": False},
            },
        }
        payload = {
            **stale_presence,
            "tools": [
                {
                    "name": "Codex",
                    "allTimeTokens": 12,
                    "allTimeMessages": 2,
                    "sessionCount": 1,
                },
                {
                    "name": "Cursor",
                    "allTimeTokens": 0,
                    "allTimeMessages": 4,
                    "sessionCount": 2,
                    "usageStatus": "unavailable",
                },
            ],
            "agents": [
                {"name": "Codex", "source": "Codex"},
                {"name": "Cursor", "source": "Cursor"},
            ],
            "agentCount": 2,
            "agentTree": [{"name": "Codex"}, {"name": "Cursor"}],
            "storage": {
                "tools": [{"name": "Codex"}, {"name": "Cursor"}],
                "categories": [],
            },
            "skills": {
                "byTool": {"Codex": [], "Cursor": []},
                "total": 0,
            },
            "toolConfigs": [{"name": "Codex"}, {"name": "Cursor"}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_workspace_dir", return_value=Path(tmp)),
                patch.object(
                    ai_assets,
                    "_current_external_tool_detection",
                    return_value=current_presence,
                ),
                patch(
                    "data_foundation.snapshots.read_dashboard_snapshot",
                    return_value=self._snapshot(payload),
                ),
            ):
                result = ai_assets._get_ai_assets_foundation()

        self.assertEqual(result["detectedToolKeys"], ["codex"])
        self.assertFalse(result["toolPresence"]["cursor"]["detected"])
        for field in ("tools", "agents", "agentTree", "toolConfigs"):
            self.assertEqual([item["name"] for item in result[field]], ["Codex"])
        self.assertEqual(
            [item["name"] for item in result["storage"]["tools"]],
            ["Codex"],
        )
        self.assertEqual(list(result["skills"]["byTool"]), ["Codex"])
        self.assertEqual(result["agentCount"], 1)
        self.assertEqual(result["totalTokens"], 12)
        self.assertEqual(result["totalMessages"], 2)
        self.assertEqual(result["totalSessions"], 1)

    def test_error_result_is_not_cached_and_next_success_is_cached(self):
        failed = ai_assets._ai_assets_snapshot_failure()
        ready = attach_dashboard_state(
            {
                "tools": [{"name": "Codex"}],
                "activeDayCount": 3,
                "dataFreshness": {"aiAssets": {"source": "foundation"}},
            }
        )
        paths = self._paths(Path("/tmp/actanara-ai-assets-state-test"))
        with (
            patch.object(ai_assets, "load_paths", return_value=paths),
            patch.object(ai_assets, "resolve_runtime_source", return_value="foundation"),
            patch.object(ai_assets, "_ai_assets_cache_key", return_value={"source": "foundation"}),
            patch.object(ai_assets, "_get_ai_assets_foundation", side_effect=[failed, ready]) as build,
        ):
            first = ai_assets.get_ai_assets_cached()
            second = ai_assets.get_ai_assets_cached()
            third = ai_assets.get_ai_assets_cached()

        self.assertEqual(first["dashboardState"]["status"], "error")
        self.assertEqual(second["dashboardState"]["status"], "ready")
        self.assertIs(third, second)
        self.assertEqual(build.call_count, 2)

    def test_all_malformed_incremental_jsonl_parsers_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project" / "malformed.jsonl"
            path.parent.mkdir()
            path.write_text("not-json\n{truncated\n", encoding="utf-8")

            parsers = (
                ai_assets._parse_claude_usage_file,
                ai_assets._parse_gemini_usage_file,
                ai_assets._parse_codex_usage_file,
            )
            for parser in parsers:
                with self.subTest(parser=parser.__name__), self.assertRaisesRegex(
                    ValueError,
                    "no valid JSON records",
                ):
                    parser(path, "fixture")

    def test_parser_version_bump_revalidates_old_ready_cache(self):
        from data_foundation.db import connect, migrate
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara")
            malformed_path = root / "malformed.jsonl"
            malformed_path.write_text("not-json\n{truncated\n", encoding="utf-8")
            source = {
                "tool": "Claude Code",
                "path": malformed_path,
                "sessionId": "malformed",
                "usageGroup": "fixture",
                "sessionCountUnit": 1,
            }
            stat_result = malformed_path.stat()
            migrate(paths)
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO ai_asset_usage_source_files(
                        tool_name, source_path, file_mtime, file_size, session_id,
                        usage_group, session_count_unit, parser_version, parsed_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                    """,
                    (
                        "Claude Code",
                        str(malformed_path),
                        stat_result.st_mtime,
                        stat_result.st_size,
                        "malformed",
                        "fixture",
                        1,
                        "ai-assets-usage-cache-v8",
                        "2026-07-11T12:00:00+00:00",
                    ),
                )

            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_iter_incremental_usage_sources", return_value=[source]),
            ):
                _entries, _counts, stats = ai_assets._scan_usage_incremental()

        self.assertEqual(ai_assets.AI_ASSET_USAGE_PARSER_VERSION, "ai-assets-usage-cache-v9")
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(stats["errors"], 1)

    def test_hermes_scan_propagates_database_errors_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hermes-state.db"
            db_path.write_bytes(b"not-a-sqlite-database")
            with patch.object(ai_assets, "_external_tool_path", return_value=db_path):
                with self.assertRaises(sqlite3.DatabaseError):
                    ai_assets._scan_all_hermes()

            connection = MagicMock()
            connection.cursor.return_value.execute.side_effect = sqlite3.OperationalError("fixture read failed")
            with (
                patch.object(ai_assets, "_external_tool_path", return_value=db_path),
                patch.object(ai_assets.sqlite3, "connect", return_value=connection),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    ai_assets._scan_all_hermes()
            connection.close.assert_called_once_with()

    def test_real_malformed_incremental_sources_degrade_snapshot_and_preserve_ready_source(self):
        from data_foundation.db import connect
        from data_foundation.paths import initialize_home
        from data_foundation.snapshots import write_dashboard_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara")
            source_root = root / "claude-project"
            source_root.mkdir()
            ready_path = source_root / "ready.jsonl"
            ready_path.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-07-11T12:00:00Z",
                        "message": {
                            "model": "fixture-model",
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "cache_read_input_tokens": 1,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            malformed_path = source_root / "malformed.jsonl"
            malformed_path.write_text("not-json\n{truncated\n", encoding="utf-8")
            malformed_hermes = root / "hermes-state.db"
            malformed_hermes.write_bytes(b"not-a-sqlite-database")
            sources = [
                {
                    "tool": "Claude Code",
                    "path": ready_path,
                    "sessionId": "ready",
                    "usageGroup": "fixture",
                    "sessionCountUnit": 1,
                },
                {
                    "tool": "Claude Code",
                    "path": malformed_path,
                    "sessionId": "malformed",
                    "usageGroup": "fixture",
                    "sessionCountUnit": 1,
                },
            ]

            with ExitStack() as stack:
                stack.enter_context(patch.object(ai_assets, "load_paths", return_value=paths))
                stack.enter_context(patch.object(ai_assets, "_iter_incremental_usage_sources", return_value=sources))
                stack.enter_context(patch.object(ai_assets, "_external_tool_path", return_value=malformed_hermes))
                stack.enter_context(patch.object(ai_assets, "_get_agents_enhanced", return_value=[]))
                stack.enter_context(patch.object(ai_assets, "_aggregate_by_workspace", return_value=[]))
                stack.enter_context(patch.object(ai_assets, "_workspace_attribution_qa", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_foundation_active_day_count", return_value=9))
                stack.enter_context(patch.object(ai_assets, "_get_diary_stats", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_memory_stats", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_skills_stats", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_git_stats", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_cron_job_stats", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_detailed_storage", return_value={"tools": [], "categories": []}))
                stack.enter_context(patch.object(ai_assets, "_get_infrastructure", return_value={}))
                stack.enter_context(patch.object(ai_assets, "_get_tool_configs", return_value=[]))
                stack.enter_context(patch.object(ai_assets, "_get_30day_trend", return_value=[]))
                stack.enter_context(patch.object(ai_assets, "_aggregate_by_model", return_value=[]))
                stack.enter_context(patch.object(ai_assets, "_get_agent_tree", return_value={}))
                stack.enter_context(
                    patch.object(
                        ai_assets,
                        "_current_external_tool_detection",
                        return_value={"detectedToolKeys": ["claudeCode"]},
                    )
                )
                payload = ai_assets.get_ai_assets_incremental(include_rag=False)

            claude = next(tool for tool in payload["tools"] if tool["name"] == "Claude Code")
            self.assertEqual(payload["usageCache"]["errors"], 2)
            self.assertEqual(payload["usageCache"]["parserVersion"], "ai-assets-usage-cache-v9")
            self.assertEqual(claude["allTimeTokens"], 6)
            self.assertEqual(claude["sessionCount"], 1)
            with connect(paths, read_only=True) as connection:
                cached_sources = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT source_path, status FROM ai_asset_usage_source_files ORDER BY source_path"
                    )
                ]
            self.assertEqual(cached_sources, [{"source_path": str(ready_path), "status": "ready"}])

            write_dashboard_snapshot(paths, payload, source_run_id=None)
            with (
                patch.object(ai_assets, "load_paths", return_value=paths),
                patch.object(ai_assets, "_workspace_dir", return_value=root),
                patch.object(
                    ai_assets,
                    "_current_external_tool_detection",
                    return_value={"detectedToolKeys": ["claudeCode"]},
                ),
            ):
                result = ai_assets._get_ai_assets_foundation()

        self.assertEqual(result["dashboardState"]["status"], "degraded")
        self.assertEqual(
            result["dashboardState"]["sourceErrors"],
            [
                {
                    "source": "ai-assets-usage-cache",
                    "code": "incremental-source-read-failed",
                    "retryable": True,
                }
            ],
        )
        self.assertEqual(result["usageCache"]["errors"], 2)
        self.assertEqual(result["totalTokens"], 6)
        self.assertEqual(result["activeDayCount"], 9)
        self.assertEqual(next(tool for tool in result["tools"] if tool["name"] == "Claude Code")["allTimeTokens"], 6)

    def test_router_failure_is_redacted_dashboard_error_envelope(self):
        marker = "secret-token=do-not-leak /Users/operator/private.sqlite3"
        with (
            patch.object(ai_assets_router.ai_assets, "get_ai_assets_cached", side_effect=RuntimeError(marker)),
            patch.object(ai_assets_router.logger, "exception") as logged,
        ):
            result = asyncio.run(ai_assets_router.api_ai_assets())

        logged.assert_called_once()
        self.assertEqual(result["dashboardState"]["status"], "error")
        self.assertEqual(result["error"], "ai-assets error")
        self.assertEqual(result["activeDayCount"], 0)
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
