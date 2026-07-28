import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.runtime_sources.opencode import OpenCodeRuntime


def _json(value):
    return json.dumps(value, separators=(",", ":"))


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _base_schema(connection: sqlite3.Connection, *, include_events: bool = True) -> None:
    connection.executescript(
        """
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            worktree TEXT,
            name TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            directory TEXT,
            title TEXT,
            agent TEXT,
            model TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    if include_events:
        connection.execute(
            """
            CREATE TABLE event (
                id TEXT PRIMARY KEY,
                aggregate_id TEXT,
                seq INTEGER,
                type TEXT,
                data TEXT
            )
            """
        )


def _insert_event(connection, event_id, sequence, event_type, data, session_id="ses-current"):
    connection.execute(
        "INSERT INTO event(id, aggregate_id, seq, type, data) VALUES (?, ?, ?, ?, ?)",
        (event_id, session_id, sequence, event_type, _json(data)),
    )


def _create_materialized_database(home: Path, *, session_id="ses-db", message_id="msg-db"):
    connection = sqlite3.connect(home / "opencode.db")
    _base_schema(connection, include_events=False)
    connection.execute(
        "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
        ("project-db", "/workspace/db", "DB project", 1_700_000_000_000, 1_700_000_003_000),
    )
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "project-db",
            "/workspace/session-fallback",
            "DB title",
            "build",
            _json({"providerID": "anthropic", "id": "claude-local"}),
            1_700_000_000_000,
            1_700_000_003_000,
        ),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            "msg-db-user",
            session_id,
            1_700_000_000_100,
            1_700_000_000_100,
            _json({"role": "user", "time": {"created": 1_700_000_000_100}}),
        ),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            1_700_000_000_200,
            1_700_000_003_000,
            _json(
                {
                    "role": "assistant",
                    "providerID": "anthropic",
                    "modelID": "claude-local",
                    "time": {
                        "created": 1_700_000_000_200,
                        "completed": 1_700_000_003_000,
                    },
                    "tokens": {
                        "input": 8,
                        "output": 3,
                        "reasoning": 2,
                        "cache": {"read": 5, "write": 1},
                    },
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            "part-db-user",
            "msg-db-user",
            session_id,
            1_700_000_000_100,
            1_700_000_000_100,
            _json({"type": "text", "text": "materialized question"}),
        ),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            "part-db-answer",
            message_id,
            session_id,
            1_700_000_002_000,
            1_700_000_002_500,
            _json({"type": "text", "text": "materialized answer"}),
        ),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            "part-db-thinking",
            message_id,
            session_id,
            1_700_000_001_000,
            1_700_000_001_500,
            _json({"type": "reasoning", "text": "private materialized chain"}),
        ),
    )
    connection.commit()
    connection.close()


def _write_legacy_session(
    home: Path,
    *,
    session_id="ses-legacy",
    message_id="msg-legacy",
    title="Legacy title",
    answer="legacy answer",
    output_tokens=11,
):
    storage = home / "storage"
    user_message_id = f"{message_id}-user"
    user_part_id = f"part-{message_id}-user"
    answer_part_id = f"part-{message_id}-answer"
    tool_part_id = f"part-{message_id}-tool"
    _write_json(
        storage / "project" / "legacy-project.json",
        {
            "id": "legacy-project",
            "worktree": "/workspace/legacy",
            "time": {"created": 1_600_000_000_000, "updated": 1_600_000_002_000},
        },
    )
    _write_json(
        storage / "session" / "legacy-project" / f"{session_id}.json",
        {
            "id": session_id,
            "projectID": "legacy-project",
            "directory": "/legacy/fallback",
            "title": title,
            "agent": "plan",
            "model": {"providerID": "openai", "id": "gpt-local"},
            "time": {"created": 1_600_000_000_000, "updated": 1_600_000_002_000},
        },
    )
    _write_json(
        storage / "message" / session_id / f"{user_message_id}.json",
        {
            "id": user_message_id,
            "sessionID": session_id,
            "role": "user",
            "time": {"created": 1_600_000_000_100},
        },
    )
    _write_json(
        storage / "message" / session_id / f"{message_id}.json",
        {
            "id": message_id,
            "sessionID": session_id,
            "role": "assistant",
            "providerID": "openai",
            "modelID": "gpt-local",
            "time": {"created": 1_600_000_000_200, "completed": 1_600_000_002_000},
            "tokens": {
                "input": 7,
                "output": output_tokens,
                "reasoning": 3,
                "cache": {"read": 4, "write": 2},
            },
        },
    )
    _write_json(
        storage / "part" / user_message_id / f"{user_part_id}.json",
        {
            "id": user_part_id,
            "messageID": user_message_id,
            "sessionID": session_id,
            "type": "text",
            "text": "legacy question",
        },
    )
    _write_json(
        storage / "part" / message_id / f"{answer_part_id}.json",
        {
            "id": answer_part_id,
            "messageID": message_id,
            "sessionID": session_id,
            "type": "text",
            "text": answer,
        },
    )
    _write_json(
        storage / "part" / message_id / f"{tool_part_id}.json",
        {
            "id": tool_part_id,
            "messageID": message_id,
            "sessionID": session_id,
            "type": "tool",
            "text": "tool payload must stay private",
        },
    )


class OpenCodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_current_event_schema_uses_final_logical_updates_and_wal(self):
        connection = sqlite3.connect(self.home / "opencode.db")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        _base_schema(connection)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        connection.execute(
            "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
            (
                "project-current",
                "/workspace/current",
                "Current project",
                1_800_000_000_000,
                1_800_000_004_000,
            ),
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ses-current",
                "project-current",
                "/workspace/session-fallback",
                "Materialized title",
                "build",
                _json({"providerID": "opencode", "id": "big-pickle"}),
                1_800_000_000_000,
                1_800_000_004_000,
            ),
        )
        _insert_event(
            connection,
            "evt-session",
            0,
            "session.updated.1",
            {
                "sessionID": "ses-current",
                "info": {
                    "id": "ses-current",
                    "projectID": "project-current",
                    "directory": "/workspace/session-fallback",
                    "title": "Event title",
                    "agent": "build",
                    "model": {"providerID": "opencode", "id": "big-pickle"},
                    "time": {"created": 1_800_000_000_000, "updated": 1_800_000_004_000},
                },
            },
        )
        _insert_event(
            connection,
            "evt-user",
            1,
            "message.updated.1",
            {
                "sessionID": "ses-current",
                "info": {
                    "id": "msg-user",
                    "role": "user",
                    "time": {"created": 1_800_000_000_100},
                },
            },
        )
        _insert_event(
            connection,
            "evt-user-part-draft",
            2,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "time": 1_800_000_000_110,
                "part": {
                    "id": "part-user",
                    "messageID": "msg-user",
                    "sessionID": "ses-current",
                    "type": "text",
                    "text": "draft text",
                },
            },
        )
        _insert_event(
            connection,
            "evt-user-part-final",
            3,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "time": 1_800_000_000_120,
                "part": {
                    "id": "part-user",
                    "messageID": "msg-user",
                    "sessionID": "ses-current",
                    "type": "text",
                    "text": "final question",
                },
            },
        )
        _insert_event(
            connection,
            "evt-assistant-start",
            4,
            "message.updated.1",
            {
                "sessionID": "ses-current",
                "info": {
                    "id": "msg-assistant",
                    "role": "assistant",
                    "providerID": "opencode",
                    "modelID": "big-pickle",
                    "time": {"created": 1_800_000_000_200},
                    "tokens": {
                        "input": 1,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
            },
        )
        _insert_event(
            connection,
            "evt-reasoning",
            5,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "part": {
                    "id": "part-reasoning",
                    "messageID": "msg-assistant",
                    "type": "reasoning",
                    "text": "secret chain of thought",
                },
            },
        )
        _insert_event(
            connection,
            "evt-tool",
            6,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "part": {
                    "id": "part-tool",
                    "messageID": "msg-assistant",
                    "type": "tool",
                    "text": "secret tool output",
                },
            },
        )
        _insert_event(
            connection,
            "evt-answer-draft",
            7,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "time": 1_800_000_002_000,
                "part": {
                    "id": "part-answer",
                    "messageID": "msg-assistant",
                    "type": "text",
                    "text": "draft answer",
                },
            },
        )
        _insert_event(
            connection,
            "evt-answer-final",
            8,
            "message.part.updated.1",
            {
                "sessionID": "ses-current",
                "time": 1_800_000_003_000,
                "part": {
                    "id": "part-answer",
                    "messageID": "msg-assistant",
                    "type": "text",
                    "text": "final answer",
                },
            },
        )
        _insert_event(
            connection,
            "evt-assistant-final",
            9,
            "message.updated.1",
            {
                "sessionID": "ses-current",
                "info": {
                    "id": "msg-assistant",
                    "role": "assistant",
                    "providerID": "opencode",
                    "modelID": "big-pickle",
                    "time": {
                        "created": 1_800_000_000_200,
                        "completed": 1_800_000_004_000,
                    },
                    "tokens": {
                        "input": 100,
                        "output": 20,
                        "reasoning": 5,
                        "cache": {"read": 30, "write": 7},
                    },
                },
            },
        )
        connection.commit()

        try:
            runtime = OpenCodeRuntime(self.home)
            self.assertEqual(runtime.tool_key, "opencode")
            self.assertEqual(runtime.artifacts(), (self.home / "opencode.db",))

            sessions = list(runtime.sessions())
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].external_session_key, "default:ses-current")
            self.assertEqual(sessions[0].title, "Event title")
            self.assertEqual(sessions[0].initial_cwd, "/workspace/current")
            self.assertEqual(sessions[0].model_key, "opencode/big-pickle")

            usage = list(runtime.usage())
            self.assertEqual(len(usage), 1)
            self.assertEqual(usage[0].external_event_key, "message:msg-assistant")
            self.assertEqual(usage[0].external_session_key, "default:ses-current")
            self.assertEqual(usage[0].model_key, "opencode/big-pickle")
            self.assertEqual(usage[0].input_tokens, 100)
            self.assertEqual(usage[0].output_tokens, 20)
            self.assertEqual(usage[0].cache_read_tokens, 30)
            self.assertEqual(usage[0].cache_write_tokens, 7)
            self.assertEqual(usage[0].reasoning_tokens, 5)
            self.assertEqual(usage[0].protocol_total_tokens, 155)
            self.assertEqual(usage[0].metadata["protocol_total_tokens"], 155)
            self.assertEqual(int(usage[0].occurred_at.timestamp() * 1000), 1_800_000_004_000)

            dialogue = list(runtime.dialogue())
            self.assertEqual([(item.role, item.content) for item in dialogue], [
                ("user", "final question"),
                ("assistant", "final answer"),
            ])
            surfaced = "\n".join(item.content for item in dialogue)
            self.assertNotIn("draft", surfaced)
            self.assertNotIn("secret", surfaced)
        finally:
            connection.close()

    def test_materialized_message_and_part_tables_are_supported(self):
        _create_materialized_database(self.home)
        runtime = OpenCodeRuntime(self.home)

        sessions = list(runtime.sessions())
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].initial_cwd, "/workspace/db")
        self.assertEqual(sessions[0].model_key, "anthropic/claude-local")

        usage = list(runtime.usage())
        self.assertEqual(len(usage), 1)
        self.assertEqual(
            (
                usage[0].input_tokens,
                usage[0].output_tokens,
                usage[0].cache_read_tokens,
                usage[0].cache_write_tokens,
                usage[0].reasoning_tokens,
            ),
            (8, 3, 5, 1, 2),
        )
        self.assertEqual(usage[0].protocol_total_tokens, 18)
        self.assertEqual(usage[0].metadata["protocol_total_tokens"], 18)
        self.assertEqual(
            [(item.role, item.content) for item in runtime.dialogue()],
            [("user", "materialized question"), ("assistant", "materialized answer")],
        )

    def test_session_token_columns_are_a_nonduplicating_usage_fallback(self):
        connection = sqlite3.connect(self.home / "opencode.db")
        connection.execute(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                directory TEXT,
                title TEXT,
                model TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                tokens_input INTEGER,
                tokens_output INTEGER,
                tokens_reasoning INTEGER,
                tokens_cache_read INTEGER,
                tokens_cache_write INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ses-aggregate",
                "/workspace/aggregate",
                "Aggregate only",
                _json({"providerID": "opencode", "id": "aggregate-model"}),
                1_700_000_000_000,
                1_700_000_003_000,
                2,
                3,
                5,
                4,
                6,
            ),
        )
        connection.commit()
        connection.close()

        usage = list(OpenCodeRuntime(self.home).usage())

        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].external_event_key, "session:ses-aggregate:aggregate")
        self.assertEqual(usage[0].protocol_total_tokens, 14)
        self.assertEqual(usage[0].cache_write_tokens, 6)
        self.assertEqual(usage[0].metadata["usage_granularity"], "session_aggregate")

    def test_legacy_json_fallback_and_dual_source_native_id_deduplication(self):
        _create_materialized_database(self.home, session_id="ses-same", message_id="msg-same")
        _write_legacy_session(
            self.home,
            session_id="ses-same",
            message_id="msg-same",
            title="Conflicting legacy title",
            answer="conflicting legacy answer",
            output_tokens=999,
        )
        _write_legacy_session(
            self.home,
            session_id="ses-legacy-only",
            message_id="msg-legacy-only",
            title="Legacy only",
            answer="legacy-only answer",
            output_tokens=11,
        )

        runtime = OpenCodeRuntime(self.home)
        self.assertEqual(
            runtime.artifacts(),
            (self.home / "opencode.db", self.home / "storage"),
        )

        sessions = {item.external_session_key: item for item in runtime.sessions()}
        self.assertEqual(set(sessions), {"default:ses-same", "default:ses-legacy-only"})
        self.assertEqual(sessions["default:ses-same"].title, "DB title")
        self.assertEqual(sessions["default:ses-legacy-only"].title, "Legacy only")
        self.assertEqual(sessions["default:ses-legacy-only"].initial_cwd, "/workspace/legacy")

        usage = {item.external_event_key: item for item in runtime.usage()}
        self.assertEqual(set(usage), {"message:msg-same", "message:msg-legacy-only"})
        self.assertEqual(usage["message:msg-same"].output_tokens, 3)
        self.assertEqual(usage["message:msg-legacy-only"].output_tokens, 11)
        self.assertEqual(usage["message:msg-legacy-only"].model_key, "openai/gpt-local")

        dialogue = {item.external_message_key: item.content for item in runtime.dialogue()}
        self.assertEqual(dialogue["message:msg-same"], "materialized answer")
        self.assertEqual(dialogue["message:msg-legacy-only"], "legacy-only answer")
        self.assertNotIn("tool payload", "\n".join(dialogue.values()))

    def test_corrupt_database_safely_falls_back_to_storage(self):
        (self.home / "opencode.db").write_bytes(b"not a sqlite database")
        _write_legacy_session(self.home)
        runtime = OpenCodeRuntime(self.home)

        self.assertEqual(
            [item.external_session_key for item in runtime.sessions()],
            ["default:ses-legacy"],
        )
        self.assertEqual(
            [item.external_event_key for item in runtime.usage()],
            ["message:msg-legacy"],
        )
        self.assertEqual(
            [(item.role, item.content) for item in runtime.dialogue()],
            [("user", "legacy question"), ("assistant", "legacy answer")],
        )

    def test_missing_tables_and_malformed_json_degrade_to_empty(self):
        connection = sqlite3.connect(self.home / "opencode.db")
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
        _write_json(self.home / "account.json", {"secret": "must-never-be-read"})
        _write_json(self.home / "auth.json", {"token": "must-never-be-read"})
        malformed = self.home / "storage" / "session" / "broken.json"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("{broken", encoding="utf-8")

        runtime = OpenCodeRuntime(self.home)
        self.assertEqual(list(runtime.sessions()), [])
        self.assertEqual(list(runtime.usage()), [])
        self.assertEqual(list(runtime.dialogue()), [])

    def test_external_ids_are_stable_across_home_paths(self):
        _write_legacy_session(self.home)
        second_home = self.home / "relocated"
        shutil.copytree(self.home / "storage", second_home / "storage")

        first = OpenCodeRuntime(self.home)
        second = OpenCodeRuntime(second_home)
        self.assertEqual(
            [item.external_session_key for item in first.sessions()],
            [item.external_session_key for item in second.sessions()],
        )
        self.assertEqual(
            [item.external_event_key for item in first.usage()],
            [item.external_event_key for item in second.usage()],
        )
        self.assertEqual(
            [item.external_message_key for item in first.dialogue()],
            [item.external_message_key for item in second.dialogue()],
        )


if __name__ == "__main__":
    unittest.main()
