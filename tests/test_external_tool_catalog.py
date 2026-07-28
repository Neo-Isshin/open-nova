import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.external_tool_catalog import (
    add_external_tool_instance,
    detect_external_tools,
    detected_external_tool_ids,
    rediscover_external_tools,
    supported_external_tool_catalog,
)
from data_foundation.paths import initialize_home
from data_foundation.settings import default_external_tool_settings, read_settings, write_settings


class ExternalToolCatalogTests(unittest.TestCase):
    def test_catalog_documents_supported_fields_and_skill_registration(self):
        catalog = supported_external_tool_catalog()
        by_id = {item["id"]: item for item in catalog["tools"]}

        self.assertIn("openclaw", by_id)
        self.assertIn("opencode", by_id)
        self.assertIn("antigravity", by_id)
        self.assertIn("cursor", by_id)
        self.assertIn("agentsRoot", by_id["openclaw"]["fields"])
        self.assertIn("~/.openclaw-*", by_id["openclaw"]["homeCandidates"])
        self.assertIn("globalSkillRegistration", by_id["codex"])
        for definition in by_id.values():
            fields = set(definition["fields"])
            for target in definition["globalSkillRegistration"]["targets"]:
                self.assertIn(target, fields)
        self.assertEqual(by_id["hermes"]["globalSkillRegistration"]["targets"], ["skillsRoot"])
        self.assertNotIn("optionalSkillsRoot", by_id["hermes"]["globalSkillRegistration"]["targets"])
        self.assertEqual(by_id["opencode"]["globalSkillRegistration"]["targets"], [])
        self.assertIn("usage-unavailable", by_id["cursor"]["capabilities"])

    def test_catalog_fields_match_settings_defaults(self):
        home = Path("/Users/example")
        defaults = default_external_tool_settings(home)
        catalog = supported_external_tool_catalog()
        by_id = {item["id"]: item for item in catalog["tools"]}

        self.assertEqual(set(defaults), set(by_id))
        for tool_id, values in defaults.items():
            self.assertEqual(set(values), set(by_id[tool_id]["fields"]))

    def test_opencode_defaults_honor_xdg_without_overriding_explicit_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_home = root / "home"
            xdg_data = root / "xdg-data"
            xdg_config = root / "xdg-config"
            with (
                patch("data_foundation.external_tool_definitions.Path.home", return_value=user_home),
                patch.dict(
                    "os.environ",
                    {
                        "XDG_DATA_HOME": str(xdg_data),
                        "XDG_CONFIG_HOME": str(xdg_config),
                    },
                    clear=False,
                ),
            ):
                defaults = default_external_tool_settings()
            explicit = default_external_tool_settings(user_home)

        self.assertEqual(defaults["opencode"]["home"], str(xdg_data / "opencode"))
        self.assertEqual(defaults["opencode"]["configPath"], str(xdg_config / "opencode" / "opencode.jsonc"))
        self.assertEqual(explicit["opencode"]["home"], str(user_home / ".local" / "share" / "opencode"))

    def test_rediscover_names_second_openclaw_instance_without_overwriting_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            primary = home / ".openclaw"
            secondary = home / ".openclaw-work"
            (primary / "agents").mkdir(parents=True)
            (primary / "config.json").write_text("{}\n", encoding="utf-8")
            (secondary / "agents").mkdir(parents=True)
            (secondary / "config.json").write_text("{}\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"externalTools": {"openclaw": {"home": str(primary)}}}, paths)

            with patch("data_foundation.external_tool_catalog.Path.home", return_value=home):
                result = rediscover_external_tools(paths)

        discoveries = {item["path"]: item for item in result["discoveries"]}
        self.assertEqual(discoveries[str(primary.absolute())]["status"], "unchanged")
        self.assertEqual(discoveries[str(secondary.absolute())]["instanceId"], "openclaw-2")
        self.assertIn("openclaw-2", result["suggestedUpdates"])

    def test_rediscover_uses_catalog_home_candidates_for_supported_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex = home / ".codex"
            gemini = home / ".gemini"
            hermes = home / ".hermes"
            opencode = home / ".local" / "share" / "opencode"
            cursor = home / ".cursor"
            (codex / "sessions").mkdir(parents=True)
            (gemini / "tmp").mkdir(parents=True)
            (gemini / "antigravity-cli" / "conversations").mkdir(parents=True)
            (hermes / "profiles").mkdir(parents=True)
            (opencode / "storage" / "session").mkdir(parents=True)
            (cursor / "chats").mkdir(parents=True)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")

            with patch("data_foundation.external_tool_catalog.Path.home", return_value=home):
                result = rediscover_external_tools(paths)

        by_tool = {item["tool"]: item for item in result["discoveries"]}
        self.assertEqual(by_tool["codex"]["status"], "unchanged")
        self.assertEqual(by_tool["codex"]["update"]["sessionsRoot"], str(codex.absolute() / "sessions"))
        self.assertEqual(by_tool["geminiCli"]["status"], "unchanged")
        self.assertEqual(by_tool["geminiCli"]["update"]["chatsRoot"], str(gemini.absolute() / "tmp" / "ssd" / "chats"))
        self.assertEqual(by_tool["hermes"]["status"], "unchanged")
        self.assertEqual(by_tool["hermes"]["update"]["stateDbPath"], str(hermes.absolute() / "state.db"))
        self.assertEqual(by_tool["opencode"]["update"]["databasePath"], str(opencode.absolute() / "opencode.db"))
        self.assertEqual(by_tool["antigravity"]["update"]["cliHome"], str(gemini.absolute() / "antigravity-cli"))
        self.assertEqual(by_tool["cursor"]["update"]["chatsRoot"], str(cursor.absolute() / "chats"))

    def test_rediscover_detects_cursor_ide_without_agent_cli_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_db = (
                home
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
                / "state.vscdb"
            )
            state_db.parent.mkdir(parents=True)
            state_db.touch()
            paths = initialize_home(
                root / "Actanara",
                legacy_diary_root=root / "Diary",
            )

            with patch(
                "data_foundation.external_tool_catalog.Path.home",
                return_value=home,
            ):
                result = rediscover_external_tools(paths)

        cursor = next(
            item for item in result["discoveries"] if item["tool"] == "cursor"
        )
        self.assertEqual(cursor["path"], str((home / ".cursor").absolute()))
        self.assertIn(
            str(state_db.absolute()),
            cursor["update"]["ideStateDbCandidates"],
        )

    def test_detect_external_tools_does_not_treat_generic_gemini_home_as_antigravity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            gemini = home / ".gemini"
            gemini.mkdir(parents=True)
            (gemini / "settings.json").write_text("{}\n", encoding="utf-8")
            paths = initialize_home(
                root / "Actanara",
                legacy_diary_root=root / "Diary",
            )

            with patch(
                "data_foundation.external_tool_catalog.Path.home",
                return_value=home,
            ):
                result = detect_external_tools(
                    paths,
                    user_home=home,
                    which=lambda _name: None,
                )

        self.assertIn("geminiCli", result["detectedToolKeys"])
        self.assertNotIn("antigravity", result["detectedToolKeys"])
        self.assertTrue(result["toolPresence"]["geminiCli"]["detected"])
        self.assertFalse(result["toolPresence"]["antigravity"]["detected"])

    def test_detect_external_tools_supports_custom_home_and_configured_primary_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "custom-codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            opencode_db = root / "custom-state" / "opencode.sqlite3"
            opencode_db.parent.mkdir()
            opencode_db.touch()

            with patch(
                "data_foundation.external_tool_catalog.Path.home",
                return_value=home,
            ):
                paths = initialize_home(
                    root / "Actanara",
                    legacy_diary_root=root / "Diary",
                )
                write_settings(
                    {
                        "externalTools": {
                            "codex": {"home": str(codex_home)},
                            "opencode": {
                                "home": str(root / "custom-opencode"),
                                "databasePath": str(opencode_db),
                            },
                        }
                    },
                    paths,
                )
                result = detect_external_tools(
                    paths,
                    user_home=home,
                    which=lambda _name: None,
                )

        self.assertIn("codex", result["detectedToolKeys"])
        self.assertEqual(
            result["toolPresence"]["codex"]["configuredHome"],
            str(codex_home.absolute()),
        )
        self.assertIn("home-marker", result["toolPresence"]["codex"]["detectedBy"])
        self.assertIn("opencode", result["detectedToolKeys"])
        opencode_evidence = result["toolPresence"]["opencode"]["evidence"]
        self.assertTrue(
            any(
                item["kind"] == "configured-path"
                and item.get("field") == "databasePath"
                and item["path"] == str(opencode_db.absolute())
                for item in opencode_evidence
            )
        )

    def test_detect_external_tools_uses_cursor_ide_binary_candidates_and_injected_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_db = (
                home
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
                / "state.vscdb"
            )
            state_db.parent.mkdir(parents=True)
            state_db.touch()
            hermes_binary = root / "bin" / "hermes-custom"
            hermes_binary.parent.mkdir()
            hermes_binary.touch()

            with patch(
                "data_foundation.external_tool_catalog.Path.home",
                return_value=home,
            ):
                paths = initialize_home(
                    root / "Actanara",
                    legacy_diary_root=root / "Diary",
                )
                write_settings(
                    {
                        "externalTools": {
                            "hermes": {
                                "binaryCandidates": [str(hermes_binary)],
                            }
                        }
                    },
                    paths,
                )
                result = detect_external_tools(
                    paths,
                    user_home=home,
                    which=lambda name: (
                        str(root / "path-bin" / "codex")
                        if name == "codex"
                        else None
                    ),
                )

        self.assertIn("cursor", result["detectedToolKeys"])
        self.assertIn(
            "detection-candidate",
            result["toolPresence"]["cursor"]["detectedBy"],
        )
        self.assertIn("hermes", result["detectedToolKeys"])
        self.assertIn(
            "binary-candidate",
            result["toolPresence"]["hermes"]["detectedBy"],
        )
        self.assertIn("codex", result["detectedToolKeys"])
        self.assertIn(
            "path-binary",
            result["toolPresence"]["codex"]["detectedBy"],
        )

    def test_detected_external_tool_ids_are_stable_and_payload_aliases_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            opencode = home / ".local" / "share" / "opencode"
            (opencode / "storage" / "session").mkdir(parents=True)

            with patch(
                "data_foundation.external_tool_catalog.Path.home",
                return_value=home,
            ):
                paths = initialize_home(
                    root / "Actanara",
                    legacy_diary_root=root / "Diary",
                )
                result = detect_external_tools(
                    paths,
                    user_home=home,
                    which=lambda _name: None,
                )
                ids = detected_external_tool_ids(
                    paths,
                    user_home=home,
                    which=lambda _name: None,
                )

        self.assertEqual(result["detectedToolKeys"], ["opencode"])
        self.assertEqual(
            result["detectedToolIds"],
            result["detectedToolKeys"],
        )
        self.assertIs(result["tools"], result["toolPresence"])
        self.assertEqual(ids, tuple(result["detectedToolKeys"]))
        catalog_order = [
            item["id"]
            for item in supported_external_tool_catalog()["tools"]
            if item["id"] in result["detectedToolKeys"]
        ]
        self.assertEqual(result["detectedToolKeys"], catalog_order)

    def test_add_external_tool_instance_persists_derived_paths_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            tool_home = root / "codex-alt"
            tool_home.mkdir()

            result = add_external_tool_instance("codex", str(tool_home), paths, instance_id="codex-alt")
            settings = read_settings(paths)

        self.assertEqual(result["added"], "codex-alt")
        self.assertEqual(settings["externalTools"]["codex-alt"]["home"], str(tool_home.absolute()))
        self.assertEqual(settings["externalTools"]["codex-alt"]["sessionsRoot"], str(tool_home.absolute() / "sessions"))
        self.assertEqual(settings["externalTools"]["codex-alt"]["configPath"], str(tool_home.absolute() / "config.toml"))

    def test_add_external_tool_instance_persists_all_catalog_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            tool_home = root / "hermes-alt"
            tool_home.mkdir()

            result = add_external_tool_instance("hermes", str(tool_home), paths, instance_id="hermes-alt")
            settings = read_settings(paths)

        self.assertEqual(result["added"], "hermes-alt")
        fields = settings["externalTools"]["hermes-alt"]
        self.assertEqual(fields["optionalSkillsRoot"], str(tool_home.absolute() / "hermes-agent" / "optional-skills"))
        self.assertEqual(fields["pluginsRoot"], str(tool_home.absolute() / "hermes-agent" / "plugins"))
        self.assertEqual(fields["configPath"], str(tool_home.absolute() / "config.yaml"))


if __name__ == "__main__":
    unittest.main()
