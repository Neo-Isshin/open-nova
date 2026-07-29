from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.local_memory_search import (
    bounded_scan_local_memory_corpus,
    ensure_local_memory_index,
    local_memory_index_path,
    local_memory_status,
    rebuild_local_memory_index,
    search_local_memory,
    sync_local_memory_index,
)
from data_foundation.paths import initialize_home
from data_foundation.pipeline import sync_local_memory_after_pipeline


def _chunk(
    identifier: str,
    text: str,
    *,
    source_id: str,
    source_set: str = "lessons",
    project: str | None = "actanara",
    agent: str | None = "codex",
    date: str = "2026-07-29",
    lineage: str | None = None,
) -> dict:
    return {
        "id": identifier,
        "text": text,
        "date": date,
        "agent": agent,
        "project": project,
        "sourceSet": source_set,
        "sourceId": source_id,
        "sourceType": "jsonl",
        "sourcePath": f"/evidence/{source_id}.jsonl",
        "lineNumber": 1,
        "lineageFamily": lineage,
        "governance": {
            "authorityRank": 80,
            "lifecycle": "canonical",
        },
        "provenance": {"role": agent},
    }


def _source(source_id: str, *, fingerprint: str, source_set: str = "lessons") -> dict:
    return {
        "sourceId": source_id,
        "sourceSet": source_set,
        "sourceType": "jsonl",
        "path": f"/evidence/{source_id}.jsonl",
        "fingerprint": fingerprint,
    }


class LocalMemorySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = initialize_home(Path(self.temporary.name) / "Actanara")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_incremental_sync_and_english_chinese_recall(self) -> None:
        chunks = [
            _chunk(
                "one",
                "跨 Agent 记忆检索可以使用本地 SQLite。 The release codename is moonharbor.",
                source_id="source-one",
            )
        ]
        sources = [_source("source-one", fingerprint="v1")]

        first = sync_local_memory_index(self.paths, chunks=chunks, sources=sources)
        second = sync_local_memory_index(self.paths, chunks=chunks, sources=sources)

        self.assertEqual(first["changedSources"], 1)
        self.assertEqual(second["changedSources"], 0)
        self.assertEqual(second["unchangedSources"], 1)
        self.assertEqual(second["documentCount"], 1)
        self.assertTrue(first["capabilities"]["exactScan"])

        chinese = search_local_memory("记忆检索", paths=self.paths, ensure_fresh=False)
        english = search_local_memory("moonharbor", paths=self.paths, ensure_fresh=False)
        self.assertEqual(chinese["results"][0]["id"], "one")
        self.assertEqual(english["results"][0]["id"], "one")
        self.assertEqual(chinese["backend"]["kind"], "local-fts")
        self.assertFalse(chinese["capabilities"]["semantic"])
        self.assertTrue(chinese["citationPack"])

    def test_incremental_fingerprint_catches_same_manifest_metadata_change(self) -> None:
        first_chunk = _chunk(
            "stable-id",
            "same length alpha",
            source_id="stable-source",
        )
        source = _source("stable-source", fingerprint="unchanged-manifest")
        sync_local_memory_index(
            self.paths,
            chunks=[first_chunk],
            sources=[source],
        )
        changed_chunk = {
            **first_chunk,
            "text": "same length bravo",
        }
        result = sync_local_memory_index(
            self.paths,
            chunks=[changed_chunk],
            sources=[source],
        )
        self.assertEqual(result["changedSources"], 1)
        self.assertEqual(
            search_local_memory(
                "bravo",
                paths=self.paths,
                ensure_fresh=False,
            )["results"][0]["id"],
            "stable-id",
        )

    def test_filters_are_parameterized_and_source_deletion_is_reflected(self) -> None:
        chunks = [
            _chunk("alpha", "needle alpha", source_id="a", project="alpha"),
            _chunk("beta", "needle beta", source_id="b", project="beta"),
        ]
        sources = [
            _source("a", fingerprint="a1"),
            _source("b", fingerprint="b1"),
        ]
        sync_local_memory_index(self.paths, chunks=chunks, sources=sources)

        filtered = search_local_memory(
            "needle",
            filters={"project": "alpha' OR 1=1 --"},
            paths=self.paths,
            ensure_fresh=False,
        )
        self.assertEqual(filtered["results"], [])
        self.assertEqual(local_memory_status(self.paths)["documentCount"], 2)

        result = sync_local_memory_index(
            self.paths,
            chunks=[chunks[0]],
            sources=[sources[0]],
        )
        self.assertEqual(result["deletedSources"], 1)
        remaining = search_local_memory("needle", paths=self.paths, ensure_fresh=False)
        self.assertEqual([item["id"] for item in remaining["results"]], ["alpha"])

    def test_all_public_metadata_filters_are_enforced(self) -> None:
        alpha = _chunk(
            "alpha",
            "shared filter needle",
            source_id="a",
            agent="codex",
            date="2026-07-20",
        )
        alpha["tags"] = ["release", "memory"]
        alpha["workType"] = "implementation"
        alpha["governance"]["lifecycle"] = "current-state"
        beta = _chunk(
            "beta",
            "shared filter needle",
            source_id="b",
            agent="claude-code",
            date="2026-07-28",
        )
        beta["tags"] = ["research"]
        beta["workType"] = "analysis"
        beta["governance"]["lifecycle"] = "episodic"
        sync_local_memory_index(
            self.paths,
            chunks=[alpha, beta],
            sources=[
                _source("a", fingerprint="a1"),
                _source("b", fingerprint="b1"),
            ],
        )

        cases = (
            ({"dateRange": {"from": "2026-07-25", "to": "2026-07-29"}}, "beta"),
            ({"lifecycle": "current-state"}, "alpha"),
            ({"workType": "analysis"}, "beta"),
            ({"agent": "codex"}, "alpha"),
            ({"tags": ["release"]}, "alpha"),
        )
        for filters, expected in cases:
            with self.subTest(filters=filters):
                response = search_local_memory(
                    "filter needle",
                    filters=filters,
                    paths=self.paths,
                    ensure_fresh=False,
                )
                self.assertEqual([item["id"] for item in response["results"]], [expected])

    def test_query_syntax_is_data_not_sql_or_fts_control(self) -> None:
        sync_local_memory_index(
            self.paths,
            chunks=[
                _chunk(
                    "syntax",
                    'literal "quoted" OR token and x); DROP TABLE memory_documents; --',
                    source_id="syntax-source",
                )
            ],
            sources=[_source("syntax-source", fingerprint="s1")],
        )

        response = search_local_memory(
            '"quoted" OR token',
            paths=self.paths,
            ensure_fresh=False,
        )
        self.assertTrue(response["available"])
        connection = sqlite3.connect(local_memory_index_path(self.paths))
        try:
            count = connection.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_index_and_parent_permissions_are_private(self) -> None:
        sync_local_memory_index(
            self.paths,
            chunks=[_chunk("one", "private recall", source_id="private")],
            sources=[_source("private", fingerprint="p1")],
        )
        index = local_memory_index_path(self.paths)
        self.assertEqual(os.stat(index).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(index.parent).st_mode & 0o777, 0o700)

    def test_index_symlink_is_unsafe_and_never_touches_external_target(self) -> None:
        index = local_memory_index_path(self.paths)
        index.parent.mkdir(parents=True, exist_ok=True)
        external = Path(self.temporary.name) / "external-index.sqlite3"
        original_bytes = b"external data must remain untouched"
        external.write_bytes(original_bytes)
        external.chmod(0o640)
        original_mode = external.stat().st_mode & 0o777
        index.symlink_to(external)

        status = local_memory_status(self.paths)
        self.assertEqual(status["status"], "unsafe")
        self.assertEqual(status["reason"], "local-memory-index-unsafe")
        with self.assertRaisesRegex(ValueError, "must not be a symbolic link"):
            sync_local_memory_index(self.paths, chunks=[], sources=[])

        fallback_file = self.paths.diary_dir / "safe-fallback.md"
        fallback_file.write_text("Fallback remembers silverharbor.", encoding="utf-8")
        response = search_local_memory("silverharbor", paths=self.paths)
        self.assertEqual(response["backend"]["kind"], "bounded-scan")
        self.assertIn("silverharbor", response["results"][0]["text"])
        self.assertEqual(external.read_bytes(), original_bytes)
        self.assertEqual(external.stat().st_mode & 0o777, original_mode)

    def test_cache_parent_symlink_is_unsafe_and_never_writes_outside_runtime(self) -> None:
        index = local_memory_index_path(self.paths)
        external_cache = Path(self.temporary.name) / "external-cache"
        external_cache.mkdir()
        external_cache.chmod(0o750)
        original_mode = external_cache.stat().st_mode & 0o777
        index.parent.rmdir()
        index.parent.symlink_to(external_cache, target_is_directory=True)

        status = local_memory_status(self.paths)
        self.assertEqual(status["status"], "unsafe")
        with self.assertRaisesRegex(ValueError, "cache directory.*symbolic link"):
            sync_local_memory_index(self.paths, chunks=[], sources=[])
        ensured = ensure_local_memory_index(self.paths)
        self.assertEqual(ensured["status"], "unsafe")
        self.assertFalse(ensured["available"])
        self.assertFalse((external_cache / index.name).exists())
        self.assertEqual(external_cache.stat().st_mode & 0o777, original_mode)

    def test_rebuild_backup_symlink_is_rejected_without_touching_target(self) -> None:
        sync_local_memory_index(
            self.paths,
            chunks=[_chunk("prior", "prior safe index", source_id="prior-source")],
            sources=[_source("prior-source", fingerprint="p1")],
        )
        index = local_memory_index_path(self.paths)
        prior_bytes = index.read_bytes()
        external = Path(self.temporary.name) / "external-backup"
        original_bytes = b"external backup target"
        external.write_bytes(original_bytes)
        external.chmod(0o640)
        original_mode = external.stat().st_mode & 0o777
        index.with_name(f"{index.name}.rebuild-backup").symlink_to(external)

        self.assertEqual(local_memory_status(self.paths)["status"], "unsafe")
        with self.assertRaisesRegex(ValueError, "must not be a symbolic link"):
            rebuild_local_memory_index(self.paths)
        self.assertEqual(index.read_bytes(), prior_bytes)
        self.assertEqual(external.read_bytes(), original_bytes)
        self.assertEqual(external.stat().st_mode & 0o777, original_mode)

    def test_rebuild_replaces_index_and_restores_prior_copy_on_failure(self) -> None:
        sync_local_memory_index(
            self.paths,
            chunks=[_chunk("prior", "prior index", source_id="prior-source")],
            sources=[_source("prior-source", fingerprint="p1")],
        )
        prior_bytes = local_memory_index_path(self.paths).read_bytes()
        replacement = _chunk("new", "new index", source_id="new-source")
        with patch(
            "data_foundation.local_memory_search.collect_local_memory_corpus",
            return_value=(
                [replacement],
                [_source("new-source", fingerprint="n1")],
                [],
            ),
        ):
            rebuilt = rebuild_local_memory_index(self.paths)
        self.assertTrue(rebuilt["rebuilt"])
        self.assertEqual(
            search_local_memory("new index", paths=self.paths, ensure_fresh=False)["results"][0]["id"],
            "new",
        )

        current_bytes = local_memory_index_path(self.paths).read_bytes()
        self.assertNotEqual(current_bytes, prior_bytes)
        with patch(
            "data_foundation.local_memory_search.sync_local_memory_index",
            side_effect=RuntimeError("simulated rebuild failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated rebuild failure"):
                rebuild_local_memory_index(self.paths)
        self.assertEqual(local_memory_index_path(self.paths).read_bytes(), current_bytes)

    def test_ensure_index_self_heals_corrupt_and_unknown_schema_sidecars(self) -> None:
        replacement = _chunk("healed", "self healing index", source_id="healed-source")
        collected = (
            [replacement],
            [_source("healed-source", fingerprint="h1")],
            [],
        )
        index = local_memory_index_path(self.paths)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b"not a sqlite database")
        with patch(
            "data_foundation.local_memory_search.collect_local_memory_corpus",
            return_value=collected,
        ):
            corrupt_result = ensure_local_memory_index(self.paths)
        self.assertTrue(corrupt_result["ready"])
        self.assertTrue(corrupt_result["rebuilt"])

        index.unlink()
        connection = sqlite3.connect(index)
        try:
            connection.execute("PRAGMA user_version=99")
            connection.execute("CREATE TABLE obsolete(value TEXT)")
            connection.commit()
        finally:
            connection.close()
        with patch(
            "data_foundation.local_memory_search.collect_local_memory_corpus",
            return_value=collected,
        ):
            schema_result = ensure_local_memory_index(self.paths)
        self.assertTrue(schema_result["ready"])
        self.assertTrue(schema_result["rebuilt"])
        self.assertEqual(local_memory_status(self.paths)["schemaVersion"], 2)

    def test_unavailable_index_uses_bounded_corpus_scan(self) -> None:
        fallback_file = self.paths.diary_dir / "fallback.md"
        fallback_file.write_text(
            "# Recovery\n\nBounded fallback remembers riverstone.",
            encoding="utf-8",
        )
        with patch(
            "data_foundation.local_memory_search.collect_local_memory_corpus",
            side_effect=AssertionError("full corpus collector must not run"),
        ), patch(
            "data_foundation.local_memory_search.ensure_local_memory_index",
            return_value={
                "ready": False,
                "available": False,
                "reason": "simulated-index-failure",
            },
        ):
            response = search_local_memory("riverstone", paths=self.paths)

        self.assertEqual(response["backend"]["kind"], "bounded-scan")
        self.assertEqual(response["backend"]["fallbackFrom"], "simulated-index-failure")
        self.assertIn("riverstone", response["results"][0]["text"])
        self.assertTrue(response["quality"]["flags"]["localLexicalFallback"])
        diagnostic = response["diagnostics"][0]
        self.assertEqual(diagnostic["scannedFiles"], 1)
        self.assertLessEqual(diagnostic["scannedBytes"], diagnostic["maxBytes"])

    def test_bounded_scan_enforces_file_and_byte_limits_before_reading_more(self) -> None:
        for name in ("a.md", "b.md", "c.md"):
            (self.paths.diary_dir / name).write_text(
                f"{name} riverstone should stay bounded",
                encoding="utf-8",
            )
        chunks, diagnostics = bounded_scan_local_memory_corpus(
            "riverstone",
            filters={},
            paths=self.paths,
            max_files=1,
            max_bytes=20,
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(diagnostics[0]["scannedFiles"], 1)
        self.assertLessEqual(diagnostics[0]["scannedBytes"], 20)
        self.assertTrue(diagnostics[0]["truncated"])

    def test_native_opt_out_is_fail_closed_even_when_resync_fails(self) -> None:
        settings_path = self.paths.config_dir / "settings.json"
        enabled = {
            "schemaVersion": 1,
            "memorySearch": {
                "nativeMemory": {
                    "enabled": True,
                    "includeInstructions": True,
                    "tools": {"codex": True, "claudeCode": False},
                }
            },
        }
        settings_path.write_text(json.dumps(enabled), encoding="utf-8")
        native = _chunk(
            "native",
            "policy needle from codex",
            source_id="native-source",
            source_set="agent-native-memory",
            agent="codex",
        )
        instruction = _chunk(
            "instruction",
            "policy needle from instructions",
            source_id="instruction-source",
            source_set="agent-native-instructions",
            agent="codex",
        )
        regular = _chunk(
            "regular",
            "policy needle from actanara",
            source_id="regular-source",
            agent="actanara",
        )
        sync_local_memory_index(
            self.paths,
            chunks=[native, instruction, regular],
            sources=[
                _source("native-source", fingerprint="n1", source_set="agent-native-memory"),
                _source(
                    "instruction-source",
                    fingerprint="i1",
                    source_set="agent-native-instructions",
                ),
                _source("regular-source", fingerprint="r1"),
            ],
        )

        enabled["memorySearch"]["nativeMemory"]["includeInstructions"] = False
        enabled["memorySearch"]["nativeMemory"]["tools"]["codex"] = False
        settings_path.write_text(json.dumps(enabled), encoding="utf-8")
        with patch(
            "data_foundation.local_memory_search.sync_local_memory_index",
            side_effect=sqlite3.OperationalError("busy"),
        ):
            response = search_local_memory("policy needle", paths=self.paths)
        self.assertEqual([item["id"] for item in response["results"]], ["regular"])

    def test_native_root_change_revokes_old_path_even_when_resync_fails(self) -> None:
        old_home = Path(self.temporary.name) / "old-codex"
        new_home = Path(self.temporary.name) / "new-codex"
        settings_path = self.paths.config_dir / "settings.json"
        payload = {
            "schemaVersion": 1,
            "externalTools": {
                "codex": {
                    "home": str(old_home),
                    "skillsRoot": str(old_home / "skills"),
                }
            },
            "memorySearch": {
                "nativeMemory": {
                    "enabled": True,
                    "tools": {"codex": True, "claudeCode": False},
                }
            },
        }
        settings_path.write_text(json.dumps(payload), encoding="utf-8")
        native = _chunk(
            "old-native",
            "revoked path contains oldharbor",
            source_id="old-native-source",
            source_set="agent-native-memory",
            agent="codex",
        )
        native["sourcePath"] = str(old_home / "memories" / "MEMORY.md")
        sync_local_memory_index(
            self.paths,
            chunks=[native],
            sources=[
                _source(
                    "old-native-source",
                    fingerprint="old",
                    source_set="agent-native-memory",
                )
            ],
        )
        self.assertEqual(
            search_local_memory(
                "oldharbor",
                paths=self.paths,
                ensure_fresh=False,
            )["results"][0]["id"],
            "old-native",
        )

        payload["externalTools"]["codex"]["home"] = str(new_home)
        payload["externalTools"]["codex"]["skillsRoot"] = str(new_home / "skills")
        settings_path.write_text(json.dumps(payload), encoding="utf-8")
        with patch(
            "data_foundation.local_memory_search.sync_local_memory_index",
            side_effect=sqlite3.OperationalError("busy"),
        ):
            response = search_local_memory("oldharbor", paths=self.paths)
        self.assertEqual(response["results"], [])

    def test_native_root_symlink_retarget_revokes_stale_rows_when_resync_fails(self) -> None:
        old_home = Path(self.temporary.name) / "old-codex-target"
        new_home = Path(self.temporary.name) / "new-codex-target"
        configured_home = Path(self.temporary.name) / "configured-codex"
        (old_home / "memories").mkdir(parents=True)
        (new_home / "memories").mkdir(parents=True)
        try:
            configured_home.symlink_to(old_home, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        settings_path = self.paths.config_dir / "settings.json"
        payload = {
            "schemaVersion": 1,
            "externalTools": {
                "codex": {
                    "home": str(configured_home),
                    "skillsRoot": str(configured_home / "skills"),
                }
            },
            "memorySearch": {
                "nativeMemory": {
                    "enabled": True,
                    "tools": {"codex": True, "claudeCode": False},
                }
            },
        }
        settings_path.write_text(json.dumps(payload), encoding="utf-8")
        native = _chunk(
            "retargeted-native",
            "retarget policy contains linkharbor",
            source_id="retargeted-native-source",
            source_set="agent-native-memory",
            agent="codex",
        )
        native["sourcePath"] = str(
            configured_home / "memories" / "MEMORY.md"
        )
        sync_local_memory_index(
            self.paths,
            chunks=[native],
            sources=[
                _source(
                    "retargeted-native-source",
                    fingerprint="old-target",
                    source_set="agent-native-memory",
                )
            ],
        )
        self.assertEqual(
            search_local_memory(
                "linkharbor",
                paths=self.paths,
                ensure_fresh=False,
            )["results"][0]["id"],
            "retargeted-native",
        )

        configured_home.unlink()
        configured_home.symlink_to(new_home, target_is_directory=True)
        with patch(
            "data_foundation.local_memory_search.sync_local_memory_index",
            side_effect=sqlite3.OperationalError("busy"),
        ):
            response = search_local_memory("linkharbor", paths=self.paths)

        self.assertEqual(response["results"], [])
        self.assertTrue(response["backend"]["nativePolicyStale"])

    def test_readonly_uri_handles_reserved_path_characters(self) -> None:
        special_paths = initialize_home(
            Path(self.temporary.name) / "Actanara ?#% instance"
        )
        sync_local_memory_index(
            special_paths,
            chunks=[_chunk("special", "uri-safe recall", source_id="special-source")],
            sources=[_source("special-source", fingerprint="s1")],
        )
        self.assertTrue(local_memory_status(special_paths)["ready"])
        response = search_local_memory(
            "uri-safe",
            paths=special_paths,
            ensure_fresh=False,
        )
        self.assertEqual(response["results"][0]["id"], "special")

    def test_local_search_can_be_disabled_in_settings(self) -> None:
        (self.paths.config_dir / "settings.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "memorySearch": {
                        "enabled": True,
                        "local": {"enabled": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = sync_local_memory_index(self.paths, chunks=[], sources=[])
        self.assertEqual(result["status"], "disabled")
        response = search_local_memory("anything", paths=self.paths)
        self.assertFalse(response["available"])
        self.assertEqual(response["reason"], "local-memory-search-disabled")

    def test_pipeline_sync_is_non_fatal_and_honors_settings(self) -> None:
        with patch(
            "data_foundation.local_memory_search.sync_local_memory_index",
            side_effect=sqlite3.OperationalError("busy"),
        ):
            degraded = sync_local_memory_after_pipeline(self.paths)
        self.assertEqual(degraded["status"], "degraded")
        self.assertIn("OperationalError", degraded["reason"])

        (self.paths.config_dir / "settings.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "memorySearch": {
                        "local": {"syncAfterPipeline": False},
                    },
                }
            ),
            encoding="utf-8",
        )
        skipped = sync_local_memory_after_pipeline(self.paths)
        self.assertEqual(skipped["status"], "skipped")

    def test_native_memory_reads_only_explicitly_enabled_tools(self) -> None:
        (self.paths.config_dir / "settings.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "memorySearch": {
                        "nativeMemory": {
                            "enabled": True,
                            "allowInRag": False,
                            "tools": {"codex": True, "claudeCode": False},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        empty = {
            "documents": [],
            "sources": [],
            "diagnostics": [],
        }
        with patch(
            "data_foundation.memory_corpus.collect_runtime_memory_corpus",
            return_value=([], []),
        ), patch(
            "data_foundation.native_memory_sources.collect_codex_native_memory",
            return_value=empty,
        ) as codex, patch(
            "data_foundation.native_memory_sources.collect_claude_native_memory",
            return_value=empty,
        ) as claude:
            from data_foundation.local_memory_search import collect_local_memory_corpus

            collect_local_memory_corpus(self.paths)

        codex.assert_called_once()
        claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
