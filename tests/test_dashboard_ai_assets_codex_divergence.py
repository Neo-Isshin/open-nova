import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import ai_assets
from data_foundation.adapters.base import SourceArtifact
from data_foundation.adapters.usage import CodexAdapter
from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings


class DashboardAiAssetsCodexDivergenceTests(unittest.TestCase):
    def test_codex_cached_input_uses_foundation_protocol_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            fixture = sessions / "rollout-cached-input.jsonl"
            _write_codex_fixture(fixture)

            with (
                patch.object(ai_assets, "HOME", root),
                patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0),
                patch.object(ai_assets, "_now_local", return_value=datetime(2026, 5, 19, 12, 0, tzinfo=ai_assets.TZ)),
            ):
                entries, session_count = ai_assets._scan_all_codex()
                aggregate = ai_assets._aggregate_tool("Codex", entries, session_count)
                models = ai_assets._aggregate_by_model({"Codex": entries})
                workspaces = ai_assets._aggregate_by_workspace({"Codex": entries})
                trend = ai_assets._get_30day_trend({"Codex": entries})

            self.assertEqual(session_count, 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["input"], 10)
            self.assertEqual(entries[0]["output"], 2)
            self.assertEqual(entries[0]["cacheRead"], 3)
            self.assertEqual(entries[0]["cacheWrite"], 0)
            self.assertEqual(entries[0]["reasoning"], 4)
            self.assertEqual(entries[0]["legacyInputWithCache"], 13)
            self.assertEqual(entries[0]["legacyOutputWithReasoning"], 6)
            self.assertEqual(entries[0]["usageGroup"], "actanara")

            self.assertEqual(aggregate["allTimeTokens"], 15)
            self.assertEqual(models[0]["tokens"], 15)
            self.assertEqual(workspaces[0]["tokens"], 15)
            trend_day = next(day for day in trend if day["date"] == "2026-05-19")
            self.assertEqual(sum(trend_day["slots"].values()), 15)

            artifact = SourceArtifact("codex", fixture, "rollout_jsonl")
            foundation_events = list(CodexAdapter(sessions).read_incremental(artifact, None))
            self.assertEqual(len(foundation_events), 1)
            payload = foundation_events[0].payload
            self.assertEqual(payload["input_tokens"], 10)
            self.assertEqual(payload["output_tokens"], 2)
            self.assertEqual(payload["cache_read_tokens"], 3)
            self.assertEqual(payload["reasoning_tokens"], 4)
            self.assertEqual(payload["input_tokens"] + payload["output_tokens"] + payload["cache_read_tokens"], 15)
            self.assertEqual(payload["metadata"]["token_semantics"], "last_token_usage")

    def test_codex_reported_total_prevents_cached_input_double_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            fixture = sessions / "rollout-input-includes-cache.jsonl"
            _write_codex_fixture(fixture, include_reported_total=True)

            with (
                patch.object(ai_assets, "HOME", root),
                patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0),
                patch.object(ai_assets, "_now_local", return_value=datetime(2026, 5, 19, 12, 0, tzinfo=ai_assets.TZ)),
            ):
                entries, session_count = ai_assets._scan_all_codex()
                aggregate = ai_assets._aggregate_tool("Codex", entries, session_count)
                workspaces = ai_assets._aggregate_by_workspace({"Codex": entries})

            self.assertEqual(session_count, 1)
            self.assertEqual(entries[0]["rawInput"], 10)
            self.assertEqual(entries[0]["input"], 7)
            self.assertEqual(entries[0]["output"], 2)
            self.assertEqual(entries[0]["cacheRead"], 3)
            self.assertEqual(entries[0]["cacheInputSemantics"], "input_includes_cached_input")
            self.assertEqual(entries[0]["legacyInputWithCache"], 10)
            self.assertEqual(aggregate["allTimeTokens"], 12)
            self.assertEqual(workspaces[0]["tokens"], 12)

            artifact = SourceArtifact("codex", fixture, "rollout_jsonl")
            foundation_events = list(CodexAdapter(sessions).read_incremental(artifact, None))
            payload = foundation_events[0].payload
            self.assertEqual(payload["input_tokens"], 7)
            self.assertEqual(payload["output_tokens"], 2)
            self.assertEqual(payload["cache_read_tokens"], 3)
            self.assertEqual(payload["input_tokens"] + payload["output_tokens"] + payload["cache_read_tokens"], 12)
            self.assertEqual(payload["metadata"]["raw_input_tokens"], 10)
            self.assertEqual(payload["metadata"]["reported_total_tokens"], 12)
            self.assertEqual(payload["metadata"]["cache_input_semantics"], "input_includes_cached_input")

    def test_model_aggregation_handles_empty_tools_and_counts_sessions_per_model(self):
        models = ai_assets._aggregate_by_model(
            {
                "Empty": [],
                "Codex": [
                    {
                        "model": "gpt-a",
                        "input": 10,
                        "output": 2,
                        "cacheRead": 0,
                        "message_count": 1,
                        "timestamp": "2026-05-19T04:00:00Z",
                    },
                    {
                        "model": "gpt-b",
                        "input": 3,
                        "output": 4,
                        "cacheRead": 0,
                        "message_count": 1,
                        "timestamp": "2026-05-19T05:00:00Z",
                    },
                ],
            }
        )

        by_name = {item["name"]: item for item in models}
        self.assertEqual(by_name["gpt-a"]["tokens"], 12)
        self.assertEqual(by_name["gpt-a"]["sessions"], 1)
        self.assertEqual(by_name["gpt-b"]["tokens"], 7)
        self.assertEqual(by_name["gpt-b"]["sessions"], 1)

    def test_workspace_usage_hides_container_and_tool_buckets(self):
        entries = []
        for group in ("actanara", "DEV", "home", "SSD", "unknown", "Codex", ".codex"):
            entries.append({
                "input": 20_000_000,
                "output": 1,
                "cacheRead": 0,
                "message_count": 1,
                "usageGroup": group,
            })

        workspaces = ai_assets._aggregate_by_workspace({"Codex": entries})

        self.assertEqual([item["name"] for item in workspaces], ["actanara"])

    def test_ai_assets_codex_scan_uses_configured_external_tool_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            sessions = root / "configured-tools" / "codex" / "sessions"
            sessions.mkdir(parents=True)
            fixture = sessions / "rollout-configured.jsonl"
            _write_codex_fixture(fixture)
            write_settings({"externalTools": {"codex": {"sessionsRoot": str(sessions)}}}, paths)

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0),
            ):
                entries, session_count = ai_assets._scan_all_codex()

        self.assertEqual(session_count, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["usageGroup"], "actanara")

    def test_codex_runtime_source_cwd_uses_pyproject_name_not_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            runtime_source = root / ".actanara" / "app" / "source"
            runtime_source.mkdir(parents=True)
            (runtime_source / "pyproject.toml").write_text('[project]\nname = "actanara"\n', encoding="utf-8")
            fixture = sessions / "rollout-runtime-source.jsonl"
            _write_codex_fixture(fixture, cwd=str(runtime_source))

            with (
                patch.object(ai_assets, "HOME", root),
                patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0),
            ):
                entries, session_count = ai_assets._scan_all_codex()

        self.assertEqual(session_count, 1)
        self.assertEqual(entries[0]["usageGroup"], "actanara")

    def test_hermes_scan_preserves_session_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes = root / ".hermes"
            hermes.mkdir()
            db_path = hermes / "state.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE sessions(
                        id TEXT PRIMARY KEY,
                        started_at TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        cache_read_tokens INTEGER,
                        cache_write_tokens INTEGER,
                        message_count INTEGER,
                        model TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("h1", "2026-05-19T04:00:00Z", 10, 2, 3, 0, 1, "MiniMax-M3"),
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(ai_assets, "HOME", root):
                entries, session_count = ai_assets._scan_all_hermes()
                hermes_stat = ai_assets._aggregate_tool("Hermes", entries, session_count)
                tree = ai_assets._get_agent_tree([hermes_stat], {"Hermes": entries})

        self.assertEqual(session_count, 1)
        self.assertEqual(entries[0]["model"], "MiniMax-M3")
        self.assertEqual(entries[0]["usageGroup"], "Hermes")
        models = ai_assets._aggregate_by_model({"Hermes": entries})
        self.assertEqual(models[0]["name"], "MiniMax-M3")
        self.assertEqual(models[0]["tokens"], 15)
        with patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0):
            workspaces = ai_assets._aggregate_by_workspace({"Hermes": entries})
        self.assertEqual([(row["tool"], row["name"]) for row in workspaces], [("Hermes", "Hermes")])
        hermes_tree = next(item for item in tree if item["name"] == "Hermes")
        self.assertEqual(hermes_tree["countLabel"], "agents")
        self.assertEqual([(item["displayName"], item["level"]) for item in hermes_tree["items"]], [("Hermes", "agent")])

    def test_workspace_attribution_qa_flags_hidden_high_token_groups(self):
        entries = {
            "Codex": [
                {
                    "input": 50_000_000,
                    "output": 1,
                    "cacheRead": 0,
                    "message_count": 1,
                    "usageGroup": "home",
                    "usageGroupSource": "cwd",
                    "usageGroupConfidence": "low",
                },
                {
                    "input": 20_000_000,
                    "output": 1,
                    "cacheRead": 0,
                    "message_count": 1,
                    "usageGroup": "actanara",
                    "usageGroupSource": "codex-transcript-path",
                    "usageGroupConfidence": "high",
                },
            ]
        }

        with patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 10_000_000):
            rows = ai_assets._aggregate_by_workspace(entries)
            qa = ai_assets._workspace_attribution_qa(entries, rows)

        self.assertEqual([row["name"] for row in rows], ["actanara"])
        self.assertEqual(qa["status"], "attention")
        self.assertEqual(qa["hiddenTokens"], 50_000_001)
        self.assertEqual(qa["lowConfidenceTokens"], 50_000_001)
        self.assertEqual(qa["codexTranscriptInferredTokens"], 20_000_001)
        self.assertEqual(qa["findings"][0]["id"], "hidden-workspace-usage")

    def test_workspace_attribution_qa_does_not_report_alias_merge_for_canonical_name(self):
        entries = {
            "Codex": [
                {
                    "input": 12_000_000,
                    "output": 0,
                    "cacheRead": 0,
                    "message_count": 1,
                    "usageGroup": "actanara",
                    "usageGroupSource": "cwd",
                    "usageGroupConfidence": "high",
                },
                {
                    "input": 8_000_000,
                    "output": 0,
                    "cacheRead": 0,
                    "message_count": 1,
                    "usageGroup": "actanara",
                    "usageGroupSource": "cwd",
                    "usageGroupConfidence": "high",
                },
            ]
        }

        with patch.object(ai_assets, "WORKSPACE_USAGE_MIN_TOKENS", 0):
            rows = ai_assets._aggregate_by_workspace(entries)
            qa = ai_assets._workspace_attribution_qa(entries, rows)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "actanara")
        self.assertEqual(rows[0]["tokens"], 20_000_000)
        self.assertEqual(qa["status"], "ready")
        self.assertEqual(qa["aliasMergedTokens"], 0)
        self.assertNotIn("canonical-alias-merged", {item["id"] for item in qa["findings"]})
        self.assertNotIn("split-workspace-names", {item["id"] for item in qa["findings"]})

    def test_claude_parser_infers_workspace_from_transcript_when_project_dir_is_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = root / "Volumes" / "Example"
            project = container / "work" / "SampleProject"
            project.mkdir(parents=True)
            (project / "package.json").write_text(json.dumps({"name": "SampleProject"}), encoding="utf-8")
            session = root / ".claude" / "projects" / "-Volumes-Example" / "session.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps({"type": "user", "message": f"edited {project / 'src' / 'main.ts'}"}) + "\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-29T08:00:00Z",
                        "message": {
                            "model": "claude-opus",
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 2,
                                "cache_read_input_tokens": 3,
                                "cache_creation_input_tokens": 0,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            entries = ai_assets._parse_claude_usage_file(session, "Example")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["usageGroup"], "SampleProject")
        self.assertEqual(entries[0]["usageGroupSource"], "claude-transcript-path")
        self.assertEqual(entries[0]["usageGroupConfidence"], "high")

    def test_claude_parser_prefers_transcript_over_low_confidence_dev_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = root / "Users" / "operator" / "work"
            project = container / "SampleProject"
            project.mkdir(parents=True)
            (project / "package.json").write_text(json.dumps({"name": "SampleProject"}), encoding="utf-8")
            session = root / ".claude" / "projects" / "-Users-operator-work" / "session.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps({"type": "user", "message": f"edited {project / 'Sources' / 'SampleProject' / 'AppDelegate.swift'}"}) + "\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-29T08:00:00Z",
                        "message": {
                            "model": "claude-opus",
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 2,
                                "cache_read_input_tokens": 3,
                                "cache_creation_input_tokens": 0,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            entries = ai_assets._parse_claude_usage_file(session, "DEV")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["usageGroup"], "SampleProject")
        self.assertEqual(entries[0]["usageGroupSource"], "claude-transcript-path")
        self.assertEqual(entries[0]["usageGroupConfidence"], "high")

    def test_gemini_parser_infers_workspace_from_transcript_when_label_is_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Volumes" / "Example" / "work" / "actanara"
            project.mkdir(parents=True)
            (project / "pyproject.toml").write_text('[project]\nname = "actanara"\n', encoding="utf-8")
            session = root / ".gemini" / "tmp" / "ssd" / "chats" / "session-test.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps({"type": "user", "content": f"inspect {project / 'src' / 'app.py'}"}) + "\n"
                + json.dumps(
                    {
                        "type": "gemini",
                        "timestamp": "2026-06-29T08:00:00Z",
                        "model": "gemini-2.5-pro",
                        "tokens": {"input": 10, "output": 2, "cached": 3},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            entries = ai_assets._parse_gemini_usage_file(session, "Example")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["usageGroup"], "actanara")
        self.assertEqual(entries[0]["usageGroupSource"], "gemini-transcript-path")
        self.assertEqual(entries[0]["usageGroupConfidence"], "high")

    def test_ai_assets_usage_cache_parser_version_bumped_for_attribution_changes(self):
        self.assertEqual(ai_assets.AI_ASSET_USAGE_PARSER_VERSION, "ai-assets-usage-cache-v9")

    def test_ai_assets_agent_list_uses_latest_usage_model(self):
        fixed_now = datetime(2026, 5, 19, 12, 0, tzinfo=ai_assets.TZ)
        with patch.object(ai_assets, "_now_local", return_value=fixed_now):
            payload = ai_assets._build_ai_assets_payload(
                {
                    "Codex": [
                        {
                            "input": 10,
                            "output": 2,
                            "cacheRead": 3,
                            "timestamp": "2026-05-19T04:00:00Z",
                            "model": "gpt-5.5",
                            "usageGroup": "actanara",
                        }
                    ]
                },
                {"Codex": 1},
                include_rag=False,
                tool_detection={"detectedToolKeys": ["codex"]},
            )

        codex_agent = next(agent for agent in payload["agents"] if agent["name"] == "Codex")
        self.assertEqual(codex_agent["model"], "gpt-5.5")
        self.assertEqual(payload["models"][0]["name"], "gpt-5.5")


def _write_codex_fixture(path: Path, *, include_reported_total: bool = False, cwd: str = "/workspace/example/actanara") -> None:
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
        {
            "timestamp": "2026-05-19T12:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": usage,
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cached_input_tokens": 300,
                        "reasoning_output_tokens": 400,
                    },
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
