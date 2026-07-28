import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.adapters.usage import LocalRuntimeAdapter
from data_foundation.aggregate import daily_diary_usage_metrics, daily_tool_totals
from data_foundation.db import connect
from data_foundation.ingest import run_shadow_ingestion
from data_foundation.paths import initialize_home
from data_foundation.runtime_sources.base import SessionRecord, UsageRecord
from data_foundation.time import business_date_for


class _Runtime:
    def __init__(self, root: Path, *, usage_status: str, sessions, usage):
        self.root = root
        self.usage_status = usage_status
        self.capabilities = {"session_inventory", "workspace_metadata"}
        self._sessions = tuple(sessions)
        self._usage = tuple(usage)

    def artifacts(self):
        return (self.root,)

    def sessions(self):
        return self._sessions

    def usage(self):
        return self._usage


class _Adapter(LocalRuntimeAdapter):
    def __init__(self, tool_key: str, runtime: _Runtime):
        self.tool_key = tool_key
        super().__init__(runtime, runtime.root)


class LocalRuntimeAdapterTests(unittest.TestCase):
    def test_session_only_runtime_is_inventory_not_zero_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            occurred = datetime(2026, 7, 27, 20, tzinfo=timezone.utc)
            target = business_date_for(occurred, paths=paths)
            runtime_root = root / "cursor"
            runtime_root.mkdir()
            adapter = _Adapter(
                "cursor",
                _Runtime(
                    runtime_root,
                    usage_status="unavailable",
                    sessions=[
                        SessionRecord(
                            external_session_key="cli:cursor-session",
                            started_at=occurred,
                            last_active_at=occurred,
                            initial_cwd="/workspace/cursor-project",
                            title="Local Cursor session",
                            source_variant="cli",
                        )
                    ],
                    usage=[],
                ),
            )

            result = run_shadow_ingestion(
                paths,
                target,
                adapters=[adapter],
                observe_assets=False,
            )

            self.assertEqual(result.events_in_window, 1)
            self.assertNotIn("cursor", daily_tool_totals(paths, target))
            with connect(paths, read_only=True) as connection:
                session = connection.execute(
                    "SELECT external_session_key, metadata_json FROM sessions WHERE tool_key = 'cursor'"
                ).fetchone()
                usage_count = connection.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE tool_key = 'cursor'"
                ).fetchone()[0]
                capabilities = connection.execute(
                    "SELECT capabilities_json FROM tool_sources WHERE tool_key = 'cursor'"
                ).fetchone()[0]
            self.assertEqual(session["external_session_key"], "cli:cursor-session")
            self.assertEqual(json.loads(session["metadata_json"])["usage_status"], "unavailable")
            self.assertEqual(usage_count, 0)
            registered_capabilities = json.loads(capabilities)
            self.assertIn("usage_unavailable", registered_capabilities)
            self.assertNotIn("usage_events", registered_capabilities)

    def test_source_protocol_total_and_tool_tokens_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            occurred = datetime(2026, 7, 27, 20, tzinfo=timezone.utc)
            target = business_date_for(occurred, paths=paths)
            runtime_root = root / "antigravity"
            runtime_root.mkdir()
            session = SessionRecord(
                external_session_key="cli:ag-session",
                started_at=occurred,
                last_active_at=occurred,
                initial_cwd="/workspace/antigravity-project",
                source_variant="cli",
            )
            usage = UsageRecord(
                external_event_key="tracking:one",
                external_session_key=session.external_session_key,
                occurred_at=occurred,
                model_key="gemini-test",
                input_tokens=2,
                output_tokens=3,
                cache_read_tokens=4,
                reasoning_tokens=5,
                tool_tokens=6,
                protocol_total_tokens=20,
                source_variant="cli",
            )
            adapter = _Adapter(
                "antigravity",
                _Runtime(
                    runtime_root,
                    usage_status="available",
                    sessions=[session],
                    usage=[usage],
                ),
            )

            run_shadow_ingestion(
                paths,
                target,
                adapters=[adapter],
                observe_assets=False,
            )

            totals = daily_tool_totals(paths, target)
            self.assertEqual(totals["antigravity"]["tokens"], 20)
            diary_metrics = daily_diary_usage_metrics(paths, target)
            self.assertEqual(diary_metrics["antigravity"]["total_tokens"], 20)
            with connect(paths, read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT occurred_at, protocol_total_tokens, metadata_json
                    FROM usage_events
                    WHERE tool_key = 'antigravity'
                    """
                ).fetchone()
            self.assertEqual(row["occurred_at"], occurred.isoformat())
            self.assertEqual(row["protocol_total_tokens"], 20)
            self.assertEqual(json.loads(row["metadata_json"])["tool_tokens"], 6)


if __name__ == "__main__":
    unittest.main()
