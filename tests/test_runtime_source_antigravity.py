from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_foundation.runtime_sources.antigravity import AntigravityRuntime


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def _field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _metadata(seconds: int, nanoseconds: int = 0) -> bytes:
    wrapped = _field(1, seconds) + _field(2, nanoseconds)
    return _field(1, wrapped)


def _payload(
    tracking_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cumulative_prompt_tokens: int,
    reasoning_tokens: int,
    tool_tokens: int,
) -> bytes:
    telemetry = b"".join(
        (
            _field(1, input_tokens),
            _field(2, output_tokens),
            _field(3, cache_read_tokens),
            _field(5, cumulative_prompt_tokens),
            _field(9, reasoning_tokens),
            _field(10, tool_tokens),
            _field(11, tracking_id.encode()),
        )
    )
    return _field(5, _field(9, telemetry))


def _create_database(
    path: Path,
    rows: list[tuple[bytes, bytes]],
    *,
    wal: bool = False,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        """
        CREATE TABLE steps(
            idx INTEGER PRIMARY KEY,
            metadata BLOB,
            step_payload BLOB
        )
        """
    )
    connection.executemany(
        "INSERT INTO steps(metadata, step_payload) VALUES (?, ?)", rows
    )
    connection.commit()
    return connection


class AntigravityRuntimeTests(unittest.TestCase):
    def test_merges_variants_and_uses_history_as_cli_session_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cli = root / "antigravity-cli"
            ide = root / "antigravity-ide"
            app = root / "antigravity"
            cli.mkdir()
            ide.mkdir()
            app.mkdir()

            history_rows = [
                {
                    "conversationId": "shared-native",
                    "display": "First local request",
                    "timestamp": 1_700_000_000_000,
                    "workspace": "/workspace/cli",
                },
                {
                    "conversationId": "shared-native",
                    "display": "Second local request",
                    "timestamp": 1_700_000_001_000,
                    "workspace": "/workspace/cli",
                },
                {
                    "display": "Unlinked text must not become dialogue",
                    "timestamp": 1_700_000_002_000,
                    "workspace": "/workspace/other",
                },
            ]
            (cli / "history.jsonl").write_text(
                "\n".join(json.dumps(row) for row in history_rows)
                + "\n{not-json}\n",
                encoding="utf-8",
            )

            cli_db = cli / "conversations" / "shared-native.db"
            cli_writer = _create_database(
                cli_db,
                [
                    (
                        _metadata(1_700_000_010, 500_000_000),
                        _payload(
                            "tracking-shared",
                            input_tokens=1,
                            output_tokens=2,
                            cache_read_tokens=3,
                            cumulative_prompt_tokens=999_999,
                            reasoning_tokens=4,
                            tool_tokens=5,
                        ),
                    )
                ],
                wal=True,
            )
            (cli / "conversations" / "cli-encrypted.pb").write_bytes(
                _payload(
                    "tracking-pb-must-not-be-read",
                    input_tokens=100,
                    output_tokens=100,
                    cache_read_tokens=100,
                    cumulative_prompt_tokens=100,
                    reasoning_tokens=100,
                    tool_tokens=100,
                )
            )
            (cli / "conversations" / "corrupt.db").write_bytes(b"not sqlite")

            (ide / "conversations").mkdir()
            (ide / "conversations" / "shared-native.pb").write_bytes(
                b"encrypted opaque bytes"
            )

            app_db = app / "conversations" / "shared-native.db"
            app_writer = _create_database(
                app_db,
                [
                    (
                        _metadata(1_700_000_020),
                        _payload(
                            "tracking-shared",
                            input_tokens=50,
                            output_tokens=50,
                            cache_read_tokens=50,
                            cumulative_prompt_tokens=50,
                            reasoning_tokens=50,
                            tool_tokens=50,
                        ),
                    ),
                    (
                        _metadata(1_700_000_021),
                        _payload(
                            "tracking-app-only",
                            input_tokens=6,
                            output_tokens=7,
                            cache_read_tokens=8,
                            cumulative_prompt_tokens=888_888,
                            reasoning_tokens=9,
                            tool_tokens=10,
                        ),
                    ),
                ],
            )

            brain = cli / "brain" / "shared-native"
            brain.mkdir(parents=True)
            brain_text = "PRIVATE BRAIN CONTENT MUST NOT BE READ"
            brain_asset = brain / "task.md"
            brain_asset.write_text(brain_text, encoding="utf-8")

            runtime = AntigravityRuntime(
                {"cli": cli, "ide": ide, "app": app}
            )
            try:
                self.assertEqual(runtime.tool_key, "antigravity")
                sessions = {
                    record.external_session_key: record
                    for record in runtime.sessions()
                }
                self.assertEqual(
                    set(sessions),
                    {
                        "cli:cli-encrypted",
                        "cli:corrupt",
                        "cli:shared-native",
                        "ide:shared-native",
                        "app:shared-native",
                    },
                )

                cli_session = sessions["cli:shared-native"]
                self.assertEqual(cli_session.source_variant, "cli")
                self.assertEqual(cli_session.initial_cwd, "/workspace/cli")
                self.assertEqual(cli_session.title, "First local request")
                self.assertEqual(int(cli_session.started_at.timestamp()), 1_700_000_000)
                self.assertEqual(
                    int(cli_session.last_active_at.timestamp()), 1_700_000_001
                )
                self.assertEqual(cli_session.metadata["runtime_family"], "antigravity")
                self.assertEqual(cli_session.metadata["brain_asset_count"], 1)
                self.assertEqual(cli_session.metadata["usage_status"], "available")

                self.assertEqual(
                    sessions["ide:shared-native"].metadata["usage_status"],
                    "unavailable",
                )
                self.assertTrue(
                    sessions["ide:shared-native"].metadata[
                        "encrypted_conversation"
                    ]
                )
                self.assertEqual(
                    sessions["cli:corrupt"].metadata["unreadable_database_count"],
                    1,
                )
                self.assertEqual(
                    sessions["cli:corrupt"].metadata["usage_status"],
                    "unavailable",
                )

                usage = list(runtime.usage())
                self.assertEqual(
                    [record.external_event_key for record in usage],
                    ["tracking:tracking-shared", "tracking:tracking-app-only"],
                )
                first = usage[0]
                self.assertEqual(first.external_session_key, "cli:shared-native")
                self.assertEqual(first.source_variant, "cli")
                self.assertEqual(first.input_tokens, 1)
                self.assertEqual(first.output_tokens, 2)
                self.assertEqual(first.cache_read_tokens, 3)
                self.assertEqual(first.reasoning_tokens, 4)
                self.assertEqual(first.tool_tokens, 5)
                self.assertEqual(first.protocol_total_tokens, 15)
                self.assertEqual(
                    first.metadata["token_field_semantics"]["5"],
                    "cumulative_prompt_tokens_ignored",
                )
                self.assertEqual(
                    int(first.occurred_at.timestamp()), 1_700_000_010
                )
                self.assertEqual(
                    usage[1].external_session_key, "app:shared-native"
                )
                self.assertEqual(usage[1].protocol_total_tokens, 40)

                dialogue = list(runtime.dialogue())
                self.assertEqual(
                    [record.content for record in dialogue],
                    ["First local request", "Second local request"],
                )
                self.assertTrue(
                    all(record.external_session_key == "cli:shared-native" for record in dialogue)
                )
                self.assertTrue(all(record.role == "user" for record in dialogue))
                self.assertNotIn(
                    brain_text,
                    "\n".join(record.content for record in dialogue),
                )

                artifacts = set(runtime.artifacts())
                self.assertIn(cli / "history.jsonl", artifacts)
                self.assertIn(cli_db, artifacts)
                self.assertIn(
                    cli / "conversations" / "cli-encrypted.pb", artifacts
                )
                self.assertIn(brain_asset, artifacts)
            finally:
                cli_writer.close()
                app_writer.close()

    def test_missing_or_malformed_databases_degrade_without_aborting(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "antigravity-cli"
            conversations = home / "conversations"
            conversations.mkdir(parents=True)
            (conversations / "broken.db").write_bytes(b"\x00\xffbroken")
            empty = sqlite3.connect(conversations / "wrong-schema.db")
            empty.execute("CREATE TABLE unrelated(value TEXT)")
            empty.commit()
            empty.close()

            runtime = AntigravityRuntime({"cli": home})
            self.assertEqual(list(runtime.usage()), [])
            sessions = list(runtime.sessions())
            self.assertEqual(
                {record.external_session_key for record in sessions},
                {"cli:broken", "cli:wrong-schema"},
            )
            self.assertTrue(
                all(
                    record.metadata["usage_status"] == "unavailable"
                    for record in sessions
                )
            )


if __name__ == "__main__":
    unittest.main()
