from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.paths import initialize_home
from data_foundation.settings import (
    read_settings,
    resolve_memory_search_settings,
    validate_operator_settings_update,
    write_operator_settings,
)


class MemorySearchSettingsTests(unittest.TestCase):
    def test_defaults_keep_local_fallback_and_all_native_memory_scopes_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings = resolve_memory_search_settings(paths)

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["backendPolicy"], "auto")
        self.assertTrue(settings["local"]["enabled"])
        self.assertTrue(settings["local"]["syncAfterPipeline"])
        self.assertTrue(settings["nativeMemory"]["enabled"])
        self.assertTrue(settings["nativeMemory"]["includeInstructions"])
        self.assertTrue(settings["nativeMemory"]["allowInRag"])
        self.assertEqual(
            settings["nativeMemory"]["tools"],
            {"codex": True, "claudeCode": True},
        )

    def test_pre_memory_search_settings_upgrade_adds_enabled_native_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            settings_path = paths.config_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "general": {"locale": "en-US"},
                    }
                ),
                encoding="utf-8",
            )

            read_settings(paths, redact_secrets=False)
            settings = resolve_memory_search_settings(paths)
            persisted = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["backendPolicy"], "auto")
        self.assertTrue(settings["local"]["enabled"])
        self.assertTrue(settings["nativeMemory"]["enabled"])
        self.assertTrue(settings["nativeMemory"]["allowInRag"])
        self.assertTrue(settings["nativeMemory"]["includeInstructions"])
        self.assertEqual(
            settings["nativeMemory"]["tools"],
            {"codex": True, "claudeCode": True},
        )
        self.assertEqual(persisted["memorySearch"]["nativeMemory"], settings["nativeMemory"])

    def test_additive_upgrade_preserves_each_explicit_false(self) -> None:
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
                                "allowInRag": False,
                                "includeInstructions": False,
                                "tools": {
                                    "codex": False,
                                    "claudeCode": False,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            read_settings(paths, redact_secrets=False)
            settings = resolve_memory_search_settings(paths)

        self.assertFalse(settings["nativeMemory"]["enabled"])
        self.assertFalse(settings["nativeMemory"]["allowInRag"])
        self.assertFalse(settings["nativeMemory"]["includeInstructions"])
        self.assertEqual(
            settings["nativeMemory"]["tools"],
            {"codex": False, "claudeCode": False},
        )

    def test_operator_update_round_trips_explicit_native_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            write_operator_settings(
                {
                    "memorySearch": {
                        "backendPolicy": "local",
                        "nativeMemory": {
                            "enabled": True,
                            "includeInstructions": False,
                            "allowInRag": False,
                            "tools": {"codex": True, "claudeCode": False},
                        },
                    }
                },
                paths,
            )
            settings = resolve_memory_search_settings(paths)

        self.assertEqual(settings["backendPolicy"], "local")
        self.assertTrue(settings["nativeMemory"]["enabled"])
        self.assertTrue(settings["nativeMemory"]["tools"]["codex"])
        self.assertFalse(settings["nativeMemory"]["tools"]["claudeCode"])

    def test_validation_rejects_unknown_tools_and_unsafe_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported memorySearch.nativeMemory tools"):
            validate_operator_settings_update(
                {
                    "memorySearch": {
                        "nativeMemory": {"tools": {"cursor": True}},
                    }
                }
            )
        with self.assertRaisesRegex(ValueError, "maxScanBytes"):
            validate_operator_settings_update(
                {
                    "memorySearch": {
                        "local": {"maxScanBytes": 0},
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
