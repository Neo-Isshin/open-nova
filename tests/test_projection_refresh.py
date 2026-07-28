import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from data_foundation.paths import initialize_home, update_runtime_manifest_paths
from data_foundation.db import connect, migrate
from data_foundation.diary_markdown import DIARY_PERIOD_PAGE_PROJECTION, materialize_diary_markdown_day, read_diary_markdown_documents
from data_foundation.jobs import begin_ingestion_run
from data_foundation.period_summary import DIARY_PERIOD_SUMMARY_PROJECTION
from data_foundation.refresh import (
    active_history_backfill_run,
    cancel_history_backfill,
    completed_period_summary_targets,
    due_scheduled_history_backfills,
    HistoryBackfillAlreadyActiveError,
    plan_history_backfill,
    projection_refresh_status,
    queue_failed_history_backfill_retry,
    queue_history_backfill,
    queue_period_summary_refresh,
    queue_projection_refresh,
    recent_projection_refresh_jobs,
    _history_backfill_existing_diary_days,
    _history_backfill_pending_items,
    _run_history_daily_actions,
    run_history_backfill,
    run_due_scheduled_history_backfills,
    run_pipeline_blank_day_materialization,
    run_pipeline_daily_materialization,
    run_period_summary_refresh,
    run_projection_refresh,
)
from data_foundation.daily_completeness import evaluate_daily_completeness
from data_foundation.reports import LEGACY_ASSET_PROJECTION, read_period_projection, write_period_projection
from data_foundation.settings import write_llm_provider, write_settings
from data_foundation.snapshots import materialize_diary_tasks_snapshot, read_dashboard_snapshot
from app.services import foundation as dashboard_foundation
from app.services import settings as dashboard_settings

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


