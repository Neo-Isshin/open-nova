import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import agents as agents_service
from app.services import ai_assets
from app.services import token_clock, tokens
from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings


def _detected(*tool_ids: str) -> dict:
    return {"detectedToolKeys": list(tool_ids)}


class DashboardLiveTokenSemanticsTests(unittest.TestCase):
    def test_token_clock_uses_foundation_protocol_total(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=token_clock.local_timezone())

        def scanner(_today, _hour):
            return [
                {
                    "input": 10,
                    "output": 2,
                    "cacheRead": 3,
                    "cacheWrite": 500,
                    "timestamp": "2026-05-19T04:00:00Z",
                    "usageGroup": "fixture",
                }
            ]

        with (
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(token_clock, "_SCANNERS", [("OpenClaw", scanner)]),
            patch.object(token_clock, "detect_external_tools", return_value=_detected("openclaw")),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual(data["totalTokens"], 15)
        self.assertEqual(data["tools"][0]["legacyOperationalTokens"], 515)
        self.assertEqual(data["tools"][0]["cacheWrite"], 500)
        self.assertEqual(data["overallCacheRate"], 23.1)
        self.assertEqual(data["semantics"]["source"], "foundation-protocol")
        self.assertFalse(data["semantics"]["legacyLive"])
        self.assertTrue(data["semantics"]["live"])
        self.assertEqual(data["semantics"]["tokenTotalFormula"], "input + output + cacheRead")
        self.assertEqual(data["semantics"]["foundationProtocolTotalFormula"], "input + output + cacheRead")
        self.assertEqual(data["semantics"]["legacyOperationalTotalFormula"], "input + output + cacheRead + cacheWrite")
        self.assertEqual(data["semantics"]["dayBoundary"], "Asia/Hong_Kong business day 04:00-03:59")

    def test_token_clock_reports_degraded_scanner_errors_without_losing_other_sources(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=token_clock.local_timezone())

        def good_scanner(today, _hour):
            self.assertEqual(today, "2026-05-19")
            return [
                {
                    "input": 6,
                    "output": 3,
                    "cacheRead": 1,
                    "timestamp": "2026-05-19T04:00:00Z",
                    "usageGroup": "included",
                }
            ]

        def broken_scanner(_today, _hour):
            raise RuntimeError("codex session unreadable")

        with (
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(token_clock, "_SCANNERS", [("OpenClaw", good_scanner), ("Codex", broken_scanner)]),
            patch.object(
                token_clock,
                "detect_external_tools",
                return_value=_detected("openclaw", "codex"),
            ),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual(data["totalTokens"], 10)
        self.assertTrue(data["degraded"])
        self.assertEqual(
            data["sourceErrors"],
            [{"source": "Codex", "code": "source-read-failed", "retryable": True}],
        )
        self.assertEqual(data["dashboardState"]["status"], "degraded")
        self.assertEqual([tool["name"] for tool in data["tools"]], ["OpenClaw", "Codex"])
        self.assertEqual(data["tools"][1]["tokens"], 0)

    def test_token_clock_semantics_report_configured_timezone(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=token_clock.local_timezone())
        with (
            patch.dict(os.environ, {"TARGET_TIMEZONE": "UTC"}, clear=False),
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(token_clock, "_SCANNERS", []),
            patch.object(token_clock, "detect_external_tools", return_value=_detected()),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual(data["semantics"]["dayBoundary"], "UTC business day 04:00-03:59")

    def test_token_clock_uses_0400_business_day_before_cutoff(self):
        fixed_now = datetime(2026, 5, 20, 3, 30, 0, tzinfo=token_clock.local_timezone())

        def scanner(today, _hour):
            self.assertEqual(today, "2026-05-19")
            return [
                {
                    "input": 4,
                    "output": 1,
                    "cacheRead": 0,
                    "timestamp": "2026-05-19T19:30:00Z",
                    "usageGroup": "included",
                },
                {
                    "input": 99,
                    "output": 1,
                    "cacheRead": 0,
                    "timestamp": "2026-05-18T19:59:59Z",
                    "usageGroup": "previous",
                },
            ]

        with (
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(token_clock, "_SCANNERS", [("OpenClaw", scanner)]),
            patch.object(token_clock, "detect_external_tools", return_value=_detected("openclaw")),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual(data["today"], "2026-05-19")
        self.assertEqual(data["totalTokens"], 5)
        self.assertEqual(data["workspaceUsage"][0]["name"], "included")

    def test_token_clock_hides_tool_infra_workspace_names(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=token_clock.local_timezone())

        def scanner(_today, _hour):
            rows = []
            for group in ("actanara", "nvm", "memories", ".opencode", "homebrew", "home", "SSD", "default", "Codex"):
                rows.append(
                    {
                        "input": 4,
                        "output": 1,
                        "cacheRead": 0,
                        "timestamp": "2026-05-19T04:00:00Z",
                        "usageGroup": group,
                    }
                )
            return rows

        with (
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(token_clock, "_SCANNERS", [("Codex", scanner)]),
            patch.object(token_clock, "detect_external_tools", return_value=_detected("codex")),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual([item["name"] for item in data["workspaceUsage"]], ["actanara"])

    def test_token_clock_runs_only_detected_usage_capable_scanners_and_keeps_zero_card(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=token_clock.local_timezone())
        calls = []

        def opencode_scanner(_today, _hour):
            calls.append("OpenCode")
            return []

        def must_not_run(_today, _hour):
            self.fail("undetected or usage-unavailable scanner executed")

        with (
            patch.object(token_clock, "_now_local", return_value=fixed_now),
            patch.object(
                token_clock,
                "_SCANNERS",
                [
                    ("OpenCode", opencode_scanner),
                    ("Antigravity", must_not_run),
                    ("Cursor", must_not_run),
                ],
            ),
            patch.object(
                token_clock,
                "detect_external_tools",
                return_value=_detected("opencode", "cursor"),
            ),
        ):
            data = token_clock.get_token_clock_data()

        self.assertEqual(calls, ["OpenCode"])
        self.assertEqual([tool["name"] for tool in data["tools"]], ["OpenCode"])
        self.assertEqual(data["tools"][0]["tokens"], 0)
        self.assertEqual(data["tools"][0]["messages"], 0)

    def test_token_clock_codex_normalizes_cached_input_when_reported_total_excludes_cache_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            sessions = root / "configured-tools" / "codex" / "sessions"
            sessions.mkdir(parents=True)
            fixture = sessions / "rollout-cached-input.jsonl"
            _write_codex_fixture(fixture, include_reported_total=True)
            write_settings({"externalTools": {"codex": {"sessionsRoot": str(sessions)}}}, paths)

            token_clock._file_cache = {}
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                entries = token_clock._scan_codex("2026-05-19", 12)
                stats = token_clock._aggregate(token_clock._filter_today(entries, "2026-05-19"), 12)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rawInput"], 10)
        self.assertEqual(entries[0]["input"], 7)
        self.assertEqual(entries[0]["output"], 2)
        self.assertEqual(entries[0]["cacheRead"], 3)
        self.assertEqual(entries[0]["cacheInputSemantics"], "input_includes_cached_input")
        self.assertEqual(stats["tokens"], 12)

    def test_dashboard_live_collectors_use_configured_external_tool_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            agents_dir = root / "configured-tools" / "openclaw" / "agents"
            agents_dir.mkdir(parents=True)
            codex_sessions = root / "configured-tools" / "codex" / "sessions"
            codex_sessions.mkdir(parents=True)
            write_settings(
                {
                    "externalTools": {
                        "openclaw": {"agentsRoot": str(agents_dir)},
                        "codex": {"sessionsRoot": str(codex_sessions)},
                    }
                },
                paths,
            )

            agents_service.AGENTS_DIR = None
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                token_path = token_clock._external_tool_path("codex", "sessionsRoot")
                agent_path = agents_service._agents_dir()

        self.assertEqual(token_path, codex_sessions.absolute())
        self.assertEqual(agent_path, agents_dir.absolute())

    def test_token_clock_tool_emojis_match_ai_assets(self):
        live = {item["name"]: item["emoji"] for item in token_clock.TOOL_DEFS}
        assets = {item["name"]: item["emoji"] for item in ai_assets.TOOL_DEFS}

        self.assertEqual(live, assets)

    def test_token_clock_project_references_include_ssd_dev_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "custom-project-parent" / "actanara"
            file_path = project_root / "src" / "dashboard" / "app.py"
            file_path.parent.mkdir(parents=True)
            file_path.write_text("# fixture\n", encoding="utf-8")
            (project_root / "pyproject.toml").write_text('[project]\nname = "actanara"\n', encoding="utf-8")

            project = token_clock._primary_referenced_project(f"editing {file_path}")

        self.assertEqual(project, "actanara")

    def test_token_clock_usage_group_helper_uses_shared_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "work" / "TokenClock"
            nested = project_root / "Sources"
            nested.mkdir(parents=True)
            (project_root / ".git").mkdir()
            openclaw_path = Path(tmp) / ".openclaw" / "agents" / "design-agent" / "sessions" / "session.jsonl"

            openclaw_group = token_clock._usage_group_for_source(
                "openclaw",
                raw_path=openclaw_path,
                fallback="agents",
            )
            codex_group = token_clock._usage_group_for_source("codex", cwd=nested)

        self.assertEqual(openclaw_group, "design-agent")
        self.assertEqual(codex_group, "TokenClock")

    def test_token_clock_hermes_uses_visible_fallback_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hermes-state.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TABLE sessions ("
                    "started_at TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                    "cache_read_tokens INTEGER, cache_write_tokens INTEGER, message_count INTEGER)"
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                    ("2026-05-19T04:00:00Z", 10, 2, 3, 4, 5),
                )
                conn.commit()
            finally:
                conn.close()

            token_clock._db_cache = {}
            with patch.object(token_clock, "_external_tool_path", return_value=db_path):
                entries = token_clock._scan_hermes("2026-05-19", 12)

        self.assertEqual(entries[0]["usageGroup"], "Hermes")
        entries[0].update(
            {
                "_dt": datetime(2026, 5, 19, 12, 0, tzinfo=token_clock.local_timezone()),
                "_hour": 12,
                "_recent": True,
            }
        )
        usage = token_clock._aggregate_workspace_usage([("Hermes", entries, {})], 12)
        self.assertEqual([row["name"] for row in usage], ["Hermes"])

    def test_token_clock_codex_runtime_source_cwd_uses_pyproject_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            sessions = root / "configured-tools" / "codex" / "sessions"
            sessions.mkdir(parents=True)
            runtime_source = root / ".actanara" / "app" / "source"
            runtime_source.mkdir(parents=True)
            (runtime_source / "pyproject.toml").write_text('[project]\nname = "actanara"\n', encoding="utf-8")
            fixture = sessions / "rollout-runtime-source.jsonl"
            _write_codex_fixture(fixture, include_reported_total=False, cwd=str(runtime_source))
            write_settings({"externalTools": {"codex": {"sessionsRoot": str(sessions)}}}, paths)

            token_clock._file_cache = {}
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                entries = token_clock._scan_codex("2026-05-19", 12)

        self.assertEqual(entries[0]["usageGroup"], "actanara")

    def test_token_clock_codex_skips_files_older_than_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            sessions = root / "configured-tools" / "codex" / "sessions"
            sessions.mkdir(parents=True)
            old_file = sessions / "rollout-old.jsonl"
            _write_codex_fixture(old_file, include_reported_total=False)
            old_mtime = time.mktime(datetime(2026, 5, 18, 1, 0, 0).timetuple())
            os.utime(old_file, (old_mtime, old_mtime))
            write_settings({"externalTools": {"codex": {"sessionsRoot": str(sessions)}}}, paths)

            token_clock._file_cache = {}
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                entries = token_clock._scan_codex("2026-05-19", 12)

        self.assertEqual(entries, [])

    def test_realtime_tokens_use_foundation_protocol_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            sessions_dir = agents_dir / "fixture-agent" / "sessions"
            sessions_dir.mkdir(parents=True)
            payload = {
                "type": "message",
                "timestamp": "2026-05-19T04:00:00Z",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 500},
                },
            }
            (sessions_dir / "session.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            tokens._CACHE = {}
            tokens._LAST_MTIME = 0.0
            with (
                patch.object(tokens, "AGENTS_DIR", agents_dir),
                patch("app.services.tz.hkt_now", return_value=datetime(2026, 5, 19, 12, 0, 0)),
            ):
                data = tokens.compute_summary()

        self.assertEqual(data["today"]["fixture-agent"]["total"], 15)
        self.assertEqual(data["today"]["fixture-agent"]["promptTotal"], 513)
        self.assertEqual(data["today"]["fixture-agent"]["legacyOperationalTotal"], 515)
        self.assertEqual(data["week"]["2026-05-19:fixture-agent"], 15)
        self.assertEqual(data["summary"]["total"], 15)
        self.assertEqual(data["semantics"]["source"], "foundation-protocol")
        self.assertFalse(data["semantics"]["legacyLive"])
        self.assertTrue(data["semantics"]["live"])
        self.assertEqual(data["semantics"]["tokenTotalFormula"], "input + output + cacheRead")
        self.assertEqual(data["semantics"]["foundationProtocolTotalFormula"], "input + output + cacheRead")
        self.assertEqual(data["semantics"]["promptTokenFormula"], "input + cacheRead + cacheWrite")
        self.assertEqual(data["semantics"]["legacyOperationalTotalFormula"], "input + output + cacheRead + cacheWrite")
        self.assertEqual(data["semantics"]["dayBoundary"], "configured business timezone 04:00-03:59 via app.services.tz")

    def test_realtime_tokens_use_business_day_before_hkt_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            sessions_dir = agents_dir / "fixture-agent" / "sessions"
            sessions_dir.mkdir(parents=True)
            payload = {
                "type": "message",
                "timestamp": "2026-05-19T19:30:00Z",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 0},
                },
            }
            (sessions_dir / "session.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            tokens._CACHE = {}
            tokens._LAST_MTIME = 0.0
            with (
                patch.object(tokens, "AGENTS_DIR", agents_dir),
                patch("app.services.tz.hkt_now", return_value=datetime(2026, 5, 20, 3, 30, 0)),
                patch("app.services.tz.hkt_today", return_value=date(2026, 5, 19)),
            ):
                data = tokens.compute_summary()

        self.assertEqual(data["today"]["fixture-agent"]["total"], 15)
        self.assertEqual(data["week"]["2026-05-19:fixture-agent"], 15)
        self.assertEqual(data["summary"]["total"], 15)

    def test_realtime_tokens_use_configured_openclaw_agents_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            agents_dir = root / "configured-tools" / "openclaw" / "agents"
            sessions_dir = agents_dir / "fixture-agent" / "sessions"
            sessions_dir.mkdir(parents=True)
            payload = {
                "type": "message",
                "timestamp": "2026-06-11T04:00:00Z",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 0},
                },
            }
            (sessions_dir / "session.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
            write_settings({"externalTools": {"openclaw": {"agentsRoot": str(agents_dir)}}}, paths)

            tokens.AGENTS_DIR = tokens._DEFAULT_AGENTS_DIR
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch("app.services.tz.hkt_today", return_value=date(2026, 6, 11)),
            ):
                data = tokens.parse_by_date(days=30)

        self.assertEqual(data["2026-06-11:fixture-agent"], 15)

def _write_codex_fixture(path: Path, *, include_reported_total: bool, cwd: str = "/tmp/actanara") -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 3,
        "reasoning_output_tokens": 4,
    }
    if include_reported_total:
        usage["total_tokens"] = 12
    rows = [
        {"type": "session_meta", "payload": {"id": "codex-session", "cwd": cwd}},
        {"timestamp": "2026-05-19T04:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
