import asyncio
import importlib.machinery
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


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

    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.__spec__ = importlib.machinery.ModuleSpec("fastapi", loader=None)
    fastapi_stub.APIRouter = lambda: _RouterStub()
    fastapi_stub.Request = object
    responses_stub = types.ModuleType("fastapi.responses")
    responses_stub.__spec__ = importlib.machinery.ModuleSpec("fastapi.responses", loader=None)
    responses_stub.JSONResponse = object
    responses_stub.StreamingResponse = object
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub


from app.routers import metrics
from app.services import diary, nova_task_review, token_clock, tokens
from app.services.dashboard_state import (
    attach_dashboard_state,
    dashboard_failure,
    state_envelope,
)


def _detected(*tool_ids: str) -> dict:
    return {"detectedToolKeys": list(tool_ids)}


class DashboardStateTests(unittest.TestCase):
    def test_state_helpers_normalize_errors_without_raw_exception_details(self):
        marker = "secret-token=do-not-leak /Users/operator/private.db"

        state = state_envelope(
            "degraded",
            source_errors=[
                {
                    "source": "Hermes",
                    "code": "sqlite-read-failed",
                    "retryable": False,
                    "error": marker,
                },
                marker,
            ],
        )
        payload = attach_dashboard_state(
            {"items": [{"id": "preserved"}]},
            source_errors=[{"id": "diary", "error": marker}],
        )
        failure = dashboard_failure(
            "token-summary",
            fallback={"summary": {}, "diagnostic": None},
        )

        self.assertEqual(
            state,
            {
                "schemaVersion": 1,
                "status": "degraded",
                "sourceErrors": [
                    {
                        "source": "Hermes",
                        "code": "sqlite-read-failed",
                        "retryable": False,
                    }
                ],
            },
        )
        self.assertEqual(payload["dashboardState"]["status"], "degraded")
        self.assertEqual(payload["items"], [{"id": "preserved"}])
        self.assertEqual(failure["dashboardState"]["status"], "error")
        self.assertEqual(failure["error"], "token-summary error")
        self.assertNotIn(marker, json.dumps([state, payload, failure]))
        with self.assertRaises(ValueError):
            state_envelope("silently-unknown")

    def test_real_malformed_hermes_sqlite_degrades_without_losing_other_source(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)

        def good_scanner(_today, _hour):
            return [
                {
                    "input": 6,
                    "output": 3,
                    "cacheRead": 1,
                    "timestamp": "2026-05-19T04:00:00Z",
                    "usageGroup": "preserved-source",
                }
            ]

        with tempfile.TemporaryDirectory() as tmp:
            malformed_db = Path(tmp) / "hermes-state.db"
            malformed_db.write_bytes(b"not-a-sqlite-database")
            token_clock._db_cache.pop(str(malformed_db), None)
            with (
                patch.object(token_clock, "local_timezone", return_value=timezone.utc),
                patch.object(token_clock, "_now_local", return_value=fixed_now),
                patch.object(
                    token_clock,
                    "_SCANNERS",
                    [("OpenClaw", good_scanner), ("Hermes", token_clock._scan_hermes)],
                ),
                patch.object(
                    token_clock,
                    "detect_external_tools",
                    return_value=_detected("openclaw", "hermes"),
                ),
                patch.object(token_clock, "_external_tool_path", return_value=malformed_db),
            ):
                result = token_clock.get_token_clock_data()

        self.assertEqual(result["totalTokens"], 10)
        self.assertEqual(result["workspaceUsage"][0]["name"], "preserved-source")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["dashboardState"]["status"], "degraded")
        self.assertEqual(
            result["dashboardState"]["sourceErrors"],
            [{"source": "Hermes", "code": "source-read-failed", "retryable": True}],
        )
        self.assertEqual([tool["name"] for tool in result["tools"]], ["OpenClaw", "Hermes"])
        self.assertEqual(result["tools"][1]["tokens"], 0)

    def test_token_clock_file_scanners_report_io_failure_instead_of_false_empty(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        scanners = (
            ("OpenClaw", token_clock._scan_openclaw, "a/sessions/12345678-1234-1234-1234-123456789abc.jsonl"),
            ("Claude Code", token_clock._scan_claude_code, "project/unreadable.jsonl"),
            ("Gemini CLI", token_clock._scan_gemini, "session-unreadable.jsonl"),
            ("Codex", token_clock._scan_codex, "rollout-unreadable.jsonl"),
        )
        for name, scanner, relative in scanners:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / relative).mkdir(parents=True)
                token_clock._file_cache = {}
                with (
                    patch.object(token_clock, "local_timezone", return_value=timezone.utc),
                    patch.object(token_clock, "_now_local", return_value=fixed_now),
                    patch.object(token_clock, "_external_tool_path", return_value=root),
                    patch.object(token_clock, "_SCANNERS", [(name, scanner)]),
                    patch.object(
                        token_clock,
                        "detect_external_tools",
                        return_value=_detected(token_clock._TOOL_ID_BY_NAME[name]),
                    ),
                ):
                    result = token_clock.get_token_clock_data()

                self.assertTrue(result["degraded"])
                self.assertEqual(result["dashboardState"]["status"], "degraded")
                self.assertEqual(result["sourceErrors"][0]["source"], name)

    def test_token_clock_all_malformed_jsonl_degrades_but_truncated_tail_is_tolerated(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "a" / "sessions" / "12345678-1234-1234-1234-123456789abc.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("not-json\n{truncated\n", encoding="utf-8")
            token_clock._file_cache = {}
            with (
                patch.object(token_clock, "local_timezone", return_value=timezone.utc),
                patch.object(token_clock, "_now_local", return_value=fixed_now),
                patch.object(token_clock, "_external_tool_path", return_value=root),
                patch.object(token_clock, "_SCANNERS", [("OpenClaw", token_clock._scan_openclaw)]),
                patch.object(token_clock, "detect_external_tools", return_value=_detected("openclaw")),
            ):
                malformed = token_clock.get_token_clock_data()

            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-05-19T08:00:00Z",
                        "message": {
                            "role": "assistant",
                            "model": "fixture",
                            "usage": {"input": 3, "output": 2, "cacheRead": 1},
                        },
                    }
                )
                + "\n{truncated-tail\n",
                encoding="utf-8",
            )
            token_clock._file_cache = {}
            with (
                patch.object(token_clock, "local_timezone", return_value=timezone.utc),
                patch.object(token_clock, "_now_local", return_value=fixed_now),
                patch.object(token_clock, "_external_tool_path", return_value=root),
                patch.object(token_clock, "_SCANNERS", [("OpenClaw", token_clock._scan_openclaw)]),
                patch.object(token_clock, "detect_external_tools", return_value=_detected("openclaw")),
            ):
                recovered = token_clock.get_token_clock_data()

        self.assertEqual(malformed["dashboardState"]["status"], "degraded")
        self.assertEqual(malformed["sourceErrors"][0]["source"], "OpenClaw")
        self.assertEqual(recovered["dashboardState"]["status"], "ready")
        self.assertEqual(recovered["totalTokens"], 6)

    def test_token_summary_real_file_io_failure_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / "agents"
            (agents / "fixture" / "sessions" / "unreadable.jsonl").mkdir(parents=True)
            tokens._CACHE = {}
            tokens._LAST_MTIME = 0.0
            with (
                patch.object(tokens, "_agents_dir", return_value=agents),
                patch.object(metrics.logger, "exception"),
            ):
                result = asyncio.run(metrics.api_tokens())

        self.assertEqual(result["dashboardState"]["status"], "error")
        self.assertEqual(result["error"], "token-summary error")

    def test_metrics_complete_failure_uses_stable_redacted_error_envelope(self):
        marker = "secret-token=do-not-leak /Users/operator/private.db"
        with (
            patch.object(metrics.tokens, "compute_summary", side_effect=RuntimeError(marker)),
            patch.object(metrics.logger, "exception") as logged,
        ):
            result = asyncio.run(metrics.api_tokens())

        logged.assert_called_once()
        self.assertEqual(result["today"], {})
        self.assertEqual(result["week"], {})
        self.assertEqual(result["summary"], {})
        self.assertEqual(result["error"], "token-summary error")
        self.assertEqual(result["dashboardState"]["status"], "error")
        self.assertEqual(
            result["dashboardState"]["sourceErrors"],
            [{"source": "token-summary", "code": "source-read-failed", "retryable": True}],
        )
        self.assertNotIn(marker, json.dumps(result))

    def test_task_board_distinguishes_enabled_empty_from_disabled_unavailable(self):
        with patch.object(nova_task_review, "tree", return_value={"roots": [], "nodes": []}):
            enabled = nova_task_review.task_board_payload(enabled=True)
        disabled = nova_task_review.task_board_payload(enabled=False)

        self.assertTrue(enabled["novaTaskEnabled"])
        self.assertEqual(enabled["dashboardState"]["status"], "empty")
        self.assertEqual(enabled["tree"], [])
        self.assertEqual(enabled["nodes"], [])
        self.assertFalse(disabled["enabled"])
        self.assertFalse(disabled["novaTaskEnabled"])
        self.assertEqual(disabled["dashboardState"]["status"], "unavailable")

    def test_diary_read_error_is_not_reported_as_true_missing_projection(self):
        marker = "secret-token=do-not-leak /Users/operator/private.db"
        with (
            patch.object(diary, "load_paths", return_value=object()),
            patch.object(diary, "_pipeline_language_profile", return_value="zh"),
            patch.object(diary, "read_diary_markdown_documents", side_effect=sqlite3.OperationalError(marker)),
        ):
            failed = diary.get_diary_page("2026-05-19")

        with (
            patch.object(diary, "load_paths", return_value=object()),
            patch.object(diary, "_pipeline_language_profile", return_value="zh"),
            patch.object(diary, "read_diary_markdown_documents", return_value=[]),
        ):
            missing = diary.get_diary_page("2026-05-19")

        failed_freshness = failed["dataFreshness"]["diaryPage"]
        missing_freshness = missing["dataFreshness"]["diaryPage"]
        self.assertEqual(failed["dashboardState"]["status"], "error")
        self.assertEqual(failed_freshness["status"], "source_error")
        self.assertFalse(failed_freshness["refreshRequired"])
        self.assertEqual(missing["dashboardState"]["status"], "empty")
        self.assertEqual(missing_freshness["status"], "projection_missing")
        self.assertTrue(missing_freshness["refreshRequired"])
        self.assertNotIn(marker, json.dumps(failed))

    def test_diary_list_partial_foundation_failure_preserves_items_as_degraded(self):
        marker = "secret-token=do-not-leak /Users/operator/private.db"
        with tempfile.TemporaryDirectory() as tmp:
            diary_root = Path(tmp) / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "日记-260620.md").write_text("# retained diary\n", encoding="utf-8")
            with (
                patch.object(diary, "_diary_root", return_value=diary_root),
                patch.object(diary, "_pipeline_language_profile", return_value="zh"),
                patch.object(diary, "load_paths", return_value=object()),
                patch.object(diary, "connect", side_effect=sqlite3.OperationalError(marker)),
            ):
                result = diary.get_diary_list(include_state=True)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["fullDate"], "2026-06-20")
        self.assertEqual(result["items"][0]["filename"], "日记-260620.md")
        self.assertEqual(result["dashboardState"]["status"], "degraded")
        self.assertEqual(
            result["dashboardState"]["sourceErrors"],
            [
                {
                    "source": "foundation-daily-tool-usage",
                    "code": "source-read-failed",
                    "retryable": True,
                }
            ],
        )
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