class ProjectionRefreshTests(unittest.TestCase):
    def test_dashboard_write_paths_preserve_selected_generated_diary_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "ConfiguredDiary"
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=root / "LegacyDiary").home,
                generated_diary_root=diary_root,
                legacy_diary_root=root / "LegacyDiary",
            )

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                selected = dashboard_foundation._dashboard_write_paths()

        self.assertEqual(selected.diary_dir.resolve(), diary_root.resolve())
        self.assertEqual(selected.legacy_diary_root.resolve(), (root / "LegacyDiary").resolve())

    def test_blank_day_materialization_only_writes_no_activity_narrative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "日记-260620.md").write_text("# standard\n", encoding="utf-8")
            (day / "日记-260620-no-activity.md").write_text(
                """# 2026年06月20日 日记

## 今日概要
今日无活动

```json
{"date": "2026-06-20", "activityState": "empty", "metrics": {}, "cronTasks": []}
```
""",
                encoding="utf-8",
            )
            (day / "技术进展-260620.md").write_text(
                "# technical\n\n## Old technical\nretained technical\n",
                encoding="utf-8",
            )
            (day / "智慧沉淀-260620.md").write_text(
                "# learning\n\n## Old learning\nretained learning\n",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            migrate(paths)
            stale_run = begin_ingestion_run(
                paths,
                trigger_type="pipeline-foundation-materialization",
                business_date=date(2026, 6, 20),
                status="running",
            )
            materialize_diary_markdown_day(paths, date(2026, 6, 20), source_run_id=stale_run)

            with patch("data_foundation.refresh.fetch_weather_for_date", return_value="weather"):
                result = run_pipeline_blank_day_materialization(paths, date(2026, 6, 20))

            with connect(paths, read_only=True) as connection:
                rows = connection.execute(
                    "SELECT document_key, report_type, relative_path, source_run_id, status FROM diary_markdown_documents WHERE business_date = ? ORDER BY relative_path",
                    ("2026-06-20",),
                ).fetchall()
                ready_row = next(row for row in rows if row["status"] == "ready")
                sections = connection.execute(
                    "SELECT heading, body_markdown FROM diary_markdown_sections WHERE document_key = ? ORDER BY ordinal",
                    (ready_row["document_key"],),
                ).fetchall()
                stale_section_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM diary_markdown_sections AS section
                    JOIN diary_markdown_documents AS document USING(document_key)
                    WHERE document.business_date = ? AND document.status = 'stale'
                    """,
                    ("2026-06-20",),
                ).fetchone()[0]
            visible_documents = read_diary_markdown_documents(paths, date(2026, 6, 20), date(2026, 6, 20))
            no_activity_markdown = (day / "日记-260620-no-activity.md").read_text(encoding="utf-8")
        self.assertEqual(result["diaryMarkdownDocuments"], 1)
        self.assertEqual(
            [(row["report_type"], row["relative_path"]) for row in rows if row["status"] == "ready"],
            [("narrative", "diary-2026/diary-2026-06/06-20/日记-260620-no-activity.md")],
        )
        self.assertEqual({row["report_type"] for row in rows if row["status"] == "stale"}, {"technical", "learning"})
        self.assertEqual({row["source_run_id"] for row in rows if row["status"] == "stale"}, {stale_run})
        self.assertEqual(stale_section_count, 2)
        self.assertEqual([document["report_type"] for document in visible_documents], ["narrative"])
        self.assertIn(("天气", "weather"), [(row["heading"], row["body_markdown"].strip()) for row in sections])
        self.assertIn("## 天气\nweather", no_activity_markdown)

    def test_blank_day_materialization_uses_english_no_activity_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "diary-260620.md").write_text("# standard\n", encoding="utf-8")
            (day / "diary-260620-no-activity.md").write_text(
                """# 2026-06-20 Diary

## Daily Overview
No activity today.

```json
{"date": "2026-06-20", "activityState": "empty", "metrics": {}, "cronTasks": []}
```
""",
                encoding="utf-8",
            )
            (day / "technical-260620.md").write_text("# technical\n", encoding="utf-8")
            (day / "learning-260620.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings({"pipeline": {"languageProfile": "en", "englishEnabled": True}}, paths)
            migrate(paths)
            stale_run = begin_ingestion_run(
                paths,
                trigger_type="pipeline-foundation-materialization",
                business_date=date(2026, 6, 20),
                status="running",
            )
            materialize_diary_markdown_day(paths, date(2026, 6, 20), source_run_id=stale_run)

            with patch("data_foundation.refresh.fetch_weather_for_date", return_value="Cloudy, 28 C"):
                result = run_pipeline_blank_day_materialization(paths, date(2026, 6, 20))

            with connect(paths, read_only=True) as connection:
                rows = connection.execute(
                    "SELECT document_key, report_type, relative_path, status FROM diary_markdown_documents WHERE business_date = ? ORDER BY relative_path",
                    ("2026-06-20",),
                ).fetchall()
                ready_row = next(row for row in rows if row["status"] == "ready")
                sections = connection.execute(
                    "SELECT heading, body_markdown FROM diary_markdown_sections WHERE document_key = ? ORDER BY ordinal",
                    (ready_row["document_key"],),
                ).fetchall()
            visible_documents = read_diary_markdown_documents(paths, date(2026, 6, 20), date(2026, 6, 20))
            no_activity_markdown = (day / "diary-260620-no-activity.md").read_text(encoding="utf-8")
        self.assertEqual(result["diaryMarkdownDocuments"], 1)
        self.assertEqual(
            [(row["report_type"], row["relative_path"]) for row in rows if row["status"] == "ready"],
            [("narrative", "diary-2026/diary-2026-06/06-20/diary-260620-no-activity.md")],
        )
        self.assertEqual({row["report_type"] for row in rows if row["status"] == "stale"}, {"technical", "learning"})
        self.assertEqual([document["report_type"] for document in visible_documents], ["narrative"])
        self.assertIn(("Weather", "Cloudy, 28 C"), [(row["heading"], row["body_markdown"].strip()) for row in sections])
        self.assertIn("## Weather\nCloudy, 28 C", no_activity_markdown)

    def test_history_backfill_diary_detection_uses_pipeline_language_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "diary-260620.md").write_text("# English diary\n", encoding="utf-8")
            (day / "technical-260620.md").write_text("# English technical\n", encoding="utf-8")
            (day / "learning-260620.md").write_text("# English learning\n", encoding="utf-8")
            (day / "日记-260621.md").write_text("# Chinese diary\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings({"pipeline": {"languageProfile": "en", "englishEnabled": True}}, paths)

            existing = _history_backfill_existing_diary_days(paths, [date(2026, 6, 20), date(2026, 6, 21)])
            pending = _history_backfill_pending_items(
                paths,
                [],
                [date(2026, 6, 20), date(2026, 6, 21)],
                include_summaries=False,
            )

        self.assertEqual(existing, [date(2026, 6, 20)])
        self.assertEqual([item["date"] for item in pending], ["2026-06-20", "2026-06-21"])
        self.assertEqual(pending[0]["missingLabels"], ["SQLite materialization", "RAG sync", "Nova-Task work graph/export"])

    def test_history_backfill_exempts_rag_sync_when_rag_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "日记-260620.md").write_text("# existing\n", encoding="utf-8")
            (day / "技术进展-260620.md").write_text("# existing technical\n", encoding="utf-8")
            (day / "智慧沉淀-260620.md").write_text("# existing learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings({"rag": {"enabled": False, "mode": "disabled"}}, paths)

            completeness = evaluate_daily_completeness(paths, date(2026, 6, 20))
            pending = _history_backfill_pending_items(
                paths,
                [],
                [date(2026, 6, 20)],
                include_summaries=False,
            )

        self.assertFalse(completeness["ragRequired"])
        self.assertIn("rag-sync", [item["key"] for item in completeness["skippedItems"]])
        self.assertNotIn("rag-sync", completeness["missingKeys"])
        self.assertEqual([item["date"] for item in pending], ["2026-06-20"])
        self.assertEqual(pending[0]["missingLabels"], ["SQLite materialization", "Nova-Task work graph/export"])

    def test_daily_completeness_marks_blank_day_nova_task_not_required(self):
        for nova_task_enabled in (True, False):
            with self.subTest(nova_task_enabled=nova_task_enabled), tempfile.TemporaryDirectory() as tmp:
                paths = initialize_home(Path(tmp) / "Actanara")
                write_settings(
                    {
                        "features": {"novaTask": nova_task_enabled, "rag": False},
                        "rag": {"enabled": False, "mode": "disabled"},
                    },
                    paths,
                )
                documents = [
                    {
                        "report_type": "narrative",
                        "relative_path": "diary-2026/diary-2026-06/06-20/diary-260620-no-activity.md",
                        "embeddedJson": {"activityState": "empty"},
                    }
                ]

                with patch("data_foundation.daily_completeness._nova_task_ready") as task_ready:
                    completeness = evaluate_daily_completeness(paths, date(2026, 6, 20), documents=documents)

                self.assertTrue(completeness["ready"])
                self.assertFalse(completeness["novaTaskRequired"])
                self.assertFalse(completeness["novaTaskUpdated"])
                self.assertIn("nova-task", [item["key"] for item in completeness["skippedItems"]])
                self.assertNotIn("nova-task", completeness["missingKeys"])
                self.assertNotIn("nova-task-work-graph", completeness["plannedActions"])
                task_ready.assert_not_called()

    def test_disabled_nova_task_is_skipped_and_does_not_block_ready_or_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day_dir = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day_dir.mkdir(parents=True)
            (day_dir / "日记-260620.md").write_text("# narrative\n", encoding="utf-8")
            (day_dir / "技术进展-260620.md").write_text("# technical\n", encoding="utf-8")
            (day_dir / "智慧沉淀-260620.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings(
                {
                    "features": {"novaTask": False, "rag": False},
                    "rag": {"enabled": False, "mode": "disabled"},
                },
                paths,
            )
            migrate(paths)
            day = date(2026, 6, 20)
            run_id = begin_ingestion_run(
                paths,
                trigger_type="u008-completeness-test",
                business_date=day,
            )
            materialize_diary_markdown_day(paths, day, source_run_id=run_id)
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO tool_sources(
                        tool_key, display_name, adapter_version, capabilities_json,
                        enabled, created_at, updated_at
                    ) VALUES (
                        'codex', 'Codex', 'test', '{}', 1,
                        '2026-06-20T12:00:00+00:00', '2026-06-20T12:00:00+00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO daily_tool_usage(
                        business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id
                    ) VALUES ('2026-06-20', 'codex', 0, 0, 0, 0, ?)
                    """,
                    (run_id,),
                )

            without_evidence = evaluate_daily_completeness(paths, day)
            pending_without_evidence = _history_backfill_pending_items(
                paths,
                [],
                [day],
                include_summaries=False,
            )

            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO nova_task_events(
                        event_id, business_date, source_type, source_path, source_sha256,
                        source_locator, matched_node_id, event_type, confidence, summary,
                        evidence_json, metadata_json, created_at
                    ) VALUES (
                        'NTEV-disabled', '2026-06-20', 'technical-report', NULL, 'sha',
                        'technical:2026-06-20', NULL, 'progress', 'high', 'Disabled feature evidence',
                        '[]', '{}', '2026-06-20T12:00:00+00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO nova_task_exports(
                        export_id, export_type, target_path, content_sha256,
                        generated_at, source_snapshot_json, metadata_json
                    ) VALUES (
                        'NTE-disabled', 'task_board_markdown', 'TASK_BOARD.md', 'sha',
                        '2026-06-20T12:01:00+00:00', '{}', '{}'
                    )
                    """
                )

            with_evidence = evaluate_daily_completeness(paths, day)
            pending_with_evidence = _history_backfill_pending_items(
                paths,
                [],
                [day],
                include_summaries=False,
            )

        for completeness, expected_updated in (
            (without_evidence, False),
            (with_evidence, True),
        ):
            with self.subTest(expected_updated=expected_updated):
                self.assertTrue(completeness["ready"])
                self.assertEqual(completeness["status"], "ready")
                self.assertFalse(completeness["novaTaskRequired"])
                self.assertEqual(completeness["novaTaskUpdated"], expected_updated)
                self.assertIn("nova-task", [item["key"] for item in completeness["skippedItems"]])
                self.assertNotIn("nova-task", completeness["missingKeys"])
                self.assertNotIn("nova-task-work-graph", completeness["plannedActions"])
        self.assertEqual(pending_without_evidence, [])
        self.assertEqual(pending_with_evidence, [])

    def test_daily_completeness_accepts_applied_nova_task_work_graph_without_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara")
            migrate(paths)
            day = date(2026, 6, 20)
            recon_dir = paths.state_dir / "nova-task" / "work-graph"
            recon_dir.mkdir(parents=True)
            (recon_dir / "2026-06-20-20260620-120000.md").write_text(
                "# Nova-Task Work Graph Reconciliation\n\n"
                "- businessDate: 2026-06-20\n"
                "- applied: true\n\n"
                "```yaml\nnova_task:\n  date: \"2026-06-20\"\n  candidate_actions: []\n```\n",
                encoding="utf-8",
            )
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO nova_task_exports(
                        export_id, export_type, target_path, content_sha256,
                        generated_at, source_snapshot_json, metadata_json
                    ) VALUES (
                        'NTE-test', 'task_board_markdown', 'TASK_BOARD.md', 'sha',
                        '2026-06-21T12:00:00+00:00', '{}', '{}'
                    )
                    """
                )

            completeness = evaluate_daily_completeness(paths, day, documents=[])

        self.assertNotIn("nova-task", completeness["missingKeys"])
        self.assertIn("nova-task", completeness["existingItems"])
        self.assertTrue(completeness["novaTaskRequired"])
        self.assertTrue(completeness["novaTaskUpdated"])

    def test_daily_completeness_rejects_stale_nova_task_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara")
            migrate(paths)
            day = date(2026, 6, 20)
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO nova_task_events(
                        event_id, business_date, source_type, source_path, source_sha256,
                        source_locator, matched_node_id, event_type, confidence, summary,
                        evidence_json, metadata_json, created_at
                    ) VALUES (
                        'NTEV-fresh', '2026-06-20', 'technical-report', NULL, 'sha',
                        'technical:2026-06-20', NULL, 'progress', 'high', 'Fresh same-day task event',
                        '[]', '{}', '2026-06-20T12:00:00+00:00'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO nova_task_exports(
                        export_id, export_type, target_path, content_sha256,
                        generated_at, source_snapshot_json, metadata_json
                    ) VALUES (
                        'NTE-stale', 'task_board_markdown', 'TASK_BOARD.md', 'sha-old',
                        '2026-06-20T11:59:00+00:00', '{}', '{}'
                    )
                    """
                )

            stale = evaluate_daily_completeness(paths, day, documents=[])
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO nova_task_exports(
                        export_id, export_type, target_path, content_sha256,
                        generated_at, source_snapshot_json, metadata_json
                    ) VALUES (
                        'NTE-fresh', 'task_board_markdown', 'TASK_BOARD.md', 'sha-new',
                        '2026-06-20T12:01:00+00:00', '{}', '{}'
                    )
                    """
                )
            fresh = evaluate_daily_completeness(paths, day, documents=[])

        self.assertIn("nova-task", stale["missingKeys"])
        self.assertTrue(stale["novaTaskRequired"])
        self.assertFalse(stale["novaTaskUpdated"])
        self.assertNotIn("nova-task", fresh["missingKeys"])
        self.assertTrue(fresh["novaTaskRequired"])
        self.assertTrue(fresh["novaTaskUpdated"])

    def test_history_backfill_treats_partial_daily_diary_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "日记-260620.md").write_text("# Narrative only\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )

            existing = _history_backfill_existing_diary_days(paths, [date(2026, 6, 20)])
            pending = _history_backfill_pending_items(
                paths,
                [],
                [date(2026, 6, 20)],
                include_summaries=False,
            )

        self.assertEqual(existing, [date(2026, 6, 20)])
        self.assertEqual([item["date"] for item in pending], ["2026-06-20"])

    def test_stale_projection_cannot_prove_readiness_or_trigger_skip_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            narrative = day / "日记-260620.md"
            technical = day / "技术进展-260620.md"
            learning = day / "智慧沉淀-260620.md"
            narrative.write_text("# narrative\n", encoding="utf-8")
            technical.write_text("# technical\n", encoding="utf-8")
            learning.write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings({"rag": {"enabled": False, "mode": "disabled"}}, paths)
            migrate(paths)
            materialize_diary_markdown_day(paths, date(2026, 6, 20), source_run_id=None)

            technical.unlink()
            materialize_diary_markdown_day(paths, date(2026, 6, 20), source_run_id=None)
            completeness = evaluate_daily_completeness(paths, date(2026, 6, 20))
            pending = _history_backfill_pending_items(
                paths,
                [],
                [date(2026, 6, 20)],
                include_summaries=False,
            )

        self.assertFalse(completeness["documentsReady"]["technical"])
        self.assertIn("diary-technical", completeness["missingKeys"])
        self.assertIn("technical-pass", completeness["plannedActions"])
        self.assertEqual([item["date"] for item in pending], ["2026-06-20"])
        self.assertIn("technical diary", pending[0]["missingLabels"])

    def test_history_backfill_treats_no_activity_day_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-19"
            day.mkdir(parents=True)
            (day / "日记-260619-no-activity.md").write_text("# No activity\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )

            existing = _history_backfill_existing_diary_days(paths, [date(2026, 6, 19)])
            pending = _history_backfill_pending_items(
                paths,
                [],
                [date(2026, 6, 19)],
                include_summaries=False,
            )

        self.assertEqual(existing, [date(2026, 6, 19)])
        self.assertEqual(pending, [])

    def test_markdown_materialization_uses_pipeline_language_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-06" / "06-20"
            day.mkdir(parents=True)
            (day / "diary-260620.md").write_text("# English diary\n", encoding="utf-8")
            (day / "technical-260620.md").write_text("# English technical\n", encoding="utf-8")
            (day / "learning-260620.md").write_text("# English learning\n", encoding="utf-8")
            (day / "日记-260620.md").write_text("# Chinese diary\n", encoding="utf-8")
            (day / "技术进展-260620.md").write_text("# Chinese technical\n", encoding="utf-8")
            (day / "智慧沉淀-260620.md").write_text("# Chinese learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            write_settings({"pipeline": {"languageProfile": "en", "englishEnabled": True}}, paths)
            migrate(paths)

            result = materialize_diary_markdown_day(paths, date(2026, 6, 20), source_run_id=None)

            with connect(paths, read_only=True) as connection:
                rows = connection.execute(
                    "SELECT report_type, relative_path FROM diary_markdown_documents WHERE business_date = ? ORDER BY relative_path",
                    ("2026-06-20",),
                ).fetchall()

        self.assertEqual(result["documents"], 3)
        self.assertEqual(
            [(row["report_type"], Path(row["relative_path"]).name) for row in rows],
            [
                ("narrative", "diary-260620.md"),
                ("learning", "learning-260620.md"),
                ("technical", "technical-260620.md"),
            ],
        )

    def test_pipeline_daily_materialization_writes_dashboard_and_current_period_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-19"
            day.mkdir(parents=True)
            (day / "日记-260519.md").write_text(
                """# 2026年05月19日 日记

## 今日概要

* **Pipeline materialization landed**：Daily pipeline owns SQLite projection refresh.
  - Wrote diary Markdown projection
  - Wrote current week projection
  - Wrote current month projection
""",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            result = run_pipeline_daily_materialization(
                paths,
                date(2026, 5, 19),
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {
                    "models": [],
                    "memoryStats": {"sessionFiles": 2},
                    "knowledgePeriodMemoryCurrent": {"sessionFiles": 2},
                },
            )

            run_id = result["runId"]
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "completed")
            self.assertEqual(read_dashboard_snapshot(paths)["sourceRunId"], run_id)
            week = read_period_projection(paths, date(2026, 5, 18), date(2026, 5, 19))
            month_page = read_period_projection(
                paths,
                date(2026, 5, 1),
                date(2026, 5, 19),
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )
            month_summary = read_period_projection(
                paths,
                date(2026, 5, 1),
                date(2026, 5, 19),
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            self.assertEqual(week["sourceRunId"], run_id)
            self.assertEqual(month_page["sourceRunId"], run_id)
            self.assertIsNone(month_summary)
            self.assertEqual(month_page["metrics"]["summaryTopics"][0]["items"][0], "Wrote diary Markdown projection")
            self.assertEqual(result["periods"][0]["label"], "current-week")
            self.assertEqual(result["periods"][1]["label"], "current-month")
            self.assertEqual(result["completedPeriodSummaries"][0]["label"], "completed-week")
            self.assertEqual(result["completedPeriodSummaries"][0]["start"], "2026-05-11")
            self.assertEqual(result["completedPeriodSummaries"][0]["end"], "2026-05-17")
            self.assertEqual(result["completedPeriodSummaries"][1]["label"], "completed-month")
            self.assertEqual(result["completedPeriodSummaries"][1]["start"], "2026-04-01")
            self.assertEqual(result["completedPeriodSummaries"][1]["end"], "2026-04-30")
            completed_week = read_period_projection(
                paths,
                date(2026, 5, 11),
                date(2026, 5, 17),
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            self.assertIsNotNone(completed_week)

    def test_completed_summary_targets_skip_existing_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            write_period_projection(
                paths,
                date(2026, 5, 11),
                date(2026, 5, 17),
                {"summary": {"lead": "existing week"}},
                source_run_id=None,
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            targets = completed_period_summary_targets(paths, date(2026, 5, 19))
            self.assertEqual([target["label"] for target in targets], ["completed-month"])

    def test_history_backfill_plan_counts_week_month_and_llm_calls_from_pending_items(self):
        plan = plan_history_backfill(
            date(2026, 4, 1),
            date(2026, 4, 30),
            grain="both",
            include_summaries=True,
        )

        self.assertEqual(plan["periodCount"], 5)
        self.assertEqual(plan["pendingDiaryDays"], 32)
        self.assertEqual(plan["pendingSummaryReports"], 5)
        self.assertEqual(plan["llmCallCount"], 101)
        self.assertEqual(
            [(item["kind"], item["start"], item["end"]) for item in plan["periods"]],
            [
                ("week", "2026-03-30", "2026-04-05"),
                ("week", "2026-04-06", "2026-04-12"),
                ("week", "2026-04-13", "2026-04-19"),
                ("week", "2026-04-20", "2026-04-26"),
                ("month", "2026-04-01", "2026-04-30"),
            ],
        )

    def test_history_backfill_plan_scans_existing_diary_and_period_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            existing_day = diary_root / "diary-2026" / "diary-2026-05" / "05-05"
            existing_day.mkdir(parents=True)
            (existing_day / "日记-260505.md").write_text("# existing\n", encoding="utf-8")
            (existing_day / "技术进展-260505.md").write_text("# existing technical\n", encoding="utf-8")
            (existing_day / "智慧沉淀-260505.md").write_text("# existing learning\n", encoding="utf-8")
            month_dir = diary_root / "diary-2026" / "diary-2026-05"
            month_dir.mkdir(parents=True, exist_ok=True)
            (month_dir / "summary-2026-W19-周报.md").write_text("# existing week\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            periods = [
                {"kind": "week", "start": "2026-05-04", "end": "2026-05-10", "label": "2026-W19"},
                {"kind": "month", "start": "2026-05-01", "end": "2026-05-31", "label": "2026-05"},
            ]

            plan = plan_history_backfill(
                date(2026, 5, 1),
                date(2026, 5, 31),
                grain="selected",
                include_summaries=True,
                periods=periods,
                paths=paths,
            )

            labels = [item["label"] for item in plan["pendingItems"]]
            self.assertIn("diary-05-05", labels)
            self.assertIn("diary-05-07", labels)
            self.assertIn("W19周报", labels)
            self.assertIn("2026-05月报", labels)
            self.assertEqual(plan["pendingDiaryDays"], 31)
            self.assertEqual(plan["pendingSummaryReports"], 2)
            self.assertEqual(plan["llmCallCount"], 92)

    def test_history_backfill_reuses_ready_periods_and_records_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            source_run_id = begin_ingestion_run(paths, trigger_type="test", business_date=date(2026, 4, 30), status="completed")
            write_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                {"kpi": {"totalTokens": 10}, "workspaceUsage": []},
                source_run_id=source_run_id,
                projection_type=LEGACY_ASSET_PROJECTION,
            )
            write_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                {"summaryTopics": [], "lessons": []},
                source_run_id=source_run_id,
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )
            write_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                {"summary": {"lead": "ready"}},
                source_run_id=source_run_id,
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                grain="month",
                include_summaries=True,
            )

            def unexpected_builder(selected_start, days):
                raise AssertionError("ready assets/page should be reused")

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
                grain="month",
                include_summaries=True,
                daily_pipeline_runner=lambda day, runtime_paths: object(),
                period_builder=unexpected_builder,
            )

            status = projection_refresh_status(paths, run_id)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["metadata"]["scope"], "history-backfill")
            self.assertEqual(status["metadata"]["skippedPeriods"], 0)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["periods"][0]["status"], "completed")

    def test_history_backfill_dry_run_and_execution_cover_daily_pipeline_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text("# 2026年04月01日 日记\n", encoding="utf-8")
            (day / "技术进展-260401.md").write_text("# technical\n", encoding="utf-8")
            (day / "智慧沉淀-260401.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}]
            plan = plan_history_backfill(
                date(2026, 4, 1),
                date(2026, 4, 2),
                grain="selected",
                periods=periods,
                paths=paths,
            )
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)
            called = []

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 2),
                grain="selected",
                periods=periods,
                daily_pipeline_runner=lambda selected_day, runtime_paths: called.append(selected_day) or object(),
                period_builder=lambda selected_start, days: {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}},
            )

            self.assertEqual(plan["dailyPipelineDays"], 2)
            self.assertEqual(plan["existingDiaryDays"], 1)
            self.assertEqual(plan["existingDiaryDates"], ["2026-04-01"])
            self.assertEqual(called, ["2026-04-01", "2026-04-02"])
            self.assertEqual(result["dailyPipeline"]["skipped"], [])
            self.assertEqual(result["dailyPipeline"]["completed"], ["2026-04-01", "2026-04-02"])

    def test_history_backfill_day_periods_do_not_create_period_projections_or_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            periods = [{"kind": "day", "start": "2026-04-01", "end": "2026-04-01", "label": "diary-04-01"}]
            plan = plan_history_backfill(
                date(2026, 4, 1),
                date(2026, 4, 1),
                grain="selected",
                periods=periods,
                paths=paths,
                include_summaries=True,
            )
            run_id = begin_ingestion_run(paths, trigger_type="dashboard-history-backfill", business_date=date(2026, 4, 1), status="queued")
            called = []

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                grain="selected",
                periods=periods,
                include_summaries=True,
                daily_pipeline_runner=lambda selected_day, runtime_paths: called.append(selected_day) or object(),
                period_builder=lambda selected_start, days: (_ for _ in ()).throw(AssertionError("day periods must not build period projections")),
            )

            self.assertEqual(plan["pendingSummaryReports"], 0)
            self.assertEqual(plan["dailyPipelineDays"], 1)
            self.assertEqual(called, ["2026-04-01"])
            self.assertEqual(result["periods"], [{**periods[0], "daily": True, "days": 1, "status": "daily-only"}])
            self.assertIsNone(read_period_projection(paths, date(2026, 4, 1), date(2026, 4, 1), projection_type=LEGACY_ASSET_PROJECTION))
            self.assertIsNone(read_period_projection(paths, date(2026, 4, 1), date(2026, 4, 1), projection_type=DIARY_PERIOD_PAGE_PROJECTION))
            self.assertIsNone(read_period_projection(paths, date(2026, 4, 1), date(2026, 4, 1), projection_type=DIARY_PERIOD_SUMMARY_PROJECTION))

    def test_history_backfill_requires_pipeline_ready_llm_provider_when_llm_calls_are_planned(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]

            with self.assertRaisesRegex(ValueError, "LLM provider is not ready"):
                queue_history_backfill(
                    paths,
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    periods=periods,
                    require_llm_ready=True,
                )

    def test_history_backfill_rejects_memory_secret_when_llm_calls_are_planned(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            with patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "memory"}):
                write_llm_provider(
                    {
                        "provider": "openai-compatible",
                        "endpoint": "https://llm.local",
                        "model": "m1",
                        "apiKey": "secret",
                    },
                    paths,
                )

            with self.assertRaisesRegex(ValueError, "process-local memory backend"):
                queue_history_backfill(
                    paths,
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    periods=periods,
                    require_llm_ready=True,
                )

    def test_history_backfill_allows_blank_day_materialization_without_llm_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "day", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-04-01"}]

            with patch(
                "data_foundation.refresh._history_backfill_pending_items",
                return_value=[
                    {
                        "kind": "diary",
                        "label": "2026-04-01",
                        "start": "2026-04-01",
                        "end": "2026-04-01",
                        "actions": ["daily-blank-materialization"],
                        "llmCalls": 0,
                    }
                ],
            ):
                run_id = queue_history_backfill(
                    paths,
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    periods=periods,
                    require_llm_ready=True,
                )

        self.assertIsInstance(run_id, int)

    def test_dashboard_history_backfill_request_requires_llm_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            with (
                patch.object(dashboard_foundation, "_dashboard_write_paths", return_value=paths),
                patch.object(dashboard_foundation, "queue_history_backfill", return_value=42) as queue,
            ):
                run_id = dashboard_foundation.queue_history_backfill_request(
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    grain="selected",
                    include_summaries=False,
                    skip_ready=True,
                    periods=periods,
                )

            self.assertEqual(run_id, 42)
            self.assertTrue(queue.call_args.kwargs["require_llm_ready"])

    def test_dashboard_provider_save_is_visible_to_history_backfill_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "day", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-04-01"}]
            with patch.dict(
                os.environ,
                {
                    "ACTANARA_HOME": str(paths.home),
                    "ACTANARA_DASHBOARD_LLM_KEY": "secret",
                },
            ), patch("data_foundation.settings.platform.system", return_value="Linux"):
                saved = dashboard_settings.update_llm_provider(
                    {
                        "provider": "openai-compatible",
                        "endpoint": "https://llm.local",
                        "model": "m1",
                        "apiKeyEnv": "ACTANARA_DASHBOARD_LLM_KEY",
                    }
                )
                run_id = dashboard_foundation.queue_history_backfill_request(
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    grain="selected",
                    include_summaries=False,
                    skip_ready=True,
                    periods=periods,
                )

        self.assertEqual(saved["settingsTransaction"]["status"], "committed")
        self.assertIsInstance(run_id, int)

    def test_history_backfill_requires_overwrite_confirmation_for_existing_daily_diary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text("# 2026年04月01日 日记\n", encoding="utf-8")
            (day / "技术进展-260401.md").write_text("# technical\n", encoding="utf-8")
            (day / "智慧沉淀-260401.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods, skip_ready=False)

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                grain="selected",
                periods=periods,
                skip_ready=False,
                daily_pipeline_runner=lambda selected_day, runtime_paths: self.fail("overwrite confirmation is required"),
            )

            self.assertEqual(result["dailyPipeline"]["failed"][0]["date"], "2026-04-01")
            self.assertIn("overwrite confirmation required", result["dailyPipeline"]["failed"][0]["error"])

    def test_history_backfill_overwrite_daily_runs_existing_diary_and_records_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text("# 2026年04月01日 日记\n", encoding="utf-8")
            (day / "技术进展-260401.md").write_text("# technical\n", encoding="utf-8")
            (day / "智慧沉淀-260401.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=periods,
                skip_ready=False,
                overwrite_daily=True,
            )
            called = []

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                grain="selected",
                periods=periods,
                skip_ready=False,
                overwrite_daily=True,
                daily_pipeline_runner=lambda selected_day, runtime_paths: called.append(selected_day) or object(),
            )
            status = projection_refresh_status(paths, run_id)

            self.assertEqual(called, ["2026-04-01"])
            self.assertEqual(result["dailyPipeline"]["completed"], ["2026-04-01"])
            self.assertTrue(status["metadata"]["overwriteDaily"])
            self.assertTrue(status["metadata"]["reuseFoundationInputsOnOverwrite"])

    def test_history_backfill_overwrite_daily_reuses_foundation_inputs_with_default_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text("# 2026年04月01日 日记\n", encoding="utf-8")
            (day / "技术进展-260401.md").write_text("# technical\n", encoding="utf-8")
            (day / "智慧沉淀-260401.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=periods,
                skip_ready=False,
                overwrite_daily=True,
            )
            calls = []

            def frozen_runner(selected_day, runtime_paths, *, reuse_foundation_inputs=False, cancellation_requested=None):
                calls.append((selected_day, runtime_paths, reuse_foundation_inputs))
                return object()

            with patch("data_foundation.refresh._default_history_daily_pipeline_runner", side_effect=frozen_runner):
                result = run_history_backfill(
                    paths,
                    run_id,
                    start_date=date(2026, 4, 1),
                    end_date=date(2026, 4, 1),
                    grain="selected",
                    periods=periods,
                    skip_ready=False,
                    overwrite_daily=True,
                )

            self.assertEqual(result["dailyPipeline"]["completed"], ["2026-04-01"])
            self.assertEqual(calls, [("2026-04-01", paths, True)])

    def test_history_backfill_reuses_existing_foundation_inputs_for_missing_daily_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=periods,
            )
            calls = []

            def frozen_runner(selected_day, runtime_paths, *, reuse_foundation_inputs=False, cancellation_requested=None):
                calls.append((selected_day, runtime_paths, reuse_foundation_inputs))
                return object()

            with (
                patch("data_foundation.refresh._daily_foundation_inputs_reusable", return_value=True),
                patch("data_foundation.refresh._default_history_daily_pipeline_runner", side_effect=frozen_runner),
            ):
                result = run_history_backfill(
                    paths,
                    run_id,
                    start_date=date(2026, 4, 1),
                    end_date=date(2026, 4, 1),
                    grain="selected",
                    periods=periods,
                    skip_ready=False,
                )

            self.assertEqual(result["dailyPipeline"]["completed"], ["2026-04-01"])
            self.assertEqual(calls, [("2026-04-01", paths, True)])

    def test_history_backfill_completes_partial_daily_diary_without_overwrite_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text("# Narrative only\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=periods,
                skip_ready=False,
            )
            called = []

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                grain="selected",
                periods=periods,
                daily_pipeline_runner=lambda selected_day, runtime_paths: called.append(selected_day) or object(),
            )

            self.assertEqual(called, ["2026-04-01"])
            self.assertEqual(result["dailyPipeline"]["completed"], ["2026-04-01"])
            self.assertEqual(result["dailyPipeline"]["failed"], [])

    def test_history_backfill_queue_allows_only_one_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}]

            first_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)

            with self.assertRaises(HistoryBackfillAlreadyActiveError) as raised:
                queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)

            self.assertEqual(raised.exception.active_run["id"], first_id)

    def test_history_backfill_active_check_reconciles_orphaned_daily_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            orphan_id = begin_ingestion_run(
                paths,
                trigger_type="dashboard-history-backfill",
                business_date=date(2026, 4, 2),
                status="running",
                adapter_versions={
                    "currentStage": "history-daily-pipeline",
                    "currentStageLabel": "Generating daily diary for 2026-04-01",
                    "currentDailyPipelineDate": "2026-04-01",
                    "dailyPipeline": {"total": 2, "completed": [], "skipped": [], "failed": []},
                },
            )
            lock_dir = paths.state_dir / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "daily-pipeline-2026-04-01.lock").write_text(
                "pid=123456\ndate=2026-04-01\nstartedAt=2026-06-30T11:39:18+08:00\n",
                encoding="utf-8",
            )

            with patch("data_foundation.refresh._pid_running", return_value=False):
                active = active_history_backfill_run(paths)
                next_id = queue_history_backfill(
                    paths,
                    date(2026, 4, 1),
                    date(2026, 4, 1),
                    periods=[{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "single"}],
                )

            orphan = projection_refresh_status(paths, orphan_id)
            self.assertIsNone(active)
            self.assertEqual(active_history_backfill_run(paths)["id"], next_id)
            self.assertEqual(orphan["status"], "partial")
            self.assertIn("worker exited unexpectedly", orphan["error_summary"])
            self.assertEqual(orphan["metadata"]["failedDailyPipelineDays"], 1)
            self.assertEqual(orphan["metadata"]["dailyPipeline"]["failed"][0]["date"], "2026-04-01")

    def test_recent_history_jobs_reconcile_orphaned_daily_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            run_id = begin_ingestion_run(
                paths,
                trigger_type="dashboard-history-backfill",
                business_date=date(2026, 4, 2),
                status="running",
                adapter_versions={
                    "currentStage": "history-daily-pipeline",
                    "currentStageLabel": "Generating daily diary for 2026-04-01",
                    "dailyPipeline": {"total": 2, "completed": [], "skipped": [], "failed": []},
                },
            )
            lock_dir = paths.state_dir / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "daily-pipeline-2026-04-01.lock").write_text(
                "pid=123456\ndate=2026-04-01\nstartedAt=2026-06-30T11:39:18+08:00\n",
                encoding="utf-8",
            )

            with patch("data_foundation.refresh._pid_running", return_value=False):
                jobs = recent_projection_refresh_jobs(paths, limit=5)

            job = next(item for item in jobs if item["id"] == run_id)
            self.assertEqual(job["status"], "partial")
            self.assertTrue(job["metadata"]["orphaned"])
            self.assertEqual(job["metadata"]["dailyPipeline"]["failed"][0]["date"], "2026-04-01")

    def test_history_backfill_active_check_reconciles_stale_queued_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            stale_id = begin_ingestion_run(
                paths,
                trigger_type="dashboard-history-backfill",
                business_date=date(2026, 4, 1),
                status="queued",
                adapter_versions={
                    "periodStart": "2026-04-01",
                    "periodEnd": "2026-04-01",
                    "scheduledAt": None,
                    "scope": "history-backfill",
                },
            )
            with connect(paths) as connection:
                connection.execute(
                    "UPDATE ingestion_runs SET started_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00+00:00", stale_id),
                )

            active = active_history_backfill_run(paths)
            stale = projection_refresh_status(paths, stale_id)
            next_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=[{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "single"}],
            )

            self.assertIsNone(active)
            self.assertEqual(stale["status"], "failed")
            self.assertTrue(stale["metadata"]["orphaned"])
            self.assertIn("worker exited unexpectedly", stale["error_summary"])
            self.assertEqual(active_history_backfill_run(paths)["id"], next_id)

    def test_history_backfill_cancel_queued_run_unblocks_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)

            result = cancel_history_backfill(paths, run_id)
            next_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)

            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "cancelled")
            self.assertEqual(active_history_backfill_run(paths)["id"], next_id)

    def test_history_backfill_cancelled_queued_run_is_not_revived_by_late_background_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)
            cancel_history_backfill(paths, run_id)

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 2),
                periods=periods,
                daily_pipeline_runner=lambda day, runtime_paths: self.fail("cancelled run should not execute"),
                period_builder=lambda selected_start, days: self.fail("cancelled run should not materialize periods"),
            )

            self.assertTrue(result["cancelled"])
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "cancelled")

    def test_history_backfill_running_cancel_stops_after_current_daily_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-03", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 3), periods=periods)
            called = []

            def runner(selected_day, runtime_paths):
                called.append(selected_day)
                cancel_history_backfill(paths, run_id)
                return object()

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 3),
                periods=periods,
                daily_pipeline_runner=runner,
                ai_assets_builder=lambda: self.fail("cancelled run should not refresh AI Assets"),
                period_builder=lambda selected_start, days: self.fail("cancelled run should not materialize periods"),
            )

            status = projection_refresh_status(paths, run_id)
            self.assertEqual(called, ["2026-04-01"])
            self.assertTrue(result["cancelled"])
            self.assertEqual(status["status"], "cancelled")
            self.assertEqual(status["metadata"]["dailyPipeline"]["completed"], ["2026-04-01"])

    def test_history_daily_nova_task_action_runs_materializer_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")

            for materialized, expected_success in ((True, True), (False, False)):
                with self.subTest(materialized=materialized):
                    with patch("data_foundation.pipeline.materialize_nova_task_outputs", return_value=materialized) as materializer:
                        result = _run_history_daily_actions(
                            date(2026, 4, 1),
                            paths,
                            ["nova-task-work-graph"],
                            reuse_foundation_inputs=False,
                        )

                    materializer.assert_called_once_with("2026-04-01", paths)
                    self.assertEqual(result.success, expected_success)
                    self.assertEqual(result.failed_step, None if expected_success else "Nova-Task Work Graph")

    def test_history_daily_combined_materialization_and_nova_task_actions_run_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")

            with (
                patch("data_foundation.pipeline.materialize_pipeline_foundation_outputs", return_value=True) as foundation,
                patch("data_foundation.pipeline.materialize_nova_task_outputs", return_value=True) as nova_task,
            ):
                result = _run_history_daily_actions(
                    date(2026, 4, 1),
                    paths,
                    ["daily-materialization", "nova-task-work-graph"],
                    reuse_foundation_inputs=False,
                )

            foundation.assert_called_once_with("2026-04-01", paths)
            nova_task.assert_called_once_with("2026-04-01", paths)
            self.assertTrue(result.success)
            self.assertIsNone(result.failed_step)

    def test_scheduled_history_backfill_runs_only_when_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-04-30"
            day.mkdir(parents=True)
            (day / "日记-260430.md").write_text("# 2026年04月30日 日记\n", encoding="utf-8")
            (day / "技术进展-260430.md").write_text("# technical\n", encoding="utf-8")
            (day / "智慧沉淀-260430.md").write_text("# learning\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            due_id = queue_history_backfill(
                paths,
                date(2026, 4, 30),
                date(2026, 4, 30),
                grain="month",
                scheduled_at="2026-06-17T01:00:00+08:00",
                periods=[{"kind": "week", "start": "2026-04-30", "end": "2026-04-30", "label": "single-day"}],
            )

            due = due_scheduled_history_backfills(paths, now=datetime(2026, 6, 17, 2, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")))
            self.assertEqual([item["id"] for item in due], [due_id])

            with patch("data_foundation.refresh._run_history_daily_actions", return_value=object()):
                results = run_due_scheduled_history_backfills(
                    paths,
                    now=datetime(2026, 6, 17, 2, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                )

            self.assertEqual(len(results), 1)
            self.assertEqual(projection_refresh_status(paths, due_id)["status"], "completed")

    def test_scheduled_history_backfill_naive_time_uses_runtime_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"general": {"timezone": "America/Los_Angeles"}}, paths)
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 30),
                date(2026, 4, 30),
                grain="month",
                scheduled_at="2026-06-17T10:00:00",
                periods=[{"kind": "week", "start": "2026-04-30", "end": "2026-04-30", "label": "single-day"}],
            )

            due = due_scheduled_history_backfills(paths, now=datetime(2026, 6, 17, 9, 30, tzinfo=ZoneInfo("UTC")))

            self.assertEqual(due, [])
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "scheduled")

    def test_history_backfill_retry_failed_periods_queues_only_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                grain="month",
                include_summaries=False,
            )

            def failing_builder(selected_start, days):
                raise RuntimeError("period failed")

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
                grain="month",
                include_summaries=False,
                daily_pipeline_runner=lambda day, runtime_paths: object(),
                period_builder=failing_builder,
            )
            retry_id = queue_failed_history_backfill_retry(paths, run_id)
            retry = projection_refresh_status(paths, retry_id)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "partial")
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(retry["metadata"]["sourceRunId"], run_id)
            self.assertFalse(retry["metadata"]["overwriteDaily"])
            self.assertEqual(len(retry["metadata"]["periods"]), 1)
            self.assertEqual(retry["metadata"]["periods"][0]["start"], "2026-04-01")
            self.assertEqual(retry["metadata"]["dailyPipelineDays"], 0)
            self.assertEqual(retry["metadata"]["llmCallCount"], 0)

    def test_history_backfill_retry_preserves_overwrite_daily_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 30),
                grain="month",
                include_summaries=False,
                overwrite_daily=True,
            )
            run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
                grain="month",
                include_summaries=False,
                daily_pipeline_runner=lambda day, runtime_paths: object(),
                period_builder=lambda selected_start, days: (_ for _ in ()).throw(RuntimeError("period failed")),
            )

            retry_id = queue_failed_history_backfill_retry(paths, run_id)
            retry = projection_refresh_status(paths, retry_id)

            self.assertTrue(retry["metadata"]["overwriteDaily"])

    def test_history_backfill_daily_failure_retry_is_snapshot_frozen_to_failed_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 2), periods=periods)

            class FailedPipeline:
                success = False
                failed_step = "Narrative Pass"

            def daily_runner(selected_day, runtime_paths):
                return FailedPipeline() if selected_day == "2026-04-02" else object()

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 2),
                grain="selected",
                periods=periods,
                daily_pipeline_runner=daily_runner,
                period_builder=lambda selected_start, days: {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}},
            )
            status = projection_refresh_status(paths, run_id)
            retry_id = queue_failed_history_backfill_retry(paths, run_id)
            retry = projection_refresh_status(paths, retry_id)

            self.assertEqual(result["dailyPipeline"]["failed"], [{"date": "2026-04-02", "error": "Narrative Pass"}])
            self.assertEqual(status["status"], "partial")
            self.assertIn("daily pipeline", status["error_summary"])
            self.assertEqual(len(retry["metadata"]["periods"]), 1)
            self.assertEqual(retry["metadata"]["periods"][0]["kind"], "day")
            self.assertEqual(retry["metadata"]["periods"][0]["start"], "2026-04-02")
            self.assertEqual(retry["metadata"]["periods"][0]["end"], "2026-04-02")
            self.assertEqual(
                [stage["id"] for stage in retry["metadata"]["requestedStages"]],
                ["daily:2026-04-02"],
            )
            self.assertEqual(retry["metadata"]["dailyPipelineDays"], 1)
            self.assertEqual(retry["metadata"]["llmCallCount"], 3)

    def test_history_backfill_refreshes_ai_assets_snapshot_when_daily_pipeline_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)

            class FailedPipeline:
                success = False
                failed_step = "Narrative Pass"

            with patch(
                "data_foundation.snapshots.detect_external_tools",
                return_value={
                    "detectedToolKeys": ["codex"],
                    "toolPresence": {
                        "codex": {
                            "id": "codex",
                            "detected": True,
                            "status": "detected",
                        }
                    },
                },
            ):
                result = run_history_backfill(
                    paths,
                    run_id,
                    start_date=date(2026, 4, 1),
                    end_date=date(2026, 4, 1),
                    grain="selected",
                    periods=periods,
                    daily_pipeline_runner=lambda selected_day, runtime_paths: FailedPipeline(),
                    ai_assets_builder=lambda: {
                        "tools": [{"name": "Codex", "allTimeTokens": 128}],
                        "totalTokens": 128,
                    },
                    period_builder=lambda selected_start, days: {
                        "models": [],
                        "workspaceUsage": [],
                        "assetHourlyHeatmap": {},
                    },
                )

            status = projection_refresh_status(paths, run_id)
            snapshot = read_dashboard_snapshot(paths)

            self.assertEqual(result["dailyPipeline"]["failed"], [{"date": "2026-04-01", "error": "Narrative Pass"}])
            self.assertEqual(status["status"], "partial")
            self.assertEqual(status["metadata"]["aiAssetsSnapshot"]["status"], "ready")
            self.assertEqual(snapshot["sourceRunId"], run_id)
            self.assertEqual(snapshot["payload"]["detectedToolKeys"], ["codex"])
            self.assertEqual(
                snapshot["payload"]["tools"],
                [{"name": "Codex", "allTimeTokens": 128}],
            )
            self.assertEqual(snapshot["payload"]["totalTokens"], 128)

    def test_history_backfill_snapshot_only_failure_is_failed_and_retries_only_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            run_id = queue_history_backfill(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                periods=[],
            )
            synthetic_secret_error = (
                "SYNTHETIC_SNAPSHOT_FAILURE "
                + "api"
                + "_key=SYNTHETIC_SECRET_VALUE"
            )

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                periods=[],
                daily_pipeline_runner=lambda day, runtime_paths: self.fail("snapshot-only run has no daily stage"),
                ai_assets_builder=lambda: (_ for _ in ()).throw(
                    RuntimeError(synthetic_secret_error)
                ),
                period_builder=lambda selected_start, days: self.fail("snapshot-only run has no period stage"),
            )
            source = projection_refresh_status(paths, run_id)
            retry_id = queue_failed_history_backfill_retry(paths, run_id)
            retry = projection_refresh_status(paths, retry_id)
            retry_result = run_history_backfill(
                paths,
                retry_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                periods=[],
                daily_pipeline_runner=lambda day, runtime_paths: self.fail("snapshot retry expanded into daily"),
                ai_assets_builder=lambda: {"tools": [], "totalTokens": 0},
                period_builder=lambda selected_start, days: self.fail("snapshot retry expanded into period"),
            )
            retried = projection_refresh_status(paths, retry_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(source["status"], "failed")
        self.assertIn("AI Assets snapshot", source["error_summary"])
        self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(source, sort_keys=True))
        self.assertIn("[REDACTED]", source["error_summary"])
        self.assertEqual([stage["id"] for stage in source["metadata"]["retryStages"]], ["snapshot:ai-assets"])
        self.assertEqual(retry["metadata"]["periods"], [])
        self.assertEqual([stage["id"] for stage in retry["metadata"]["requestedStages"]], ["snapshot:ai-assets"])
        self.assertEqual(retry["metadata"]["dailyPipelineDays"], 0)
        self.assertEqual(retry["metadata"]["llmCallCount"], 0)
        self.assertEqual(retry_result["status"], "completed")
        self.assertEqual(retried["status"], "completed")

    def test_history_backfill_zero_commits_with_daily_and_snapshot_failures_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "day", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-04-01"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)

            class FailedPipeline:
                success = False
                failed_step = "Narrative Pass"

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                periods=periods,
                daily_pipeline_runner=lambda day, runtime_paths: FailedPipeline(),
                ai_assets_builder=lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
            )
            status = projection_refresh_status(paths, run_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(
            status["metadata"]["failedStages"],
            ["daily:2026-04-01", "snapshot:ai-assets"],
        )
        self.assertEqual(len(status["metadata"]["retryStages"]), 2)

    def test_history_backfill_cancel_during_claimed_final_period_wins_terminal_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)
            entered = threading.Event()
            release = threading.Event()
            observed = {}

            def builder(selected_start, days):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test did not release period builder")
                return {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}}

            def worker():
                try:
                    observed["result"] = run_history_backfill(
                        paths,
                        run_id,
                        start_date=date(2026, 4, 1),
                        end_date=date(2026, 4, 1),
                        periods=periods,
                        daily_pipeline_runner=lambda day, runtime_paths: object(),
                        period_builder=builder,
                    )
                except Exception as error:
                    observed["error"] = error

            with (
                patch("data_foundation.refresh.materialize_diary_markdown_period_documents", return_value={}),
                patch("data_foundation.refresh.materialize_diary_period_page_snapshot", return_value="page"),
            ):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(timeout=5))
                cancel_result = cancel_history_backfill(paths, run_id)
                release.set()
                thread.join(timeout=10)

            status = projection_refresh_status(paths, run_id)
            second_cancel = cancel_history_backfill(paths, run_id)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", observed)
        self.assertEqual(cancel_result["status"], "cancel_requested")
        self.assertTrue(observed["result"]["cancelled"])
        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["metadata"]["cancelRequested"])
        self.assertFalse(second_cancel["cancelRequested"])

    def test_history_backfill_snapshot_and_period_orphans_unblock_queue(self):
        for stage_kind in ("snapshot", "period"):
            with self.subTest(stage_kind=stage_kind), tempfile.TemporaryDirectory() as tmp:
                paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
                periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
                run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)
                run = projection_refresh_status(paths, run_id)
                requested = run["metadata"]["requestedStages"]
                stage = next(item for item in requested if item["kind"] == stage_kind)
                metadata = dict(run["metadata"])
                metadata.update(
                    {
                        "currentStage": "history-ai-assets-snapshot" if stage_kind == "snapshot" else "history-backfill-period",
                        "currentStageId": stage["id"],
                        "stageClaimedAt": "2000-01-01T00:00:00+00:00",
                        "heartbeatAt": "2000-01-01T00:00:00+00:00",
                        "workerPid": os.getpid(),
                    }
                )
                with connect(paths) as connection:
                    connection.execute(
                        "UPDATE ingestion_runs SET status = 'running', started_at = ?, adapter_versions_json = ? WHERE id = ?",
                        ("2000-01-01T00:00:00+00:00", json.dumps(metadata, sort_keys=True), run_id),
                    )

                active = active_history_backfill_run(paths)
                orphan = projection_refresh_status(paths, run_id)
                next_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)

                self.assertIsNone(active)
                self.assertEqual(orphan["status"], "failed")
                self.assertTrue(orphan["metadata"]["orphaned"])
                self.assertIn(stage["id"], orphan["metadata"]["failedStages"])
                self.assertEqual(active_history_backfill_run(paths)["id"], next_id)

    def test_history_backfill_legacy_failed_ledger_retry_is_read_only_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            period = {"kind": "week", "start": "2026-04-01", "end": "2026-04-02", "label": "2026-W14"}
            source_id = begin_ingestion_run(
                paths,
                trigger_type="dashboard-history-backfill",
                business_date=date(2026, 4, 2),
                status="failed",
                adapter_versions={
                    "periodStart": "2026-04-01",
                    "periodEnd": "2026-04-02",
                    "grain": "selected",
                    "includeSummaries": False,
                    "skipReady": True,
                    "overwriteDaily": False,
                    "periods": [period],
                    "failedPeriodDetails": [{**period, "status": "failed", "error": "legacy failure"}],
                },
            )
            with connect(paths, read_only=True) as connection:
                before = connection.execute(
                    "SELECT adapter_versions_json FROM ingestion_runs WHERE id = ?",
                    (source_id,),
                ).fetchone()[0]

            retry_id = queue_failed_history_backfill_retry(paths, source_id)
            retry = projection_refresh_status(paths, retry_id)
            with connect(paths, read_only=True) as connection:
                after = connection.execute(
                    "SELECT adapter_versions_json FROM ingestion_runs WHERE id = ?",
                    (source_id,),
                ).fetchone()[0]

        self.assertEqual(after, before)
        self.assertEqual(retry["metadata"]["sourceRunId"], source_id)
        self.assertEqual(retry["metadata"]["outcomeProvenance"], "native-v2")
        self.assertTrue(retry["metadata"]["requestedStages"])

    def test_history_backfill_materializes_diary_markdown_before_asset_projection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026" / "diary-2026-04" / "04-01"
            day.mkdir(parents=True)
            (day / "日记-260401.md").write_text(
                """# 2026年04月01日 日记

## 天气
weather

## 今日概要
Generated before asset projection failed.
""",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(
                initialize_home(root / "Actanara", legacy_diary_root=diary_root).home,
                generated_diary_root=diary_root,
                legacy_diary_root=diary_root,
            )
            periods = [{"kind": "week", "start": "2026-04-01", "end": "2026-04-01", "label": "2026-W14"}]
            run_id = queue_history_backfill(paths, date(2026, 4, 1), date(2026, 4, 1), periods=periods)

            result = run_history_backfill(
                paths,
                run_id,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                grain="selected",
                periods=periods,
                daily_pipeline_runner=lambda selected_day, runtime_paths: object(),
                ai_assets_builder=lambda: {"tools": [], "totalTokens": 0},
                period_builder=lambda selected_start, days: (_ for _ in ()).throw(RuntimeError("asset projection failed")),
            )

            docs = read_diary_markdown_documents(paths, date(2026, 4, 1), date(2026, 4, 1))
            source = projection_refresh_status(paths, run_id)
            retry_id = queue_failed_history_backfill_retry(paths, run_id)
            retry = projection_refresh_status(paths, retry_id)
            retry_stage = retry["metadata"]["requestedStages"][0]
            page_before_retry = read_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )

            with (
                patch("data_foundation.refresh.materialize_diary_markdown_period_documents") as materialize_documents,
                patch("data_foundation.refresh.materialize_diary_period_page_snapshot") as materialize_page,
            ):
                retry_result = run_history_backfill(
                    paths,
                    retry_id,
                    start_date=date(2026, 4, 1),
                    end_date=date(2026, 4, 1),
                    grain="selected",
                    periods=retry["metadata"]["periods"],
                    period_builder=lambda selected_start, days: {
                        "models": [],
                        "workspaceUsage": [],
                        "assetHourlyHeatmap": {},
                    },
                )

            page_after_retry = read_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )
            assets_after_retry = read_period_projection(
                paths,
                date(2026, 4, 1),
                date(2026, 4, 1),
                projection_type=LEGACY_ASSET_PROJECTION,
            )

            self.assertEqual(result["periods"][0]["status"], "failed")
            self.assertEqual(source["status"], "partial")
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["report_type"], "narrative")
            self.assertEqual(docs[0]["sections"][0]["heading"], "天气")
            self.assertEqual(retry_stage["retryArtifacts"], ["assets"])
            self.assertEqual(retry_stage["preservedArtifacts"], ["diaryDocuments", "page"])
            self.assertEqual(retry_result["status"], "completed")
            materialize_documents.assert_not_called()
            materialize_page.assert_not_called()
            self.assertEqual(page_before_retry["sourceRunId"], run_id)
            self.assertEqual(page_after_retry["sourceRunId"], run_id)
            self.assertEqual(assets_after_retry["sourceRunId"], retry_id)

    def test_pipeline_generates_current_week_summary_only_when_week_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-24"
            day.mkdir(parents=True)
            (day / "日记-260524.md").write_text(
                """# 2026年05月24日 日记

## 今日概要

### Weekly finalization
- Finished the weekly summary gating.
""",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            result = run_pipeline_daily_materialization(
                paths,
                date(2026, 5, 24),
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}},
            )
            summaries = {item["label"]: item for item in result["completedPeriodSummaries"]}
            self.assertEqual(summaries["completed-week"]["start"], "2026-05-18")
            self.assertEqual(summaries["completed-week"]["end"], "2026-05-24")
            rolling_summary = read_period_projection(
                paths,
                date(2026, 5, 1),
                date(2026, 5, 24),
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            completed_week = read_period_projection(
                paths,
                date(2026, 5, 18),
                date(2026, 5, 24),
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            self.assertIsNone(rolling_summary)
            self.assertIsNotNone(completed_week)

    def test_due_period_summary_failure_does_not_fail_pipeline_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-24"
            day.mkdir(parents=True)
            (day / "日记-260524.md").write_text("# 2026年05月24日 日记\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)

            def flaky_period_builder(selected_start, days):
                if selected_start == date(2026, 4, 1):
                    raise RuntimeError("summary assets failed")
                return {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}}

            result = run_pipeline_daily_materialization(
                paths,
                date(2026, 5, 24),
                ai_assets_builder=lambda: {"tools": []},
                period_builder=flaky_period_builder,
            )

            self.assertEqual(projection_refresh_status(paths, result["runId"])["status"], "completed")
            failed = [item for item in result["completedPeriodSummaries"] if item["label"] == "completed-month"][0]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "summary assets failed")

    def test_due_period_summary_detection_failure_does_not_fail_pipeline_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-24"
            day.mkdir(parents=True)
            (day / "日记-260524.md").write_text("# 2026年05月24日 日记\n", encoding="utf-8")
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)

            with patch("data_foundation.refresh.completed_period_summary_targets", side_effect=RuntimeError("target lookup failed")):
                result = run_pipeline_daily_materialization(
                    paths,
                    date(2026, 5, 24),
                    ai_assets_builder=lambda: {"tools": []},
                    period_builder=lambda selected_start, days: {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}},
                )

            self.assertEqual(projection_refresh_status(paths, result["runId"])["status"], "completed")
            self.assertEqual(result["completedPeriodSummaries"][0]["label"], "completed-period-detection")
            self.assertEqual(result["completedPeriodSummaries"][0]["status"], "failed")
            self.assertEqual(result["completedPeriodSummaries"][0]["error"], "target lookup failed")

    def test_queued_projection_refresh_materializes_both_models_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            end = date(2026, 5, 19)
            start = date(2026, 5, 13)
            run_id = queue_projection_refresh(paths, end, period_start=start)
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "queued")
            run_projection_refresh(
                paths,
                run_id,
                period_start=start,
                period_days=7,
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {"models": [], "workspaceUsage": [], "assetHourlyHeatmap": {}},
            )
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "completed")
            self.assertEqual(read_dashboard_snapshot(paths)["sourceRunId"], run_id)
            self.assertEqual(read_period_projection(paths, start, end)["sourceRunId"], run_id)
            self.assertEqual(
                read_period_projection(paths, start, end, projection_type=DIARY_PERIOD_PAGE_PROJECTION)["sourceRunId"],
                run_id,
            )

    def test_recent_refresh_jobs_exposes_period_scope_and_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            end = date(2026, 5, 19)
            start = date(2026, 5, 13)
            run_id = queue_projection_refresh(paths, end, period_start=start)
            with self.assertRaises(RuntimeError):
                run_projection_refresh(
                    paths,
                    run_id,
                    period_start=start,
                    period_days=7,
                    ai_assets_builder=lambda: {"tools": []},
                    period_builder=lambda selected_start, days: (_ for _ in ()).throw(RuntimeError("boom")),
                )
            jobs = recent_projection_refresh_jobs(paths, limit=5)
            self.assertEqual(jobs[0]["id"], run_id)
            self.assertEqual(jobs[0]["status"], "failed")
            self.assertEqual(jobs[0]["error_summary"], "boom")
            self.assertEqual(jobs[0]["metadata"]["periodStart"], "2026-05-13")
            self.assertEqual(jobs[0]["metadata"]["periodDays"], 7)

    def test_queued_period_summary_refresh_materializes_page_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-19"
            day.mkdir(parents=True)
            (day / "日记-260519.md").write_text(
                """# 2026年05月19日 日记

## 今日概要

### Summary button backend landed
- Added additive snapshot
""",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            end = date(2026, 5, 19)
            start = date(2026, 5, 19)
            run_id = queue_period_summary_refresh(paths, end, period_start=start)
            queued = projection_refresh_status(paths, run_id)
            self.assertEqual(queued["metadata"]["workEstimate"]["periodDays"], 1)
            self.assertEqual(queued["metadata"]["workEstimate"]["llmCalls"], 1)
            self.assertFalse(queued["metadata"]["workEstimate"]["longRunning"])
            run_period_summary_refresh(paths, run_id, period_start=start, period_days=1)
            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "completed")
            summary = read_period_projection(paths, start, end, projection_type=DIARY_PERIOD_SUMMARY_PROJECTION)
            self.assertEqual(summary["sourceRunId"], run_id)
            self.assertIn("Summary button backend landed", summary["metrics"]["summary"]["lead"])

    def test_period_summary_refresh_reuses_ready_asset_and_page_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            migrate(paths)
            start = end = date(2026, 5, 19)
            source_run_id = begin_ingestion_run(paths, trigger_type="test", business_date=end, status="completed")
            write_period_projection(
                paths,
                start,
                end,
                {"kpi": {"totalTokens": 100}, "workspaceUsage": []},
                source_run_id=source_run_id,
                projection_type=LEGACY_ASSET_PROJECTION,
            )
            write_period_projection(
                paths,
                start,
                end,
                {"summaryTopics": [{"title": "Ready page projection", "items": ["Already materialized"]}], "lessons": []},
                source_run_id=source_run_id,
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )
            run_id = queue_period_summary_refresh(paths, end, period_start=start)

            def unexpected_builder(selected_start, days):
                raise AssertionError("ready period asset projection should be reused")

            run_period_summary_refresh(paths, run_id, period_start=start, period_days=1, period_builder=unexpected_builder)

            status = projection_refresh_status(paths, run_id)
            summary = read_period_projection(paths, start, end, projection_type=DIARY_PERIOD_SUMMARY_PROJECTION)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["metadata"]["currentStage"], "completed")
            self.assertIn("Ready page projection", summary["metrics"]["summary"]["lead"])

    def test_period_summary_refresh_materializes_missing_page_projection_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            day = diary_root / "diary-2026-05-19"
            day.mkdir(parents=True)
            (day / "日记-260519.md").write_text(
                """# 2026年05月19日 日记

## 今日概要

### Missing page projection
- Rebuild page only
""",
                encoding="utf-8",
            )
            paths = update_runtime_manifest_paths(initialize_home(root / "Actanara", legacy_diary_root=diary_root).home, generated_diary_root=diary_root, legacy_diary_root=diary_root)
            migrate(paths)
            start = end = date(2026, 5, 19)
            source_run_id = begin_ingestion_run(paths, trigger_type="test", business_date=end, status="completed")
            write_period_projection(
                paths,
                start,
                end,
                {"kpi": {"totalTokens": 100}, "workspaceUsage": []},
                source_run_id=source_run_id,
                projection_type=LEGACY_ASSET_PROJECTION,
            )
            run_id = queue_period_summary_refresh(paths, end, period_start=start)

            def unexpected_builder(selected_start, days):
                raise AssertionError("ready period asset projection should be reused")

            run_period_summary_refresh(paths, run_id, period_start=start, period_days=1, period_builder=unexpected_builder)

            self.assertEqual(projection_refresh_status(paths, run_id)["status"], "completed")
            self.assertEqual(read_period_projection(paths, start, end, projection_type=DIARY_PERIOD_PAGE_PROJECTION)["sourceRunId"], run_id)
            summary = read_period_projection(paths, start, end, projection_type=DIARY_PERIOD_SUMMARY_PROJECTION)
            self.assertIn("Missing page projection", summary["metrics"]["summary"]["lead"])

    def test_dashboard_refresh_initializes_default_runtime_with_diary_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Actanara"
            with patch.dict(os.environ, {"ACTANARA_HOME": str(home)}):
                run_id = dashboard_foundation.queue_period_summary(date(2026, 5, 19), period_start=date(2026, 5, 19))
            self.assertGreater(run_id, 0)
            self.assertTrue((home / "config" / "runtime.json").exists())
            self.assertIn(str(home / "artifacts" / "diary"), (home / "config" / "runtime.json").read_text(encoding="utf-8"))

    def test_dashboard_refresh_job_list_reports_latest_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            ok_id = queue_projection_refresh(paths, date(2026, 5, 19))
            run_projection_refresh(paths, ok_id, ai_assets_builder=lambda: {"tools": []})
            failed_id = queue_projection_refresh(paths, date(2026, 5, 20), period_start=date(2026, 5, 20))
            with self.assertRaises(RuntimeError):
                run_projection_refresh(
                    paths,
                    failed_id,
                    period_start=date(2026, 5, 20),
                    period_days=1,
                    ai_assets_builder=lambda: {"tools": []},
                    period_builder=lambda selected_start, days: (_ for _ in ()).throw(RuntimeError("planned failure")),
                )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                result = dashboard_foundation.list_refresh_jobs(limit=10)
            self.assertEqual(result["latest"]["id"], failed_id)
            self.assertEqual(result["latestFailed"]["id"], failed_id)
            self.assertEqual(result["jobs"][1]["id"], ok_id)

    def test_readiness_reports_materialized_non_rag_models_as_enableable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara")
            end = date(2026, 5, 19)
            start = date(2026, 5, 13)
            run_id = queue_projection_refresh(paths, end, period_start=start)
            run_projection_refresh(
                paths,
                run_id,
                period_start=start,
                period_days=7,
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {
                    "models": [],
                    "memoryStats": {"sessionFiles": 2},
                    "knowledgePeriodMemoryCurrent": {"sessionFiles": 2},
                },
            )
            materialize_diary_tasks_snapshot(
                paths,
                end,
                run_id,
                builder=lambda: {"InProgress": 1, "Completed": 0},
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                readiness = dashboard_foundation.get_reader_readiness(period_start=start, period_days=7)
            self.assertEqual(readiness["configuredSources"]["aiAssets"], "foundation")
            self.assertEqual(readiness["configuredSources"]["periodAssets"], "foundation")
            self.assertEqual(readiness["configuredSources"]["diaryMetrics"], "foundation")
            self.assertEqual(readiness["configuredSources"]["diaryMemory"], "foundation")
            self.assertEqual(readiness["configuredSources"]["diaryTasks"], "foundation")
            self.assertEqual(readiness["configuredSources"]["taskAuditSink"], "foundation")
            self.assertEqual(readiness["configuredSourceEnvNames"]["diaryMemory"], "DIARY_MEMORY_SOURCE")
            self.assertEqual(readiness["configuredSourceFields"]["diaryMemory"], "diaryMemorySource")
            self.assertEqual(readiness["preservedSources"]["rag"], "v2")
            self.assertTrue(readiness["canEnable"]["dashboardReadSourceFoundation"])
            self.assertTrue(readiness["canEnable"]["reportReadSourceFoundation"])
            self.assertTrue(readiness["canEnable"]["diaryMetricsSourceFoundation"])
            self.assertTrue(readiness["canEnable"]["diaryMemorySourceFoundation"])
            self.assertTrue(readiness["canEnable"]["diaryTasksSourceFoundation"])
            self.assertTrue(readiness["canEnable"]["taskAuditSinkFoundation"])
            self.assertTrue(readiness["diaryMemory"]["ready"])
            self.assertEqual(readiness["taskAuditSink"]["source"], "nova-task-v2-sqlite")
            self.assertTrue(readiness["periodAssets"]["memoryReady"])
            self.assertTrue(readiness["periodPage"]["ready"])
            self.assertEqual(readiness["periodSummary"]["status"], "missing")

    def test_readiness_blocks_diary_tasks_enablement_when_task_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara")
            end = date(2026, 5, 19)
            start = date(2026, 5, 13)
            run_id = queue_projection_refresh(paths, end, period_start=start)
            run_projection_refresh(
                paths,
                run_id,
                period_start=start,
                period_days=7,
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {
                    "models": [],
                    "memoryStats": {"sessionFiles": 2},
                    "knowledgePeriodMemoryCurrent": {"sessionFiles": 2},
                },
            )

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                readiness = dashboard_foundation.get_reader_readiness(period_start=start, period_days=7)

            self.assertFalse(readiness["canEnable"]["diaryTasksSourceFoundation"])
            self.assertEqual(readiness["diaryTasks"]["status"], "diary_tasks_snapshot_missing")
            self.assertEqual(readiness["diaryTasks"]["businessDate"], "2026-05-19")

    def test_readiness_blocks_period_enablement_when_memory_projection_is_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara")
            end = date(2026, 5, 19)
            start = date(2026, 5, 13)
            run_id = queue_projection_refresh(paths, end, period_start=start)
            run_projection_refresh(
                paths,
                run_id,
                period_start=start,
                period_days=7,
                ai_assets_builder=lambda: {"tools": []},
                period_builder=lambda selected_start, days: {"models": []},
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                readiness = dashboard_foundation.get_reader_readiness(period_start=start, period_days=7)
            self.assertFalse(readiness["canEnable"]["reportReadSourceFoundation"])
            self.assertEqual(readiness["periodAssets"]["status"], "memory_fields_missing")

    def test_readiness_blocks_period_enablement_when_page_projection_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara")
            migrate(paths)
            start = date(2026, 5, 13)
            end = date(2026, 5, 19)
            write_period_projection(
                paths,
                start,
                end,
                {
                    "models": [],
                    "memoryStats": {"sessionFiles": 2},
                    "knowledgePeriodMemoryCurrent": {"sessionFiles": 2},
                },
                source_run_id=None,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                readiness = dashboard_foundation.get_reader_readiness(period_start=start, period_days=7)
            self.assertTrue(readiness["periodAssets"]["ready"])
            self.assertEqual(readiness["periodPage"]["status"], "missing")
            self.assertFalse(readiness["canEnable"]["reportReadSourceFoundation"])

    def test_readiness_reports_period_summary_without_blocking_report_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara")
            migrate(paths)
            start = date(2026, 5, 13)
            end = date(2026, 5, 19)
            write_period_projection(
                paths,
                start,
                end,
                {
                    "models": [],
                    "memoryStats": {"sessionFiles": 2},
                    "knowledgePeriodMemoryCurrent": {"sessionFiles": 2},
                },
                source_run_id=None,
            )
            write_period_projection(
                paths,
                start,
                end,
                {"summaryTopics": []},
                source_run_id=None,
                projection_type=DIARY_PERIOD_PAGE_PROJECTION,
            )
            write_period_projection(
                paths,
                start,
                end,
                {"summary": {"lead": "ready"}},
                source_run_id=None,
                projection_type=DIARY_PERIOD_SUMMARY_PROJECTION,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                readiness = dashboard_foundation.get_reader_readiness(period_start=start, period_days=7)
            self.assertTrue(readiness["canEnable"]["reportReadSourceFoundation"])
            self.assertTrue(readiness["periodSummary"]["ready"])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_ai_assets_refresh_endpoint_queues_background_task(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        with (
            patch.object(ai_assets_router.foundation, "queue_refresh", return_value=41) as queue,
            patch.object(ai_assets_router.foundation, "execute_refresh") as execute,
        ):
            response = asyncio.run(
                ai_assets_router.api_refresh_ai_assets(tasks, {"businessDate": "2026-05-19"})
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(json.loads(response.body)["runId"], 41)
            queue.assert_called_once_with(date(2026, 5, 19))
            self.assertEqual(len(tasks.tasks), 1)
            asyncio.run(tasks())
            execute.assert_called_once_with(41)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_tool_config_discovery_endpoint_reports_cache_side_effects(self):
        from app.routers import ai_assets as ai_assets_router

        payload = {
            "toolConfigs": [{"name": "Codex"}],
            "sideEffects": ["tool-config-snapshot-write", "ai-assets-cache-invalidation"],
            "snapshotPath": "/tmp/.dashboard-tool-configs.json",
        }
        with patch.object(ai_assets_router.ai_assets, "refresh_tool_configs_with_metadata", return_value=payload) as refresh:
            response = asyncio.run(ai_assets_router.api_discover_tool_configs())

        self.assertEqual(response, payload)
        refresh.assert_called_once_with()

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_endpoint_rejects_concurrent_active_run(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        payload = {
            "start": "2026-06-01",
            "end": "2026-06-07",
            "grain": "selected",
            "periods": [{"kind": "week", "start": "2026-06-01", "end": "2026-06-07", "label": "2026-W23"}],
        }
        active = {"id": 23, "status": "running"}
        with (
            patch.object(ai_assets_router.foundation, "plan_history_backfill_request", return_value={"periodCount": 1, "llmCallCount": 3, "dailyPipelineDays": 1}),
            patch.object(
                ai_assets_router.foundation,
                "queue_history_backfill_request",
                side_effect=ai_assets_router.HistoryBackfillAlreadyActiveError(active),
            ),
        ):
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill(tasks, payload))

        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body)
        self.assertEqual(body["activeRunId"], 23)
        self.assertEqual(body["activeStatus"], "running")
        self.assertEqual(len(tasks.tasks), 0)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_endpoint_rejects_overwrite_without_confirmation(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        payload = {
            "start": "2026-06-01",
            "end": "2026-06-01",
            "grain": "selected",
            "periods": [{"kind": "week", "start": "2026-06-01", "end": "2026-06-01", "label": "2026-W23"}],
            "skipReady": False,
        }
        with patch.object(
            ai_assets_router.foundation,
            "plan_history_backfill_request",
            return_value={
                "periodCount": 1,
                "llmCallCount": 3,
                "dailyPipelineDays": 1,
                "existingDiaryDays": 1,
                "overwriteItemCount": 1,
            },
        ):
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill(tasks, payload))

        self.assertEqual(response.status_code, 400)
        self.assertIn("overwriteDaily", json.loads(response.body)["error"])
        self.assertEqual(len(tasks.tasks), 0)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_endpoint_allows_skip_ready_false_without_existing_diary(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        payload = {
            "start": "2026-06-01",
            "end": "2026-06-01",
            "grain": "selected",
            "periods": [{"kind": "week", "start": "2026-06-01", "end": "2026-06-01", "label": "2026-W23"}],
            "skipReady": False,
        }
        with (
            patch.object(
                ai_assets_router.foundation,
                "plan_history_backfill_request",
                return_value={"periodCount": 1, "llmCallCount": 3, "dailyPipelineDays": 1, "existingDiaryDays": 0},
            ),
            patch.object(ai_assets_router.foundation, "queue_history_backfill_request", return_value=41) as queue,
        ):
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill(tasks, payload))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(queue.call_args.kwargs["skip_ready"], False)
        self.assertEqual(queue.call_args.kwargs["overwrite_daily"], False)
        self.assertEqual(len(tasks.tasks), 1)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_endpoint_queues_confirmed_overwrite(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        payload = {
            "start": "2026-06-01",
            "end": "2026-06-01",
            "grain": "selected",
            "periods": [{"kind": "week", "start": "2026-06-01", "end": "2026-06-01", "label": "2026-W23"}],
            "skipReady": False,
            "overwriteDaily": True,
        }
        with (
            patch.object(ai_assets_router.foundation, "plan_history_backfill_request", return_value={"periodCount": 1, "llmCallCount": 3, "dailyPipelineDays": 1}),
            patch.object(ai_assets_router.foundation, "queue_history_backfill_request", return_value=42) as queue,
        ):
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill(tasks, payload))

        self.assertEqual(response.status_code, 202)
        body = json.loads(response.body)
        self.assertTrue(body["overwriteDaily"])
        self.assertEqual(queue.call_args.kwargs["skip_ready"], False)
        self.assertEqual(queue.call_args.kwargs["overwrite_daily"], True)
        self.assertEqual(len(tasks.tasks), 1)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_retry_endpoint_preserves_overwrite_execution_flag(self):
        from fastapi import BackgroundTasks
        from app.routers import ai_assets as ai_assets_router

        tasks = BackgroundTasks()
        status = {
            "metadata": {
                "periodStart": "2026-06-01",
                "periodEnd": "2026-06-01",
                "grain": "selected",
                "includeSummaries": False,
                "skipReady": False,
                "overwriteDaily": True,
                "periods": [{"kind": "week", "start": "2026-06-01", "end": "2026-06-01", "label": "2026-W23"}],
            }
        }
        with (
            patch.object(ai_assets_router.foundation, "queue_failed_history_backfill_retry_request", return_value=43),
            patch.object(ai_assets_router.foundation, "get_refresh_status", return_value=status),
            patch.object(ai_assets_router.foundation, "execute_history_backfill") as execute,
        ):
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill_retry_failed(tasks, 42))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(tasks.tasks), 1)
        asyncio.run(tasks())
        execute.assert_called_once_with(
            43,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            grain="selected",
            include_summaries=False,
            skip_ready=False,
            overwrite_daily=True,
            periods=[{"kind": "week", "start": "2026-06-01", "end": "2026-06-01", "label": "2026-W23"}],
        )

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_history_backfill_cancel_endpoint_returns_cancel_status(self):
        from app.routers import ai_assets as ai_assets_router

        with patch.object(
            ai_assets_router.foundation,
            "cancel_history_backfill_request",
            return_value={"runId": 42, "status": "cancel_requested", "cancelRequested": True},
        ) as cancel:
            response = asyncio.run(ai_assets_router.api_foundation_history_backfill_cancel(42))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body)["status"], "cancel_requested")
        cancel.assert_called_once_with(42)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_period_refresh_endpoint_validates_input_and_queues_requested_range(self):
        from fastapi import BackgroundTasks
        from app.routers import diary as diary_router

        tasks = BackgroundTasks()
        with (
            patch.object(diary_router.foundation, "queue_refresh", return_value=42) as queue,
            patch.object(diary_router.foundation, "execute_refresh") as execute,
        ):
            response = asyncio.run(
                diary_router.api_refresh_weekly_report(tasks, {"start": "2026-05-13", "days": 7})
            )
            self.assertEqual(response.status_code, 202)
            queue.assert_called_once_with(date(2026, 5, 19), period_start=date(2026, 5, 13))
            asyncio.run(tasks())
            execute.assert_called_once_with(42, period_start=date(2026, 5, 13), period_days=7)
        invalid = asyncio.run(diary_router.api_refresh_weekly_report(BackgroundTasks(), {"days": 7}))
        self.assertEqual(invalid.status_code, 400)
        oversized = asyncio.run(diary_router.api_refresh_weekly_report(BackgroundTasks(), {"start": "2026-05-13", "days": 999}))
        self.assertEqual(oversized.status_code, 400)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_period_summary_refresh_endpoint_validates_input_and_queues_requested_range(self):
        from fastapi import BackgroundTasks
        from app.routers import diary as diary_router

        tasks = BackgroundTasks()
        with (
            patch.object(diary_router.foundation, "queue_period_summary", return_value=43) as queue,
            patch.object(diary_router.foundation, "execute_period_summary") as execute,
        ):
            response = asyncio.run(
                diary_router.api_refresh_weekly_report_summary(tasks, {"start": "2026-05-13", "days": 7})
            )
            self.assertEqual(response.status_code, 202)
            queue.assert_called_once_with(date(2026, 5, 19), period_start=date(2026, 5, 13))
            asyncio.run(tasks())
            execute.assert_called_once_with(43, period_start=date(2026, 5, 13), period_days=7)
        invalid = asyncio.run(diary_router.api_refresh_weekly_report_summary(BackgroundTasks(), {"days": 7}))
        self.assertEqual(invalid.status_code, 400)
        oversized = asyncio.run(
            diary_router.api_refresh_weekly_report_summary(BackgroundTasks(), {"start": "2026-05-13", "days": 999})
        )
        self.assertEqual(oversized.status_code, 400)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_readiness_endpoint_validates_period_and_passes_requested_range(self):
        from app.routers import ai_assets as ai_assets_router

        with patch.object(ai_assets_router.foundation, "get_reader_readiness", return_value={"status": "ready"}) as status:
            response = asyncio.run(ai_assets_router.api_foundation_readiness("2026-05-13", 7))
            self.assertEqual(response, {"status": "ready"})
            status.assert_called_once_with(period_start=date(2026, 5, 13), period_days=7)
        invalid = asyncio.run(ai_assets_router.api_foundation_readiness("2026-05-13", 0))
        self.assertEqual(invalid.status_code, 400)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_refresh_jobs_endpoint_validates_limit(self):
        from app.routers import ai_assets as ai_assets_router

        with patch.object(ai_assets_router.foundation, "list_refresh_jobs", return_value={"jobs": []}) as listing:
            response = asyncio.run(ai_assets_router.api_refresh_jobs(5))
            self.assertEqual(response, {"jobs": []})
            listing.assert_called_once_with(limit=5)
        invalid = asyncio.run(ai_assets_router.api_refresh_jobs(0))
        self.assertEqual(invalid.status_code, 400)


if __name__ == "__main__":
    unittest.main()
