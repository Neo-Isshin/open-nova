from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_foundation.runtime_sources.cursor as cursor_source
from data_foundation.runtime_sources.cursor import CursorRuntime


SHARED_ID = "11111111-1111-4111-8111-111111111111"
HEADER_ONLY_ID = "22222222-2222-4222-8222-222222222222"
TRANSCRIPT_ONLY_ID = "33333333-3333-4333-8333-333333333333"
CLI_ID = "44444444-4444-4444-8444-444444444444"
BROKEN_ID = "55555555-5555-4555-8555-555555555555"
WORKSPACE_ID = "workspace-one"
SENSITIVE_SENTINEL = "must-never-enter-python"


def _create_ide_state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        headers = {
            "allComposers": [
                {
                    "composerId": SHARED_ID,
                    "name": "Shared composer",
                    "createdAt": 1_700_000_000_000,
                    "lastUpdatedAt": 1_700_000_100_000,
                    "unifiedMode": "agent",
                    "workspaceIdentifier": {"id": WORKSPACE_ID},
                    "isArchived": False,
                    "subtitle": "not dialogue",
                },
                {
                    "composerId": HEADER_ONLY_ID,
                    "name": "Header only",
                    "createdAt": 1_700_001_000_000,
                    "unifiedMode": "chat",
                    "workspaceIdentifier": {
                        "id": WORKSPACE_ID,
                        "uri": {"fsPath": "/fixture/direct-workspace"},
                    },
                },
            ]
        }
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("composer.composerHeaders", json.dumps(headers)),
        )
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", SENSITIVE_SENTINEL),
        )
        connection.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("cursorAuth/refreshToken", SENSITIVE_SENTINEL),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            (
                f"composerData:{SHARED_ID}",
                json.dumps(
                    {
                        "composerId": SHARED_ID,
                        "createdAt": 1_699_999_000_000,
                        "modelConfig": {"modelName": "fixture-model"},
                        "status": "completed",
                        "blobEncryptionKey": SENSITIVE_SENTINEL,
                        "conversationMap": {"secret": SENSITIVE_SENTINEL},
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", SENSITIVE_SENTINEL),
        )
        connection.commit()
    finally:
        connection.close()


def _write_transcript(home: Path, native_id: str, lines: list[dict]) -> Path:
    path = (
        home
        / "projects"
        / "fixture-project"
        / "agent-transcripts"
        / native_id
        / f"{native_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


def _create_cli_store(home: Path, native_id: str) -> Path:
    path = home / "chats" / "cwd-hash" / native_id / "store.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        metadata = {
            "agentId": native_id,
            "name": "CLI session",
            "mode": "default",
            "createdAt": 1_700_002_000_000,
            "lastUpdatedAt": 1_700_002_100_000,
            "lastUsedModel": "cli-model",
            "cwd": "file:///fixture/cli-workspace",
        }
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("0", json.dumps(metadata).encode("utf-8").hex()),
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", SENSITIVE_SENTINEL),
        )
        connection.executemany(
            "INSERT INTO blobs(id, data) VALUES (?, ?)",
            [
                (
                    "blob-user",
                    json.dumps(
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "CLI question"}],
                        }
                    ).encode(),
                ),
                (
                    "blob-assistant",
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "text": "hidden reasoning"},
                                {"type": "text", "text": "CLI answer"},
                                {"type": "tool-call", "text": "hidden tool"},
                            ],
                            "providerOptions": {
                                "cursor": {"modelName": "cli-message-model"}
                            },
                        }
                    ).encode(),
                ),
                (
                    "blob-system",
                    json.dumps({"role": "system", "content": "hidden system"}).encode(),
                ),
                ("blob-protobuf", b"\x0a\x04\x00\xffnot-json"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _runtime_fixture(tmp_path: Path) -> tuple[CursorRuntime, dict[str, Path]]:
    home = tmp_path / ".cursor"
    ide_state = tmp_path / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    workspace_root = tmp_path / "Cursor" / "User" / "workspaceStorage"
    workspace_json = workspace_root / WORKSPACE_ID / "workspace.json"
    workspace_json.parent.mkdir(parents=True, exist_ok=True)
    workspace_json.write_text(
        json.dumps({"folder": "file:///fixture/mapped-workspace"}),
        encoding="utf-8",
    )
    _create_ide_state(ide_state)
    shared_transcript = _write_transcript(
        home,
        SHARED_ID,
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "Shared question"},
                        {"type": "tool", "text": "hidden transcript tool"},
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "text": "hidden transcript thinking"},
                        {"type": "text", "text": "Shared answer"},
                    ]
                },
            },
            {"role": "system", "message": {"content": "hidden transcript system"}},
            {"type": "turn_ended", "status": "success"},
        ],
    )
    transcript_only = _write_transcript(
        home,
        TRANSCRIPT_ONLY_ID,
        [
            {"role": "user", "message": {"content": "Transcript-only question"}},
            {
                "role": "assistant",
                "message": {"content": [{"type": "output_text", "text": "Transcript-only answer"}]},
            },
        ],
    )
    os.utime(shared_transcript, (1_900_000_000, 1_900_000_000))
    os.utime(transcript_only, (1_710_000_000, 1_710_000_000))
    cli_store = _create_cli_store(home, CLI_ID)

    broken_store = home / "chats" / "broken-hash" / BROKEN_ID / "store.db"
    broken_store.parent.mkdir(parents=True, exist_ok=True)
    broken_store.write_bytes(b"not a sqlite database")
    os.utime(broken_store, (1_720_000_000, 1_720_000_000))

    runtime = CursorRuntime(
        home,
        ide_state_dbs=(ide_state,),
        workspace_storage_roots=(workspace_root,),
    )
    return runtime, {
        "ide_state": ide_state,
        "workspace_json": workspace_json,
        "shared_transcript": shared_transcript,
        "transcript_only": transcript_only,
        "cli_store": cli_store,
        "broken_store": broken_store,
    }


