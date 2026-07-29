from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentic_rag.rag_settings import resolve_rag_settings
from agentic_rag.rag_profile import settings_embedding_profile, source_profile_hash
from agentic_rag.rag_status import read_rag_status
from agentic_rag.rag_v2_indexer import _source_profile, collect_candidate_chunks
from data_foundation.paths import initialize_home


class NativeMemoryRagIntegrationTests(unittest.TestCase):
    def test_missing_native_settings_use_enabled_defaults_in_rag_source_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings_path = paths.config_dir / "settings.json"
            settings_path.write_text(
                json.dumps({"schemaVersion": 1}),
                encoding="utf-8",
            )

            resolved = resolve_rag_settings(paths)
            profile = _source_profile(resolved, resolved.indexing_source_sets)

            self.assertIn("agent-native-memory", resolved.indexing_source_sets)
            self.assertIn("agent-native-instructions", resolved.indexing_source_sets)
            self.assertTrue(profile["nativeMemory"]["enabled"])
            self.assertTrue(profile["nativeMemory"]["allowInRag"])
            self.assertTrue(profile["nativeMemory"]["includeInstructions"])
            self.assertEqual(
                profile["nativeMemory"]["tools"],
                {"codex": True, "claudeCode": True},
            )

    def test_explicit_native_disable_removes_default_rag_source_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings_path = paths.config_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "memorySearch": {
                            "nativeMemory": {
                                "enabled": False,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            resolved = resolve_rag_settings(paths)

            self.assertNotIn("agent-native-memory", resolved.indexing_source_sets)
            self.assertNotIn("agent-native-instructions", resolved.indexing_source_sets)

    def test_native_memory_is_local_only_until_explicitly_allowed_in_rag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = initialize_home(root / "Actanara")
            codex_home = root / "codex"
            memory_file = codex_home / "memories" / "MEMORY.md"
            instructions_file = codex_home / "AGENTS.md"
            memory_file.parent.mkdir(parents=True)
            memory_file.write_text("# Native recall\n\nRemember moonharbor.", encoding="utf-8")
            instructions_file.write_text("# Instructions\n\nPrefer concise output.", encoding="utf-8")

            base = {
                "schemaVersion": 1,
                "features": {"rag": True},
                "externalTools": {"codex": {"home": str(codex_home)}},
                "memorySearch": {
                    "nativeMemory": {
                        "enabled": True,
                        "allowInRag": False,
                        "includeInstructions": True,
                        "tools": {"codex": True, "claudeCode": False},
                    }
                },
            }
            settings_path = paths.config_dir / "settings.json"
            settings_path.write_text(json.dumps(base), encoding="utf-8")
            local_only = resolve_rag_settings(paths)
            self.assertNotIn("agent-native-memory", local_only.indexing_source_sets)
            self.assertNotIn("agent-native-instructions", local_only.indexing_source_sets)

            base["memorySearch"]["nativeMemory"]["allowInRag"] = True
            settings_path.write_text(json.dumps(base), encoding="utf-8")
            opted_in = resolve_rag_settings(paths)
            self.assertIn("agent-native-memory", opted_in.indexing_source_sets)
            self.assertIn("agent-native-instructions", opted_in.indexing_source_sets)

            chunks, sources = collect_candidate_chunks(
                opted_in,
                ("agent-native-memory", "agent-native-instructions"),
            )
            self.assertEqual(
                {item["sourceSet"] for item in chunks},
                {"agent-native-memory", "agent-native-instructions"},
            )
            self.assertEqual(len(sources), 2)

            memory_only, memory_sources = collect_candidate_chunks(
                opted_in,
                ("agent-native-memory",),
            )
            self.assertEqual(
                {item["sourceSet"] for item in memory_only},
                {"agent-native-memory"},
            )
            self.assertEqual(
                {item["sourceSet"] for item in memory_sources},
                {"agent-native-memory"},
            )

            instructions_only, instruction_sources = collect_candidate_chunks(
                opted_in,
                ("agent-native-instructions",),
            )
            self.assertEqual(
                {item["sourceSet"] for item in instructions_only},
                {"agent-native-instructions"},
            )
            self.assertEqual(
                {item["sourceSet"] for item in instruction_sources},
                {"agent-native-instructions"},
            )

            base["memorySearch"]["nativeMemory"]["tools"]["codex"] = False
            settings_path.write_text(json.dumps(base), encoding="utf-8")
            no_tools = resolve_rag_settings(paths)
            self.assertNotIn("agent-native-memory", no_tools.indexing_source_sets)
            self.assertNotIn("agent-native-instructions", no_tools.indexing_source_sets)

    def test_native_tool_policy_changes_rag_source_profile_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings_path = paths.config_dir / "settings.json"
            payload = {
                "schemaVersion": 1,
                "memorySearch": {
                    "nativeMemory": {
                        "enabled": True,
                        "allowInRag": True,
                        "includeInstructions": True,
                        "tools": {"codex": True, "claudeCode": True},
                    }
                },
            }
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            both = resolve_rag_settings(paths)
            both_profile = _source_profile(both, both.indexing_source_sets)

            payload["memorySearch"]["nativeMemory"]["tools"]["codex"] = False
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            claude_only = resolve_rag_settings(paths)
            claude_profile = _source_profile(
                claude_only,
                claude_only.indexing_source_sets,
            )

            self.assertEqual(
                set(both.indexing_source_sets),
                set(claude_only.indexing_source_sets),
            )
            self.assertNotEqual(
                source_profile_hash(both_profile),
                source_profile_hash(claude_profile),
            )
            self.assertTrue(both_profile["nativeMemory"]["tools"]["codex"])
            self.assertFalse(claude_profile["nativeMemory"]["tools"]["codex"])

    def test_native_tool_policy_change_marks_existing_rag_index_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings_path = paths.config_dir / "settings.json"
            payload = {
                "schemaVersion": 1,
                "features": {"rag": True},
                "rag": {"enabled": True, "mode": "v2"},
                "memorySearch": {
                    "nativeMemory": {
                        "enabled": True,
                        "allowInRag": True,
                        "includeInstructions": True,
                        "tools": {"codex": True, "claudeCode": True},
                    }
                },
            }
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            indexed_settings = resolve_rag_settings(paths)
            indexed_profile = _source_profile(
                indexed_settings,
                indexed_settings.indexing_source_sets,
            )
            active_index = (
                indexed_settings.v2_store_path
                / "indexes"
                / "active"
                / "native-policy"
                / "index.jsonl"
            )
            active_index.parent.mkdir(parents=True)
            active_index.write_text("{}\n", encoding="utf-8")
            (indexed_settings.v2_store_path / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "dimension": indexed_settings.embedding_dimension,
                        "activeIndexPath": str(active_index),
                        "embeddingProfile": settings_embedding_profile(indexed_settings),
                        "sourceProfile": indexed_profile,
                    }
                ),
                encoding="utf-8",
            )

            payload["memorySearch"]["nativeMemory"]["tools"]["codex"] = False
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            status = read_rag_status(
                settings=resolve_rag_settings(paths),
                count_legacy_entries=False,
            )

            self.assertTrue(status["ready"])
            self.assertTrue(status["sourceProfile"]["mismatch"])
            self.assertTrue(status["sourceProfile"]["migrationRequired"])
            self.assertEqual(status["freshness"]["status"], "source-profile-mismatch")
            self.assertFalse(status["searchAvailable"])
            self.assertTrue(
                status["sourceProfile"]["active"]["nativeMemory"]["tools"]["codex"]
            )
            self.assertFalse(
                status["sourceProfile"]["configured"]["nativeMemory"]["tools"]["codex"]
            )


if __name__ == "__main__":
    unittest.main()
