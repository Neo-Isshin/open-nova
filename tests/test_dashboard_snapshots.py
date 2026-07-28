import os
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import ai_assets
from data_foundation.db import migrate
from data_foundation.db import connect
from data_foundation.jobs import begin_ingestion_run
from data_foundation.infrastructure import apply_infrastructure_updates
from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings
from data_foundation.snapshots import (
    _foundation_ai_assets_non_rag_payload,
    _foundation_workspace_usage_from_events,
    apply_external_tool_visibility,
    materialize_ai_assets_non_rag_snapshot,
    read_dashboard_snapshot,
    read_rag_daily_status_snapshot,
    write_rag_daily_status_snapshot,
)


def _tool_detection(*tool_ids: str) -> dict:
    return {
        "detectedToolKeys": list(tool_ids),
        "toolPresence": {
            tool_id: {
                "id": tool_id,
                "detected": tool_id in tool_ids,
            }
            for tool_id in (
                "openclaw",
                "claudeCode",
                "codex",
                "geminiCli",
                "hermes",
                "opencode",
                "antigravity",
                "cursor",
            )
        },
    }


class DashboardSnapshotTests(unittest.TestCase):
    def _home(self, root: Path):
        paths = initialize_home(root / "Actanara", legacy_diary_root=root / "legacy")
        migrate(paths)
        run_id = begin_ingestion_run(paths, trigger_type="fixture", business_date=date(2026, 5, 19))
        return paths, run_id

    def test_non_rag_snapshot_round_trips_materialized_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            payload = {
                "tools": [
                    {
                        "name": "Codex",
                        "allTimeTokens": 7,
                        "allTimeMessages": 2,
                        "sessionCount": 1,
                    }
                ],
                "storage": {"tools": [], "categories": []},
            }
            with patch(
                "data_foundation.snapshots.detect_external_tools",
                return_value=_tool_detection("codex"),
            ):
                materialize_ai_assets_non_rag_snapshot(
                    paths,
                    run_id,
                    builder=lambda: payload,
                )
            snapshot = read_dashboard_snapshot(paths)
            self.assertEqual(snapshot["payload"]["tools"], payload["tools"])
            self.assertEqual(snapshot["payload"]["detectedToolKeys"], ["codex"])
            self.assertEqual(snapshot["payload"]["totalTokens"], 7)
            self.assertEqual(snapshot["payload"]["totalMessages"], 2)
            self.assertEqual(snapshot["payload"]["totalSessions"], 1)
            self.assertEqual(snapshot["projectionType"], "foundation-ai-assets-non-rag-v2")

    def test_external_tool_visibility_filters_every_runtime_card_collection(self):
        payload = {
            "tools": [
                {
                    "name": "Codex",
                    "allTimeTokens": 11,
                    "allTimeMessages": 2,
                    "sessionCount": 1,
                },
                {
                    "name": "Cursor",
                    "allTimeTokens": 0,
                    "allTimeMessages": 3,
                    "sessionCount": 2,
                    "usageStatus": "unavailable",
                },
                {
                    "name": "OpenCode",
                    "allTimeTokens": 99,
                    "allTimeMessages": 9,
                    "sessionCount": 4,
                },
            ],
            "agents": [
                {"name": "Codex", "source": "Codex"},
                {"name": "Cursor", "source": "Cursor"},
                {"name": "OpenCode", "source": "OpenCode"},
            ],
            "agentCount": 3,
            "agentTree": [
                {"name": "Codex"},
                {"name": "Cursor"},
                {"name": "OpenCode"},
            ],
            "storage": {
                "tools": [
                    {"name": "Codex"},
                    {"name": "Cursor"},
                    {"name": "OpenCode"},
                ],
                "categories": [{"label": "Diary"}],
            },
            "skills": {
                "byTool": {
                    "Codex": [{"id": "one"}],
                    "Cursor": [{"id": "two"}],
                    "OpenCode": [{"id": "three"}],
                },
                "total": 3,
            },
            "toolConfigs": [
                {"name": "Codex"},
                {"name": "Cursor"},
                {"name": "OpenCode"},
            ],
            "workspaceUsage": [{"tool": "OpenCode", "tokens": 99}],
        }

        filtered = apply_external_tool_visibility(
            payload,
            _tool_detection("codex", "cursor"),
        )

        for field in ("tools", "agents", "agentTree", "toolConfigs"):
            self.assertEqual(
                [item["name"] for item in filtered[field]],
                ["Codex", "Cursor"],
            )
        self.assertEqual(
            [item["name"] for item in filtered["storage"]["tools"]],
            ["Codex", "Cursor"],
        )
        self.assertEqual(
            list(filtered["skills"]["byTool"]),
            ["Codex", "Cursor"],
        )
        self.assertEqual(filtered["skills"]["total"], 2)
        self.assertEqual(filtered["agentCount"], 2)
        self.assertEqual(filtered["totalTokens"], 11)
        self.assertEqual(filtered["totalMessages"], 5)
        self.assertEqual(filtered["totalSessions"], 3)
        self.assertEqual(filtered["tools"][1]["usageStatus"], "unavailable")
        self.assertEqual(filtered["detectedToolKeys"], ["codex", "cursor"])
        self.assertEqual(filtered["workspaceUsage"], payload["workspaceUsage"])

    def test_rag_daily_status_snapshot_round_trips_by_business_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            status = {"entries": 7, "sizeMB": 1.5, "health": "ready", "source": "v2"}
            write_rag_daily_status_snapshot(paths, date(2026, 5, 19), status, source_run_id=run_id)

            snapshot = read_rag_daily_status_snapshot(paths, date(2026, 5, 19))

            self.assertEqual(snapshot["projectionType"], "rag-daily-status-v1")
            self.assertEqual(snapshot["payload"], {"businessDate": "2026-05-19", **status})
            self.assertIsNone(read_rag_daily_status_snapshot(paths, date(2026, 5, 20)))

    def test_non_rag_snapshot_default_builder_uses_full_non_rag_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            expected = {
                "tools": [{"name": "Codex", "allTimeTokens": 42}],
                "models": [{"name": "gpt-test", "tokens": 42}],
                "workspaceUsage": [{"name": "actanara", "tool": "Codex", "emoji": "🤖", "tokens": 42}],
                "agents": [{"displayName": "🤖 Codex", "model": "gpt-test", "source": "Codex"}],
                "agentCount": 1,
                "trend30d": [{"date": "2026-06-05", "slots": {"上午": 42}}],
                "diary": {"count": 1},
                "memory": {"sessionFiles": 1},
                "skills": {"byTool": {"Codex": []}, "total": 0},
                "git": {"commits": 1},
                "cronJobs": {"total": 1},
                "storage": {"tools": [], "categories": []},
                "infrastructure": {"devices": []},
                "toolConfigs": [],
                "agentTree": [],
            }
            rag_status = {"entries": 7, "updatedAt": "2026-06-30 10:00", "health": "ready"}
            storage = {"tools": [], "categories": [{"label": "RAG 索引", "sizeMB": 1.5}]}
            with (
                patch.object(ai_assets, "get_ai_assets_incremental", return_value=dict(expected)) as incremental,
                patch.object(ai_assets, "_get_detailed_storage", return_value=storage) as detailed_storage,
                patch.object(ai_assets, "_get_rag_stats", return_value=rag_status) as rag_stats,
                patch(
                    "data_foundation.snapshots.detect_external_tools",
                    return_value=_tool_detection("codex"),
                ),
            ):
                materialize_ai_assets_non_rag_snapshot(paths, run_id, business_date=date(2026, 5, 19))

            snapshot = read_dashboard_snapshot(paths)
            payload = snapshot["payload"]
            rag_snapshot = read_rag_daily_status_snapshot(paths, date(2026, 5, 19))

            self.assertEqual(snapshot["projectionType"], "foundation-ai-assets-non-rag-v2")
            self.assertEqual(
                payload,
                {
                    **expected,
                    "storage": storage,
                    "rag": rag_status,
                    "detectedToolKeys": ["codex"],
                    "toolPresence": _tool_detection("codex")["toolPresence"],
                    "totalTokens": 42,
                    "totalMessages": 0,
                    "totalSessions": 0,
                },
            )
            self.assertEqual(rag_snapshot["payload"], {"businessDate": "2026-05-19", **rag_status})
            incremental.assert_called_once_with(include_rag=False)
            detailed_storage.assert_called_once_with(include_rag=True)
            rag_stats.assert_called_once_with()

    def test_foundation_workspace_usage_filters_container_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _run_id = self._home(root)
            project = root / "project" / "actanara"
            project.mkdir(parents=True)
            (project / "pyproject.toml").write_text('[project]\nname = "actanara"\n', encoding="utf-8")
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tool_sources(
                        tool_key, display_name, adapter_version, capabilities_json,
                        enabled, created_at, updated_at
                    ) VALUES ('codex', 'Codex', 'test', '{}', 1, '2026-05-19T00:00:00+00:00', '2026-05-19T00:00:00+00:00')
                    """
                )
                for index, cwd in enumerate((str(project), str(Path.home()), "/Volumes/Example"), start=1):
                    cursor = connection.execute(
                        """
                        INSERT INTO sessions(tool_key, external_session_key, started_at, last_active_at, initial_cwd, metadata_json)
                        VALUES ('codex', ?, '2026-05-19T04:00:00+00:00', '2026-05-19T04:00:00+00:00', ?, '{}')
                        """,
                        (f"session-{index}", cwd),
                    )
                    connection.execute(
                        """
                        INSERT INTO usage_events(
                            tool_key, session_id, external_event_key, occurred_at, business_date,
                            protocol_total_tokens, message_count, raw_locator_json, metadata_json
                        ) VALUES ('codex', ?, ?, '2026-05-19T04:00:00+00:00', '2026-05-19', 20000000, 1, '{}', '{}')
                        """,
                        (cursor.lastrowid, f"event-{index}"),
                    )

                rows = _foundation_workspace_usage_from_events(connection)

            self.assertEqual([row["name"] for row in rows], ["actanara"])
            self.assertEqual(rows[0]["emoji"], "🤖")

    def test_non_rag_snapshot_rollup_fallback_filters_container_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tool_sources(
                        tool_key, display_name, adapter_version, capabilities_json,
                        enabled, created_at, updated_at
                    ) VALUES ('codex', 'Codex', 'test', '{}', 1, '2026-05-19T00:00:00+00:00', '2026-05-19T00:00:00+00:00')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO daily_tool_usage(
                        business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
                    ) VALUES ('2026-05-19', 'codex', 80000000, 8, 4, 0, ?)
                    """,
                    (run_id,),
                )
                for bucket in ("project:actanara", "home", "SSD", "unattributed"):
                    connection.execute(
                        """
                        INSERT INTO daily_project_usage(
                            business_date, project_id_or_bucket, tool_key, tokens, messages,
                            active_sessions, evidence_confidence, source_run_id
                        ) VALUES ('2026-05-19', ?, 'codex', 20000000, 2, 1, 'medium', ?)
                        """,
                        (bucket, run_id),
                    )

            payload = _foundation_ai_assets_non_rag_payload(paths)

            self.assertEqual([row["name"] for row in payload["workspaceUsage"]], ["actanara"])
            self.assertEqual(payload["workspaceUsage"][0]["emoji"], "🤖")

    def test_foundation_ai_assets_counts_only_non_blank_non_cron_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            with connect(paths) as connection:
                for tool in ("codex", "cron"):
                    connection.execute(
                        """
                        INSERT INTO tool_sources(
                            tool_key, display_name, adapter_version, capabilities_json,
                            enabled, created_at, updated_at
                        ) VALUES (?, ?, 'test', '{}', 1,
                                  '2026-05-18T00:00:00+00:00', '2026-05-18T00:00:00+00:00')
                        """,
                        (tool, tool.title()),
                    )
                for day, tool, tokens in (
                    ("2026-05-18", "codex", 10),
                    ("2026-05-19", "codex", 0),
                    ("2026-05-20", "cron", 10),
                ):
                    connection.execute(
                        """
                        INSERT INTO daily_tool_usage(
                            business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
                        ) VALUES (?, ?, ?, 0, 0, 0, ?)
                        """,
                        (day, tool, tokens, run_id),
                    )

            payload = _foundation_ai_assets_non_rag_payload(paths)

            self.assertEqual(payload["activeDayCount"], 1)

    def test_foundation_ai_assets_payload_reads_infrastructure_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, _run_id = self._home(Path(tmp))
            apply_infrastructure_updates(
                paths,
                "2026-07-03",
                [
                    {"entityType": "device", "name": "Mac mini", "status": "online", "role": "local host"},
                    {
                        "entityType": "service",
                        "name": "Dashboard server",
                        "host": "Mac mini",
                        "field": "port",
                        "currentValue": "3036",
                        "change": "Dashboard server port confirmed",
                    },
                ],
            )

            payload = _foundation_ai_assets_non_rag_payload(paths)

            self.assertEqual(payload["infrastructure"]["dataAuthority"], "foundation-infrastructure-graph-v1")
            self.assertEqual(payload["infrastructure"]["devices"][0]["name"], "Mac mini")
            self.assertEqual(payload["infrastructure"]["devices"][0]["services"][0]["name"], "Dashboard server")

    def test_foundation_ai_assets_reader_uses_static_snapshot_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, run_id = self._home(Path(tmp))
            expected_fields = json.loads(
                (ROOT / "tests" / "fixtures" / "phase0" / "api-contract.json").read_text(encoding="utf-8")
            )["aiAssetsTopLevelFields"]
            payload = {key: {} for key in expected_fields if key not in {"tools", "storage"}}
            payload.update({
                "tools": [{"name": "Codex", "allTimeTokens": 42}],
                "storage": {"tools": [], "categories": [{"label": "正式日记", "sizeMB": 1.0}]},
            })
            with patch(
                "data_foundation.snapshots.detect_external_tools",
                return_value=_tool_detection("codex"),
            ):
                materialize_ai_assets_non_rag_snapshot(
                    paths,
                    run_id,
                    builder=lambda: payload,
                )
            write_settings({"runtimeSources": {"dashboardReadSource": "foundation"}}, paths)
            ai_assets._cache = {"data": None, "ts": 0}
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(
                    ai_assets,
                    "_current_external_tool_detection",
                    return_value=_tool_detection("codex"),
                ),
                patch.object(ai_assets, "get_ai_assets", side_effect=AssertionError("live non-RAG assembly called")),
                patch.object(ai_assets, "_get_rag_stats", side_effect=AssertionError("live RAG status called")),
                patch.object(ai_assets, "_rag_storage_category", side_effect=AssertionError("live RAG storage scan called")),
            ):
                result = ai_assets.get_ai_assets_cached()
            self.assertEqual(result["tools"], payload["tools"])
            self.assertEqual(result["rag"], payload["rag"])
            self.assertEqual(result["storage"]["categories"], [{"label": "正式日记", "sizeMB": 1.0}])
            self.assertTrue(set(expected_fields) <= set(result))
            self.assertEqual(result["dataFreshness"]["aiAssets"]["source"], "foundation")
            self.assertTrue(result["dataFreshness"]["aiAssets"]["staticSnapshotOnly"])
            self.assertEqual(result["dataFreshness"]["aiAssets"]["ragStatusSource"], "snapshot")

    def test_missing_foundation_ai_assets_snapshot_reports_refresh_without_legacy_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, _ = self._home(Path(tmp))
            write_settings({"runtimeSources": {"dashboardReadSource": "foundation"}}, paths)
            ai_assets._cache = {"data": None, "ts": 0}
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(ai_assets, "get_ai_assets", side_effect=AssertionError("legacy assembly called")),
                patch.object(ai_assets, "_get_rag_stats", side_effect=AssertionError("live RAG scan called")),
                patch.object(ai_assets, "_rag_storage_category", side_effect=AssertionError("live RAG storage scan called")),
            ):
                result = ai_assets.get_ai_assets_cached()
            self.assertEqual(result["dataFreshness"]["aiAssets"]["source"], "snapshot-missing")
            self.assertTrue(result["dataFreshness"]["aiAssets"]["refreshRequired"])
            self.assertEqual(result["tools"], [])

    def test_non_rag_tool_config_discovery_does_not_write_dashboard_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "tool-configs.json"
            with (
                patch.object(ai_assets, "TOOL_CONFIG_SNAPSHOT", snapshot_path),
                patch.object(ai_assets, "_find_tool_executable", return_value=None),
            ):
                configs = ai_assets.discover_tool_configs(persist=False)
            self.assertIsInstance(configs, list)
            self.assertFalse(snapshot_path.exists())

    def test_tool_config_refresh_response_contains_only_currently_detected_tools(self):
        configs = [
            {"name": "Codex", "status": "detected"},
            # Cursor can be present through IDE state.vscdb while its CLI home
            # is absent, so the legacy config probe can still say "missing".
            {"name": "Cursor", "status": "missing"},
            {"name": "OpenCode", "status": "missing"},
        ]
        original_cache = ai_assets._cache
        ai_assets._cache = {"data": {"stale": True}, "ts": 7}
        self.addCleanup(setattr, ai_assets, "_cache", original_cache)
        with (
            patch.object(ai_assets, "discover_tool_configs", return_value=configs),
            patch.object(
                ai_assets,
                "_current_external_tool_detection",
                return_value=_tool_detection("codex", "cursor"),
            ),
            patch.object(
                ai_assets,
                "_tool_config_snapshot_path",
                return_value=Path("/tmp/tool-configs.json"),
            ),
        ):
            result = ai_assets.refresh_tool_configs_with_metadata()

        self.assertEqual(
            [item["name"] for item in result["toolConfigs"]],
            ["Codex", "Cursor"],
        )
        self.assertEqual(
            [item["status"] for item in result["toolConfigs"]],
            ["detected", "detected"],
        )
        self.assertEqual(result["detectedToolKeys"], ["codex", "cursor"])
        self.assertEqual(result["toolPresence"], _tool_detection("codex", "cursor")["toolPresence"])
        self.assertIsNone(ai_assets._cache["data"])
        self.assertEqual(ai_assets._cache["ts"], 0)

    def test_incremental_ai_assets_usage_cache_reuses_unchanged_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _ = self._home(root)
            codex_sessions = root / "codex" / "sessions"
            codex_sessions.mkdir(parents=True)
            session = codex_sessions / "rollout-test.jsonl"
            session.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"cwd": str(ROOT)}}),
                    json.dumps({
                        "type": "event_msg",
                        "timestamp": "2026-06-14T12:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 5,
                                    "cached_input_tokens": 2,
                                    "total_tokens": 17,
                                }
                            },
                        },
                    }),
                ])
                + "\n",
                encoding="utf-8",
            )
            write_settings({
                "externalTools": {
                    "openclaw": {"agentsRoot": str(root / "missing-openclaw")},
                    "claudeCode": {"projectsRoot": str(root / "missing-claude")},
                    "geminiCli": {
                        "chatsRoot": str(root / "missing-gemini"),
                        "projectsPath": str(root / "missing-gemini-projects.json"),
                    },
                    "codex": {"sessionsRoot": str(codex_sessions)},
                    "hermes": {"stateDbPath": str(root / "missing-hermes.sqlite")},
                }
            }, paths)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                first = ai_assets.get_ai_assets_incremental(include_rag=False)
                with patch.object(ai_assets, "_parse_codex_usage_file", side_effect=AssertionError("unchanged source reparsed")):
                    second = ai_assets.get_ai_assets_incremental(include_rag=False)

            codex_first = next(tool for tool in first["tools"] if tool["name"] == "Codex")
            codex_second = next(tool for tool in second["tools"] if tool["name"] == "Codex")
            self.assertEqual(codex_first["allTimeTokens"], 17)
            self.assertEqual(codex_second["allTimeTokens"], 17)
            self.assertEqual(second["usageCache"]["cached"], 1)
            self.assertEqual(second["usageCache"]["reparsed"], 0)
            with connect(paths, read_only=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_asset_usage_source_files").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_asset_usage_records").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
