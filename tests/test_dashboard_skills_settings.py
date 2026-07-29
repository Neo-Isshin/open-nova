import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import settings as dashboard_settings
from app.services import skills
from data_foundation.paths import initialize_home
from data_foundation.settings import read_settings, write_settings


class DashboardSkillsSettingsTests(unittest.TestCase):
    def test_skills_service_uses_configured_openclaw_skill_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            custom_root = root / "configured-tools" / "openclaw" / "workspace" / "skills"
            system_root = root / "configured-tools" / "openclaw" / "skills"
            _write_skill(custom_root / "custom-one", "custom description")
            _write_skill(system_root / "system-one", "system description")
            write_settings(
                {
                    "externalTools": {
                        "openclaw": {
                            "skillsRoot": str(custom_root),
                            "systemSkillsRoot": str(system_root),
                        }
                    }
                },
                paths,
            )

            skills.CUSTOM_SKILLS_DIR = skills._DEFAULT_CUSTOM_SKILLS_DIR
            skills.SYSTEM_SKILLS_DIR = skills._DEFAULT_SYSTEM_SKILLS_DIR
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                payload = skills.get_all_skills()

        self.assertEqual([item["id"] for item in payload["custom"]], ["custom-one"])
        self.assertEqual(payload["custom"][0]["description"], "custom description")
        self.assertEqual([item["id"] for item in payload["system"]], ["system-one"])
        self.assertEqual(payload["system"][0]["description"], "system description")


class DashboardSettingsBundleRegressionTests(unittest.TestCase):
    def test_native_memory_bundle_persists_explicit_consent_without_replacing_local_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            read_settings(paths, redact_secrets=False)
            raw_before = _read_raw_settings(paths)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=True):
                saved = dashboard_settings.update_settings_bundle(
                    {
                        "settings": {
                            "memorySearch": {
                                "nativeMemory": {
                                    "enabled": True,
                                    "allowInRag": False,
                                    "includeInstructions": True,
                                    "tools": {
                                        "codex": True,
                                        "claudeCode": False,
                                    },
                                }
                            }
                        }
                    }
                )

            raw_after = _read_raw_settings(paths)

        self.assertTrue(saved["memorySearch"]["nativeMemory"]["enabled"])
        self.assertTrue(saved["memorySearch"]["nativeMemory"]["includeInstructions"])
        self.assertTrue(saved["memorySearch"]["nativeMemory"]["tools"]["codex"])
        self.assertFalse(saved["memorySearch"]["nativeMemory"]["tools"]["claudeCode"])
        self.assertEqual(
            raw_after["memorySearch"]["local"],
            raw_before["memorySearch"]["local"],
        )

    def test_general_only_bundle_preserves_fresh_llm_groups_and_skips_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            read_settings(paths, redact_secrets=False)
            raw_before = _read_raw_settings(paths)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=True):
                with patch.object(
                    dashboard_settings,
                    "_raise_if_llm_provider_not_pipeline_ready",
                ) as readiness:
                    saved = dashboard_settings.update_settings_bundle(
                        {"settings": {"general": {"locale": "en-US"}}}
                    )

            raw_after = _read_raw_settings(paths)

        self.assertEqual(saved["general"]["locale"], "en-US")
        self.assertEqual(raw_after["llmProvider"], raw_before["llmProvider"])
        self.assertEqual(raw_after["llmProviderSecrets"], raw_before["llmProviderSecrets"])
        readiness.assert_not_called()

    def test_explicit_incomplete_llm_provider_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            read_settings(paths, redact_secrets=False)
            raw_before = _read_raw_settings(paths)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=True):
                with self.assertRaises(ValueError):
                    dashboard_settings.update_settings_bundle(
                        {
                            "llmProvider": {
                                "mode": "custom",
                                "provider": "custom",
                                "endpoint": "",
                                "model": "",
                            }
                        }
                    )

            raw_after = _read_raw_settings(paths)

        self.assertEqual(raw_after, raw_before)


def _read_raw_settings(paths) -> dict:
    return json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))


def _write_skill(path: Path, description: str) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\ndescription: {description}\n---\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