def _check_sessions_union_header_transcript_cli_and_safe_fallback(tmp_path: Path) -> None:
    runtime, _ = _runtime_fixture(tmp_path)

    sessions = {record.external_session_key: record for record in runtime.sessions()}

    assert set(sessions) == {
        f"cursor:{SHARED_ID}",
        f"cursor:{HEADER_ONLY_ID}",
        f"cursor:{TRANSCRIPT_ONLY_ID}",
        f"cursor:{CLI_ID}",
        f"cursor:{BROKEN_ID}",
    }

    shared = sessions[f"cursor:{SHARED_ID}"]
    assert shared.source_variant == "composer"
    assert shared.initial_cwd == "/fixture/mapped-workspace"
    assert shared.model_key == "fixture-model"
    assert shared.title == "Shared composer"
    assert shared.started_at.timestamp() == 1_700_000_000
    assert shared.last_active_at.timestamp() == 1_700_000_100
    assert shared.metadata["source_variants"] == ["composer", "transcript"]
    assert shared.metadata["time_confidence"] == "metadata"
    assert shared.metadata["usage_status"] == "unavailable"

    header_only = sessions[f"cursor:{HEADER_ONLY_ID}"]
    assert header_only.source_variant == "composer"
    assert header_only.initial_cwd == "/fixture/direct-workspace"

    transcript_only = sessions[f"cursor:{TRANSCRIPT_ONLY_ID}"]
    assert transcript_only.source_variant == "transcript"
    assert transcript_only.metadata["time_confidence"] == "mtime"
    assert transcript_only.started_at.timestamp() == 1_710_000_000

    cli = sessions[f"cursor:{CLI_ID}"]
    assert cli.source_variant == "agent-cli"
    assert cli.initial_cwd == "/fixture/cli-workspace"
    assert cli.model_key == "cli-model"
    assert cli.title == "CLI session"
    assert cli.started_at.timestamp() == 1_700_002_000

    broken = sessions[f"cursor:{BROKEN_ID}"]
    assert broken.source_variant == "agent-cli"
    assert broken.metadata["time_confidence"] == "mtime"
    assert broken.started_at.timestamp() == 1_720_000_000


