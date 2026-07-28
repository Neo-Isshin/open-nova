import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from ai_assets_center import unified_source_collector
from app.services import ai_assets, token_clock
from data_foundation.runtime_sources.base import DialogueRecord, SessionRecord, UsageRecord


class _Runtime:
    tool_key = "antigravity"

    def __init__(self, sessions=(), usage=(), dialogue=()):
        self._sessions = tuple(sessions)
        self._usage = tuple(usage)
        self._dialogue = tuple(dialogue)

    def sessions(self):
        return self._sessions

    def usage(self):
        return self._usage

    def dialogue(self):
        return self._dialogue


class NewRuntimeIntegrationTests(unittest.TestCase):
    def tearDown(self):
        ai_assets._RUNTIME_SESSION_RECORDS.clear()
        ai_assets._RUNTIME_DIALOGUE_ACTIVITY.clear()

    def test_ai_assets_honors_source_total_and_cursor_usage_unavailable(self):
        occurred = datetime(2026, 7, 27, 20, tzinfo=timezone.utc)
        session = SessionRecord(
            external_session_key="cli:one",
            started_at=occurred,
            last_active_at=occurred,
            initial_cwd="/workspace/example",
            source_variant="cli",
        )
        usage = UsageRecord(
            external_event_key="tracking:one",
            external_session_key=session.external_session_key,
            occurred_at=occurred,
            input_tokens=2,
            output_tokens=3,
            cache_read_tokens=4,
            reasoning_tokens=5,
            tool_tokens=6,
            protocol_total_tokens=20,
            source_variant="cli",
        )
        entries, session_count = ai_assets._scan_all_normalized_runtime(
            "Antigravity",
            _Runtime(
                sessions=[session],
                usage=[usage],
                dialogue=[
                    DialogueRecord(
                        external_message_key="cli:private",
                        external_session_key=session.external_session_key,
                        role="user",
                        content="dialogue content must not stay in dashboard memory",
                        occurred_at=occurred,
                        source_variant="cli",
                    )
                ],
            ),
        )
        stat = ai_assets._aggregate_tool("Antigravity", entries, session_count)
        self.assertEqual(stat["allTimeTokens"], 20)
        self.assertEqual(stat["usageStatus"], "local-partial")
        self.assertEqual(entries[0]["sourceVariant"], "cli")
        self.assertNotIn(
            "dialogue content must not stay",
            repr(ai_assets._RUNTIME_DIALOGUE_ACTIVITY["Antigravity"]),
        )

        cursor_dialogue = DialogueRecord(
            external_message_key="cli:message:one",
            external_session_key="cli:cursor-one",
            role="assistant",
            content="done",
            occurred_at=occurred,
            source_variant="cli",
        )
        ai_assets._RUNTIME_SESSION_RECORDS["Cursor"] = (
            SessionRecord(
                external_session_key="cli:cursor-one",
                started_at=occurred,
                last_active_at=occurred,
                source_variant="cli",
            ),
        )
        ai_assets._RUNTIME_DIALOGUE_ACTIVITY["Cursor"] = (
            (cursor_dialogue.external_session_key, cursor_dialogue.occurred_at),
        )
        cursor_stat = ai_assets._aggregate_tool("Cursor", [], 1)
        self.assertEqual(cursor_stat["allTimeTokens"], 0)
        self.assertEqual(cursor_stat["allTimeMessages"], 1)
        self.assertEqual(cursor_stat["usageStatus"], "unavailable")
        self.assertEqual(
            cursor_stat["lastActivity"],
            occurred.astimezone(ai_assets._local_tz()).strftime("%Y-%m-%d"),
        )

    def test_token_clock_uses_explicit_antigravity_protocol_total(self):
        occurred = datetime.now(timezone.utc)
        runtime = _Runtime(
            sessions=[
                SessionRecord(
                    external_session_key="cli:one",
                    started_at=occurred,
                    last_active_at=occurred,
                    initial_cwd="/workspace/example",
                    source_variant="cli",
                )
            ],
            usage=[
                UsageRecord(
                    external_event_key="tracking:one",
                    external_session_key="cli:one",
                    occurred_at=occurred,
                    input_tokens=2,
                    output_tokens=3,
                    cache_read_tokens=4,
                    reasoning_tokens=5,
                    tool_tokens=6,
                    protocol_total_tokens=20,
                    source_variant="cli",
                )
            ],
        )
        entries = token_clock._normalized_runtime_usage_entries(runtime)
        filtered = token_clock._filter_today(
            entries,
            token_clock.business_date_for(
                occurred,
                tz=token_clock.local_timezone(),
            ).isoformat(),
        )
        stat = token_clock._aggregate(filtered, occurred.astimezone(token_clock.local_timezone()).hour)
        self.assertEqual(stat["tokens"], 20)
        self.assertEqual(entries[0]["toolTokens"], 6)

    def test_ai_assets_marks_partial_local_usage_in_runtime_and_demo_ui(self):
        for path in (
            ROOT / "src" / "dashboard" / "app" / "static" / "js" / "app.js",
            ROOT / "docs" / "dashboard-demo" / "js" / "app.js",
        ):
            script = path.read_text(encoding="utf-8")
            self.assertIn("t.usageStatus === 'local-partial'", script)
            self.assertIn("localUsagePartial", script)

    def test_diary_collector_merges_antigravity_variants(self):
        occurred = datetime(2026, 7, 27, 20, tzinfo=timezone.utc)
        runtime = _Runtime(
            dialogue=[
                DialogueRecord(
                    external_message_key="cli:one",
                    external_session_key="cli:session",
                    role="user",
                    content="CLI question",
                    occurred_at=occurred,
                    source_variant="cli",
                ),
                DialogueRecord(
                    external_message_key="app:two",
                    external_session_key="app:session",
                    role="assistant",
                    content="App answer",
                    occurred_at=occurred + timedelta(seconds=1),
                    source_variant="app",
                ),
                DialogueRecord(
                    external_message_key="cli:tool",
                    external_session_key="cli:session",
                    role="tool",
                    content="private tool payload",
                    occurred_at=occurred,
                    source_variant="cli",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            diary_root = Path(tmp)
            with (
                patch.object(unified_source_collector, "_local_runtime", return_value=runtime),
                patch.object(unified_source_collector, "_diary_root", return_value=diary_root),
            ):
                count = unified_source_collector.collect_runtime_records(
                    "antigravity",
                    "2026-07-27",
                    occurred.timestamp() - 1,
                    occurred.timestamp() + 120,
                )
            filtered = (
                diary_root
                / "__diary_daily"
                / "2026-07-27"
                / "_filtered"
                / "antigravity"
                / "unified_daily.jsonl"
            )
            rows = [
                json.loads(line)
                for line in filtered.read_text(encoding="utf-8").splitlines()
            ]
            cli_variant_dir_exists = (
                diary_root / "__diary_daily" / "2026-07-27" / "_filtered" / "cli"
            ).exists()
            app_variant_dir_exists = (
                diary_root / "__diary_daily" / "2026-07-27" / "_filtered" / "app"
            ).exists()

        self.assertEqual(count, 2)
        self.assertEqual([row["content"] for row in rows], ["CLI question", "App answer"])
        self.assertFalse(cli_variant_dir_exists)
        self.assertFalse(app_variant_dir_exists)


if __name__ == "__main__":
    unittest.main()