def _check_dialogue_only_emits_explicit_user_assistant_text(tmp_path: Path) -> None:
    runtime, _ = _runtime_fixture(tmp_path)

    first = list(runtime.dialogue())
    second = list(runtime.dialogue())

    assert [record.external_message_key for record in first] == [
        record.external_message_key for record in second
    ]
    by_session: dict[str, list] = {}
    for record in first:
        by_session.setdefault(record.external_session_key, []).append(record)

    assert [(record.role, record.content) for record in by_session[f"cursor:{SHARED_ID}"]] == [
        ("user", "Shared question"),
        ("assistant", "Shared answer"),
    ]
    assert [
        (record.role, record.content) for record in by_session[f"cursor:{TRANSCRIPT_ONLY_ID}"]
    ] == [
        ("user", "Transcript-only question"),
        ("assistant", "Transcript-only answer"),
    ]
    assert [(record.role, record.content) for record in by_session[f"cursor:{CLI_ID}"]] == [
        ("user", "CLI question"),
        ("assistant", "CLI answer"),
    ]
    serialized = repr(first)
    assert "hidden reasoning" not in serialized
    assert "hidden tool" not in serialized
    assert "hidden system" not in serialized
    assert SENSITIVE_SENTINEL not in serialized
    assert all(record.occurred_at is not None for record in first)
    assert {
        record.metadata.get("timestamp_confidence")
        for record in first
    } == {"low"}


def _check_local_usage_is_explicitly_unavailable(tmp_path: Path) -> None:
    runtime, _ = _runtime_fixture(tmp_path)

    assert tuple(runtime.usage()) == ()
    assert runtime.usage_status == "unavailable"
    assert "usage" not in runtime.capabilities
    assert runtime.tool_key == "cursor"


def _check_artifacts_include_only_existing_local_sources(tmp_path: Path) -> None:
    runtime, paths = _runtime_fixture(tmp_path)

    assert set(runtime.artifacts()) == set(paths.values())


def _check_auth_and_encryption_values_never_reach_json_decoder(tmp_path: Path) -> None:
    runtime, _ = _runtime_fixture(tmp_path)
    real_loads = json.loads

    def guarded_loads(value, *args, **kwargs):
        if isinstance(value, bytes):
            inspected = value.decode("utf-8", errors="ignore")
        else:
            inspected = str(value)
        assert SENSITIVE_SENTINEL not in inspected
        return real_loads(value, *args, **kwargs)

    with patch.object(cursor_source.json, "loads", guarded_loads):
        sessions = list(runtime.sessions())
        dialogue = list(runtime.dialogue())
    assert sessions
    assert dialogue


def _check_malformed_ide_database_degrades_without_exception(tmp_path: Path) -> None:
    home = tmp_path / ".cursor"
    malformed = tmp_path / "state.vscdb"
    malformed.write_bytes(b"not sqlite")
    transcript = _write_transcript(
        home,
        TRANSCRIPT_ONLY_ID,
        [{"role": "user", "message": {"content": "still available"}}],
    )
    runtime = CursorRuntime(home, ide_state_dbs=(malformed,))

    sessions = list(runtime.sessions())
    dialogue = list(runtime.dialogue())

    assert [record.external_session_key for record in sessions] == [
        f"cursor:{TRANSCRIPT_ONLY_ID}"
    ]
    assert [record.content for record in dialogue] == ["still available"]
    assert malformed in runtime.artifacts()
    assert transcript in runtime.artifacts()


class CursorRuntimeTests(unittest.TestCase):
    def _run_with_tmp_path(self, check) -> None:
        with tempfile.TemporaryDirectory() as directory:
            check(Path(directory))

    def test_sessions_union_header_transcript_cli_and_safe_fallback(self) -> None:
        self._run_with_tmp_path(
            _check_sessions_union_header_transcript_cli_and_safe_fallback
        )

    def test_dialogue_only_emits_explicit_user_assistant_text(self) -> None:
        self._run_with_tmp_path(_check_dialogue_only_emits_explicit_user_assistant_text)

    def test_local_usage_is_explicitly_unavailable(self) -> None:
        self._run_with_tmp_path(_check_local_usage_is_explicitly_unavailable)

    def test_artifacts_include_only_existing_local_sources(self) -> None:
        self._run_with_tmp_path(_check_artifacts_include_only_existing_local_sources)

    def test_auth_and_encryption_values_never_reach_json_decoder(self) -> None:
        self._run_with_tmp_path(
            _check_auth_and_encryption_values_never_reach_json_decoder
        )

    def test_malformed_ide_database_degrades_without_exception(self) -> None:
        self._run_with_tmp_path(_check_malformed_ide_database_degrades_without_exception)
