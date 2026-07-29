import asyncio
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))
os.environ["ACTANARA_SECRET_BACKEND"] = "memory"

from data_foundation import settings as foundation_settings
from data_foundation import settings_transaction as foundation_settings_transaction
from data_foundation.secret_store import SecretRef, read_secret
from data_foundation.paths import initialize_home
from data_foundation.paths import runtime_paths_for_home
from data_foundation.time import business_date_for, business_window, detect_system_timezone, resolve_timezone_name
from data_foundation.llm_provider_catalog import auto_pipeline_gate_tokens, llm_provider_catalog, llm_provider_operations_status
from data_foundation.adapters.usage import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    CronAdapter,
    CursorRuntimeAdapter,
    GeminiCliAdapter,
    HermesAdapter,
    OpenCodeAdapter,
    OpenClawAdapter,
    default_usage_adapters,
)
from data_foundation.settings_status import (
    dump_actanara_settings_status_json,
    format_actanara_settings_status,
    actanara_settings_status,
)
from data_foundation.onboarding_status import (
    dump_actanara_onboarding_status_json,
    format_actanara_onboarding_status,
    actanara_onboarding_status,
)
from data_foundation.onboarding_plan import (
    dump_onboarding_one_liner_dry_run_json,
    dump_onboarding_subsystem_plan_json,
    format_onboarding_one_liner_dry_run,
    format_onboarding_subsystem_plan,
    onboarding_approval_packet,
    onboarding_apply_runtime_bootstrap,
    onboarding_apply_scheduler_register,
    onboarding_apply_scheduler_plist_write,
    onboarding_apply_scheduler_sandbox,
    onboarding_apply_scheduler_unregister,
    onboarding_apply_sandbox,
    onboarding_one_liner_apply,
    onboarding_one_liner_dry_run,
    onboarding_one_liner_release_gate,
    onboarding_one_liner_status,
    onboarding_one_liner_validation_matrix,
    onboarding_release_gate,
    onboarding_rollback_plan_status,
    onboarding_subsystem_plan,
    installer_v2_contract,
    onboarding_apply_write_contract,
    onboarding_apply_preflight,
    scheduler_apply_approval_contract,
    rag_cloud_config_surface,
    rag_readiness_plan,
)
from data_foundation import scheduler_preview as foundation_scheduler_preview
from data_foundation.dependency_profiles import dependency_profiles_status
from data_foundation.settings_audit import settings_hardcode_audit
from data_foundation.settings import (
    MASKED_SECRET,
    build_agent_schedule_prompt,
    default_external_tool_settings,
    external_tool_access_summary,
    read_scheduler_state,
    read_settings,
    resolve_feature_flags,
    read_llm_provider,
    resolve_dashboard_settings,
    resolve_external_tool_paths,
    resolve_general_settings,
    is_nova_task_enabled,
    llm_provider_readiness_error,
    resolve_llm_provider,
    resolve_pipeline_settings,
    resolve_runtime_source,
    runtime_authority_contract,
    runtime_environment_overrides,
    settings_authority_inventory,
    validate_operator_settings_update,
    write_operator_settings_bundle,
    write_llm_provider,
    write_operator_settings,
    write_runtime_sources,
    write_settings,
)
from data_foundation.pipeline_language import resolve_pipeline_language_profile
from app.services import rag_index_jobs, scheduler, settings as dashboard_settings
from app.services import tz as dashboard_tz

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


async def _dashboard_save_llm_provider_for_test(settings_router, payload: dict):
    with patch.object(dashboard_settings, "llm_provider_readiness_error", return_value=None):
        return await settings_router.api_update_llm_provider(payload)


@contextmanager
def _persistent_secret_store_for_test():
    """Model a cross-process-readable store without touching the user Keychain."""
    values: dict[tuple[str, str], str] = {}

    def fake_store(ref, value: str):
        payload = ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)
        payload["backend"] = "test-persistent"
        values[(str(payload.get("service") or ""), str(payload.get("account") or ""))] = value
        return payload

    def fake_read(ref) -> str:
        payload = ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)
        return values.get((str(payload.get("service") or ""), str(payload.get("account") or "")), "")

    def fake_delete(ref) -> bool:
        payload = ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)
        return values.pop((str(payload.get("service") or ""), str(payload.get("account") or "")), None) is not None

    original_transaction_ref = foundation_settings.settings_transaction_secret_ref

    def fake_transaction_ref(runtime_home: str, transaction_id: str, *, provider_id: str):
        ref = original_transaction_ref(runtime_home, transaction_id, provider_id=provider_id)
        return SecretRef(backend="test-persistent", service=ref.service, account=ref.account)

    with (
        patch.object(foundation_settings, "store_secret", side_effect=fake_store),
        patch.object(foundation_settings, "read_secret", side_effect=fake_read),
        patch.object(foundation_settings, "settings_transaction_secret_ref", side_effect=fake_transaction_ref),
        patch.object(foundation_settings_transaction, "store_secret", side_effect=fake_store),
        patch.object(foundation_settings_transaction, "delete_secret", side_effect=fake_delete),
        patch.object(dashboard_settings, "default_secret_backend", return_value="test-persistent"),
        patch.object(dashboard_settings, "read_secret", side_effect=fake_read),
    ):
        yield


def _v2_runtime_source_manifest(source_locator: dict[str, object]) -> dict[str, object]:
    digest = "0" * 64
    return {
        "schemaVersion": 2,
        "product": "actanara",
        "sourceLocator": source_locator,
        "deployedSourceLocator": {
            "kind": "runtime-relative",
            "pathComponents": ["app", "source"],
        },
        "releaseLocator": {
            "kind": "runtime-relative",
            "pathComponents": ["app", "releases", "candidate"],
        },
        "deploymentMode": "release-symlink",
        "copiedAt": "2026-07-11T00:00:00-07:00",
        "pyprojectVersion": "1.0.1",
        "git": {
            "available": True,
            "commit": "abc1234",
            "branch": "main",
            "remote": None,
            "dirty": False,
        },
        "databaseCompatibility": {
            "schemaVersion": 1,
            "policy": "rollback-compatible-additive-only",
            "preCommitWriterContract": "prior-reader-compatible-v1",
            "minimumReadableSchema": "unversioned",
            "maximumReadableSchema": "0001_base",
            "migrationSetSha256": digest,
            "migrations": [
                {
                    "version": "0001_base",
                    "sha256": digest,
                    "rollbackClass": "rollback-compatible-additive",
                }
            ],
        },
        "payload": {
            "fileCount": 1,
            "files": [{"path": "pyproject.toml", "sha256": digest, "size": 1}],
            "sha256": digest,
        },
        "cleanScan": {
            "status": "passed",
            "scanner": "data_foundation.release_clean.repository_clean_deployment_check",
            "scannedFiles": 1,
            "findingCount": 0,
        },
    }


class RuntimeSettingsTests(unittest.TestCase):
    def test_settings_transaction_lock_is_reentrant_in_one_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(
                Path(tmp) / "Actanara",
                legacy_diary_root=Path(tmp) / "Diary",
            )
            with foundation_settings_transaction._transaction_lock(paths):
                with foundation_settings_transaction._transaction_lock(paths):
                    pass

    def test_linux_initialized_runtime_read_persists_defaults_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(
                Path(tmp) / "Actanara",
                legacy_diary_root=Path(tmp) / "Diary",
            )
            with patch(
                "data_foundation.settings.platform.system",
                return_value="Linux",
            ):
                payload = read_settings(paths)

            self.assertEqual(payload["schemaVersion"], 1)
            self.assertTrue((paths.config_dir / "settings.json").is_file())

    def setUp(self):
        # This legacy integration module defines the macOS/launchd contract.
        # Linux-specific behavior is covered by test_systemd_user and explicit
        # per-test platform overrides below, so keep these tests deterministic
        # when the same release suite runs on a Debian host.
        patchers = (
            patch("data_foundation.platform_support.platform.system", return_value="Darwin"),
            patch("data_foundation.settings.platform.system", return_value="Darwin"),
            patch("data_foundation.scheduler_preview.platform.system", return_value="Darwin"),
            patch("data_foundation.settings_status.platform.system", return_value="Darwin"),
            patch("data_foundation.onboarding_plan.platform.system", return_value="Darwin"),
            patch(
                "data_foundation.settings.detect_system_timezone_authority",
                return_value="UTC",
            ),
            patch(
                "data_foundation.scheduler_preview.detect_system_timezone_authority",
                return_value="UTC",
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_config_does_not_auto_load_workspace_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / ".actanara"
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "LLM_API_KEY",
                    "DIARY_OUTPUT_DIR",
                    "WORKSPACE_DIR",
                    "TMP_WORKSPACE",
                    "ACTANARA_DATA_DB_PATH",
                    "ACTANARA_DATA_EXPORT_DIR",
                    "ACTANARA_LOCATION_FILE",
                }
            }
            env["ACTANARA_HOME"] = str(runtime)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import config; "
                        "print(config.LLM_API_KEY == ''); "
                        "print(str(config.DIARY_OUTPUT_DIR) == r'%s')"
                    )
                    % str(runtime / "artifacts" / "diary"),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["True", "True"])

    def test_config_reads_active_settings_before_legacy_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "general": {
                        "workspaceRoot": str(root / "settings-workspace"),
                        "timezone": "UTC",
                    },
                    "paths": {
                        "diary": {"generatedDiary": str(root / "settings-diary")},
                        "runtime": {
                            "database": str(root / "settings-data" / "actanara_data.sqlite3"),
                            "snapshots": str(root / "settings-snapshots"),
                        },
                        "tasks": {"legacyTaskDatabase": str(root / "settings-data" / "nova_tasks.db")},
                        "logsCacheTmp": {"tmp": str(root / "settings-tmp")},
                    },
                    "runtimeSources": {"dashboardReadSource": "foundation"},
                    "llmProvider": {
                        "provider": "custom",
                        "endpoint": "https://settings-llm.local",
                        "model": "settings-model",
                        "apiKey": "",
                    },
                },
                paths,
            )
            location = root / "location.json"
            location.write_text(json.dumps({"actanaraHome": str(paths.home)}), encoding="utf-8")
            env = {
                **os.environ,
                "ACTANARA_LOCATION_FILE": str(location),
                "WORKSPACE_DIR": str(root / "env-workspace"),
                "DIARY_OUTPUT_DIR": str(root / "env-diary"),
                "TMP_WORKSPACE": str(root / "env-tmp"),
                "ACTANARA_DATA_DB_PATH": str(root / "env-data" / "actanara_data.sqlite3"),
                "ACTANARA_DATA_EXPORT_DIR": str(root / "env-snapshots"),
                "TASK_DB_PATH": str(root / "env-data" / "nova_tasks.db"),
                "DASHBOARD_READ_SOURCE": "legacy",
                "LLM_HOST": "https://env-llm.local",
                "LLM_MODEL_NAME": "env-model",
                "LLM_API_KEY": "env-secret",
            }
            env.pop("ACTANARA_HOME", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import config; "
                        "print(config.WORKSPACE_DIR); "
                        "print(config.DIARY_OUTPUT_DIR); "
                        "print(config.TMP_WORKSPACE); "
                        "print(config.ACTANARA_DATA_DB_PATH); "
                        "print(config.ACTANARA_DATA_EXPORT_DIR); "
                        "print(config.TASK_DB_PATH); "
                        "print(config.DASHBOARD_READ_SOURCE); "
                        "print(config.LLM_HOST); "
                        "print(config.LLM_MODEL_NAME); "
                        "print(config.LLM_API_KEY == '')"
                    ),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            [
                str(root / "settings-workspace"),
                str(root / "settings-diary"),
                str(root / "settings-tmp"),
                str(root / "settings-data" / "actanara_data.sqlite3"),
                str(root / "settings-snapshots"),
                str(root / "settings-data" / "nova_tasks.db"),
                "foundation",
                "https://settings-llm.local",
                "settings-model",
                "True",
            ],
        )

    def test_generated_diary_setting_does_not_repoint_legacy_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            generated = root / "settings-diary"
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)

            write_operator_settings({"paths": {"diary": {"generatedDiary": str(generated)}}}, paths)
            manifest = json.loads((paths.config_dir / "runtime.json").read_text(encoding="utf-8"))
            settings = read_settings(paths)

        self.assertEqual(manifest["generatedDiaryRoot"], str(generated))
        self.assertEqual(manifest["legacyDiaryRoot"], str(legacy))
        self.assertEqual(settings["paths"]["diary"]["generatedDiary"], str(generated))
        self.assertEqual(settings["paths"]["diary"]["legacyDiaryRoot"], str(legacy))

    def test_first_read_never_persists_env_llm_key_to_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"LLM_API_KEY": "env-secret"}):
                redacted = read_settings(paths)
                resolved = resolve_llm_provider(paths, redact_secrets=False)
            raw = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(raw["llmProvider"]["apiKey"], "")
        self.assertNotIn("secretRef", raw["llmProvider"])
        self.assertFalse(redacted["llmProvider"]["hasApiKey"])
        self.assertEqual(resolved["apiKey"], "env-secret")
        self.assertEqual(resolved["source"]["apiKey"], "env")

    def test_settings_file_is_created_under_selected_actanara_home_and_masks_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"provider": "openai-compatible", "endpoint": "https://llm.local", "model": "m1", "apiKey": "secret"}, paths)
            redacted = read_settings(paths)
            self.assertEqual(redacted["settingsPath"], str(paths.config_dir / "settings.json"))
            self.assertFalse(redacted["llmProvider"]["hasApiKey"])
            self.assertTrue(redacted["llmProvider"]["hasSecretRef"])
            self.assertFalse(redacted["llmProvider"]["secretReadable"])
            self.assertEqual(redacted["llmProvider"]["apiKey"], "")
            raw = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["llmProvider"]["apiKey"], "")
            self.assertEqual(raw["llmProvider"]["secretRef"]["backend"], "memory")
            resolved = resolve_llm_provider(paths, redact_secrets=False)
            self.assertEqual(resolved["apiKey"], "secret")
            self.assertEqual(resolved["source"]["apiKey"], "secret-store")

    def test_write_llm_provider_resets_secret_like_api_key_env_when_secret_store_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            raw_path = paths.config_dir / "settings.json"
            read_settings(paths, redact_secrets=False)
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["llmProvider"]["apiKeyEnv"] = "sk-test-value-that-should-not-remain"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            write_llm_provider(
                {
                    "provider": "openai-compatible",
                    "endpoint": "https://llm.local",
                    "model": "m1",
                    "apiKey": "secret",
                },
                paths,
            )
            persisted = json.loads(raw_path.read_text(encoding="utf-8"))
            resolved = resolve_llm_provider(paths, redact_secrets=False)

        self.assertEqual(persisted["llmProvider"]["apiKeyEnv"], "LLM_API_KEY")
        self.assertEqual(resolved["apiKey"], "secret")
        self.assertEqual(resolved["source"]["apiKey"], "secret-store")

    def test_runtime_environment_exports_configured_llm_api_key_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "provider": "openai-compatible",
                    "endpoint": "https://llm.local",
                    "model": "m1",
                    "apiKey": "secret",
                    "apiKeyEnv": "CUSTOM_LLM_KEY",
                },
                paths,
            )

            overrides = runtime_environment_overrides(paths)

        self.assertEqual(overrides["LLM_API_KEY"], "secret")
        self.assertEqual(overrides["CUSTOM_LLM_KEY"], "secret")

    def test_legacy_plaintext_key_is_scrubbed_when_secret_backend_is_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            read_settings(paths, redact_secrets=False)
            raw_path = paths.config_dir / "settings.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["llmProvider"]["apiKey"] = "legacy-secret"
            raw["llmProvider"].pop("secretRef", None)
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "process-env"}):
                settings = read_settings(paths, redact_secrets=False)
            persisted = json.loads(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(settings["llmProvider"]["apiKey"], "")
        self.assertEqual(persisted["llmProvider"]["apiKey"], "")
        self.assertNotIn("secretRef", persisted["llmProvider"])

    def test_timezone_resolver_preserves_default_business_day_and_allows_env_override(self):
        occurred = datetime.fromisoformat("2026-05-20T19:30:00+00:00")
        self.assertEqual(resolve_timezone_name(), "Asia/Hong_Kong")
        self.assertEqual(business_date_for(occurred).isoformat(), "2026-05-20")
        start, end = business_window(date(2026, 5, 20))
        self.assertEqual(start.isoformat(), "2026-05-19T20:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-05-20T20:00:00+00:00")

        with patch.dict(os.environ, {"TARGET_TIMEZONE": "UTC"}):
            self.assertEqual(resolve_timezone_name(), "UTC")
            self.assertEqual(business_date_for(occurred).isoformat(), "2026-05-20")
            self.assertEqual(dashboard_tz.utc_ts_to_hkt("2026-05-20T03:30:00Z"), (date(2026, 5, 19), 3))

    def test_dashboard_timestamp_conversion_reuses_supplied_timezone(self):
        with patch("app.services.tz.resolve_timezone", side_effect=AssertionError("should not resolve per event")):
            self.assertEqual(dashboard_tz.utc_ts_to_hkt("2026-05-20T03:30:00Z", tz=ZoneInfo("UTC")), (date(2026, 5, 19), 3))

    def test_default_settings_uses_detected_system_timezone_for_new_runtime(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TZ": "UTC"}, clear=False),
            patch("data_foundation.settings.detect_system_timezone_authority", return_value="UTC"),
        ):
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            settings = read_settings(paths)
            self.assertEqual(detect_system_timezone(), "UTC")
            self.assertEqual(settings["general"]["timezone"], "UTC")
            self.assertEqual(settings["schedule"]["timezone"], "UTC")

    def test_masked_or_empty_llm_key_update_preserves_existing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": "secret",
                },
                paths,
            )
            write_llm_provider({"provider": "minimax-cn", "model": "MiniMax-M2.7-highspeed", "apiKey": MASKED_SECRET}, paths)
            raw = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["llmProvider"]["apiKey"], "")
            self.assertEqual(raw["llmProvider"]["secretRef"]["backend"], "memory")
            self.assertEqual(resolve_llm_provider(paths, redact_secrets=False)["apiKey"], "secret")

    def test_llm_provider_readiness_requires_pipeline_readable_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": "secret",
                },
                paths,
            )

            self.assertIsNone(llm_provider_readiness_error(paths))
            self.assertIn(
                "process-local memory backend",
                llm_provider_readiness_error(paths, require_cross_process_secret=True),
            )

            raw_path = paths.config_dir / "settings.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["llmProvider"]["secretRef"]["account"] = "missing-account"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            self.assertIn("apiKey is not readable", llm_provider_readiness_error(paths))

    def test_llm_provider_readiness_redacts_invalid_api_key_env_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            secret_like_env = "sk-test-value-that-should-not-be-printed"
            read_settings(paths, redact_secrets=False)
            raw_path = paths.config_dir / "settings.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["llmProvider"].update(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "endpoint": "https://api.minimaxi.com",
                    "model": "MiniMax-M3",
                    "api": "anthropic-messages",
                    "apiKey": "",
                    "apiKeyEnv": secret_like_env,
                }
            )
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            with patch.dict(os.environ, {"LLM_API_KEY": ""}):
                message = llm_provider_readiness_error(paths, require_cross_process_secret=True)

        self.assertIn("missing apiKey", message)
        self.assertIn("configured apiKeyEnv", message)
        self.assertNotIn(secret_like_env, message)

    def test_llm_provider_readiness_is_a_read_only_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(
                Path(tmp) / "Actanara",
                legacy_diary_root=Path(tmp) / "Diary",
            )
            read_settings(paths, redact_secrets=False)

            with patch.object(
                foundation_settings,
                "migrate_persisted_secret_refs",
                side_effect=AssertionError("readiness must not migrate Settings"),
            ):
                message = llm_provider_readiness_error(paths)

        self.assertIn("missing apiKey", message)

    def test_nova_task_feature_flag_defaults_on_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            self.assertTrue(resolve_feature_flags(paths)["novaTask"])
            self.assertTrue(is_nova_task_enabled(paths))

            write_settings({"features": {"novaTask": False}}, paths)

            self.assertFalse(resolve_feature_flags(paths)["novaTask"])
            self.assertFalse(is_nova_task_enabled(paths))

    def test_llm_provider_preset_uses_catalog_metadata_and_redacts_secret(self):
        with _persistent_secret_store_for_test(), tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "glm",
                    "model": "glm-5.1",
                    "endpoint": "https://operator-ignored.example",
                    "contextWindow": 1,
                    "pipelineConcurrency": "4",
                    "pipelineGateTokens": "25000",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["provider"], "glm")
            self.assertEqual(saved["api"], "anthropic-messages")
            self.assertEqual(saved["endpoint"], "https://open.bigmodel.cn/api/anthropic")
            self.assertEqual(saved["model"], "glm-5.1")
            self.assertEqual(saved["contextWindow"], 200000)
            self.assertEqual(saved["pipelineConcurrency"], 4)
            self.assertEqual(saved["pipelineGateTokens"], 25000)
            self.assertEqual(saved["pipelineGateMode"], "manual")
            self.assertEqual(saved["autoPipelineGateTokens"], 30000)
            self.assertEqual(saved["apiKey"], MASKED_SECRET)
            self.assertIn("catalog", read_llm_provider(paths))
            resolved = resolve_llm_provider(paths, redact_secrets=True)
            self.assertEqual(resolved["api"], "anthropic-messages")
            self.assertEqual(resolved["pipelineConcurrency"], 4)
            self.assertEqual(resolved["pipelineGateTokens"], 25000)
            self.assertEqual(resolved["pipelineGateMode"], "manual")
            self.assertEqual(resolved["autoPipelineGateTokens"], 30000)
            self.assertTrue(resolved["pipelineGateDrift"])
            self.assertEqual(resolved["apiKey"], MASKED_SECRET)

    def test_llm_provider_preset_requires_explicit_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with self.assertRaisesRegex(ValueError, "endpoint is required"):
                write_llm_provider({"mode": "preset", "apiKey": MASKED_SECRET}, paths)

    def test_llm_provider_preset_defaults_gate_from_context_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["contextWindow"], 524288)
            self.assertEqual(saved["pipelineGateTokens"], 78643)
            self.assertEqual(saved["pipelineGateMode"], "auto")
            self.assertEqual(saved["timeoutSeconds"], 300)

            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "timeoutSeconds": 480,
                    "apiKey": MASKED_SECRET,
                },
                paths,
            )
            self.assertEqual(saved["timeoutSeconds"], 480)
            self.assertEqual(resolve_llm_provider(paths, redact_secrets=True)["timeoutSeconds"], 480)
            self.assertEqual(saved["autoPipelineGateTokens"], 78643)

            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": MASKED_SECRET,
                },
                paths,
            )
            self.assertEqual(saved["contextWindow"], 204800)
            self.assertEqual(saved["pipelineGateTokens"], 30720)
            self.assertEqual(saved["pipelineGateMode"], "auto")
            self.assertEqual(saved["autoPipelineGateTokens"], 30720)

    def test_llm_provider_manual_gate_overrides_auto_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "pipelineGateTokens": "42000",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["pipelineGateTokens"], 42000)
            self.assertEqual(saved["pipelineGateMode"], "manual")
            self.assertEqual(saved["autoPipelineGateTokens"], 78643)

            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": MASKED_SECRET,
                },
                paths,
            )
            self.assertEqual(saved["pipelineGateTokens"], 42000)
            self.assertEqual(saved["pipelineGateMode"], "manual")
            self.assertEqual(saved["autoPipelineGateTokens"], 30720)

            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "pipelineGateMode": "auto",
                    "apiKey": MASKED_SECRET,
                },
                paths,
            )
            self.assertEqual(saved["pipelineGateTokens"], 78643)
            self.assertEqual(saved["pipelineGateMode"], "auto")

    def test_legacy_pipeline_gate_without_mode_is_preserved_as_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            settings_path = paths.config_dir / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "updatedAt": "2026-06-11T00:00:00+08:00",
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "presetProvider": "minimax-cn",
                            "endpoint": "https://api.minimaxi.com",
                            "model": "MiniMax-M3",
                            "api": "anthropic-messages",
                            "contextWindow": 524288,
                            "maxTokens": 128000,
                            "pipelineConcurrency": 3,
                            "pipelineGateTokens": 30000,
                            "apiKey": "secret",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            redacted = read_settings(paths)
            resolved = resolve_llm_provider(paths, redact_secrets=True)

        self.assertEqual(redacted["llmProvider"]["pipelineGateMode"], "manual")
        self.assertEqual(redacted["llmProvider"]["pipelineGateTokens"], 30000)
        self.assertEqual(redacted["llmProvider"]["autoPipelineGateTokens"], 78643)
        self.assertEqual(resolved["pipelineGateMode"], "manual")
        self.assertEqual(resolved["pipelineGateTokens"], 30000)
        self.assertEqual(resolved["autoPipelineGateTokens"], 78643)
        self.assertTrue(resolved["pipelineGateDrift"])

    def test_llm_provider_openclaw_static_preset_uses_catalog_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "qwen",
                    "model": "qwen3.5-plus",
                    "endpoint": "https://operator-ignored.example",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["provider"], "qwen")
            self.assertEqual(saved["endpoint"], "https://coding-intl.dashscope.aliyuncs.com/v1")
            self.assertEqual(saved["model"], "qwen3.5-plus")
            self.assertEqual(saved["contextWindow"], 1000000)
            self.assertEqual(saved["maxTokens"], 65536)
            self.assertEqual(saved["api"], "openai-compatible")

    def test_auto_pipeline_gate_caps_context_window(self):
        self.assertEqual(auto_pipeline_gate_tokens(204800), 30720)
        self.assertEqual(auto_pipeline_gate_tokens(524288), 78643)
        self.assertEqual(auto_pipeline_gate_tokens(1000000), 80000)
        self.assertEqual(auto_pipeline_gate_tokens(None), 30000)

    def test_llm_provider_kimi_code_uses_anthropic_compatible_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "kimi-code",
                    "model": "kimi-for-coding",
                    "endpoint": "https://operator-ignored.example",
                    "api": "openai-compatible",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["provider"], "kimi-code")
            self.assertEqual(saved["endpoint"], "https://api.kimi.com/coding/")
            self.assertEqual(saved["model"], "kimi-for-coding")
            self.assertEqual(saved["api"], "anthropic-messages")

    def test_official_openai_and_anthropic_presets_are_api_key_supported(self):
        catalog = llm_provider_catalog()
        by_id = {item["id"]: item for item in catalog}
        self.assertTrue(by_id["openai"]["enabled"])
        self.assertEqual(by_id["openai"]["api"], "openai-compatible")
        self.assertEqual(by_id["openai"]["endpoint"], "https://api.openai.com/v1")
        self.assertTrue(by_id["anthropic"]["enabled"])
        self.assertEqual(by_id["anthropic"]["api"], "anthropic-messages")
        self.assertEqual(by_id["anthropic"]["endpoint"], "https://api.anthropic.com/v1/messages")

    def test_latest_official_model_ids_are_available(self):
        catalog = llm_provider_catalog()
        by_id = {item["id"]: item for item in catalog}
        model_ids = {
            provider_id: {model["id"] for model in by_id[provider_id]["models"]}
            for provider_id in ("glm", "openai", "anthropic", "moonshot")
        }
        self.assertIn("glm-5.2", model_ids["glm"])
        self.assertTrue({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= model_ids["openai"])
        self.assertTrue({"claude-sonnet-5", "claude-fable-5"} <= model_ids["anthropic"])
        self.assertTrue({"kimi-k2.7-code", "kimi-k2.7-code-highspeed"} <= model_ids["moonshot"])
        self.assertNotIn("kimi-latest", model_ids["moonshot"])

        by_model = {
            provider_id: {model["id"]: model for model in by_id[provider_id]["models"]}
            for provider_id in model_ids
        }
        self.assertEqual(by_model["glm"]["glm-5.2"]["contextWindow"], 1000000)
        self.assertEqual(by_model["openai"]["gpt-5.6-sol"]["contextWindow"], 1050000)
        self.assertEqual(by_model["anthropic"]["claude-sonnet-5"]["maxTokens"], 128000)
        self.assertEqual(by_model["moonshot"]["kimi-k2.7-code"]["contextWindow"], 262144)

    def test_llm_provider_catalog_covers_openclaw_onboard_entries(self):
        catalog = llm_provider_catalog()
        ids = {item["id"] for item in catalog}
        for provider_id in {
            "openai",
            "anthropic",
            "xai",
            "google",
            "arcee",
            "brave",
            "byteplus",
            "cerebras",
            "chutes",
            "cloudflare-ai-gateway",
            "codex",
            "copilot",
            "custom",
            "deepinfra",
            "deepseek",
            "fireworks",
            "gmi",
            "google-vertex",
            "groq",
            "huggingface",
            "kilocode",
            "litellm",
            "lmstudio",
            "microsoft-foundry",
            "minimax",
            "mistral",
            "moonshot",
            "novita",
            "nvidia",
            "ollama",
            "opencode",
            "openrouter",
            "qianfan",
            "qwen",
            "sglang",
            "stepfun",
            "synthetic",
            "tencent",
            "together",
            "venice",
            "vercel-ai-gateway",
            "vllm",
            "volcengine",
            "xiaomi",
            "zai",
        }:
            self.assertIn(provider_id, ids)
        self.assertGreaterEqual(len(catalog), 45)
        self.assertTrue(next(item for item in catalog if item["id"] == "deepseek")["enabled"])
        self.assertFalse(next(item for item in catalog if item["id"] == "google")["enabled"])

    def test_llm_provider_operations_status_records_catalog_and_helper_policy(self):
        status = llm_provider_operations_status()
        self.assertGreaterEqual(status["catalogCount"], 45)
        self.assertEqual(status["onboardCoverage"]["missing"], [])
        self.assertFalse(status["secretPolicy"]["catalogContainsSecrets"])
        self.assertTrue(status["customProviderEnabled"])
        self.assertIn("deepseek", status["enabledPresetIds"])
        helper_by_path = {item["path"]: item for item in status["helperScripts"]}
        self.assertEqual(helper_by_path["src/diary_generator/diary_summary.py"]["decision"], "retain")
        self.assertEqual(helper_by_path["src/diary_generator/diary_summary.py"]["classification"], "migration-only")
        self.assertEqual(helper_by_path["src/diary_generator/diary_summary_editor.py"]["decision"], "defer-removal")
        self.assertTrue(helper_by_path["src/diary_generator/diary_summary_editor.py"]["requiresExplicitCleanupApproval"])

    def test_llm_provider_unsupported_preset_is_not_saved_as_enabled_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with self.assertRaises(ValueError):
                write_llm_provider(
                    {
                        "mode": "preset",
                        "provider": "google",
                        "model": "gemini-2.5-pro",
                        "apiKey": "secret",
                    },
                    paths,
                )

    def test_llm_provider_custom_preserves_operator_endpoint_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_llm_provider(
                {
                    "mode": "custom",
                    "provider": "custom",
                    "endpoint": "https://llm.local/v1",
                    "model": "custom-model",
                    "api": "openai-compatible",
                    "contextWindow": "123456",
                    "maxTokens": "4096",
                    "apiKey": "secret",
                },
                paths,
            )
            self.assertEqual(saved["provider"], "custom")
            self.assertEqual(saved["endpoint"], "https://llm.local/v1")
            self.assertEqual(saved["model"], "custom-model")
            self.assertEqual(saved["api"], "openai-compatible")
            self.assertEqual(saved["contextWindow"], 123456)
            self.assertEqual(saved["maxTokens"], 4096)

    def test_llm_provider_explicit_catalog_provider_switches_custom_to_preset(self):
        for provider_field in ("provider", "presetProvider"):
            with self.subTest(provider_field=provider_field), tempfile.TemporaryDirectory() as tmp:
                paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
                custom = write_llm_provider(
                    {
                        "mode": "custom",
                        "provider": "custom",
                        "endpoint": "http://127.0.0.1:63185/v1",
                        "model": "closeout-smoke",
                        "api": "openai-compatible",
                        "apiKeyEnv": "ACTANARA_LOCAL_SMOKE_KEY",
                    },
                    paths,
                )
                custom_model_update = write_llm_provider(
                    {"model": "closeout-smoke-v2"},
                    paths,
                )

                saved = write_llm_provider(
                    {
                        provider_field: "minimax-cn",
                        "model": "MiniMax-M2.5",
                    },
                    paths,
                )

            self.assertEqual(custom["mode"], "custom")
            self.assertEqual(custom_model_update["mode"], "custom")
            self.assertEqual(custom_model_update["provider"], "custom")
            self.assertEqual(custom_model_update["model"], "closeout-smoke-v2")
            self.assertEqual(custom_model_update["endpoint"], "http://127.0.0.1:63185/v1")
            self.assertEqual(saved["mode"], "preset")
            self.assertEqual(saved["provider"], "minimax-cn")
            self.assertEqual(saved["presetProvider"], "minimax-cn")
            self.assertEqual(saved["model"], "MiniMax-M2.5")
            self.assertEqual(saved["endpoint"], "https://api.minimaxi.com")
            self.assertEqual(saved["api"], "anthropic-messages")

    def test_runtime_source_uses_settings_before_legacy_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"runtimeSources": {"dashboardReadSource": "foundation"}}, paths)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DASHBOARD_READ_SOURCE", None)
                self.assertEqual(resolve_runtime_source("DASHBOARD_READ_SOURCE", paths), "foundation")
            with patch.dict(os.environ, {"DASHBOARD_READ_SOURCE": "legacy"}):
                self.assertEqual(resolve_runtime_source("DASHBOARD_READ_SOURCE", paths), "foundation")

    def test_write_runtime_sources_is_the_dedicated_source_switch_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            saved = write_runtime_sources(
                {
                    "dashboardReadSource": "foundation",
                    "reportReadSource": "legacy",
                },
                paths,
            )
            with patch.dict(os.environ, {"REPORT_READ_SOURCE": "foundation"}):
                effective = resolve_runtime_source("REPORT_READ_SOURCE", paths)
            raw = read_settings(paths, redact_secrets=False)

        self.assertEqual(saved["dashboardReadSource"], "foundation")
        self.assertEqual(saved["reportReadSource"], "legacy")
        self.assertEqual(effective, "legacy")
        self.assertEqual(raw["runtimeSources"]["reportReadSource"], "legacy")
        with self.assertRaisesRegex(ValueError, "unknown runtime source fields"):
            write_runtime_sources({"unknownSource": "foundation"}, paths)
        with self.assertRaisesRegex(ValueError, "must be one of"):
            write_runtime_sources({"dashboardReadSource": "bad"}, paths)

    def test_default_runtime_sources_are_foundation_and_do_not_persist_env_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(
                os.environ,
                {
                    "DASHBOARD_READ_SOURCE": "foundation",
                    "REPORT_READ_SOURCE": "foundation",
                    "DIARY_METRICS_SOURCE": "foundation",
                },
            ):
                settings = read_settings(paths, redact_secrets=False)
            self.assertEqual(settings["runtimeSources"]["dashboardReadSource"], "foundation")
            self.assertEqual(settings["runtimeSources"]["reportReadSource"], "foundation")
            self.assertEqual(settings["runtimeSources"]["diaryMetricsSource"], "foundation")

    def test_runtime_source_settings_are_used_without_foundation_enablement_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"runtimeSources": {"diaryMetricsSource": "foundation"}}, paths)
            with patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(root / "location.json")}, clear=False):
                os.environ.pop("ACTANARA_HOME", None)
                os.environ.pop("ACTANARA_DATA_FOUNDATION_ENABLED", None)
                os.environ.pop("DIARY_METRICS_SOURCE", None)
                self.assertEqual(resolve_runtime_source("DIARY_METRICS_SOURCE"), "foundation")
            with patch.dict(os.environ, {"ACTANARA_DATA_FOUNDATION_ENABLED": "true"}):
                self.assertEqual(resolve_runtime_source("DIARY_METRICS_SOURCE", paths), "foundation")

    def test_runtime_environment_overrides_exports_settings_when_env_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "runtimeSources": {"diaryMetricsSource": "foundation"},
                    "llmProvider": {"endpoint": "https://llm.local", "model": "m1", "api": "openai-compatible", "apiKey": "secret"},
                },
                paths,
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DIARY_METRICS_SOURCE", None)
                os.environ.pop("LLM_HOST", None)
                os.environ.pop("LLM_MODEL_NAME", None)
                os.environ.pop("LLM_API", None)
                os.environ.pop("LLM_API_KEY", None)
                overrides = runtime_environment_overrides(paths)
            self.assertEqual(overrides["DIARY_METRICS_SOURCE"], "foundation")
            self.assertEqual(overrides["ACTANARA_HOME"], str(paths.home))
            self.assertEqual(overrides["LLM_HOST"], "https://llm.local")
            self.assertEqual(overrides["LLM_MODEL_NAME"], "m1")
            self.assertEqual(overrides["LLM_API"], "openai-compatible")
            self.assertEqual(overrides["LLM_API_KEY"], "secret")
            self.assertEqual(overrides["LLM_PIPELINE_CONCURRENCY"], "3")
            self.assertEqual(overrides["LLM_PIPELINE_GATE_TOKENS"], "30000")

    def test_llm_provider_resolution_redacts_and_prefers_settings_over_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "custom", "endpoint": "https://settings.local", "model": "settings-model", "apiKey": "settings-secret"}, paths)
            with patch.dict(os.environ, {"LLM_HOST": "https://env.local", "LLM_MODEL_NAME": "env-model"}):
                provider = resolve_llm_provider(paths, redact_secrets=True)
            self.assertEqual(provider["endpoint"], "https://settings.local")
            self.assertEqual(provider["model"], "settings-model")
            self.assertEqual(provider["apiKey"], MASKED_SECRET)
            self.assertTrue(provider["hasApiKey"])
            self.assertEqual(provider["source"]["endpoint"], "settings")
            contract = runtime_authority_contract(paths)
            self.assertEqual(contract["precedence"][0], "settings.json")
            self.assertIn("settingsAuthority", contract)

    def test_llm_provider_does_not_fallback_to_default_model_when_settings_are_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "llmProvider": {
                        "mode": "custom",
                        "provider": "custom",
                        "presetProvider": "",
                        "endpoint": "",
                        "model": "",
                        "api": "openai-compatible",
                        "apiKey": "secret",
                    }
                },
                paths,
            )
            provider = resolve_llm_provider(paths, redact_secrets=True)

        self.assertEqual(provider["endpoint"], "")
        self.assertEqual(provider["model"], "")
        self.assertEqual(provider["apiKey"], MASKED_SECRET)
        self.assertEqual(provider["source"]["endpoint"], "unset")
        self.assertEqual(provider["source"]["model"], "unset")

    def test_settings_authority_inventory_reports_sources_modes_and_redaction(self):
        with _persistent_secret_store_for_test(), tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"runtimeSources": {"dashboardReadSource": "foundation"}}, paths)
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "pipelineGateTokens": "42000",
                    "apiKey": "secret",
                },
                paths,
            )
            with patch.dict(os.environ, {"DASHBOARD_READ_SOURCE": "legacy"}):
                inventory = settings_authority_inventory(paths)

        groups = {group["group"]: group for group in inventory["groups"]}
        self.assertIn("runtimePaths", groups)
        self.assertIn("runtimeSources", groups)
        self.assertIn("llmProvider", groups)
        self.assertIn("rag", groups)
        source_fields = {field["path"]: field for field in groups["runtimeSources"]["fields"]}
        self.assertEqual(source_fields["runtimeSources.dashboardReadSource"]["source"], "settings")
        self.assertTrue(source_fields["runtimeSources.dashboardReadSource"]["envOverride"])
        self.assertEqual(source_fields["runtimeSources.dashboardReadSource"]["settingsValue"], "foundation")
        self.assertEqual(source_fields["runtimeSources.dashboardReadSource"]["effectiveValue"], "foundation")
        llm_fields = {field["path"]: field for field in groups["llmProvider"]["fields"]}
        self.assertEqual(llm_fields["llmProvider.apiKey"]["settingsValue"], MASKED_SECRET)
        self.assertEqual(llm_fields["llmProvider.apiKey"]["effectiveValue"], MASKED_SECRET)
        self.assertEqual(llm_fields["llmProvider.pipelineGateTokens"]["mode"], "manual")
        self.assertEqual(llm_fields["llmProvider.pipelineGateTokens"]["autoValue"], 78643)
        self.assertTrue(llm_fields["llmProvider.pipelineGateTokens"]["drift"])
        self.assertEqual(llm_fields["llmProvider.pipelineGateTokens"]["effectiveValue"], 42000)

    def test_settings_authority_inventory_treats_rag_env_as_diagnostic_not_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"NOVA_RAG_MODE": "legacy"}):
                inventory = settings_authority_inventory(paths)

        groups = {group["group"]: group for group in inventory["groups"]}
        rag_fields = {field["path"]: field for field in groups["rag"]["fields"]}
        self.assertEqual(rag_fields["rag.mode"]["diagnosticEnv"], "NOVA_RAG_MODE")
        self.assertFalse(rag_fields["rag.mode"]["envOverride"])
        self.assertEqual(rag_fields["rag.mode"]["source"], "settings")
        self.assertEqual(rag_fields["rag.mode"]["effectiveValue"], "v2")
        feature_fields = {field["path"]: field for field in groups["features"]["fields"]}
        self.assertIn("runtimeSources.taskAuditSink", feature_fields["features.taskAuditSink"]["defaultSource"])

    def test_settings_bundle_is_transactional_across_settings_rag_and_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"schedule": {"dailyPipelineTime": "03:10"}}, paths)

            saved = write_operator_settings_bundle(
                {
                    "settings": {"schedule": {"dailyPipelineTime": "04:20"}},
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "apiKey": "do-not-store",
                        }
                    },
                },
                paths,
            )
            self.assertEqual(saved["schedule"]["dailyPipelineTime"], "04:20")
            self.assertNotIn("apiKey", saved["rag"]["embedding"])
            self.assertTrue(saved["rag"]["embedding"]["secretRef"])

            with self.assertRaises(ValueError):
                write_operator_settings_bundle(
                    {
                        "settings": {"schedule": {"dailyPipelineTime": "05:30"}},
                        "llmProvider": {"provider": "custom-openai", "model": "gpt-test"},
                    },
                    paths,
                )
            self.assertEqual(read_settings(paths)["schedule"]["dailyPipelineTime"], "04:20")

    def test_settings_bundle_rejects_rag_language_profile_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"rag": {"languageProfile": "zh"}}, paths)

            with self.assertRaisesRegex(ValueError, "rag.languageProfile is immutable"):
                write_operator_settings_bundle({"rag": {"languageProfile": "en"}}, paths)

            self.assertEqual(read_settings(paths)["rag"]["languageProfile"], "zh")

    def test_settings_bundle_rejects_invalid_schedule_time_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"schedule": {"dailyPipelineTime": "04:00", "dashboardAggregationTime": "04:30"}}, paths)

            with self.assertRaises(ValueError):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "bad-time"}}},
                    paths,
                )

            self.assertEqual(read_settings(paths)["schedule"]["dailyPipelineTime"], "04:00")

    def test_settings_bundle_rejects_invalid_iana_timezones_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {"general": {"timezone": "UTC"}, "schedule": {"timezone": "UTC"}},
                paths,
            )

            for group in ("general", "schedule"):
                for invalid_timezone in ("Not/A-Timezone", " UTC "):
                    with self.subTest(group=group, timezone=invalid_timezone):
                        with self.assertRaisesRegex(ValueError, rf"{group}\.timezone must be a valid IANA timezone"):
                            write_operator_settings_bundle(
                                {"settings": {group: {"timezone": invalid_timezone}}},
                                paths,
                            )
                        saved = read_settings(paths)
                        self.assertEqual(saved["general"]["timezone"], "UTC")
                        self.assertEqual(saved["schedule"]["timezone"], "UTC")

            with patch("data_foundation.settings.detect_system_timezone_authority", return_value="America/Los_Angeles"):
                saved = write_operator_settings_bundle(
                    {
                        "settings": {
                            "general": {"timezone": "America/Los_Angeles"},
                            "schedule": {"timezone": "America/Los_Angeles"},
                        }
                    },
                    paths,
                )
            self.assertEqual(saved["general"]["timezone"], "America/Los_Angeles")
            self.assertEqual(saved["schedule"]["timezone"], "America/Los_Angeles")

    def test_settings_bundle_syncs_runtime_manifest_path_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            db_path = root / "custom" / "data.sqlite3"
            snapshots = root / "custom" / "snapshots"
            reports = root / "custom" / "reports"
            archives = root / "custom" / "archives"
            task_board = root / "custom" / "task-board.json"
            task_intel = root / "custom" / "task-intelligence"

            write_operator_settings_bundle(
                {
                    "settings": {
                        "paths": {
                            "runtime": {"database": str(db_path), "snapshots": str(snapshots)},
                            "diary": {"reports": str(reports)},
                            "intermediate": {"archives": str(archives), "taskIntelligence": str(task_intel)},
                            "tasks": {"taskBoard": str(task_board)},
                        }
                    }
                },
                paths,
            )
            resolved = runtime_paths_for_home(paths.home)

            self.assertEqual(resolved.db_path, db_path)
            self.assertEqual(resolved.snapshots_dir, snapshots)
            self.assertEqual(resolved.reports_dir, reports)
            self.assertEqual(resolved.archives_dir, archives)
            self.assertEqual(resolved.task_board_path, task_board)
            self.assertEqual(resolved.task_intelligence_dir, task_intel)
            self.assertTrue(db_path.parent.exists())
            self.assertTrue(snapshots.exists())
            self.assertTrue(reports.exists())

    def test_external_tool_settings_default_to_common_user_paths(self):
        home = Path("/Users/example")
        defaults = default_external_tool_settings(home)

        self.assertEqual(defaults["openclaw"]["agentsRoot"], "/Users/example/.openclaw/agents")
        self.assertEqual(defaults["openclaw"]["credentialsPath"], "/Users/example/.openclaw/credentials.json")
        self.assertEqual(defaults["openclaw"]["workspaceRoot"], "/Users/example/.openclaw/workspace")
        self.assertEqual(defaults["openclaw"]["projectsRoot"], "/Users/example/.openclaw/workspace/PROJECTS")
        self.assertEqual(defaults["openclaw"]["systemSkillsRoot"], "/Users/example/.openclaw/skills")
        self.assertEqual(defaults["openclaw"]["cronJobsPath"], "/Users/example/.openclaw/cron/jobs.json")
        self.assertEqual(defaults["openclaw"]["toolConfigSnapshotPath"], "/Users/example/.openclaw/workspace/.dashboard-tool-configs.json")
        self.assertEqual(defaults["claudeCode"]["projectsRoot"], "/Users/example/.claude/projects")
        self.assertEqual(defaults["claudeCode"]["skillsRoot"], "/Users/example/.claude/skills")
        self.assertEqual(defaults["codex"]["sessionsRoot"], "/Users/example/.codex/sessions")
        self.assertEqual(defaults["codex"]["skillsRoot"], "/Users/example/.codex/skills")
        self.assertEqual(defaults["geminiCli"]["chatsRoot"], "/Users/example/.gemini/tmp/ssd/chats")
        self.assertEqual(defaults["geminiCli"]["skillsRoot"], "/Users/example/.gemini/skills")
        self.assertEqual(defaults["hermes"]["stateDbPath"], "/Users/example/.hermes/state.db")
        self.assertEqual(defaults["hermes"]["sessionsRoot"], "/Users/example/.hermes/sessions")
        self.assertEqual(defaults["hermes"]["profilesRoot"], "/Users/example/.hermes/profiles")

    def test_general_pipeline_dashboard_settings_resolve_with_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "general": {
                        "appName": "Nova Custom",
                        "environment": "beta",
                        "timezone": "Asia/Hong_Kong",
                        "locale": "zh-CN",
                        "workspaceRoot": str(root / "workspace"),
                        "tmpWorkspace": str(root / "tmp"),
                    },
                    "pipeline": {
                        "pythonExecutable": "python3.13",
                        "workingDirectory": str(root / "pipeline"),
                        "languageProfile": "zh",
                        "englishEnabled": False,
                        "diarySchemaVersion": "diary-v1-zh",
                        "promptPayloadProfile": "zh-CN",
                        "thinkingMode": "off",
                        "stepTimeoutSeconds": 111,
                        "stepTimeouts": {"technical_pass.py": 222},
                        "totalWatchdogSeconds": 333,
                    },
                    "dashboard": {
                        "projectRoot": str(root / "dashboard-root"),
                        "pythonExecutable": str(root / "venv" / "bin" / "python"),
                        "host": "127.0.0.1",
                        "port": 3036,
                        "healthPath": "/health",
                    },
                },
                paths,
            )
            with patch.dict(
                os.environ,
                {
                    "ACTANARA_ENVIRONMENT": "env-beta",
                    "TARGET_TIMEZONE": "UTC",
                    "ACTANARA_PIPELINE_PYTHON": "python-env",
                    "LLM_THINKING_MODE": "on",
                    "ACTANARA_PIPELINE_STEP_TIMEOUT_SECONDS": "444",
                    "ACTANARA_PIPELINE_TOTAL_WATCHDOG_SECONDS": "555",
                    "ACTANARA_DASHBOARD_HOST": "0.0.0.0",
                    "ACTANARA_DASHBOARD_PORT": "4545",
                },
            ):
                general = resolve_general_settings(paths)
                pipeline = resolve_pipeline_settings(paths)
                dashboard = resolve_dashboard_settings(paths)
                contract = runtime_authority_contract(paths)

        self.assertEqual(general["appName"], "Nova Custom")
        self.assertEqual(general["environment"], "beta")
        self.assertEqual(general["timezone"], "Asia/Hong_Kong")
        self.assertEqual(general["locale"], "zh-CN")
        self.assertEqual(pipeline["languageProfile"], "zh")
        self.assertEqual(pipeline["languageStatus"], "production")
        self.assertFalse(pipeline["englishEnabled"])
        self.assertEqual(pipeline["displayLocale"], "zh-CN")
        self.assertEqual(pipeline["diarySchemaVersion"], "diary-v1-zh")
        self.assertEqual(pipeline["promptPayloadProfile"], "zh-CN")
        self.assertEqual(pipeline["ragLanguageProfile"], "zh")
        self.assertEqual(pipeline["pythonExecutable"], "python3.13")
        self.assertEqual(pipeline["thinkingMode"], "off")
        self.assertEqual(pipeline["stepTimeoutSeconds"], 111)
        self.assertEqual(pipeline["stepTimeouts"]["technical_pass.py"], 222)
        self.assertEqual(pipeline["totalWatchdogSeconds"], 333)
        self.assertEqual(dashboard["host"], "127.0.0.1")
        self.assertEqual(dashboard["port"], 3036)
        self.assertEqual(dashboard["url"], "http://127.0.0.1:3036/health")
        self.assertEqual(contract["general"]["environment"], "beta")
        self.assertEqual(contract["pipeline"]["pythonExecutable"], "python3.13")
        self.assertEqual(contract["pipeline"]["languageProfile"], "zh")
        self.assertFalse(contract["pipeline"]["englishEnabled"])
        self.assertEqual(contract["dashboard"]["port"], 3036)

    def test_pipeline_language_profile_contract_derives_language_defaults(self):
        self.assertEqual(resolve_pipeline_language_profile("zh-CN").profile_id, "zh")
        self.assertEqual(resolve_pipeline_language_profile("en-US").profile_id, "en")
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"pipeline": {"languageProfile": "en", "englishEnabled": True}}, paths)

            pipeline = resolve_pipeline_settings(paths)

        self.assertEqual(pipeline["languageProfile"], "en")
        self.assertEqual(pipeline["languageStatus"], "gated")
        self.assertTrue(pipeline["englishEnabled"])
        self.assertEqual(pipeline["displayLocale"], "en-US")
        self.assertEqual(pipeline["diarySchemaVersion"], "diary-v1-en")
        self.assertEqual(pipeline["promptPayloadProfile"], "en-US")
        self.assertEqual(pipeline["ragLanguageProfile"], "en")

    def test_agent_schedule_prompt_defaults_to_chinese_profile_copy(self):
        prompt = build_agent_schedule_prompt(
            {
                "schedule": {"timezone": "Asia/Hong_Kong", "dailyPipelineTime": "04:00", "dashboardAggregationTime": "04:30"},
                "paths": {"runtime": {"actanaraHome": "/tmp/actanara"}},
                "pipeline": {"languageProfile": "zh"},
            }
        )

        self.assertIn("唯一的外部定时触发器", prompt)
        self.assertIn("严禁两套 scheduler 同时运行", prompt)
        self.assertIn('任务 1 — 每日管线，执行时间 04:00："/tmp/actanara/bin/actanara" pipeline', prompt)
        self.assertIn('"/tmp/actanara/.venv/bin/python" "/tmp/actanara/app/source/advanced/pipeline/run_dashboard_foundation_refresh.py"', prompt)
        self.assertIn("最多重试一次", prompt)
        self.assertNotIn("Phase 25", prompt)
        self.assertNotIn("python advanced/pipeline/run_daily_pipeline.py", prompt)

    def test_agent_schedule_prompt_uses_english_copy_for_english_profile(self):
        prompt = build_agent_schedule_prompt(
            {
                "schedule": {"timezone": "America/Los_Angeles", "dailyPipelineTime": "04:00", "dashboardAggregationTime": "04:30"},
                "paths": {"runtime": {"actanaraHome": "/tmp/actanara-en"}},
                "pipeline": {"languageProfile": "en", "englishEnabled": True},
            }
        )

        self.assertIn("sole external scheduler", prompt)
        self.assertIn("Timezone: America/Los_Angeles", prompt)
        self.assertIn('Job 1 — daily pipeline at 04:00: "/tmp/actanara-en/bin/actanara" pipeline', prompt)
        self.assertIn('"/tmp/actanara-en/.venv/bin/python" "/tmp/actanara-en/app/source/advanced/pipeline/run_dashboard_foundation_refresh.py"', prompt)
        self.assertIn("retry at most once", prompt)
        self.assertNotIn("Phase 25", prompt)
        self.assertNotIn("python advanced/pipeline/run_daily_pipeline.py", prompt)
        self.assertNotIn("唯一的外部定时触发器", prompt)

    def test_settings_authority_inventory_includes_system_subsystems(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            inventory = settings_authority_inventory(paths)

        groups = {group["group"]: group for group in inventory["groups"]}
        for group in ("general", "pipeline", "dashboard", "rag", "externalTools"):
            self.assertIn(group, groups)
        general_fields = {field["path"]: field for field in groups["general"]["fields"]}
        self.assertEqual(general_fields["general.appName"]["effectiveValue"], "Actanara")
        pipeline_fields = {field["path"]: field for field in groups["pipeline"]["fields"]}
        self.assertIn("run_daily_pipeline.py", pipeline_fields["pipeline.stableCommand"]["effectiveValue"])
        self.assertEqual(pipeline_fields["pipeline.languageProfile"]["effectiveValue"], "zh")
        self.assertFalse(pipeline_fields["pipeline.englishEnabled"]["effectiveValue"])
        self.assertEqual(pipeline_fields["pipeline.diarySchemaVersion"]["effectiveValue"], "diary-v1-zh")
        self.assertEqual(pipeline_fields["pipeline.promptPayloadProfile"]["effectiveValue"], "zh-CN")
        self.assertEqual(pipeline_fields["pipeline.stepTimeoutSeconds"]["effectiveValue"], 1800)
        self.assertIn("technical_pass.py", pipeline_fields["pipeline.stepTimeouts"]["effectiveValue"])
        self.assertEqual(pipeline_fields["pipeline.totalWatchdogSeconds"]["effectiveValue"], 7200)
        dashboard_fields = {field["path"]: field for field in groups["dashboard"]["fields"]}
        self.assertEqual(dashboard_fields["dashboard.host"]["effectiveValue"], "127.0.0.1")

    def test_external_tool_paths_resolve_from_settings_and_report_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            openclaw_agents = root / "tools" / "openclaw" / "agents"
            openclaw_session = openclaw_agents / "agent-one" / "sessions" / "session.jsonl"
            openclaw_session.parent.mkdir(parents=True)
            openclaw_session.write_text("{}\n", encoding="utf-8")
            cron_runs = root / "tools" / "openclaw" / "cron" / "runs"
            cron_runs.mkdir(parents=True)
            (cron_runs / "run.jsonl").write_text("{}\n", encoding="utf-8")
            claude_projects = root / "tools" / "claude" / "projects"
            claude_projects.mkdir(parents=True)
            (claude_projects / "session.jsonl").write_text("{}\n", encoding="utf-8")
            codex_sessions = root / "tools" / "codex" / "sessions"
            codex_sessions.mkdir(parents=True)
            (codex_sessions / "rollout-test.jsonl").write_text("{}\n", encoding="utf-8")
            gemini_chats = root / "tools" / "gemini" / "chats"
            gemini_chats.mkdir(parents=True)
            (gemini_chats / "session-test.json").write_text('{"messages":[]}\n', encoding="utf-8")
            gemini_projects = root / "tools" / "gemini" / "projects.json"
            gemini_projects.write_text("{}\n", encoding="utf-8")
            hermes_db = root / "tools" / "hermes" / "state.db"
            hermes_db.parent.mkdir(parents=True)
            hermes_db.write_bytes(b"")

            write_settings(
                {
                    "externalTools": {
                        "openclaw": {
                            "agentsRoot": str(openclaw_agents),
                            "cronRunsRoot": str(cron_runs),
                        },
                        "claudeCode": {"projectsRoot": str(claude_projects)},
                        "codex": {"sessionsRoot": str(codex_sessions)},
                        "geminiCli": {
                            "chatsRoot": str(gemini_chats),
                            "projectsPath": str(gemini_projects),
                        },
                        "hermes": {"stateDbPath": str(hermes_db)},
                    }
                },
                paths,
            )

            resolved = resolve_external_tool_paths(paths)
            summary = external_tool_access_summary(paths)

        self.assertEqual(resolved["openclaw"]["agentsRoot"], openclaw_agents.absolute())
        self.assertEqual(resolved["openclaw"]["cronRunsRoot"], cron_runs.absolute())
        self.assertEqual(resolved["claudeCode"]["projectsRoot"], claude_projects.absolute())
        self.assertEqual(resolved["codex"]["sessionsRoot"], codex_sessions.absolute())
        self.assertEqual(resolved["geminiCli"]["chatsRoot"], gemini_chats.absolute())
        self.assertEqual(resolved["geminiCli"]["projectsPath"], gemini_projects.absolute())
        self.assertEqual(resolved["hermes"]["stateDbPath"], hermes_db.absolute())
        checks = summary["checks"]
        self.assertEqual(checks["openclaw.agentsRoot"]["sampleCount"], 1)
        self.assertEqual(checks["openclaw.cronRunsRoot"]["sampleCount"], 1)
        self.assertEqual(checks["claudeCode.projectsRoot"]["sampleCount"], 1)
        self.assertEqual(checks["codex.sessionsRoot"]["sampleCount"], 1)
        self.assertEqual(checks["geminiCli.chatsRoot"]["sampleCount"], 1)
        self.assertTrue(checks["geminiCli.projectsPath"]["readable"])
        self.assertTrue(checks["hermes.stateDbPath"]["readable"])

    def test_default_usage_adapters_use_configured_external_tool_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "externalTools": {
                        "openclaw": {
                            "agentsRoot": str(root / "openclaw" / "agents"),
                            "cronRunsRoot": str(root / "openclaw" / "cron" / "runs"),
                        },
                        "claudeCode": {"projectsRoot": str(root / "claude" / "projects")},
                        "codex": {"sessionsRoot": str(root / "codex" / "sessions")},
                        "geminiCli": {"chatsRoot": str(root / "gemini" / "chats")},
                        "hermes": {"stateDbPath": str(root / "hermes" / "state.db")},
                        "opencode": {"home": str(root / "opencode")},
                        "antigravity": {
                            "cliHome": str(root / "antigravity-cli"),
                            "ideHome": str(root / "antigravity-ide"),
                            "appHome": str(root / "antigravity"),
                        },
                        "cursor": {
                            "home": str(root / "cursor"),
                            "ideStateDbCandidates": [str(root / "cursor-ide" / "state.vscdb")],
                            "workspaceStorageRoots": [str(root / "cursor-ide" / "workspaceStorage")],
                        },
                    }
                },
                paths,
            )

            adapters = default_usage_adapters(paths)

        by_type = {type(adapter): adapter for adapter in adapters}
        self.assertEqual(by_type[OpenClawAdapter].root, (root / "openclaw" / "agents").absolute())
        self.assertEqual(by_type[ClaudeCodeAdapter].root, (root / "claude" / "projects").absolute())
        self.assertEqual(by_type[CodexAdapter].root, (root / "codex" / "sessions").absolute())
        self.assertEqual(by_type[GeminiCliAdapter].root, (root / "gemini" / "chats").absolute())
        self.assertEqual(by_type[HermesAdapter].db_path, (root / "hermes" / "state.db").absolute())
        self.assertEqual(by_type[OpenCodeAdapter].runtime.home, (root / "opencode").absolute())
        self.assertEqual(
            by_type[AntigravityAdapter].runtime.variant_homes,
            {
                "cli": (root / "antigravity-cli").absolute(),
                "ide": (root / "antigravity-ide").absolute(),
                "app": (root / "antigravity").absolute(),
            },
        )
        self.assertEqual(by_type[CursorRuntimeAdapter].runtime.home, (root / "cursor").absolute())
        self.assertEqual(
            by_type[CursorRuntimeAdapter].runtime._configured_ide_state_dbs,
            ((root / "cursor-ide" / "state.vscdb").absolute(),),
        )
        self.assertEqual(by_type[CronAdapter].root, (root / "openclaw" / "cron" / "runs").absolute())

    def test_llm_provider_resolution_ignores_process_env_when_settings_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "provider": "minimax",
                    "endpoint": "https://api.minimax.io/anthropic",
                    "model": "MiniMax-M3",
                    "api": "anthropic-messages",
                    "apiKey": "settings-secret",
                },
                paths,
            )
            old_env_values = {
                "LLM_HOST": "https://api.minimaxi.com",
                "LLM_MODEL_NAME": "MiniMax-M2.7-highspeed",
                "LLM_API_KEY": "dotenv-secret",
            }
            with patch.dict(os.environ, old_env_values):
                provider = resolve_llm_provider(paths, redact_secrets=True)
            self.assertEqual(provider["endpoint"], "https://api.minimax.io/anthropic")
            self.assertEqual(provider["model"], "MiniMax-M3")
            self.assertEqual(provider["apiKey"], MASKED_SECRET)
            self.assertEqual(provider["source"]["endpoint"], "settings")
            self.assertEqual(provider["source"]["model"], "settings")
            self.assertEqual(provider["source"]["apiKey"], "secret-store")

    def test_operator_settings_write_policy_accepts_dashboard_editable_groups(self):
        allowed = validate_operator_settings_update(
            {
                "general": {"environment": "local"},
                "dashboard": {
                    "host": "127.0.0.1",
                    "port": 3036,
                    "publicBaseUrl": "http://127.0.0.1:3036",
                    "allowedOrigins": ["http://127.0.0.1:3036", "https://actanara.example.com"],
                    "logsDir": "/tmp/logs",
                },
                "schedule": {"enabled": True},
                "features": {"dashboard": True},
                "externalTools": {"codex": {"sessionsRoot": "/tmp/codex", "binaryCandidates": ["/tmp/codex-bin"]}},
                "paths": {"diary": {"generatedDiary": "/tmp/diary"}},
                "pipeline": {
                    "pythonExecutable": "/tmp/python",
                    "workingDirectory": "/tmp/source",
                    "stepTimeoutSeconds": 60,
                },
                "runtimeSources": {"dashboardReadSource": "foundation"},
                "weather": {"enabled": True, "locationMode": "manual", "latitude": 37.7749, "longitude": -122.4194},
                "todos": {"githubUrl": ""},
            }
        )
        self.assertEqual(
            sorted(allowed),
            ["dashboard", "externalTools", "features", "general", "paths", "pipeline", "runtimeSources", "schedule", "todos", "weather"],
        )
        for key in ("llmProvider", "rag", "schemaVersion", "updatedAt"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "dedicated API"):
                    validate_operator_settings_update({key: {}})
        with self.assertRaisesRegex(ValueError, "unsupported settings groups"):
            validate_operator_settings_update({"unknown": True})
        invalid_updates = (
            {"general": {"locale": ""}},
            {"general": {"workspaceRoot": "bad\x00path"}},
            {"dashboard": {"host": ""}},
            {"dashboard": {"port": 0}},
            {"dashboard": {"port": 70000}},
            {"dashboard": {"publicBaseUrl": "ftp://127.0.0.1:3036"}},
            {"dashboard": {"allowedOrigins": "http://127.0.0.1:3036"}},
            {"dashboard": {"allowedOrigins": ["not-an-origin"]}},
            {"dashboard": {"logsDir": ""}},
            {"externalTools": {"codex": []}},
            {"externalTools": {"codex": {"sessionsRoot": ""}}},
            {"externalTools": {"claudeCode": {"binaryCandidates": "claude"}}},
            {"paths": {"diary": {"generatedDiary": ""}}},
            {"paths": {"diary": []}},
            {"paths": {"runtime": {"actanaraHome": "/tmp/actanara"}}},
            {"pipeline": {"pythonExecutable": ""}},
            {"pipeline": {"languageProfile": "en"}},
            {"pipeline": {"diarySchemaVersion": "diary-v1-en"}},
            {"pipeline": {"promptPayloadProfile": "en-US"}},
            {"pipeline": {"englishEnabled": True}},
            {"pipeline": {"englishEnabled": "true"}},
            {"pipeline": {"stepTimeoutSeconds": 0}},
            {"pipeline": {"stepTimeouts": {"narrative_pass.py": 0}}},
            {"runtimeSources": {"dashboardReadSource": "unknown"}},
            {"runtimeSources": {"unknownSource": "foundation"}},
            {"weather": {"locationMode": "gps"}},
            {"weather": {"latitude": 91}},
            {"weather": {"longitude": -181}},
        )
        for invalid in invalid_updates:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_operator_settings_update(invalid)

    def test_pipeline_language_profile_is_installer_only_not_operator_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            internal = write_settings({"pipeline": {"languageProfile": "en", "englishEnabled": True}}, paths)

            pipeline = resolve_pipeline_settings(paths)

        self.assertEqual(internal["pipeline"]["languageProfile"], "en")
        self.assertEqual(pipeline["languageProfile"], "en")
        self.assertTrue(pipeline["englishEnabled"])
        with self.assertRaisesRegex(ValueError, "install-time language fields are immutable"):
            validate_operator_settings_update({"pipeline": {"languageProfile": "zh"}})
        with self.assertRaisesRegex(ValueError, "install-time language fields are immutable"):
            validate_operator_settings_update({"pipeline": {"englishEnabled": False}})

    def test_write_operator_settings_preserves_protected_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": "secret",
                },
                paths,
            )
            updated = write_operator_settings({"schedule": {"dailyPipelineTime": "05:15"}}, paths)
            raw = read_settings(paths, redact_secrets=False)

        self.assertEqual(updated["schedule"]["dailyPipelineTime"], "05:15")
        self.assertEqual(raw["llmProvider"]["apiKey"], "")
        self.assertEqual(raw["llmProvider"]["secretRef"]["backend"], "memory")

    def test_write_operator_settings_marks_registered_system_timer_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "04:00",
                        "systemTimer": {
                            "provider": "launchd",
                            "label": "nova.test",
                            "registered": True,
                            "lastActionStatus": "success",
                        },
                    }
                },
                paths,
            )

            updated = write_operator_settings({"schedule": {"dailyPipelineTime": "04:15"}}, paths)

        timer = updated["schedule"]["systemTimer"]
        self.assertTrue(timer["registered"])
        self.assertTrue(timer["stale"])
        self.assertTrue(timer["reinstallRequired"])
        self.assertEqual(timer["staleReason"], "operator-settings-changed")
        self.assertEqual(timer["lastActionStatus"], "registered-stale")

    def test_write_operator_settings_marks_registered_systemd_timer_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "04:00",
                        "systemTimer": {
                            "provider": "systemd",
                            "label": "actanara.test",
                            "registered": True,
                            "lastActionStatus": "success",
                        },
                    }
                },
                paths,
            )

            updated = write_operator_settings({"schedule": {"dailyPipelineTime": "04:15"}}, paths)

        timer = updated["schedule"]["systemTimer"]
        self.assertTrue(timer["registered"])
        self.assertTrue(timer["stale"])
        self.assertTrue(timer["reinstallRequired"])
        self.assertEqual(timer["staleReason"], "operator-settings-changed")
        self.assertEqual(timer["lastActionStatus"], "registered-stale")

    def test_write_operator_settings_syncs_generated_diary_path_to_runtime_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            target = root / "GeneratedDiary"

            updated = write_operator_settings({"paths": {"diary": {"generatedDiary": str(target)}}}, paths)
            manifest = json.loads((paths.config_dir / "runtime.json").read_text(encoding="utf-8"))
            reloaded = initialize_home(paths.home, legacy_diary_root=Path(manifest["legacyDiaryRoot"]))

        self.assertEqual(updated["paths"]["diary"]["generatedDiary"], str(target))
        self.assertEqual(updated["paths"]["diary"]["legacyDiaryRoot"], str(root / "Diary"))
        self.assertEqual(manifest["generatedDiaryRoot"], str(target))
        self.assertEqual(manifest["legacyDiaryRoot"], str(root / "Diary"))
        self.assertEqual(str(reloaded.diary_dir), str(target))
        self.assertEqual(str(reloaded.legacy_diary_root), str(root / "Diary"))

    def test_write_operator_settings_preserves_stale_legacy_diary_root_when_generated_is_already_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            target = root / "GeneratedDiary"
            write_settings({"paths": {"diary": {"generatedDiary": str(target)}}}, paths)

            updated = write_operator_settings({"paths": {"diary": {"generatedDiary": str(target)}}}, paths)
            manifest = json.loads((paths.config_dir / "runtime.json").read_text(encoding="utf-8"))

        self.assertEqual(updated["paths"]["diary"]["generatedDiary"], str(target))
        self.assertEqual(updated["paths"]["diary"]["legacyDiaryRoot"], str(root / "Diary"))
        self.assertEqual(manifest["generatedDiaryRoot"], str(target))
        self.assertEqual(manifest["legacyDiaryRoot"], str(root / "Diary"))

    def test_actanara_settings_status_reports_readonly_doctor_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_sessions = root / "codex" / "sessions"
            codex_sessions.mkdir(parents=True)
            (codex_sessions / "rollout-test.jsonl").write_text("{}\n", encoding="utf-8")
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M2.7-highspeed",
                    "apiKey": "secret",
                },
                paths,
            )
            write_settings(
                {
                    "dashboard": {"projectRoot": str(root / "dashboard-source")},
                    "runtimeSources": {"dashboardReadSource": "foundation"},
                    "externalTools": {"codex": {"sessionsRoot": str(codex_sessions)}},
                },
                paths,
            )

            payload = actanara_settings_status(paths)
            text = format_actanara_settings_status(payload)
            raw_json = dump_actanara_settings_status_json(payload)

        self.assertTrue(payload["readOnly"])
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["general"]["appName"], "Actanara")
        self.assertEqual(payload["runtime"]["actanaraHome"], str(paths.home))
        self.assertIn("run_daily_pipeline.py", payload["pipeline"]["stableCommand"])
        self.assertEqual(payload["dashboard"]["healthPath"], "/health")
        self.assertEqual(payload["sources"]["DASHBOARD_READ_SOURCE"], "foundation")
        self.assertEqual(payload["llmProvider"]["apiKey"], MASKED_SECRET)
        self.assertTrue(payload["llmProvider"]["hasApiKey"])
        self.assertIn("secretVisibility", payload["llmProvider"])
        self.assertEqual(payload["externalTools"]["checks"]["codex.sessionsRoot"]["sampleCount"], 1)
        self.assertIn("settingsAudit", payload)
        self.assertIn("residualRisks", payload["settingsAudit"])
        self.assertIn("active-env-overrides", {bucket["id"] for bucket in payload["settingsAudit"]["residualRisks"]["buckets"]})
        self.assertEqual(payload["runtimeSource"]["status"], "missing")
        self.assertEqual(
            payload["runtimeSource"]["productVersionAuthority"],
            "active-runtime-source-manifest",
        )
        self.assertIsNone(payload["runtimeSource"]["sourceVersion"])
        self.assertIn("runtime-source-provenance", {check["id"] for check in payload["checks"]})
        self.assertEqual(payload["resourceProfile"]["dashboard"]["expectedResidentProcesses"], 1)
        self.assertIn("rag", payload["resourceProfile"])
        self.assertEqual(payload["resourceProfile"]["pipeline"]["expectedResidentProcesses"], 0)
        self.assertEqual(payload["dependencyProfiles"]["schemaVersion"], 1)
        self.assertIn("core-foundation", {profile["id"] for profile in payload["dependencyProfiles"]["profiles"]})
        self.assertIn("settings-hardcode-audit", {check["id"] for check in payload["checks"]})
        self.assertIn("llm-launchd-secret-visibility", {check["id"] for check in payload["checks"]})
        self.assertIn("Actanara · System status", text)
        self.assertIn("Data folder", text)
        self.assertIn("Dashboard", text)
        self.assertIn("AI model", text)
        self.assertIn("Checks", text)
        self.assertIn('"languageProfile"', raw_json)
        self.assertNotIn("任务看板", text)
        self.assertNotIn("设置", text)
        self.assertNotIn("Runtime source:", text)
        self.assertNotIn("versionAuthority=", text)
        self.assertNotIn("residual=", text)
        self.assertNotIn("settings-hardcode-audit", text)
        self.assertEqual(json.loads(raw_json)["runtime"]["actanaraHome"], str(paths.home))

    def test_actanara_settings_status_supports_layered_doctor_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_sessions = root / "codex" / "sessions"
            codex_sessions.mkdir(parents=True)
            write_settings({"externalTools": {"codex": {"sessionsRoot": str(codex_sessions)}}}, paths)

            pipeline = actanara_settings_status(paths, doctor_profile="pipeline")
            scheduler = actanara_settings_status(paths, doctor_profile="scheduler")
            rag = actanara_settings_status(paths, doctor_profile="rag")
            pipeline_text = format_actanara_settings_status(pipeline)
            scheduler_text = format_actanara_settings_status(scheduler)
            rag_text = format_actanara_settings_status(rag)
            scheduler_ids = {check["id"] for check in scheduler["checks"]}
            rag_ids = {check["id"] for check in rag["checks"]}

        self.assertEqual(pipeline["doctorProfile"], "pipeline")
        self.assertIn("Actanara · Daily diary check", pipeline_text)
        self.assertTrue(any(check["id"].startswith("llm-") for check in pipeline["checks"]))
        self.assertTrue(any(check["id"].startswith("external-tool:") for check in pipeline["checks"]))
        self.assertNotIn("runtime-home", {check["id"] for check in pipeline["checks"]})
        self.assertEqual(scheduler["doctorProfile"], "scheduler")
        self.assertNotIn("actanara model", scheduler_text)
        self.assertTrue(any(item.endswith("registration:dashboard") for item in scheduler_ids))
        self.assertFalse(any(item.endswith("registration:rag-server") for item in scheduler_ids))
        self.assertEqual(rag["doctorProfile"], "rag")
        self.assertNotIn("actanara model", rag_text)
        self.assertTrue(any(item.endswith("registration:rag-server") for item in rag_ids))

    def test_rag_server_settings_reject_new_nonloopback_write_and_doctor_blocks_legacy_value(self):
        with self.assertRaisesRegex(ValueError, "must be localhost or a numeric loopback"):
            foundation_settings.normalize_rag_settings_update({"server": {"host": "0.0.0.0"}})
        accepted = foundation_settings.normalize_rag_settings_update({"server": {"host": "::1"}})
        self.assertEqual(accepted["server"]["host"], "::1")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            raw = read_settings(paths, redact_secrets=False)
            raw.setdefault("rag", {}).setdefault("server", {})["host"] = "0.0.0.0"
            (paths.config_dir / "settings.json").write_text(json.dumps(raw), encoding="utf-8")

            payload = actanara_settings_status(paths, doctor_profile="rag")

        boundary = payload["resourceProfile"]["rag"]["networkBoundary"]
        check = next(item for item in payload["checks"] if item["id"] == "rag-server-loopback-boundary")
        self.assertEqual(boundary["status"], "blocked")
        self.assertEqual(boundary["issueCode"], "rag-server-non-loopback")
        self.assertEqual(check["status"], "error")
        self.assertIn("Blocked: rag-server-non-loopback", check["message"])

    def test_actanara_settings_status_warns_when_llm_key_is_env_only_for_launchd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "llmProvider": {
                        "provider": "openai-compatible",
                        "endpoint": "https://llm.local",
                        "model": "m1",
                        "apiKeyEnv": "CUSTOM_LLM_KEY",
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"CUSTOM_LLM_KEY": "secret"}):
                payload = actanara_settings_status(paths)

        visibility = payload["llmProvider"]["secretVisibility"]
        self.assertEqual(visibility["source"], "env")
        self.assertFalse(visibility["launchdSafe"])
        self.assertEqual(visibility["status"], "launchd-unsafe")
        by_id = {check["id"]: check for check in payload["checks"]}
        self.assertEqual(by_id["llm-launchd-secret-visibility"]["status"], "warn")
        self.assertIn("CUSTOM_LLM_KEY", by_id["llm-launchd-secret-visibility"]["message"])

    def test_actanara_settings_status_reports_fresh_runtime_source_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_remote = "file:///Users/private-operator/actanara"
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            source_root = root / "source-checkout"
            project_root = root / "runtime-source"
            launch_home = root / "launch-home"
            source_root.mkdir()
            project_root.mkdir()
            launch_agents = launch_home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            with (launch_agents / "com.actanara.dashboard.plist").open("wb") as handle:
                plistlib.dump({"ProgramArguments": ["/bin/zsh", "-lc", f"cd {project_root} && python3 -m uvicorn app.main:app"]}, handle)
            with (launch_agents / "com.actanara.dashboard.watchdog.plist").open("wb") as handle:
                plistlib.dump({"ProgramArguments": ["python3", str(project_root / "advanced" / "dashboard" / "dashboard_launch_agent.py"), "check"]}, handle)
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sourceRoot": str(source_root),
                        "deployedSourceRoot": str(project_root),
                        "copiedAt": "2026-06-24T00:00:00+08:00",
                        "pyprojectVersion": "0.0.0",
                        "git": {"commit": "abc123", "branch": "main", "remote": private_remote, "dirty": False},
                    }
                ),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            def fake_git(_root, *args):
                if args == ("rev-parse", "HEAD"):
                    return "abc123"
                if args == ("status", "--porcelain"):
                    return ""
                raise AssertionError(args)

            with (
                patch("data_foundation.settings_status._git_value", side_effect=fake_git),
                patch.object(Path, "home", return_value=launch_home),
            ):
                payload = actanara_settings_status(paths)

        self.assertEqual(payload["runtimeSource"]["status"], "fresh")
        self.assertFalse(payload["runtimeSource"]["stale"])
        self.assertEqual(payload["runtimeSource"]["freshness"], "fresh")
        self.assertEqual(payload["runtimeSource"]["sourceLocator"]["kind"], "legacy-absolute")
        self.assertNotIn(str(source_root), json.dumps(payload["runtimeSource"]))
        self.assertNotIn(private_remote, json.dumps(payload["runtimeSource"]))
        self.assertTrue(payload["runtimeSource"]["manifest"]["git"]["remoteAvailable"])
        self.assertEqual(payload["runtimeSource"]["launchAgentMismatches"], [])
        check = next(item for item in payload["checks"] if item["id"] == "runtime-source-provenance")
        self.assertEqual(check["status"], "ok")
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(by_id["runtime-source-launchagent-alignment"]["status"], "ok")

    def test_actanara_settings_status_uses_source_manifest_version_when_dist_info_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            project_root = root / "runtime-source"
            project_root.mkdir()
            manifest = _v2_runtime_source_manifest(
                {"kind": "unavailable", "issue": "outside-login-home"}
            )
            manifest["pyprojectVersion"] = "1.0.2"
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            stale_dist_info = (
                paths.home
                / ".venv"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "actanara-1.0.1.dist-info"
            )
            stale_dist_info.mkdir(parents=True)
            (stale_dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: actanara\nVersion: 1.0.1\n",
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            payload = actanara_settings_status(paths)
            text = format_actanara_settings_status(payload)

        runtime_source = payload["runtimeSource"]
        self.assertEqual(runtime_source["productVersionAuthority"], "active-runtime-source-manifest")
        self.assertEqual(runtime_source["sourceVersion"], "1.0.2")
        self.assertEqual(runtime_source["manifest"]["pyprojectVersion"], "1.0.2")
        self.assertNotIn("distributionVersion", runtime_source)
        self.assertNotIn("metadataMayLag", runtime_source)
        self.assertIn("Installed app files", text)
        self.assertNotIn("version=1.0.2", text)
        self.assertNotIn("versionAuthority=active-runtime-source-manifest", text)

    def test_actanara_settings_status_reads_v2_locator_without_echoing_private_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            login_home = root / "login-home"
            source_root = login_home / "work" / "actanara"
            project_root = root / "runtime-source"
            source_root.mkdir(parents=True)
            project_root.mkdir()
            manifest = _v2_runtime_source_manifest(
                {
                    "kind": "login-home-relative",
                    "pathComponents": ["work", "actanara"],
                }
            )
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            def fake_git(_root, *args):
                self.assertEqual(_root, source_root.resolve())
                if args == ("rev-parse", "HEAD"):
                    return "abc1234"
                if args == ("status", "--porcelain"):
                    return ""
                raise AssertionError(args)

            with (
                patch("data_foundation.settings_status._login_home_path", return_value=login_home),
                patch("data_foundation.settings_status._git_value", side_effect=fake_git),
                patch.object(Path, "home", return_value=login_home),
            ):
                payload = actanara_settings_status(paths)
                text = format_actanara_settings_status(payload)

        runtime_source = payload["runtimeSource"]
        self.assertEqual(runtime_source["status"], "fresh")
        self.assertEqual(runtime_source["freshness"], "fresh")
        self.assertEqual(runtime_source["manifestSchemaVersion"], 2)
        self.assertEqual(runtime_source["sourceLocator"], {"kind": "login-home-relative", "available": True})
        self.assertNotIn(str(source_root), json.dumps(runtime_source))
        self.assertNotIn(str(source_root), text)
        self.assertNotIn("pathComponents", runtime_source["manifest"]["sourceLocator"])

    def test_actanara_settings_status_reports_unknown_for_unavailable_v2_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            project_root = root / "runtime-source"
            project_root.mkdir()
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(
                    _v2_runtime_source_manifest(
                        {"kind": "unavailable", "issue": "outside-login-home"}
                    )
                ),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            with patch.object(Path, "home", return_value=root / "login-home"):
                payload = actanara_settings_status(paths)

        runtime_source = payload["runtimeSource"]
        self.assertEqual(runtime_source["status"], "present")
        self.assertEqual(runtime_source["freshness"], "unknown")
        self.assertIsNone(runtime_source["stale"])
        self.assertEqual(runtime_source["sourceLocator"]["issue"], "outside-login-home")

    def test_actanara_settings_status_rejects_invalid_v2_without_git_probe_or_private_echo(self):
        private_marker = "/Users/private-operator/Desktop/actanara"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            project_root = root / "runtime-source"
            project_root.mkdir()
            manifest = _v2_runtime_source_manifest(
                {"kind": "login-home-relative", "pathComponents": ["work", "actanara"]}
            )
            manifest["debugPath"] = private_marker
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            with patch("data_foundation.settings_status._git_value") as git_value:
                payload = actanara_settings_status(paths)

        runtime_source = payload["runtimeSource"]
        self.assertEqual(runtime_source["status"], "invalid")
        self.assertEqual(runtime_source["freshness"], "unknown")
        self.assertIsNone(runtime_source["stale"])
        self.assertNotIn(private_marker, json.dumps(runtime_source))
        git_value.assert_not_called()

    def test_actanara_settings_status_rejects_invalid_v2_scalar_types_without_git_probe(self):
        mutations = (
            {"pyprojectVersion": "/Users/private-operator/Desktop/actanara"},
            {
                "git": {
                    "available": True,
                    "commit": "0" * 40,
                    "branch": "main",
                    "remote": 123,
                    "dirty": False,
                }
            },
            {
                "git": {
                    "available": True,
                    "commit": "abc123",
                    "branch": "main",
                    "remote": None,
                    "dirty": False,
                }
            },
            {
                "releaseLocator": {
                    "kind": "runtime-relative",
                    "pathComponents": ["app", "releases", "候选"],
                }
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
                project_root = root / "runtime-source"
                project_root.mkdir()
                manifest = _v2_runtime_source_manifest(
                    {"kind": "login-home-relative", "pathComponents": ["work", "actanara"]}
                )
                manifest.update(mutation)
                (project_root / ".actanara-runtime-source.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

                with patch("data_foundation.settings_status._git_value") as git_value:
                    payload = actanara_settings_status(paths)

            self.assertEqual(payload["runtimeSource"]["status"], "invalid")
            self.assertEqual(payload["runtimeSource"]["freshness"], "unknown")
            self.assertNotIn("/Users/private-operator", json.dumps(payload["runtimeSource"]))
            git_value.assert_not_called()

    def test_launchagent_alignment_uses_path_boundary_and_redacts_private_content(self):
        private_marker = "/Users/private-operator/Desktop/actanara"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            project_root = root / "runtime-source"
            launch_home = root / "launch-home"
            project_root.mkdir()
            launch_agents = launch_home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            with (launch_agents / "com.actanara.dashboard.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "ProgramArguments": [
                            "/bin/zsh",
                            "-lc",
                            f"cd {project_root}-old && echo {private_marker}",
                        ],
                        "WorkingDirectory": str(project_root),
                        "EnvironmentVariables": {
                            "UNRELATED_ROOT": str(project_root),
                            "ACTANARA_DASHBOARD_PROJECT_ROOT": str(project_root),
                        },
                    },
                    handle,
                )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            with patch.object(Path, "home", return_value=launch_home):
                payload = actanara_settings_status(paths)

        runtime_source = payload["runtimeSource"]
        by_label = {item["label"]: item for item in runtime_source["launchAgents"]}
        self.assertFalse(by_label["com.actanara.dashboard"]["aligned"])
        self.assertEqual(by_label["com.actanara.dashboard"]["status"], "mismatch")
        self.assertNotIn(private_marker, json.dumps(runtime_source))
        self.assertNotIn(str(project_root), json.dumps(runtime_source))

    def test_actanara_settings_status_warns_when_launchagent_points_at_stale_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            source_root = root / "source-checkout"
            project_root = root / "runtime-source"
            stale_root = root / "old-runtime-source"
            launch_home = root / "launch-home"
            source_root.mkdir()
            project_root.mkdir()
            stale_root.mkdir()
            launch_agents = launch_home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            with (launch_agents / "com.actanara.dashboard.plist").open("wb") as handle:
                plistlib.dump({"ProgramArguments": ["/bin/zsh", "-lc", f"cd {stale_root} && python3 -m uvicorn app.main:app"]}, handle)
            with (launch_agents / "com.actanara.dashboard.watchdog.plist").open("wb") as handle:
                plistlib.dump({"ProgramArguments": ["python3", str(project_root / "advanced" / "dashboard" / "dashboard_launch_agent.py"), "check"]}, handle)
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sourceRoot": str(source_root),
                        "deployedSourceRoot": str(project_root),
                        "copiedAt": "2026-06-24T00:00:00+08:00",
                        "pyprojectVersion": "0.0.0",
                        "git": {"commit": "abc123", "branch": "main", "remote": "origin", "dirty": False},
                    }
                ),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            def fake_git(_root, *args):
                if args == ("rev-parse", "HEAD"):
                    return "abc123"
                if args == ("status", "--porcelain"):
                    return ""
                raise AssertionError(args)

            with (
                patch("data_foundation.settings_status._git_value", side_effect=fake_git),
                patch.object(Path, "home", return_value=launch_home),
            ):
                payload = actanara_settings_status(paths)

        self.assertEqual(payload["runtimeSource"]["status"], "fresh")
        self.assertEqual(payload["runtimeSource"]["launchAgentMismatches"][0]["label"], "com.actanara.dashboard")
        self.assertIn("actanara dashboard restart", payload["runtimeSource"]["postSyncReloadCommand"])
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(by_id["runtime-source-launchagent-alignment"]["status"], "warn")

    def test_actanara_settings_status_recommends_source_only_sync_for_stale_runtime_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            source_root = root / "source-checkout"
            project_root = root / "runtime-source"
            source_root.mkdir()
            (source_root / "install").mkdir()
            (source_root / "install" / "install.sh").write_text("#!/usr/bin/env zsh\n", encoding="utf-8")
            project_root.mkdir()
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sourceRoot": str(source_root),
                        "deployedSourceRoot": str(project_root),
                        "copiedAt": "2026-06-24T00:00:00+08:00",
                        "pyprojectVersion": "0.0.0",
                        "git": {"commit": "abc123", "branch": "main", "remote": "origin", "dirty": False},
                    }
                ),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            def fake_git(_root, *args):
                if args == ("rev-parse", "HEAD"):
                    return "def456"
                if args == ("status", "--porcelain"):
                    return ""
                raise AssertionError(args)

            with patch("data_foundation.settings_status._git_value", side_effect=fake_git):
                payload = actanara_settings_status(paths)
                text = format_actanara_settings_status(payload)

        runtime_source = payload["runtimeSource"]
        self.assertEqual(runtime_source["status"], "stale")
        self.assertTrue(runtime_source["stale"])
        self.assertEqual(runtime_source["staleReasons"], ["source-commit-mismatch"])
        actions = {item["id"]: item for item in runtime_source["recommendedActions"]}
        self.assertIn("sync-runtime-source", actions)
        self.assertIn("upgrade-runtime", actions)
        self.assertIn("--source-only", actions["sync-runtime-source"]["command"])
        self.assertIn("--runtime <runtime-home>", actions["sync-runtime-source"]["command"])
        self.assertIn("--source-root <source-root>", actions["sync-runtime-source"]["command"])
        self.assertNotIn(str(source_root), json.dumps(runtime_source))
        self.assertIn("Next step", text)
        self.assertIn("--source-only", text)
        self.assertNotIn("Runtime source action", text)
        self.assertNotIn(str(source_root), text)
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(by_id["runtime-source-provenance"]["status"], "warn")

    def test_actanara_settings_status_warns_when_expected_service_launchagent_is_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            project_root = root / "runtime-source"
            project_root.mkdir()
            launch_home = root / "launch-home"
            launch_agents = launch_home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            with (launch_agents / "com.actanara.rag-server.plist").open("wb") as handle:
                plistlib.dump({"ProgramArguments": ["python3", str(project_root / "advanced" / "dashboard" / "rag_server_launch_agent.py"), "start"]}, handle)
            write_settings(
                {
                    "features": {"dashboard": True, "rag": True},
                    "dashboard": {
                        "projectRoot": str(project_root),
                        "server": {"enabled": True},
                    },
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "server": {"enabled": True},
                    },
                },
                paths,
            )

            with patch.object(Path, "home", return_value=launch_home):
                payload = actanara_settings_status(paths)

        services = {item["id"]: item for item in payload["serviceRegistration"]["services"]}
        self.assertEqual(services["dashboard"]["status"], "not-registered")
        self.assertEqual(services["rag-server"]["status"], "plist-present-audit-missing")
        self.assertIn("settings registration audit is missing", services["rag-server"]["message"])
        self.assertTrue(services["rag-server"]["plistsPresent"])
        launch_agents_by_label = {item["label"]: item for item in payload["runtimeSource"]["launchAgents"]}
        self.assertTrue(launch_agents_by_label["com.actanara.rag-server"]["exists"])
        self.assertTrue(launch_agents_by_label["com.actanara.rag-server"]["aligned"])
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(by_id["launchagent-registration:dashboard"]["status"], "warn")
        self.assertEqual(by_id["launchagent-registration:rag-server"]["status"], "warn")

    def test_actanara_settings_status_skips_dashboard_launchagent_check_when_server_opted_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "features": {"dashboard": True, "rag": False},
                    "dashboard": {"server": {"enabled": False}},
                    "rag": {"enabled": False, "server": {"enabled": False}},
                },
                paths,
            )

            payload = actanara_settings_status(paths)

        services = {item["id"]: item for item in payload["serviceRegistration"]["services"]}
        self.assertEqual(services["dashboard"]["status"], "not-expected")
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertNotIn("launchagent-registration:dashboard", by_id)
        self.assertNotIn("launchagent-registration:rag-server", by_id)

    def test_runtime_source_dirty_checkout_warns_without_marking_runtime_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            source_root = root / "source-checkout"
            project_root = root / "runtime-source"
            source_root.mkdir()
            project_root.mkdir()
            (project_root / ".actanara-runtime-source.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sourceRoot": str(source_root),
                        "deployedSourceRoot": str(project_root),
                        "copiedAt": "2026-06-24T00:00:00+08:00",
                        "pyprojectVersion": "0.0.0",
                        "git": {"commit": "abc123", "branch": "main", "remote": "origin", "dirty": False},
                    }
                ),
                encoding="utf-8",
            )
            write_settings({"dashboard": {"projectRoot": str(project_root)}}, paths)

            def fake_git(_root, *args):
                if args == ("rev-parse", "HEAD"):
                    return "abc123"
                if args == ("status", "--porcelain"):
                    return " M src/example.py"
                raise AssertionError(args)

            with patch("data_foundation.settings_status._git_value", side_effect=fake_git):
                payload = actanara_settings_status(paths)

        self.assertEqual(payload["runtimeSource"]["status"], "fresh")
        self.assertFalse(payload["runtimeSource"]["stale"])
        self.assertTrue(payload["runtimeSource"]["sourceCheckoutDirty"])
        by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(by_id["runtime-source-provenance"]["status"], "ok")
        self.assertEqual(by_id["runtime-source-checkout-dirty"]["status"], "warn")

    def test_dependency_profiles_are_readonly_and_grouped_for_onboarding(self):
        status = dependency_profiles_status()
        by_id = {profile["id"]: profile for profile in status["profiles"]}

        self.assertTrue(status["readOnly"])
        self.assertEqual(status["schemaVersion"], 1)
        self.assertIn("core-foundation", by_id)
        self.assertIn("dashboard", by_id)
        self.assertIn("rag-local", by_id)
        self.assertIn("scheduler-macos", by_id)
        self.assertIn("scheduler-linux", by_id)
        self.assertIn("dev-test", by_id)
        self.assertEqual(by_id["core-foundation"]["status"], "ready")
        self.assertIn("python3", {item["name"] for item in by_id["core-foundation"]["checks"]})
        rag_local_checks = {item["name"] for item in by_id["rag-local"]["checks"]}
        self.assertEqual(by_id["rag-local"]["label"], "nova-RAG Local Runtime")
        self.assertTrue(
            {
                "sentence_transformers",
                "torch",
                "numpy",
                "fastapi",
                "uvicorn",
                "pydantic",
            }.issubset(rag_local_checks)
        )
        optional = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
        rag_local_packages = {item.split(">=", 1)[0].split("<", 1)[0] for item in optional["rag-local"]}
        self.assertTrue({"sentence-transformers", "torch", "numpy", "fastapi", "uvicorn", "pydantic"}.issubset(rag_local_packages))

    def test_actanara_onboarding_status_aggregates_readonly_new_user_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "03:10",
                        "dashboardAggregationTime": "03:40",
                        "systemTimer": {"provider": "cron", "label": "nova.test"},
                    },
                    "rag": {"mode": "disabled"},
                },
                paths,
            )

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                payload = actanara_onboarding_status(paths)
            text = format_actanara_onboarding_status(payload)
            raw_json = dump_actanara_onboarding_status_json(payload)

        self.assertTrue(payload["readOnly"])
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["profileModel"], "product-v2")
        self.assertEqual(payload["runtime"]["actanaraHome"], str(paths.home))
        self.assertEqual(payload["scheduler"]["provider"], "cron")
        self.assertFalse(payload["scheduler"]["registrationImplemented"])
        self.assertEqual(payload["selectedDependencyProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertEqual(
            {profile["id"] for profile in payload["dependencyProfiles"]["profiles"]},
            {"core-foundation", "dashboard"},
        )
        self.assertNotIn("rag-provider", {item["id"] for item in payload["requiredInputs"]})
        self.assertIn("dependencyProfiles", payload)
        self.assertIn("dependencyGroups", payload)
        self.assertIn("requirementSets", payload)
        self.assertIn("packagingPlan", payload)
        self.assertIn("resourceProfile", payload)
        self.assertIn("rag", payload)
        self.assertIn("scheduler-preview", {check["id"] for check in payload["readiness"]["checks"]})
        groups = {item["id"]: item for item in payload["dependencyGroups"]}
        self.assertTrue(groups["actanara"]["selected"])
        self.assertTrue(groups["dashboard"]["selected"])
        self.assertFalse(groups["nova-rag"]["selected"])
        self.assertFalse(groups["dev-test"]["selected"])
        self.assertIn("rag-provider", groups["nova-rag"]["providerInputs"])
        requirement_sets = {item["id"]: item for item in payload["requirementSets"]}
        dependency_profiles = {item["id"]: item for item in payload["dependencyProfiles"]["profiles"]}
        self.assertEqual(requirement_sets["actanara-core"]["status"], "pending-input")
        self.assertEqual(requirement_sets["dashboard"]["status"], dependency_profiles["dashboard"]["status"])
        self.assertEqual(requirement_sets["rag-provider-derived"]["status"], "not-selected")
        self.assertEqual(requirement_sets["nova-task"]["status"], "planned")
        self.assertEqual(requirement_sets["dev-test"]["status"], "not-selected")
        self.assertIn("llm-provider", requirement_sets["actanara-core"]["pendingInputs"])
        self.assertNotIn("rag-provider", requirement_sets["rag-provider-derived"]["pendingInputs"])
        packaging_plan = payload["packagingPlan"]
        self.assertTrue(packaging_plan["readOnly"])
        self.assertFalse(packaging_plan["installsDependencies"])
        self.assertEqual(packaging_plan["packageManager"], "undecided")
        self.assertFalse(packaging_plan["schedulerIncluded"])
        packaging_groups = {item["id"]: item for item in packaging_plan["groups"]}
        self.assertTrue(packaging_groups["base"]["selected"])
        self.assertTrue(packaging_groups["dashboard"]["selected"])
        self.assertFalse(packaging_groups["nova-rag-local"]["selected"])
        self.assertFalse(packaging_groups["nova-rag-cloud"]["selected"])
        self.assertEqual(packaging_groups["nova-rag-local"]["status"], "not-selected")
        self.assertEqual(packaging_groups["nova-rag-cloud"]["dependencySource"], "provider-derived")
        self.assertEqual(packaging_plan["summary"]["pendingProviderDerivedGroups"], 0)
        self.assertIn("Actanara · Setup status", text)
        self.assertIn("Features", text)
        self.assertIn("Checks", text)
        self.assertNotIn("Requirement sets", text)
        self.assertNotIn("Packaging", text)
        self.assertEqual(json.loads(raw_json)["scheduler"]["provider"], "cron")

    def test_actanara_onboarding_status_filters_dependency_checks_by_selected_profiles(self):
        real_find_spec = importlib.util.find_spec

        def missing_dashboard_dependencies(name: str, *args, **kwargs):
            if name in {"fastapi", "uvicorn"}:
                return None
            return real_find_spec(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch("data_foundation.dependency_profiles.importlib.util.find_spec", side_effect=missing_dashboard_dependencies):
                payload = actanara_onboarding_status(paths, selected_profiles=["nova-rag"])

        self.assertEqual(payload["selectedDependencyProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        self.assertEqual(
            {profile["id"] for profile in payload["dependencyProfiles"]["profiles"]},
            {"core-foundation", "dashboard"},
        )
        groups = {item["id"]: item for item in payload["dependencyGroups"]}
        self.assertTrue(groups["actanara"]["selected"])
        self.assertTrue(groups["nova-rag"]["selected"])
        self.assertTrue(groups["dashboard"]["selected"])
        requirement_sets = {item["id"]: item for item in payload["requirementSets"]}
        self.assertTrue(requirement_sets["dashboard"]["selected"])
        self.assertEqual(requirement_sets["dashboard"]["status"], "missing-required")
        self.assertTrue(requirement_sets["rag-provider-derived"]["selected"])
        self.assertIn("rag-provider", {item["id"] for item in payload["requiredInputs"]})

    def test_actanara_onboarding_status_derives_ready_inputs_from_candidate_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "provider": "custom",
                    "endpoint": "https://llm.invalid",
                    "model": "daily-model",
                    "apiKey": "test-secret",
                },
                paths,
            )

            payload = actanara_onboarding_status(paths, selected_profiles=["actanara"])

        inputs = {item["id"]: item for item in payload["requiredInputs"]}
        self.assertEqual(inputs["output-path"]["status"], "ready")
        self.assertEqual(inputs["llm-provider"]["status"], "ready")
        self.assertEqual(inputs["llm-api-key"]["status"], "ready")
        requirement_sets = {item["id"]: item for item in payload["requirementSets"]}
        self.assertNotIn("output-path", requirement_sets["actanara-core"]["pendingInputs"])
        self.assertNotIn("llm-provider", requirement_sets["actanara-core"]["pendingInputs"])
        self.assertNotIn("llm-api-key", requirement_sets["actanara-core"]["pendingInputs"])

    def test_onboarding_subsystem_plan_is_readonly_and_profile_selectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "systemTimer": {"provider": "systemd", "label": "nova.test"},
                    }
                },
                paths,
            )

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                payload = onboarding_subsystem_plan(["nova-rag", "nova-task"], paths)
            text = format_onboarding_subsystem_plan(payload)
            raw_json = dump_onboarding_subsystem_plan_json(payload)

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["planOnly"])
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["profileModel"], "product-v2")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        profile_ids = {profile["id"] for profile in payload["dependencyProfiles"]["profiles"]}
        self.assertEqual(profile_ids, {"core-foundation", "dashboard"})
        action_ids = {action["id"] for action in payload["actions"]}
        self.assertIn("select-rag-provider", action_ids)
        self.assertIn("enable-nova-task-authority", action_ids)
        self.assertIn("linux-scheduler-registration-blocked", action_ids)
        self.assertTrue(all(action["executesShell"] is False for action in payload["actions"]))
        self.assertEqual(payload["scheduler"]["provider"], "systemd")
        self.assertEqual(payload["scheduler"]["selectionModel"], "derived-from-platform-and-scheduled-run-choice")
        self.assertIn("llm-provider", {item["id"] for item in payload["requiredInputs"]})
        groups = {item["id"]: item for item in payload["dependencyGroups"]}
        self.assertTrue(groups["actanara"]["selected"])
        self.assertTrue(groups["nova-rag"]["selected"])
        self.assertTrue(groups["nova-task"]["selected"])
        self.assertTrue(groups["dashboard"]["selected"])
        self.assertIn("Actanara · Setup preview", text)
        self.assertIn("Features", text)
        self.assertIn("What Actanara will do", text)
        self.assertIn('"selectedProfiles"', raw_json)
        self.assertNotIn("待确认", text)
        self.assertEqual(groups["nova-rag"]["requirementSets"], ["rag-provider-derived"])
        requirement_sets = {item["id"]: item for item in payload["requirementSets"]}
        self.assertTrue(requirement_sets["actanara-core"]["selected"])
        self.assertTrue(requirement_sets["rag-provider-derived"]["selected"])
        self.assertTrue(requirement_sets["nova-task"]["selected"])
        self.assertTrue(requirement_sets["dashboard"]["selected"])
        self.assertEqual(requirement_sets["rag-provider-derived"]["pendingInputs"], ["rag-provider", "rag-embedding-model"])
        packaging_groups = {item["id"]: item for item in payload["packagingPlan"]["groups"]}
        self.assertTrue(packaging_groups["base"]["selected"])
        self.assertTrue(packaging_groups["dashboard"]["selected"])
        self.assertFalse(packaging_groups["nova-rag-local"]["selected"])
        self.assertTrue(packaging_groups["nova-rag-local"]["profileSelected"])
        self.assertEqual(packaging_groups["nova-rag-local"]["currentDetection"], "compatibility-detected-only")
        self.assertEqual(packaging_groups["nova-rag-local"]["installIntent"], "nova-rag-local")
        self.assertEqual(packaging_groups["nova-rag-local"]["pyprojectExtra"], "rag-local")
        self.assertEqual(packaging_groups["nova-rag-cloud"]["currentDetection"], "not-detected-today")
        self.assertTrue(packaging_groups["nova-task"]["selected"])
        self.assertEqual(payload["packagingPlan"]["summary"]["groups"], 3)
        self.assertEqual(payload["packagingPlan"]["summary"]["pendingProviderDerivedGroups"], 2)
        self.assertEqual(payload["summary"]["requirementSets"], 4)
        self.assertEqual(payload["summary"]["packagingGroups"], 3)
        self.assertEqual(payload["summary"]["pendingRequirementSets"], 2)
        self.assertIn("Actanara · Setup preview", text)
        self.assertIn("Features", text)
        self.assertIn("What Actanara will do", text)
        self.assertNotIn("Requirement sets", text)
        self.assertNotIn("Packaging", text)
        self.assertEqual(json.loads(raw_json)["selectedProfiles"][0], "actanara")

    def test_onboarding_subsystem_plan_rejects_legacy_profile_aliases(self):
        with self.assertRaisesRegex(ValueError, "unknown onboarding profile"):
            onboarding_subsystem_plan(["rag-local", "scheduler-linux"])

    def test_onboarding_subsystem_plan_defaults_to_product_profiles(self):
        payload = onboarding_subsystem_plan()

        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertIn("llm-provider", {item["id"] for item in payload["requiredInputs"]})
        self.assertNotIn("rag-provider", {item["id"] for item in payload["requiredInputs"]})
        selected_groups = [item["id"] for item in payload["dependencyGroups"] if item["selected"]]
        self.assertEqual(selected_groups, ["actanara", "dashboard", "nova-task"])
        self.assertEqual(payload["summary"]["dependencyGroups"], 3)
        selected_requirement_sets = [item["id"] for item in payload["requirementSets"] if item["selected"]]
        self.assertEqual(selected_requirement_sets, ["actanara-core", "dashboard", "nova-task"])
        self.assertEqual(payload["summary"]["requirementSets"], 3)
        selected_packaging_groups = [item["id"] for item in payload["packagingPlan"]["groups"] if item["selected"]]
        self.assertEqual(selected_packaging_groups, ["base", "dashboard", "nova-task"])
        self.assertEqual(payload["summary"]["packagingGroups"], 3)
        self.assertEqual(payload["packagingPlan"]["summary"]["pendingProviderDerivedGroups"], 0)

    def test_pyproject_is_installer_v2_dependency_manifest_authority(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        optional = project["optional-dependencies"]

        self.assertEqual(project["name"], "actanara")
        project_dependencies = {item.split(">=", 1)[0].split("<", 1)[0] for item in project["dependencies"]}
        self.assertNotIn("python-dotenv", project_dependencies)
        self.assertIn("dashboard", optional)
        self.assertIn("rag-local", optional)
        self.assertIn("dev-test", optional)
        self.assertIn("fastapi", {item.split(">=", 1)[0].split("<", 1)[0] for item in optional["dashboard"]})
        self.assertIn("uvicorn", {item.split(">=", 1)[0].split("<", 1)[0] for item in optional["dashboard"]})
        self.assertIn("sentence-transformers", {item.split(">=", 1)[0].split("<", 1)[0] for item in optional["rag-local"]})
        self.assertIn("torch", {item.split(">=", 1)[0].split("<", 1)[0] for item in optional["rag-local"]})

    def test_installer_v2_contract_defaults_to_base_dashboard_and_keeps_rag_local_opt_in(self):
        payload = onboarding_one_liner_dry_run()
        installer = payload["installerV2Plan"]

        self.assertTrue(installer["readOnly"])
        self.assertTrue(installer["contractOnly"])
        self.assertTrue(installer["installerImplemented"])
        self.assertEqual(installer["manifestAuthority"]["path"], "pyproject.toml")
        self.assertEqual(installer["manifestAuthority"]["defaultInstallSpec"], ".[dashboard]")
        self.assertEqual(installer["manifestAuthority"]["installIntentVocabulary"], "packagingPlan.groups[].id")
        self.assertEqual(installer["manifestAuthority"]["pyprojectExtraByInstallIntent"]["nova-rag-local"], "rag-local")
        self.assertEqual(installer["defaultInstallGroups"], ["base", "dashboard", "nova-task"])
        self.assertEqual(installer["defaultSelectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertEqual(installer["ordinaryWizardChoiceGroups"], ["nova-rag"])
        self.assertEqual(installer["fixedWizardGroups"], ["base", "dashboard", "nova-task"])
        self.assertEqual(installer["advancedCliOnlyGroups"], ["dev-test"])
        self.assertIn("nova-rag-local", installer["optInGroups"])
        self.assertIn("rag-local", installer["legacyPyprojectOptInExtras"])
        self.assertTrue(installer["heavyLocalRagOptIn"])
        self.assertTrue(installer["dependencyInstallation"]["allowedByDecision"])
        self.assertTrue(installer["dependencyInstallation"]["implementedInCurrentPhase"])
        self.assertTrue(installer["dependencyInstallation"]["invokesPipInCurrentPhase"])
        self.assertFalse(installer["dependencyInstallation"]["invokesGitInCurrentPhase"])
        self.assertTrue(installer["dashboardServer"]["defaultEnabled"])
        self.assertTrue(installer["dashboardServer"]["enabled"])
        self.assertTrue(installer["dashboardServer"]["serviceStartImplementedInCurrentPhase"])
        self.assertIn("realtime-overview", installer["dashboardServer"]["requiredForFeatures"])
        self.assertIn("task-board-ui", installer["dashboardServer"]["requiredForFeatures"])
        self.assertTrue(installer["dashboardServer"]["novaTaskUnaffected"])
        self.assertEqual(installer["runtime"]["installTarget"], "~/.actanara")
        self.assertEqual(installer["runtime"]["activeRuntimePointer"], "~/.config/actanara/location.json")

    def test_installer_v2_scheduler_defaults_on_macos_and_supports_no_scheduler_opt_out(self):
        default_payload = installer_v2_contract(["dashboard"], platform_system="Darwin")
        opt_out_payload = installer_v2_contract(["dashboard"], scheduler_opt_out=True, platform_system="Darwin")
        linux_payload = installer_v2_contract(["dashboard"], platform_system="Linux")

        self.assertTrue(default_payload["scheduler"]["defaultEnabled"])
        self.assertFalse(default_payload["scheduler"]["optOutApplied"])
        self.assertEqual(default_payload["scheduler"]["optOutFlag"], "--no-scheduler")
        self.assertTrue(default_payload["scheduler"]["managedLabelsOnly"])
        self.assertEqual(default_payload["scheduler"]["managedLabelPrefix"], "actanara.daily.")
        self.assertFalse(default_payload["scheduler"]["writesLaunchAgentsInCurrentPhase"])
        self.assertFalse(default_payload["scheduler"]["callsLaunchctlInCurrentPhase"])
        self.assertFalse(opt_out_payload["scheduler"]["defaultEnabled"])
        self.assertTrue(opt_out_payload["scheduler"]["optOutApplied"])
        self.assertFalse(linux_payload["scheduler"]["defaultEnabled"])
        self.assertEqual(linux_payload["scheduler"]["unsupportedPlatformBehavior"], "skip-scheduler-registration")

    def test_installer_v2_dashboard_server_opt_out_does_not_disable_nova_task(self):
        payload = installer_v2_contract(["dashboard"], dashboard_server_enabled=False)
        dashboard_server = payload["dashboardServer"]

        self.assertFalse(dashboard_server["enabled"])
        self.assertEqual(dashboard_server["optOutFlag"], "--no-dashboard-server")
        self.assertIn("realtime overview", dashboard_server["disabledImpact"])
        self.assertIn("task board", dashboard_server["disabledImpact"])
        self.assertTrue(dashboard_server["novaTaskUnaffected"])
        self.assertEqual(dashboard_server["novaTaskAuthority"], "data_foundation.nova_task")

    def test_installer_v2_rag_embedding_server_deploys_after_installer_without_blocking(self):
        payload = installer_v2_contract(["dashboard", "nova-rag"], rag_enabled=True, deploy_embedding_server=True)
        rag = payload["rag"]

        self.assertTrue(rag["enabled"])
        self.assertTrue(rag["deployEmbeddingServerSelected"])
        self.assertEqual(rag["deploymentMode"], "background-after-installer")
        self.assertFalse(rag["blocksInstaller"])
        self.assertEqual(rag["expectedDuration"], "long-running")
        self.assertTrue(rag["requiresRagLocalExtra"])
        self.assertEqual(rag["installIntent"], "nova-rag-local")
        self.assertEqual(rag["pyprojectExtra"], "rag-local")
        self.assertEqual(rag["installSpec"], ".[rag-local]")

    def test_onboarding_one_liner_dry_run_reports_v1_apply_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            payload = onboarding_one_liner_dry_run(["dashboard", "nova-rag"], paths)
        text = format_onboarding_one_liner_dry_run(payload)
        raw_json = dump_onboarding_one_liner_dry_run_json(payload)

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["dryRunOnly"])
        self.assertEqual(payload["defaultRuntimeTarget"]["id"], "user-home-dot-actanara")
        self.assertTrue(payload["defaultRuntimeTarget"]["path"].endswith(".actanara"))
        self.assertTrue(payload["defaultRuntimeTarget"]["requiresExplicitUseDefaultRuntimeFlag"])
        self.assertEqual(payload["oneLinerState"], "v1-apply-ready")
        self.assertIn("Actanara · Setup preview", text)
        self.assertIn("Next step", text)
        self.assertIn("actanara onboarding runtime-apply", text)
        self.assertNotIn("Command draft", text)
        self.assertIn('"selectedProfiles"', raw_json)
        self.assertNotIn("生成历史数据", text)
        self.assertEqual(payload["applyState"], "runtime-bootstrap-apply-implemented")
        self.assertTrue(payload["applyImplemented"])
        self.assertFalse(payload["installerImplemented"])
        self.assertEqual(payload["profileModel"], "product-v2")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        self.assertTrue(payload["commandDraft"]["copyPasteReady"])
        self.assertFalse(payload["commandDraft"]["executesShell"])
        self.assertIn("runtime-apply", payload["commandDraft"]["argv"])
        self.assertIn("--language", payload["commandDraft"]["argv"])
        self.assertIn("zh-CN", payload["commandDraft"]["argv"])
        self.assertEqual(payload["commandDraft"]["argv"], payload["oneLinerV1Command"]["argv"])
        self.assertTrue(payload["blockedApplyCommand"]["implemented"])
        self.assertTrue(payload["blockedApplyCommand"]["blocked"])
        self.assertEqual(payload["blockedApplyCommand"]["exitCode"], 1)
        self.assertIn("onboarding apply", payload["blockedApplyCommand"]["display"])
        self.assertTrue(payload["blockedApplyCommand"]["writeContractIncluded"])
        self.assertFalse(payload["executionPolicy"]["allowed"])
        self.assertFalse(payload["executionPolicy"]["writesSettings"])
        self.assertFalse(payload["executionPolicy"]["installsDependencies"])
        self.assertFalse(payload["executionPolicy"]["registersScheduler"])
        self.assertFalse(payload["executionPolicy"]["changesRagAuthority"])
        self.assertFalse(payload["executionPolicy"]["changesNovaTaskAuthority"])
        self.assertTrue(payload["executionPolicy"]["oneLinerApplyImplemented"])
        self.assertIn("runtime-apply", payload["executionPolicy"]["oneLinerApplyCommand"])
        self.assertTrue(payload["safetyPolicy"]["dryRunFirst"])
        self.assertTrue(payload["safetyPolicy"]["exactConfirmationRequired"])
        self.assertFalse(payload["safetyPolicy"]["nonInteractiveYesAllowed"])
        self.assertFalse(payload["safetyPolicy"]["writesSettings"])
        self.assertFalse(payload["safetyPolicy"]["registersScheduler"])
        self.assertFalse(payload["safetyPolicy"]["installsDependencies"])
        self.assertFalse(payload["executionPolicy"]["safetyPolicy"]["registersScheduler"])
        self.assertEqual(payload["schedulerPlan"]["platformTarget"], "macos-first")
        self.assertEqual(payload["schedulerPlan"]["provider"], "launchd-user")
        self.assertFalse(payload["schedulerPlan"]["applyImplemented"])
        self.assertTrue(payload["schedulerPlan"]["registrationPlanned"])
        self.assertTrue(payload["schedulerPlan"]["dryRunOnly"])
        self.assertEqual(payload["schedulerPlan"]["confirmationPhrase"], "REGISTER ACTANARA SCHEDULER")
        self.assertTrue(payload["schedulerPlan"]["auditRequired"])
        self.assertTrue(payload["schedulerPlan"]["rollbackRequired"])
        self.assertTrue(payload["schedulerPlan"]["managedPlistSerializationReady"])
        self.assertFalse(payload["schedulerPlan"]["wouldWriteManagedPlists"])
        self.assertFalse(payload["schedulerPlan"]["wouldCallLaunchctl"])
        self.assertTrue(payload["schedulerPlan"]["managedPlists"])
        self.assertTrue(payload["schedulerPlan"]["jobs"])
        scheduler_job = payload["schedulerPlan"]["jobs"][0]
        self.assertEqual(scheduler_job["label"], payload["schedulerPlan"]["labelPreview"])
        self.assertEqual(scheduler_job["plistPath"], payload["schedulerPlan"]["plistPathPreview"])
        self.assertIn("run_daily_pipeline.py", " ".join(scheduler_job["programArguments"]))
        self.assertEqual(scheduler_job["startCalendarInterval"], {"Hour": 4, "Minute": 0})
        self.assertTrue(scheduler_job["stdoutPath"].endswith(".out.log"))
        self.assertTrue(scheduler_job["stderrPath"].endswith(".err.log"))
        self.assertTrue(scheduler_job["dryRunOnly"])
        self.assertFalse(scheduler_job["wouldWritePlist"])
        self.assertFalse(scheduler_job["wouldCallLaunchctl"])
        managed_plist = scheduler_job["managedPlist"]
        parsed_plist = plistlib.loads(managed_plist["serializedPlist"].encode("utf-8"))
        self.assertTrue(managed_plist["dryRunOnly"])
        self.assertFalse(managed_plist["wouldWritePlist"])
        self.assertFalse(managed_plist["wouldCallLaunchctl"])
        self.assertEqual(parsed_plist["Label"], scheduler_job["label"])
        self.assertEqual(parsed_plist["ProgramArguments"], scheduler_job["programArguments"])
        self.assertEqual(managed_plist["plistPath"], scheduler_job["plistPath"])
        self.assertEqual(managed_plist["pathPolicy"]["target"], "user-launch-agents-preview")
        self.assertFalse(managed_plist["pathPolicy"]["writesAllowedInCurrentPhase"])
        self.assertTrue(payload["ragReadiness"]["selected"])
        self.assertEqual(payload["ragReadiness"]["providerMode"], "pending")
        self.assertEqual(payload["ragReadiness"]["readinessState"], "rag-provider-pending")
        self.assertEqual(payload["ragReadiness"]["finalSyncPolicy"], "skip-until-provider-ready")
        self.assertIn("apiKeyEnv", payload["ragReadiness"]["cloudConfigFields"])
        self.assertFalse(payload["ragReadiness"]["cloudApiCalls"])
        self.assertFalse(payload["ragReadiness"]["installsLocalDependencies"])
        self.assertEqual(payload["sourceBoundaryApprovals"]["phase"], 35)
        self.assertEqual(payload["sourceBoundaryApprovals"]["novaTaskAuthority"], "data_foundation.nova_task")
        write_contract = payload["applyWriteContract"]
        self.assertTrue(write_contract["readOnly"])
        self.assertFalse(write_contract["applyImplemented"])
        self.assertFalse(write_contract["writesAllowed"])
        self.assertEqual(write_contract["writePlan"]["allowlistVersion"], "v1-read-only")
        self.assertEqual(write_contract["writePlan"]["confirmationPhrase"], "APPLY ACTANARA ONBOARDING")
        self.assertFalse(write_contract["writePlan"]["productionPathWritesAllowed"])
        self.assertIn("install-dependencies", write_contract["writePlan"]["deniedOperations"])
        write_operations = {item["id"]: item for item in write_contract["writePlan"]["operations"]}
        self.assertIn("create-runtime-home", write_operations)
        self.assertIn("write-runtime-settings", write_operations)
        self.assertIn("write-rag-provider-settings", write_operations)
        self.assertTrue(any(item["category"] == "scheduler-plist" for item in write_operations.values()))
        self.assertTrue(all(item["allowedInCurrentPhase"] is False for item in write_operations.values()))
        self.assertFalse(any(item["writesSecretValues"] for item in write_operations.values()))
        self.assertTrue(write_contract["auditPlan"]["auditRequired"])
        self.assertFalse(write_contract["auditPlan"]["writesAudit"])
        self.assertEqual(write_contract["auditPlan"]["auditPath"], "$ACTANARA_HOME/state/onboarding/onboarding-audit.jsonl")
        self.assertTrue(write_contract["auditPlan"]["redactionPolicy"]["redactSecretValues"])
        self.assertTrue(write_contract["rollbackPlan"]["rollbackRequired"])
        self.assertFalse(write_contract["rollbackPlan"]["rollbackImplemented"])
        self.assertEqual(
            len(write_contract["rollbackPlan"]["operations"]),
            len(write_contract["writePlan"]["operations"]),
        )
        self.assertIn("packagingPlan", payload)
        self.assertFalse(payload["packagingPlan"]["installsDependencies"])
        self.assertEqual(payload["summary"]["packagingGroups"], 3)
        self.assertEqual(payload["summary"]["ragReadinessState"], "rag-provider-pending")
        self.assertEqual(payload["summary"]["schedulerProvider"], "launchd-user")
        self.assertEqual(payload["summary"]["schedulerApprovalState"], "registration-gated")
        self.assertEqual(payload["summary"]["plannedWriteOperations"], len(write_contract["writePlan"]["operations"]))
        self.assertEqual(payload["summary"]["rollbackOperations"], len(write_contract["rollbackPlan"]["operations"]))
        scheduler_approval = payload["schedulerApprovalContract"]
        self.assertEqual(scheduler_approval["status"], "registration-gated")
        self.assertTrue(scheduler_approval["sandboxApplyImplemented"])
        self.assertTrue(scheduler_approval["realLaunchAgentsWriteImplemented"])
        self.assertTrue(scheduler_approval["launchctlImplemented"])
        self.assertTrue(scheduler_approval["allowedCurrentPhase"]["writeRealLaunchAgents"])
        self.assertTrue(scheduler_approval["allowedCurrentPhase"]["callLaunchctl"])
        self.assertTrue(payload["oneLinerV1Command"]["copyPasteReady"])
        self.assertFalse(payload["oneLinerV1Command"]["registersScheduler"])
        self.assertTrue(payload["sourcePlan"]["planOnly"])
        self.assertIn("packagingPlan", payload["sourcePlan"])
        self.assertTrue(all(step["executesShell"] is False for step in payload["dryRunSteps"]))
        self.assertIn("What Actanara will do", format_onboarding_subsystem_plan(payload["sourcePlan"]))
        self.assertNotIn("packagingGroups=", text)
        self.assertIn("Actanara · Setup preview", text)
        self.assertFalse(json.loads(raw_json)["executionPolicy"]["allowed"])

    def test_onboarding_one_liner_dry_run_matches_schema_fixture_contract(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "onboarding" / "runtime-dry-run-contract.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            payload = onboarding_one_liner_dry_run(paths=paths)

        self.assertEqual(payload["schemaVersion"], fixture["schemaVersion"])
        self.assertEqual(payload["profileModel"], fixture["profileModel"])
        self.assertEqual(payload["oneLinerState"], fixture["oneLinerState"])
        self.assertEqual(payload["applyState"], fixture["applyState"])
        self.assertEqual(payload["selectedProfiles"], fixture["selectedProfiles"])
        self.assertEqual([item["id"] for item in payload["requiredInputs"]], fixture["requiredInputs"])
        self.assertEqual([item["id"] for item in payload["dependencyGroups"] if item["selected"]], fixture["dependencyGroups"])
        self.assertEqual([item["id"] for item in payload["requirementSets"] if item["selected"]], fixture["requirementSets"])
        self.assertEqual([item["id"] for item in payload["packagingPlan"]["groups"] if item["selected"]], fixture["packagingPlan"]["selectedGroups"])
        self.assertEqual(
            payload["packagingPlan"]["summary"]["pendingProviderDerivedGroups"],
            fixture["packagingPlan"]["pendingProviderDerivedGroups"],
        )
        self.assertFalse(payload["packagingPlan"]["installsDependencies"])
        self.assertFalse(payload["packagingPlan"]["schedulerIncluded"])
        for key, expected in fixture["installerV2Plan"].items():
            self.assertEqual(payload["installerV2Plan"][key], expected)
        self.assertEqual(payload["commandDraft"]["id"], fixture["commandDraft"]["id"])
        self.assertEqual(payload["commandDraft"]["copyPasteReady"], fixture["commandDraft"]["copyPasteReady"])
        self.assertEqual(payload["commandDraft"]["executesShell"], fixture["commandDraft"]["executesShell"])
        for key, expected in fixture["blockedApplyCommand"].items():
            self.assertEqual(payload["blockedApplyCommand"][key], expected)
        for key, expected in fixture["applyWriteContract"].items():
            self.assertEqual(payload["applyWriteContract"][key], expected)
        for key, expected in fixture["executionPolicy"].items():
            self.assertEqual(payload["executionPolicy"][key], expected)
        for key, expected in fixture["safetyPolicy"].items():
            self.assertEqual(payload["safetyPolicy"][key], expected)
        for key, expected in fixture["schedulerPlan"].items():
            self.assertEqual(payload["schedulerPlan"][key], expected)
        for key, expected in fixture["ragReadiness"].items():
            self.assertEqual(payload["ragReadiness"][key], expected)
        for key, expected in fixture["sourceBoundaryApprovals"].items():
            self.assertEqual(payload["sourceBoundaryApprovals"][key], expected)
        self.assertTrue(all(step["executesShell"] is fixture["dryRunStepContract"]["executesShell"] for step in payload["dryRunSteps"]))
        self.assertTrue(any(step["wouldWrite"] for step in payload["dryRunSteps"]))

    def test_onboarding_one_liner_dry_run_reports_rag_disabled_when_not_selected(self):
        payload = onboarding_one_liner_dry_run(["dashboard"])

        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertFalse(payload["ragReadiness"]["selected"])
        self.assertEqual(payload["ragReadiness"]["providerMode"], "disabled")
        self.assertEqual(payload["ragReadiness"]["readinessState"], "rag-disabled")
        self.assertEqual(payload["ragReadiness"]["finalSyncPolicy"], "skip-disabled")
        self.assertIn("not selected", payload["ragReadiness"]["skipReason"])

    def test_onboarding_apply_write_contract_is_readonly_and_profile_aware(self):
        contract = onboarding_apply_write_contract(["nova-task"])

        self.assertEqual(contract["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertTrue(contract["readOnly"])
        self.assertFalse(contract["writesAllowed"])
        self.assertFalse(contract["applyImplemented"])
        operations = {item["id"]: item for item in contract["writePlan"]["operations"]}
        self.assertIn("create-runtime-home", operations)
        self.assertIn("write-runtime-settings", operations)
        self.assertIn("initialize-nova-task-state", operations)
        self.assertNotIn("write-rag-provider-settings", operations)
        self.assertEqual(contract["writePlan"]["summary"]["schedulerWrites"], 0)
        self.assertEqual(contract["auditPlan"]["operationIds"], list(operations))
        self.assertEqual(contract["rollbackPlan"]["summary"]["operations"], len(operations))

    def test_onboarding_apply_preflight_checks_confirmation_and_pending_inputs_without_writes(self):
        missing_confirmation = onboarding_apply_preflight(["nova-rag"])

        self.assertTrue(missing_confirmation["readOnly"])
        self.assertTrue(missing_confirmation["preflightOnly"])
        self.assertFalse(missing_confirmation["applyImplemented"])
        self.assertFalse(missing_confirmation["allowedToApply"])
        self.assertFalse(missing_confirmation["confirmationProvided"])
        self.assertFalse(missing_confirmation["confirmationAccepted"])
        self.assertIn("apply-implementation-blocked", missing_confirmation["blockingReasons"])
        self.assertIn("exact-confirmation", missing_confirmation["blockingReasons"])
        self.assertIn("required-inputs-ready", missing_confirmation["blockingReasons"])
        self.assertIn("llm-provider", missing_confirmation["pendingRequiredInputs"])
        self.assertIn("rag-provider", missing_confirmation["pendingRequiredInputs"])

        wrong_confirmation = onboarding_apply_preflight(["nova-rag"], confirmation_text="yes")
        self.assertTrue(wrong_confirmation["confirmationProvided"])
        self.assertFalse(wrong_confirmation["confirmationAccepted"])
        self.assertIn("exact-confirmation", wrong_confirmation["blockingReasons"])

        accepted_confirmation = onboarding_apply_preflight(
            ["nova-rag"],
            confirmation_text="APPLY ACTANARA ONBOARDING",
        )
        self.assertTrue(accepted_confirmation["confirmationProvided"])
        self.assertTrue(accepted_confirmation["confirmationAccepted"])
        self.assertNotIn("exact-confirmation", accepted_confirmation["blockingReasons"])
        self.assertIn("apply-implementation-blocked", accepted_confirmation["blockingReasons"])
        self.assertIn("required-inputs-ready", accepted_confirmation["blockingReasons"])
        checks = {item["id"]: item for item in accepted_confirmation["checks"]}
        self.assertIn("llm-provider-configured", checks)
        self.assertFalse(checks["llm-provider-configured"]["blocking"])
        self.assertIn("liveProbe", checks["llm-provider-configured"]["details"])
        self.assertTrue(checks["write-contract-readonly"]["passed"])
        self.assertTrue(checks["audit-preview-readonly"]["passed"])
        self.assertTrue(checks["rollback-preview-readonly"]["passed"])
        self.assertTrue(checks["no-side-effects"]["passed"])

    def test_onboarding_apply_preflight_reads_llm_from_candidate_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider(
                {
                    "provider": "custom",
                    "endpoint": "https://llm.invalid",
                    "model": "daily-model",
                    "apiKey": "test-secret",
                },
                paths,
            )

            payload = onboarding_apply_preflight(
                ["actanara"],
                confirmation_text="APPLY ACTANARA ONBOARDING",
                paths=paths,
            )

        self.assertNotIn("required-inputs-ready", payload["blockingReasons"])
        self.assertEqual(payload["pendingRequiredInputs"], [])
        checks = {item["id"]: item for item in payload["checks"]}
        self.assertTrue(checks["llm-provider-configured"]["passed"])
        self.assertTrue(checks["llm-provider-configured"]["details"]["hasApiKey"])
        self.assertEqual(checks["llm-provider-configured"]["details"]["provider"], "custom")

    def test_onboarding_apply_sandbox_rejects_bad_confirmation_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            payload = onboarding_apply_sandbox(["nova-task"], paths, confirmation_text="yes")

            self.assertEqual(payload["status"], "sandbox-rejected")
            self.assertEqual(payload["exitCode"], 1)
            self.assertFalse(runtime.exists())
            self.assertFalse(payload["safetyPolicy"]["writesSettings"])
            self.assertFalse(payload["safetyPolicy"]["writesAudit"])

    def test_onboarding_apply_sandbox_writes_only_under_explicit_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            payload = onboarding_apply_sandbox(
                ["nova-task"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
            )

            settings_path = runtime / "config" / "settings.json"
            audit_path = runtime / "state" / "onboarding" / "onboarding-audit.jsonl"
            rollback_path = runtime / "state" / "onboarding" / "rollback-plan.json"

            self.assertEqual(payload["status"], "sandbox-applied")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(settings_path.exists())
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertFalse(payload["safetyPolicy"]["registersScheduler"])
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])
            self.assertFalse(payload["safetyPolicy"]["installsDependencies"])
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["paths"]["runtime"]["actanaraHome"], str(runtime))
            audit = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(audit["phase"], "onboarding-apply-sandbox")
            self.assertIn("write-runtime-settings", audit["operations"])
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            self.assertTrue(rollback["sandboxOnly"])
            self.assertFalse(rollback["rollbackImplemented"])

    def test_onboarding_apply_runtime_bootstrap_rejects_bad_confirmation_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            payload = onboarding_apply_runtime_bootstrap(["nova-task"], paths, confirmation_text="yes")

            self.assertEqual(payload["status"], "runtime-bootstrap-rejected")
            self.assertEqual(payload["exitCode"], 1)
            self.assertFalse(runtime.exists())
            self.assertFalse(payload["safetyPolicy"]["writesSettings"])
            self.assertFalse(payload["safetyPolicy"]["writesBootstrapLocation"])

    def test_onboarding_apply_runtime_bootstrap_writes_runtime_settings_audit_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            payload = onboarding_apply_runtime_bootstrap(
                ["nova-task"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
            )

            settings_path = runtime / "config" / "settings.json"
            audit_path = runtime / "state" / "onboarding" / "onboarding-audit.jsonl"
            rollback_path = runtime / "state" / "onboarding" / "runtime-bootstrap-rollback-plan.json"
            bootstrap_path = Path(tmp) / "location.json"

            self.assertEqual(payload["status"], "runtime-bootstrap-applied")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(settings_path.exists())
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertFalse(bootstrap_path.exists())
            self.assertFalse(payload["runtime"]["selectedAsActiveRuntime"])
            self.assertFalse(payload["safetyPolicy"]["writesBootstrapLocation"])
            self.assertFalse(payload["safetyPolicy"]["registersScheduler"])
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["paths"]["runtime"]["actanaraHome"], str(runtime))
            audit = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(audit["phase"], "onboarding-runtime-bootstrap")
            self.assertIn("write-runtime-settings", audit["operations"])
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            self.assertTrue(rollback["runtimeBootstrapOnly"])
            self.assertFalse(rollback["rollbackImplemented"])

    def test_onboarding_runtime_bootstrap_can_materialize_english_language_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            payload = onboarding_apply_runtime_bootstrap(
                ["nova-rag"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
                language_profile="en-US",
            )

            settings = json.loads((runtime / "config" / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "runtime-bootstrap-applied")
        self.assertEqual(settings["general"]["locale"], "en-US")
        self.assertEqual(settings["pipeline"]["languageProfile"], "en")
        self.assertTrue(settings["pipeline"]["englishEnabled"])
        self.assertEqual(settings["pipeline"]["diarySchemaVersion"], "diary-v1-en")
        self.assertEqual(settings["pipeline"]["promptPayloadProfile"], "en-US")
        self.assertEqual(settings["rag"]["languageProfile"], "en")

    def test_onboarding_apply_runtime_bootstrap_can_select_active_runtime_with_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            bootstrap = Path(tmp) / "location.json"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=Path(tmp) / "Diary")

            with patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(bootstrap)}, clear=False):
                payload = onboarding_apply_runtime_bootstrap(
                    ["nova-task"],
                    paths,
                    confirmation_text="APPLY ACTANARA ONBOARDING",
                    select_active_runtime=True,
                )

            self.assertEqual(payload["status"], "runtime-bootstrap-applied")
            self.assertTrue(bootstrap.exists())
            pointer = json.loads(bootstrap.read_text(encoding="utf-8"))
            self.assertEqual(pointer["actanaraHome"], str(runtime))
            self.assertTrue(payload["runtime"]["selectedAsActiveRuntime"])
            self.assertEqual(payload["runtime"]["selectionBootstrapPath"], str(bootstrap))
            self.assertTrue(payload["safetyPolicy"]["writesBootstrapLocation"])
            operation_ids = {item["id"] for item in payload["operationResults"]}
            self.assertIn("select-active-runtime", operation_ids)
            audit_path = Path(payload["runtime"]["auditPath"])
            audit = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertTrue(audit["activeRuntimeSelected"])
            rollback_path = Path(payload["runtime"]["rollbackPath"])
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            self.assertTrue(rollback["selectionRollback"]["required"])
            self.assertEqual(rollback["selectionRollback"]["bootstrapPath"], str(bootstrap))

    def test_scheduler_apply_approval_contract_is_plist_write_gated(self):
        dry_run = onboarding_one_liner_dry_run(["dashboard"])
        contract = scheduler_apply_approval_contract(dry_run["schedulerPlan"])

        self.assertTrue(contract["readOnly"])
        self.assertEqual(contract["status"], "registration-gated")
        self.assertTrue(contract["sandboxApplyImplemented"])
        self.assertTrue(contract["plistWriteApplyImplemented"])
        self.assertTrue(contract["registrationImplemented"])
        self.assertTrue(contract["realLaunchAgentsWriteImplemented"])
        self.assertTrue(contract["launchctlImplemented"])
        self.assertTrue(contract["allowedCurrentPhase"]["writeFakeLaunchAgents"])
        self.assertTrue(contract["allowedCurrentPhase"]["writeRealLaunchAgents"])
        self.assertTrue(contract["allowedCurrentPhase"]["registerScheduler"])
        self.assertEqual(contract["plistWriteConfirmationPhrase"], "WRITE ACTANARA LAUNCHAGENTS")
        self.assertEqual(contract["registrationConfirmationPhrase"], "REGISTER ACTANARA SCHEDULER")

    def test_onboarding_apply_scheduler_sandbox_writes_fake_launch_agents_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            fake_home = root / "FakeHome"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_sandbox(
                ["dashboard"],
                paths,
                scheduler_home=fake_home,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

            launch_agents = fake_home / "Library" / "LaunchAgents"
            written = sorted(launch_agents.glob("*.plist"))
            audit_path = Path(payload["runtime"]["auditPath"])
            rollback_path = Path(payload["runtime"]["rollbackPath"])

            self.assertEqual(payload["status"], "scheduler-sandbox-applied")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(written)
            self.assertTrue(all(path.parent == launch_agents for path in written))
            parsed = plistlib.loads(written[0].read_bytes())
            self.assertIn("Label", parsed)
            self.assertFalse(payload["safetyPolicy"]["writesRealLaunchAgents"])
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())

    def test_onboarding_apply_scheduler_sandbox_rejects_bad_confirmation_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            fake_home = root / "FakeHome"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_sandbox(
                ["dashboard"],
                paths,
                scheduler_home=fake_home,
                confirmation_text="yes",
            )

        self.assertEqual(payload["status"], "scheduler-sandbox-rejected")
        self.assertFalse(runtime.exists())
        self.assertFalse((fake_home / "Library" / "LaunchAgents").exists())
        self.assertFalse(payload["safetyPolicy"]["writesFakeLaunchAgents"])

    def test_onboarding_apply_scheduler_plist_write_writes_launch_agents_without_launchctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )

            launch_agents = launch_agent_home / "Library" / "LaunchAgents"
            written = sorted(launch_agents.glob("*.plist"))
            audit_path = Path(payload["runtime"]["auditPath"])
            rollback_path = Path(payload["runtime"]["rollbackPath"])

            self.assertEqual(payload["status"], "scheduler-plist-applied")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(written)
            parsed = plistlib.loads(written[0].read_bytes())
            self.assertIn("Label", parsed)
            self.assertTrue(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["registersScheduler"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            self.assertTrue(rollback["schedulerPlistWriteOnly"])

    def test_onboarding_apply_scheduler_plist_write_backs_up_existing_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")
            existing = launch_agent_home / "Library" / "LaunchAgents" / "actanara.daily.pipeline.plist"
            existing.parent.mkdir(parents=True)
            existing.write_text("old plist\n", encoding="utf-8")

            payload = onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )

            backup_paths = [item.get("backupPath") for item in payload["operationResults"] if item.get("backupPath")]

            self.assertTrue(backup_paths)
            self.assertEqual(Path(backup_paths[0]).read_text(encoding="utf-8"), "old plist\n")
            self.assertNotEqual(existing.read_text(encoding="utf-8"), "old plist\n")

    def test_onboarding_apply_scheduler_plist_write_rejects_bad_confirmation_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

        self.assertEqual(payload["status"], "scheduler-plist-rejected")
        self.assertFalse(runtime.exists())
        self.assertFalse((launch_agent_home / "Library" / "LaunchAgents").exists())
        self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])

    def test_onboarding_apply_scheduler_register_uses_launchctl_runner_for_existing_plists(self):
        class FakeLaunchctlResult:
            returncode = 0
            stdout = "bootstrapped"
            stderr = ""

        commands = []

        def fake_runner(command):
            commands.append(command)
            return FakeLaunchctlResult()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")
            onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )

            payload = onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

            audit_path = Path(payload["runtime"]["auditPath"])
            rollback_path = Path(payload["runtime"]["rollbackPath"])
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            settings = read_settings(paths)
            status = onboarding_one_liner_status(paths)

            self.assertEqual(payload["status"], "scheduler-registered")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(commands)
            self.assertTrue(any(command[:2] == ["launchctl", "bootout"] for command in commands))
            self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in commands))
            self.assertTrue(any(item["id"].startswith("launchctl-bootout-stale:") for item in payload["operationResults"]))
            self.assertTrue(any(item["id"].startswith("launchctl-bootstrap:") for item in payload["operationResults"]))
            self.assertTrue(payload["safetyPolicy"]["callsLaunchctl"])
            self.assertTrue(payload["safetyPolicy"]["registersScheduler"])
            self.assertFalse(payload["safetyPolicy"]["installsDependencies"])
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertTrue(rollback["schedulerRegisterOnly"])
            self.assertTrue(rollback["rollbackImplemented"])
            self.assertTrue(rollback["automaticCompensation"])
            self.assertTrue(rollback["commandPreview"])
            self.assertEqual(payload["handoff"]["status"], "committed")
            self.assertTrue(all("stdout" not in item and "stderr" not in item for item in payload["operationResults"]))
            self.assertTrue(settings["schedule"]["systemTimer"]["registered"])
            self.assertEqual(settings["schedule"]["systemTimer"]["registrationManagedBy"], "one-liner")
            self.assertTrue(settings["schedule"]["systemTimer"]["jobs"])
            self.assertEqual(status["schedulerRegistration"]["status"], "registered")
            self.assertEqual(status["schedulerRegistration"]["registrationManagedBy"], "one-liner")

    def test_onboarding_apply_scheduler_register_reloads_stale_loaded_jobs(self):
        class FakeLaunchctlResult:
            def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        commands = []

        def fake_runner(command):
            commands.append(command)
            if command[1] == "bootout":
                return FakeLaunchctlResult(3, stderr="service not loaded")
            return FakeLaunchctlResult(0, stdout="bootstrapped")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")
            onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )

            payload = onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

        self.assertEqual(payload["status"], "scheduler-registered")
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap", "bootout", "bootstrap"])
        stale_results = [item for item in payload["operationResults"] if item["id"].startswith("launchctl-bootout-stale:")]
        self.assertEqual(len(stale_results), 2)
        self.assertTrue(all(item["status"] == "skipped" for item in stale_results))
        self.assertTrue(all(item["allowFailure"] for item in stale_results))

    def test_onboarding_scheduler_register_second_bootstrap_failure_compensates_both_jobs_and_settings(self):
        class FakeLaunchctlResult:
            def __init__(self, returncode: int):
                self.returncode = returncode
                self.stdout = "sensitive-output-must-not-be-recorded"
                self.stderr = "sensitive-error-must-not-be-recorded"

        bootstrap_calls = 0

        def fake_runner(command):
            nonlocal bootstrap_calls
            if command[1] == "bootstrap":
                bootstrap_calls += 1
                return FakeLaunchctlResult(70 if bootstrap_calls == 2 else 0)
            return FakeLaunchctlResult(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")
            onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )
            plist_paths = sorted((launch_agent_home / "Library" / "LaunchAgents").glob("*.plist"))
            plist_before = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in plist_paths}
            settings_path = paths.config_dir / "settings.json"
            settings_before = settings_path.read_bytes() if settings_path.exists() else None

            payload = onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

            self.assertEqual(payload["status"], "scheduler-register-failed")
            self.assertEqual(settings_path.read_bytes() if settings_path.exists() else None, settings_before)
            for path, (content, mode) in plist_before.items():
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(path.stat().st_mode & 0o777, mode)
            journals = list((paths.state_dir / "scheduler-handoffs").glob("*/journal.json"))
            self.assertEqual(len(journals), 1)
            self.assertEqual(json.loads(journals[0].read_text(encoding="utf-8"))["status"], "compensated")
            operation_text = json.dumps(payload["operationResults"], sort_keys=True)
            self.assertNotIn("sensitive-output", operation_text)
            self.assertNotIn("sensitive-error", operation_text)

    def test_onboarding_apply_scheduler_register_rejects_bad_confirmation_without_launchctl(self):
        calls = []

        def fake_runner(command):
            calls.append(command)
            raise AssertionError("launchctl runner should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launchctl_runner=fake_runner,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )

        self.assertEqual(payload["status"], "scheduler-register-rejected")
        self.assertEqual(calls, [])
        self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_apply_scheduler_register_fails_without_existing_plists(self):
        calls = []

        def fake_runner(command):
            calls.append(command)
            raise AssertionError("launchctl runner should not be called without plists")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

        self.assertEqual(payload["status"], "scheduler-register-failed")
        self.assertIn("managed plist does not exist", payload["reason"])
        self.assertEqual(calls, [])
        self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_apply_scheduler_unregister_uses_launchctl_runner_and_updates_settings(self):
        class FakeLaunchctlResult:
            returncode = 0
            stdout = "ok"
            stderr = ""

        commands = []

        def fake_runner(command):
            commands.append(command)
            return FakeLaunchctlResult()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            launch_agent_home = root / "Home"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")
            onboarding_apply_scheduler_plist_write(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                confirmation_text="WRITE ACTANARA LAUNCHAGENTS",
            )
            onboarding_apply_scheduler_register(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

            payload = onboarding_apply_scheduler_unregister(
                ["dashboard"],
                paths,
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
                confirmation_text="UNREGISTER ACTANARA SCHEDULER",
            )

            settings = read_settings(paths)
            status = onboarding_one_liner_status(paths)
            rollback_path = Path(payload["runtime"]["rollbackPath"])
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["status"], "scheduler-unregistered")
            self.assertEqual(payload["exitCode"], 0)
            self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in commands))
            self.assertTrue(any(command[:2] == ["launchctl", "bootout"] for command in commands))
            self.assertTrue(payload["safetyPolicy"]["callsLaunchctl"])
            self.assertTrue(payload["safetyPolicy"]["unregistersScheduler"])
            self.assertFalse(payload["safetyPolicy"]["installsDependencies"])
            self.assertFalse(settings["schedule"]["systemTimer"]["registered"])
            self.assertEqual(settings["schedule"]["systemTimer"]["registrationManagedBy"], "one-liner")
            self.assertEqual(status["schedulerRegistration"]["status"], "not-registered-by-one-liner")
            self.assertTrue(rollback["schedulerUnregisterOnly"])
            self.assertTrue(rollback["rollbackImplemented"])
            self.assertTrue(rollback["automaticCompensation"])
            self.assertTrue(rollback["commandPreview"])
            self.assertEqual(payload["handoff"]["status"], "committed")
            self.assertFalse(any((launch_agent_home / "Library" / "LaunchAgents").glob("*.plist")))

    def test_onboarding_apply_scheduler_unregister_rejects_bad_confirmation_without_launchctl(self):
        calls = []

        def fake_runner(command):
            calls.append(command)
            raise AssertionError("launchctl runner should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            paths = runtime_paths_for_home(runtime, legacy_diary_root=root / "Diary")

            payload = onboarding_apply_scheduler_unregister(
                ["dashboard"],
                paths,
                launchctl_runner=fake_runner,
                confirmation_text="REGISTER ACTANARA SCHEDULER",
            )

        self.assertEqual(payload["status"], "scheduler-unregister-rejected")
        self.assertEqual(calls, [])
        self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_one_liner_apply_bootstraps_runtime_and_keeps_scheduler_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")

            payload = onboarding_one_liner_apply(
                ["dashboard"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
            )

        self.assertEqual(payload["status"], "one-liner-applied")
        self.assertEqual(payload["exitCode"], 0)
        self.assertEqual(payload["runtimeBootstrap"]["status"], "runtime-bootstrap-applied")
        self.assertFalse(payload["schedulerRegistration"]["registersScheduler"])
        self.assertFalse(payload["schedulerRegistration"]["writesLaunchdPlist"])
        self.assertFalse(payload["schedulerRegistration"]["callsLaunchctl"])
        self.assertFalse(payload["schedulerRegistration"]["requested"])
        self.assertEqual(payload["dependencyInstallation"]["status"], "detect-only")

    def test_onboarding_one_liner_apply_can_materialize_english_language_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")

            payload = onboarding_one_liner_apply(
                ["nova-rag"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
                language_profile="en-US",
            )
            settings = json.loads((paths.home / "config" / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "one-liner-applied")
        self.assertEqual(payload["runtimeBootstrap"]["status"], "runtime-bootstrap-applied")
        self.assertEqual(settings["general"]["locale"], "en-US")
        self.assertEqual(settings["pipeline"]["languageProfile"], "en")
        self.assertTrue(settings["pipeline"]["englishEnabled"])
        self.assertEqual(settings["pipeline"]["diarySchemaVersion"], "diary-v1-en")
        self.assertEqual(settings["pipeline"]["promptPayloadProfile"], "en-US")
        self.assertEqual(settings["rag"]["languageProfile"], "en")

    def test_onboarding_one_liner_apply_with_scheduler_runs_plist_and_register_steps(self):
        class FakeLaunchctlResult:
            returncode = 0
            stdout = "ok"
            stderr = ""

        commands = []

        def fake_runner(command):
            commands.append(command)
            return FakeLaunchctlResult()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")
            launch_agent_home = root / "Home"

            payload = onboarding_one_liner_apply(
                ["dashboard"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
                with_scheduler=True,
                scheduler_confirmation_text="REGISTER ACTANARA SCHEDULER",
                launch_agent_home=launch_agent_home,
                launchctl_runner=fake_runner,
            )

            settings = read_settings(paths)

        self.assertEqual(payload["status"], "one-liner-applied")
        self.assertEqual(payload["exitCode"], 0)
        self.assertEqual(payload["schedulerRegistration"]["status"], "scheduler-registered")
        self.assertTrue(payload["schedulerRegistration"]["requested"])
        self.assertTrue(payload["schedulerRegistration"]["writesLaunchdPlist"])
        self.assertTrue(payload["schedulerRegistration"]["registersScheduler"])
        self.assertTrue(payload["schedulerRegistration"]["callsLaunchctl"])
        self.assertTrue(commands)
        self.assertTrue(settings["schedule"]["systemTimer"]["registered"])

    def test_onboarding_one_liner_apply_with_scheduler_requires_scheduler_confirmation(self):
        calls = []

        def fake_runner(command):
            calls.append(command)
            raise AssertionError("launchctl runner should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")

            payload = onboarding_one_liner_apply(
                ["dashboard"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
                with_scheduler=True,
                scheduler_confirmation_text="yes",
                launchctl_runner=fake_runner,
            )

        self.assertEqual(payload["status"], "one-liner-rejected")
        self.assertEqual(payload["exitCode"], 1)
        self.assertEqual(payload["schedulerRegistration"]["status"], "scheduler-confirmation-missing")
        self.assertEqual(calls, [])

    def test_onboarding_one_liner_status_reads_artifacts_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")
            before = paths.home.exists()
            missing = onboarding_one_liner_status(paths)
            applied = onboarding_one_liner_apply(
                ["dashboard"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
            )
            status = onboarding_one_liner_status(paths)

        self.assertFalse(before)
        self.assertEqual(missing["status"], "not-initialized")
        self.assertEqual(applied["status"], "one-liner-applied")
        self.assertEqual(status["status"], "initialized")
        self.assertTrue(status["readOnly"])
        self.assertTrue(status["artifacts"]["settings"]["exists"])
        self.assertTrue(status["artifacts"]["audit"]["exists"])
        self.assertTrue(status["artifacts"]["runtimeBootstrapRollback"]["exists"])
        self.assertFalse(status["schedulerRegistration"]["registersScheduler"])
        self.assertTrue(status["summary"]["hasRollbackPlan"])

    def test_onboarding_rollback_plan_status_aggregates_without_executing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runtime_paths_for_home(root / ".actanara", legacy_diary_root=root / "Diary")
            missing = onboarding_rollback_plan_status(paths)
            onboarding_one_liner_apply(
                ["dashboard"],
                paths,
                confirmation_text="APPLY ACTANARA ONBOARDING",
            )
            available = onboarding_rollback_plan_status(paths)

        self.assertEqual(missing["status"], "missing")
        self.assertEqual(available["status"], "available")
        self.assertTrue(available["readOnly"])
        self.assertFalse(available["executionPolicy"]["executesRollback"])
        self.assertFalse(available["executionPolicy"]["deletesFiles"])
        self.assertGreater(available["summary"]["operations"], 0)

    def test_onboarding_one_liner_release_gate_passes_minimal_v1(self):
        payload = onboarding_one_liner_release_gate()

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["oneLinerReleaseGateOnly"])
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertFalse(payload["withScheduler"])
        gate_ids = {gate["id"] for gate in payload["gates"]}
        self.assertIn("runtime-bootstrap-apply", gate_ids)
        self.assertIn("scheduler-optional", gate_ids)
        self.assertEqual(payload["blockingGates"], [])
        self.assertEqual(payload["failedGates"], [])

    def test_onboarding_one_liner_release_gate_blocks_when_rag_selected_without_readiness(self):
        payload = onboarding_one_liner_release_gate(["nova-rag"])

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        self.assertIn("rag-not-required-for-minimal-v1", payload["blockingGates"])

    def test_onboarding_one_liner_release_gate_with_scheduler_passes_scheduler_gates(self):
        payload = onboarding_one_liner_release_gate(["dashboard"], with_scheduler=True)
        gate_status = {gate["id"]: gate["status"] for gate in payload["gates"]}

        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["withScheduler"])
        self.assertEqual(gate_status["scheduler-plist-write-gate"], "passed")
        self.assertEqual(gate_status["scheduler-registration-gate"], "passed")
        self.assertEqual(gate_status["scheduler-unregister-gate"], "passed")

    def test_onboarding_one_liner_validation_matrix_passes_clean_machine_cases(self):
        payload = onboarding_one_liner_validation_matrix()
        cases = {case["id"]: case for case in payload["cases"]}

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["oneLinerValidationMatrix"])
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["failedCases"], [])
        self.assertEqual(cases["minimal-v1-release-gate"]["expected"]["exitCode"], 0)
        self.assertEqual(cases["scheduler-opt-in-release-gate"]["expected"]["exitCode"], 0)
        self.assertEqual(cases["rag-out-of-minimal-v1-scope"]["expected"]["status"], "blocked")
        self.assertEqual(cases["rag-out-of-minimal-v1-scope"]["expected"]["exitCode"], 1)
        self.assertFalse(cases["default-runtime-apply-contract"]["evidence"]["registersSchedulerByDefault"])
        self.assertFalse(cases["default-runtime-apply-contract"]["evidence"]["installsDependencies"])

    def test_onboarding_release_gate_aggregates_readonly_gates(self):
        payload = onboarding_release_gate(["nova-rag"], confirmation_text="APPLY ACTANARA ONBOARDING")

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["releaseGateOnly"])
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["confirmationAccepted"])
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        gate_ids = {gate["id"] for gate in payload["gates"]}
        self.assertIn("one-liner-dry-run-schema", gate_ids)
        self.assertIn("blocked-apply-command", gate_ids)
        self.assertIn("write-contract-readonly", gate_ids)
        self.assertIn("sandbox-apply-harness", gate_ids)
        self.assertIn("runtime-bootstrap-apply", gate_ids)
        self.assertIn("default-runtime-target", gate_ids)
        self.assertIn("active-runtime-selection", gate_ids)
        self.assertIn("apply-preflight", gate_ids)
        self.assertIn("scheduler-registration", gate_ids)
        self.assertIn("scheduler-managed-plist-serialization", gate_ids)
        self.assertIn("scheduler-plist-write-gate", gate_ids)
        self.assertIn("rag-provider-readiness", gate_ids)
        self.assertIn("audit-schema", gate_ids)
        self.assertIn("rollback-schema", gate_ids)
        self.assertIn("dependency-and-metadata-writes", gate_ids)
        self.assertIn("no-production-clean-extraction", gate_ids)
        self.assertIn("apply-preflight", payload["blockingGates"])
        self.assertNotIn("scheduler-registration", payload["blockingGates"])
        self.assertIn("rag-provider-readiness", payload["blockingGates"])
        self.assertTrue(payload["sourcePayloads"]["oneLinerDryRunIncluded"])
        self.assertTrue(payload["sourcePayloads"]["blockedApplyIncluded"])
        self.assertTrue(payload["sourcePayloads"]["sandboxApplyHarnessIncluded"])
        self.assertTrue(payload["sourcePayloads"]["runtimeBootstrapApplyIncluded"])
        self.assertTrue(payload["sourcePayloads"]["defaultRuntimeTargetIncluded"])
        self.assertTrue(payload["sourcePayloads"]["activeRuntimeSelectionIncluded"])
        self.assertTrue(payload["sourcePayloads"]["schedulerManagedPlistSerializationIncluded"])
        self.assertTrue(payload["sourcePayloads"]["schedulerPlistWriteGateIncluded"])
        self.assertGreater(payload["summary"]["passed"], 0)
        self.assertGreater(payload["summary"]["blocked"], 0)

    def test_onboarding_approval_packet_lists_required_operator_decisions(self):
        payload = onboarding_approval_packet(["nova-rag"], confirmation_text="APPLY ACTANARA ONBOARDING")

        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["approvalPacketOnly"])
        self.assertEqual(payload["status"], "approval-required")
        self.assertEqual(payload["releaseGateStatus"], "blocked")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        item_ids = {item["id"] for item in payload["operatorApprovalItems"]}
        self.assertIn("approve-settings-writes", item_ids)
        self.assertIn("approve-runtime-directory-writes", item_ids)
        self.assertIn("approve-audit-writes", item_ids)
        self.assertIn("approve-rollback-command", item_ids)
        self.assertIn("approve-launchd-registration", item_ids)
        self.assertIn("approve-rag-provider-readiness-policy", item_ids)
        self.assertTrue(all(item["requiredBeforeImplementation"] for item in payload["operatorApprovalItems"]))
        self.assertFalse(payload["implementationReadiness"]["readyForWriteImplementation"])
        self.assertIn("approve-settings-writes", payload["implementationReadiness"]["requiredApprovalItems"])
        self.assertIn("sandbox-apply-harness", payload["implementationReadiness"]["requiredPassingGates"])
        self.assertIn("runtime-bootstrap-apply", payload["implementationReadiness"]["requiredPassingGates"])
        self.assertIn("default-runtime-target", payload["implementationReadiness"]["requiredPassingGates"])
        self.assertIn("active-runtime-selection", payload["implementationReadiness"]["requiredPassingGates"])
        self.assertIn("scheduler-registration", payload["implementationReadiness"]["requiredPassingGates"])
        self.assertIn("no dependency installation without explicit approval", payload["nonNegotiableBoundaries"])
        self.assertTrue(payload["sourcePayloads"]["releaseGateIncluded"])
        self.assertEqual(payload["summary"]["approvalItems"], len(payload["operatorApprovalItems"]))
        self.assertGreater(payload["summary"]["blockingGates"], 0)

    def test_rag_readiness_plan_covers_provider_states_without_side_effects(self):
        local_missing = rag_readiness_plan(
            ["actanara", "nova-rag"],
            provider_mode="local",
            local_dependency_availability={"sentence-transformers": False, "torch": True, "numpy": True},
        )
        self.assertEqual(local_missing["readinessState"], "rag-local-dependencies-missing")
        self.assertEqual(local_missing["missingLocalDependencies"], ["sentence-transformers"])
        self.assertEqual(local_missing["finalSyncPolicy"], "skip-missing-local-dependencies")
        self.assertFalse(local_missing["installsLocalDependencies"])

        local_ready = rag_readiness_plan(
            ["actanara", "nova-rag"],
            provider_mode="local",
            local_dependency_availability={"sentence-transformers": True, "torch": True, "numpy": True},
        )
        self.assertEqual(local_ready["readinessState"], "rag-local-ready")
        self.assertEqual(local_ready["missingLocalDependencies"], [])
        self.assertEqual(local_ready["finalSyncPolicy"], "run-final-sync-when-pipeline-completes")

        cloud_missing = rag_readiness_plan(
            ["actanara", "nova-rag"],
            provider_mode="cloud",
            cloud_config={"provider": "example-cloud", "apiKeyEnv": "NOVA_RAG_CLOUD_API_KEY"},
        )
        self.assertEqual(cloud_missing["readinessState"], "rag-cloud-config-missing")
        self.assertIn("endpoint", cloud_missing["missingCloudConfigFields"])
        self.assertFalse(cloud_missing["cloudApiCalls"])

        cloud_ready = rag_readiness_plan(
            ["actanara", "nova-rag"],
            provider_mode="cloud",
            cloud_config={
                "provider": "example-cloud",
                "endpoint": "https://example.invalid/embeddings",
                "model": "example-embedding-model",
                "dimension": 1024,
                "apiKeyEnv": "NOVA_RAG_CLOUD_API_KEY",
                "batchSize": 64,
                "timeoutSeconds": 30,
                "indexingSourceSets": ["filtered-dialogue-daily"],
                "syncPolicy": "post-pipeline-final-sync",
            },
        )
        self.assertEqual(cloud_ready["readinessState"], "rag-cloud-ready")
        self.assertEqual(cloud_ready["missingCloudConfigFields"], [])
        self.assertFalse(cloud_ready["cloudApiCalls"])

        sync_skipped = rag_readiness_plan(
            ["actanara", "nova-rag"],
            provider_mode="local",
            sync_status="skipped",
            sync_skip_reason="missing-local-embedding-dependency",
        )
        self.assertEqual(sync_skipped["readinessState"], "rag-sync-skipped")
        self.assertEqual(sync_skipped["skipReason"], "missing-local-embedding-dependency")

        sync_complete = rag_readiness_plan(["actanara", "nova-rag"], provider_mode="cloud", sync_status="complete")
        self.assertEqual(sync_complete["readinessState"], "rag-sync-complete")
        self.assertEqual(sync_complete["finalSyncPolicy"], "sync-complete")

    def test_rag_cloud_config_surface_is_readonly_and_secret_reference_only(self):
        surface = rag_cloud_config_surface()

        self.assertTrue(surface["readOnly"])
        self.assertFalse(surface["writesSettings"])
        self.assertFalse(surface["cloudApiCalls"])
        fields = {field["id"]: field for field in surface["fields"]}
        self.assertEqual(list(fields), [
            "provider",
            "endpoint",
            "model",
            "dimension",
            "apiKeyEnv",
            "batchSize",
            "timeoutSeconds",
            "indexingSourceSets",
            "syncPolicy",
        ])
        self.assertTrue(all(field["required"] for field in fields.values()))
        self.assertFalse(fields["apiKeyEnv"]["secret"])
        self.assertFalse(surface["secretPolicy"]["persistSecretValues"])
        self.assertEqual(surface["secretPolicy"]["apiKeyInputMode"], "environment-variable-reference")

    def test_data_foundation_scheduler_preview_serializes_launchd_without_system_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            fake_home = root / "Home"
            release = paths.home / "app" / "releases" / "resolved-release"
            venv = paths.home / "app" / "venvs" / "resolved-venv"
            release.mkdir(parents=True)
            venv.mkdir(parents=True)
            (paths.home / "app" / "source").symlink_to(Path("releases") / release.name)
            (paths.home / ".venv").symlink_to(Path("app") / "venvs" / venv.name)
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "02:15",
                        "dashboardAggregationTime": "02:45",
                        "systemTimer": {"provider": "launchd", "label": "nova.phase38"},
                    },
                    "pipeline": {"workingDirectory": str(root / "project"), "pythonExecutable": str(root / "venv" / "bin" / "python")},
                },
                paths,
            )

            with (
                patch.object(foundation_scheduler_preview.Path, "home", return_value=fake_home),
                patch.object(foundation_scheduler_preview.platform, "system", return_value="Darwin"),
            ):
                preview = foundation_scheduler_preview.preview_system_timer(paths)

            jobs = {job["kind"]: job for job in preview["jobs"]}

        self.assertEqual(preview["provider"], "launchd")
        self.assertTrue(preview["supported"])
        self.assertFalse((fake_home / "Library" / "LaunchAgents").exists())
        self.assertEqual(
            jobs["daily-pipeline"]["plistPath"],
            str(fake_home / "Library" / "LaunchAgents" / "nova.phase38.pipeline.plist"),
        )
        self.assertEqual(jobs["daily-pipeline"]["program"], jobs["daily-pipeline"]["programArguments"][0])
        self.assertEqual(jobs["daily-pipeline"]["startCalendarInterval"], {"Hour": 2, "Minute": 15})
        self.assertTrue(jobs["daily-pipeline"]["stdoutPath"].endswith("nova.phase38.pipeline.out.log"))
        self.assertTrue(jobs["daily-pipeline"]["stderrPath"].endswith("nova.phase38.pipeline.err.log"))
        self.assertIn("run_dashboard_foundation_refresh.py", " ".join(jobs["dashboard-aggregation"]["programArguments"]))
        managed = jobs["daily-pipeline"]["managedPlist"]
        parsed = plistlib.loads(managed["serializedPlist"].encode("utf-8"))
        env = parsed["EnvironmentVariables"]
        self.assertEqual(managed["provider"], "launchd-user")
        self.assertEqual(managed["label"], "nova.phase38.pipeline")
        self.assertEqual(env["ACTANARA_HOME"], str(paths.home))
        self.assertEqual(env["PATH"], foundation_scheduler_preview.MANAGED_LAUNCHD_PATH)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        stable_source = paths.home / "app" / "source"
        stable_python = paths.home / ".venv" / "bin" / "python"
        self.assertEqual(
            env["PYTHONPATH"],
            f"{stable_source}:{stable_source / 'src'}:{stable_source / 'src' / 'dashboard'}",
        )
        self.assertNotIn("DIARY_OUTPUT_DIR", env)
        self.assertNotIn("TMP_WORKSPACE", env)
        self.assertNotIn("ACTANARA_DATA_DB_PATH", env)
        self.assertNotIn("ACTANARA_DATA_EXPORT_DIR", env)
        self.assertNotIn("WORKSPACE_DIR", env)
        self.assertEqual(parsed["Label"], "nova.phase38.pipeline")
        self.assertEqual(parsed["WorkingDirectory"], str(stable_source))
        self.assertEqual(parsed["ProgramArguments"][0], str(stable_python))
        self.assertEqual(
            parsed["ProgramArguments"][1],
            str(stable_source / "advanced" / "pipeline" / "run_daily_pipeline.py"),
        )
        self.assertEqual(parsed["ProgramArguments"], jobs["daily-pipeline"]["programArguments"])
        self.assertEqual(parsed["StartCalendarInterval"], {"Hour": 2, "Minute": 15})
        self.assertEqual(managed["plistPath"], jobs["daily-pipeline"]["plistPath"])
        self.assertFalse(managed["wouldWritePlist"])
        self.assertFalse(managed["wouldCallLaunchctl"])
        self.assertEqual(managed["registrationCommandPreview"][0:2], ["launchctl", "bootstrap"])
        serialized_jobs = json.dumps(jobs, sort_keys=True)
        self.assertNotIn(str(root / "project"), serialized_jobs)
        self.assertNotIn(str(root / "venv"), serialized_jobs)
        self.assertNotIn(str(release), serialized_jobs)
        self.assertNotIn(str(venv), serialized_jobs)

    def test_onboarding_subsystem_plan_rejects_unknown_profile(self):
        with self.assertRaisesRegex(ValueError, "unknown onboarding profile"):
            onboarding_subsystem_plan(["dashboard", "unknown-subsystem"])

    def test_settings_hardcode_audit_reports_key_names_without_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LLM_API_KEY=real-secret-value\n", encoding="utf-8")
            legacy = root / "src" / "diary_generator"
            legacy.mkdir(parents=True)
            (legacy / "diary_summary.py").write_text(
                "import config\nAPI_KEY = config.LLM_API_KEY\nOPENCLAW_GATEWAY_TOKEN='x'\ncredentials.json\n",
                encoding="utf-8",
            )
            rag = root / "src" / "agentic_rag"
            rag.mkdir(parents=True)
            (rag / "rag_config.py").write_text("NOVA_RAG_CLOUD_API_KEY RAG_CLOUD_API_KEY\n", encoding="utf-8")

            audit = settings_hardcode_audit(root)
            raw = json.dumps(audit, ensure_ascii=False)

        self.assertEqual(audit["summary"]["status"], "attention")
        by_id = {finding["id"]: finding for finding in audit["findings"]}
        self.assertTrue(by_id["workspace-dotenv-llm-key"]["matched"])
        self.assertEqual(by_id["workspace-dotenv-llm-key"]["secretKeysPresent"], ["LLM_API_KEY"])
        residual_by_id = {bucket["id"]: bucket for bucket in audit["residualRisks"]["buckets"]}
        self.assertIn("active-env-overrides", residual_by_id)
        self.assertIn("protected-setting-groups", residual_by_id)
        self.assertIn("rag-legacy-env-boundary", residual_by_id)
        self.assertNotIn("real-secret-value", raw)

    def test_settings_hardcode_audit_reports_env_override_residuals_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"LLM_MODEL_NAME": "secret-model-override"}):
                audit = settings_hardcode_audit(root)
            raw = json.dumps(audit, ensure_ascii=False)

        residual_by_id = {bucket["id"]: bucket for bucket in audit["residualRisks"]["buckets"]}
        self.assertEqual(residual_by_id["active-env-overrides"]["status"], "attention")
        self.assertIn("LLM_MODEL_NAME", {item["env"] for item in residual_by_id["active-env-overrides"]["items"]})
        self.assertNotIn("secret-model-override", raw)

    def test_settings_hardcode_audit_classifies_diagnostic_env_without_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"DASHBOARD_READ_SOURCE": "legacy"}, clear=True):
                audit = settings_hardcode_audit(root)

        residual_by_id = {bucket["id"]: bucket for bucket in audit["residualRisks"]["buckets"]}
        env_bucket = residual_by_id["active-env-overrides"]
        self.assertEqual(env_bucket["status"], "ok")
        self.assertEqual(env_bucket["runtimeAttention"], 0)
        self.assertEqual(env_bucket["bySemantics"]["diagnostic-guard"], 1)
        self.assertIn("diagnostic-guard", {item["semantics"] for item in env_bucket["items"]})

    def test_actanara_settings_status_script_json_mode(self):
        module_path = ROOT / "advanced" / "pipeline" / "run_actanara_settings_status.py"
        spec = importlib.util.spec_from_file_location("run_actanara_settings_status", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            with patch("sys.stdout") as stdout:
                code = module.main(["--runtime", str(paths.home), "--legacy-diary-root", str(root / "Diary"), "--json"])
            written = "".join(call.args[0] for call in stdout.write.call_args_list)

        payload = json.loads(written)
        self.assertEqual(code, 1)
        self.assertEqual(payload["runtime"]["actanaraHome"], str(paths.home))
        self.assertEqual(payload["summary"]["status"], "error")
        self.assertIn("llm-provider", {check["id"] for check in payload["checks"] if check["status"] == "error"})

    def test_actanara_settings_status_runtime_inspection_does_not_initialize_home(self):
        module_path = ROOT / "advanced" / "pipeline" / "run_actanara_settings_status.py"
        spec = importlib.util.spec_from_file_location("run_actanara_settings_status_readonly", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = initialize_home(root / "ActiveNova", legacy_diary_root=root / "ActiveDiary")
            candidate = root / "CandidateNova"
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(active.home)}, clear=False),
                patch("sys.stdout") as stdout,
            ):
                code = module.main(["--runtime", str(candidate), "--legacy-diary-root", str(root / "CandidateDiary"), "--json"])
            written = "".join(call.args[0] for call in stdout.write.call_args_list)

        payload = json.loads(written)
        self.assertEqual(code, 1)
        self.assertEqual(payload["runtime"]["actanaraHome"], str(candidate))
        self.assertFalse(candidate.exists())
        self.assertTrue(payload["readOnly"])

    def test_scheduler_refreshes_current_day_week_and_month_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "enabled": True,
                        "mode": "system",
                        "timezone": "UTC",
                        "dashboardAggregationTime": "04:30",
                    }
                },
                paths,
            )
            now = datetime(2026, 5, 29, 5, 0, tzinfo=ZoneInfo("UTC"))
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(scheduler.foundation, "queue_refresh", side_effect=[101, 102, 103]) as queue,
                patch.object(scheduler.foundation, "execute_refresh") as execute,
            ):
                first = scheduler.run_due_snapshot_refresh(now)
                second = scheduler.run_due_snapshot_refresh(now)
                state = read_scheduler_state(paths)
            self.assertTrue(first["ran"])
            self.assertEqual(first["runIds"], [101, 102, 103])
            self.assertEqual(second["reason"], "already_ran_today")
            self.assertTrue(state["lastDashboardAggregationAt"].endswith("+00:00"))
            self.assertEqual(queue.call_count, 3)
            self.assertEqual(execute.call_count, 3)

    def test_scheduler_does_not_run_before_enabled_system_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "enabled": True,
                        "mode": "system",
                        "timezone": "Asia/Hong_Kong",
                        "dashboardAggregationTime": "04:30",
                    }
                },
                paths,
            )
            now = datetime(2026, 5, 29, 4, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                result = scheduler.run_due_snapshot_refresh(now)
            self.assertEqual(result["reason"], "before_scheduled_time")

    def test_scheduler_disabled_does_not_run_due_history_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"schedule": {"enabled": False, "mode": "system", "dashboardAggregationTime": "04:30"}}, paths)
            now = datetime(2026, 5, 29, 5, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(scheduler.foundation, "execute_due_scheduled_history_backfills") as execute,
            ):
                result = scheduler.run_due_snapshot_refresh(now)
            execute.assert_not_called()
            self.assertFalse(result["ran"])
            self.assertEqual(result["reason"], "disabled")
            self.assertEqual(result["scheduledHistoryBackfills"], [])

    def test_scheduler_runs_due_history_backfill_before_aggregation_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "enabled": True,
                        "mode": "system",
                        "timezone": "Asia/Hong_Kong",
                        "dashboardAggregationTime": "04:30",
                    }
                },
                paths,
            )
            now = datetime(2026, 5, 29, 4, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(scheduler.foundation, "execute_due_scheduled_history_backfills", return_value=[{"runId": 7}]) as execute,
            ):
                result = scheduler.run_due_snapshot_refresh(now)
            execute.assert_called_once_with()
            self.assertTrue(result["ran"])
            self.assertEqual(result["reason"], "before_scheduled_time")
            self.assertEqual(result["scheduledHistoryBackfills"], [{"runId": 7}])

    def test_system_timer_preview_includes_pipeline_and_dashboard_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "03:10",
                        "dashboardAggregationTime": "03:40",
                        "systemTimer": {"provider": "launchd", "label": "nova.test"},
                    }
                },
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(foundation_scheduler_preview.platform, "system", return_value="Darwin"),
            ):
                preview = scheduler.preview_system_timer()
            jobs = {job["kind"]: job for job in preview["jobs"]}
            self.assertTrue(preview["supported"])
            self.assertEqual(jobs["daily-pipeline"]["label"], "nova.test.pipeline")
            self.assertEqual(jobs["daily-pipeline"]["time"], "03:10")
            self.assertIn("run_daily_pipeline.py", " ".join(jobs["daily-pipeline"]["programArguments"]))
            self.assertEqual(jobs["dashboard-aggregation"]["label"], "nova.test.dashboard-aggregation")
            self.assertEqual(jobs["dashboard-aggregation"]["time"], "03:40")
            self.assertIn("run_dashboard_foundation_refresh.py", " ".join(jobs["dashboard-aggregation"]["programArguments"]))

    def test_system_timer_preview_can_probe_launchd_actual_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            fake_home = root / "Home"
            write_settings(
                {
                    "schedule": {
                        "systemTimer": {
                            "provider": "launchd",
                            "label": "nova.test",
                            "registered": False,
                        }
                    }
                },
                paths,
            )
            commands = []

            def loaded_runner(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "state = not running", "")

            with (
                patch.object(foundation_scheduler_preview.platform, "system", return_value="Darwin"),
                patch.object(foundation_scheduler_preview.os, "getuid", return_value=501),
            ):
                preview = foundation_scheduler_preview.preview_system_timer(
                    paths,
                    launch_agent_home=fake_home,
                    probe_runtime=True,
                    launchctl_runner=loaded_runner,
                )

        self.assertTrue(preview["registered"])
        self.assertFalse(preview["configuredRegistered"])
        self.assertTrue(preview["actualRegistered"])
        self.assertTrue(preview["registrationMismatch"])
        self.assertEqual(preview["registrationSource"], "launchd-probe")
        self.assertEqual(preview["runtimeProbe"]["status"], "loaded")
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command[:2] == ["launchctl", "print"] for command in commands))

    def test_scheduler_status_effective_enabled_follows_actual_system_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "enabled": False,
                        "mode": "system",
                        "systemTimer": {"provider": "launchd", "label": "nova.test"},
                    }
                },
                paths,
            )
            timer_status = {
                "provider": "launchd",
                "supported": True,
                "registered": True,
                "configuredRegistered": False,
                "actualRegistered": True,
                "registrationSource": "launchd-probe",
                "jobs": [],
            }

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(scheduler, "preview_system_timer", return_value=timer_status),
            ):
                status = scheduler.scheduler_status()

        self.assertFalse(status["enabled"])
        self.assertTrue(status["effectiveEnabled"])
        self.assertTrue(status["actualSystemEnabled"])
        self.assertEqual(status["systemTimer"]["registrationSource"], "launchd-probe")

    def test_linux_system_timer_preview_is_readonly_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "schedule": {
                        "dailyPipelineTime": "03:10",
                        "dashboardAggregationTime": "03:40",
                        "systemTimer": {"provider": "systemd", "label": "nova.test"},
                    }
                },
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch("data_foundation.scheduler_preview.platform.system", return_value="Linux"),
            ):
                preview = scheduler.preview_system_timer()

            jobs = {job["kind"]: job for job in preview["jobs"]}
            self.assertEqual(preview["provider"], "systemd")
            self.assertTrue(preview["registrationImplemented"])
            self.assertIn("~/.config/systemd/user", " ".join(preview["installPlan"]))
            self.assertEqual(jobs["daily-pipeline"]["timerName"], "nova.test.pipeline.timer")
            self.assertIn("run_daily_pipeline.py", jobs["daily-pipeline"]["command"])
            self.assertEqual(jobs["dashboard-aggregation"]["time"], "03:40")

    def test_non_launchd_system_timer_install_and_uninstall_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"schedule": {"systemTimer": {"provider": "systemd", "label": "nova.test"}}}, paths)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                with self.assertRaises(ValueError):
                    scheduler.install_system_timer()
                with self.assertRaises(ValueError):
                    scheduler.uninstall_system_timer()

    def test_system_timer_install_marks_settings_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"schedule": {"timezone": "UTC", "systemTimer": {"provider": "launchd", "label": "nova.test"}}},
                paths,
            )

            def plist_path(label: str) -> Path:
                return root / "LaunchAgents" / f"{label}.plist"

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch("data_foundation.scheduler_preview.detect_system_timezone_authority", return_value="UTC"),
                patch.object(scheduler, "_launch_agent_path", side_effect=plist_path),
                patch.object(scheduler, "_launchctl") as launchctl,
                patch.object(
                    scheduler,
                    "_probe_handoff_job",
                    side_effect=[
                        {"loaded": False, "running": False, "aligned": False},
                        {"loaded": False, "running": False, "aligned": False},
                        {"loaded": True, "running": False, "aligned": True},
                        {"loaded": True, "running": False, "aligned": True},
                    ],
                ),
            ):
                result = scheduler.install_system_timer(
                    {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION}
                )
                raw = read_settings(paths, redact_secrets=False)
            self.assertEqual(len(result["installed"]), 2)
            self.assertTrue((root / "LaunchAgents" / "nova.test.pipeline.plist").exists())
            self.assertTrue(raw["schedule"]["systemTimer"]["registered"])
            self.assertTrue(raw["schedule"]["systemTimer"]["registeredAt"].endswith("+00:00"))
            self.assertEqual(launchctl.call_count, 4)

    def test_system_timer_install_failure_restores_preimage_and_handoff_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"schedule": {"timezone": "UTC", "systemTimer": {"provider": "launchd", "label": "nova.test"}}},
                paths,
            )
            settings_before = (paths.config_dir / "settings.json").read_bytes()

            def plist_path(label: str) -> Path:
                return root / "LaunchAgents" / f"{label}.plist"

            def fail_bootstrap(action, label, plist_path, allow_failure=False):
                if action == "bootstrap":
                    raise RuntimeError("bootstrap failed")

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch("data_foundation.scheduler_preview.detect_system_timezone_authority", return_value="UTC"),
                patch.object(scheduler, "_launch_agent_path", side_effect=plist_path),
                patch.object(scheduler, "_launchctl", side_effect=fail_bootstrap),
                patch.object(
                    scheduler,
                    "_probe_handoff_job",
                    side_effect=[
                        {"loaded": False, "running": False, "aligned": False},
                        {"loaded": False, "running": False, "aligned": False},
                        {"loaded": False, "running": False, "aligned": False},
                        {"loaded": False, "running": False, "aligned": False},
                    ],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "settings transaction"):
                    scheduler.install_system_timer(
                        {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION}
                    )
            self.assertEqual((paths.config_dir / "settings.json").read_bytes(), settings_before)
            self.assertFalse(any((root / "LaunchAgents").glob("*.plist")))
            journals = list((paths.state_dir / "scheduler-handoffs").glob("*/journal.json"))
            self.assertEqual(len(journals), 1)
            self.assertEqual(json.loads(journals[0].read_text(encoding="utf-8"))["status"], "compensated")

    def test_system_timer_install_requires_confirmation_and_supports_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"schedule": {"systemTimer": {"provider": "launchd", "label": "nova.test"}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(scheduler, "_launchctl") as launchctl,
            ):
                dry_run = scheduler.install_system_timer({"dryRun": True})
                with self.assertRaises(ValueError):
                    scheduler.install_system_timer({"confirmationText": "wrong"})

            self.assertTrue(dry_run["dryRun"])
            self.assertEqual(dry_run["confirmationTextRequired"], scheduler.SCHEDULER_INSTALL_CONFIRMATION)
            launchctl.assert_not_called()

    def test_runtime_path_select_initializes_and_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap = root / "location.json"
            home = root / "Actanara"
            with patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(bootstrap)}, clear=False):
                os.environ.pop("ACTANARA_HOME", None)
                validation = dashboard_settings.validate_runtime_path(str(home))
                self.assertTrue(validation["validation"]["valid"])
                self.assertFalse(validation["validation"]["initialized"])
                selected = dashboard_settings.select_runtime_path(
                    {
                        "path": str(home),
                        "mode": "initialize",
                        "legacyDiaryRoot": str(root / "Diary"),
                        "confirmationText": dashboard_settings.RUNTIME_PATH_SELECT_CONFIRMATION,
                    }
                )
                self.assertTrue(selected["validation"]["initialized"])
                self.assertEqual(selected["selected"]["actanaraHome"], str(home))
                self.assertEqual(json.loads(bootstrap.read_text(encoding="utf-8"))["actanaraHome"], str(home))
                audit_path = Path(selected["audit"]["path"])
                self.assertTrue(audit_path.exists())
                audit = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
                self.assertEqual(audit["mode"], "initialize")
                self.assertEqual(audit["candidate"], str(home))
                current = dashboard_settings.current_runtime_path()
                self.assertEqual(current["selected"]["actanaraHome"], str(home))

    def test_dashboard_runtime_path_rejects_legacy_import_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap = root / "location.json"
            legacy = root / "Diary"
            (legacy / "__diary_daily" / "2026-05-19" / "_filtered" / "codex").mkdir(parents=True)
            (legacy / "__diary_daily" / "2026-05-19" / "_filtered" / "codex" / "one.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(bootstrap)}, clear=False):
                os.environ.pop("ACTANARA_HOME", None)
                with self.assertRaisesRegex(ValueError, "mode must be one of use, initialize"):
                    dashboard_settings.select_runtime_path(
                        {
                            "path": str(root / "Actanara"),
                            "mode": "import_legacy",
                            "legacyDiaryRoot": str(legacy),
                            "confirmationText": dashboard_settings.RUNTIME_PATH_SELECT_CONFIRMATION,
                        }
                    )
            self.assertFalse(
                (root / "Actanara" / "sources" / "archives" / "2026-05-19" / "filtered" / "codex" / "one.jsonl").exists()
            )

    def test_runtime_path_select_rejects_invalid_mode(self):
        with self.assertRaises(ValueError):
            dashboard_settings.select_runtime_path(
                {
                    "path": "/tmp/nova",
                    "mode": "delete",
                    "confirmationText": dashboard_settings.RUNTIME_PATH_SELECT_CONFIRMATION,
                }
            )

    def test_runtime_path_select_requires_confirmation(self):
        with self.assertRaisesRegex(ValueError, "confirmationText must be exactly"):
            dashboard_settings.select_runtime_path({"path": "/tmp/nova", "mode": "use"})

    def test_diary_projection_rebuild_requires_confirmation_for_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.object(dashboard_settings, "plan_diary_projection_rebuild", return_value={"dryRun": True}) as plan,
                patch.object(dashboard_settings, "rebuild_diary_projections", return_value={"dryRun": False}) as rebuild,
            ):
                dry_run = dashboard_settings.rebuild_diary_path_projection(
                    {"startDate": "2026-06-01", "endDate": "2026-06-02", "dryRun": True}
                )
                with self.assertRaisesRegex(ValueError, "confirmationText must be exactly"):
                    dashboard_settings.rebuild_diary_path_projection(
                        {"startDate": "2026-06-01", "endDate": "2026-06-02", "dryRun": False}
                    )
                executed = dashboard_settings.rebuild_diary_path_projection(
                    {
                        "startDate": "2026-06-01",
                        "endDate": "2026-06-02",
                        "dryRun": False,
                        "confirmationText": dashboard_settings.DIARY_PROJECTION_REBUILD_CONFIRMATION,
                    }
                )

        self.assertEqual(dry_run["confirmationTextRequired"], dashboard_settings.DIARY_PROJECTION_REBUILD_CONFIRMATION)
        self.assertFalse(executed["dryRun"])
        plan.assert_called_once()
        rebuild.assert_called_once()

    def test_dashboard_rag_settings_status_and_actions_are_control_plane_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            index = root / "Diary" / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text("{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                payload = dashboard_settings.update_rag_settings(
                    {
                        "enabled": True,
                        "mode": "legacy",
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 384},
                        "server": {"enabled": True, "host": "127.0.0.1", "port": 3037, "healthPath": "/health"},
                    }
                )
                status = dashboard_settings.get_rag_status(probe_server=False)
                with patch.object(
                    dashboard_settings,
                    "read_server_process_state",
                    return_value={"running": False, "health": {"healthy": False}, "status": "stopped"},
                ), patch.object(
                    dashboard_settings,
                    "start_rag_server",
                    return_value={"accepted": True, "status": "starting", "lifecycle": {"pid": 1234}},
                ) as start_server:
                    action = dashboard_settings.rag_operator_action(
                        "server-start",
                        {"confirmationText": dashboard_settings.RAG_SERVER_START_CONFIRMATION},
                    )
                with patch.object(
                    dashboard_settings,
                    "sync_v2_production_index",
                    return_value={
                        "status": "candidate-ready",
                        "build": {
                            "candidateManifest": str(paths.home / "reserved" / "rag" / "v2" / "manifest.json"),
                            "manifest": {"chunkCount": 1, "embeddingCount": 1},
                        },
                        "gates": {"status": "passed"},
                        "mutationPolicy": {"activeSnapshotPromoted": False},
                    },
                ) as sync:
                    index_action = dashboard_settings.rag_operator_action("index-run")
                with patch.object(
                    dashboard_settings,
                    "get_rag_status",
                    return_value={"searchAvailable": False, "server": {"healthy": False}},
                ):
                    search = dashboard_settings.rag_search({"query": "hello", "topK": 3})
            self.assertEqual(payload["rag"]["embedding"]["dimension"], 384)
            self.assertEqual(status["activeSource"], "retired")
            self.assertFalse(status["legacy"]["metadataRead"])
            self.assertIsNone(status["legacy"]["entries"])
            self.assertEqual(action["status"], "starting")
            self.assertTrue(action["accepted"])
            start_server.assert_called_once()
            self.assertEqual(index_action["status"], "candidate-ready")
            self.assertEqual(index_action["action"], "index-run")
            sync.assert_called_once()
            _, kwargs = sync.call_args
            self.assertEqual(kwargs["requested_by"], "dashboard")
            self.assertFalse(kwargs["promote"])
            self.assertFalse(search["available"])
            self.assertEqual(search["results"], [])

    def test_dashboard_rag_settings_stores_raw_api_key_and_preserves_explicit_secret_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            with patch.dict(
                os.environ,
                {"ACTANARA_HOME": str(paths.home), "ACTANARA_SECRET_BACKEND": "runtime-file"},
                clear=False,
            ):
                stored = dashboard_settings.update_rag_settings(
                    {
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embed.example.invalid/v1",
                            "apiKey": "should-not-persist",
                        }
                    }
                )
                stored_ref = stored["rag"]["embedding"]["secretRef"]
                self.assertEqual(stored_ref["backend"], "runtime-file")
                self.assertEqual(
                    read_secret(stored_ref, runtime_home=paths.home),
                    "should-not-persist",
                )
                payload = dashboard_settings.update_rag_settings(
                    {
                        "enabled": True,
                        "mode": "v2",
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embed.example.invalid/v1",
                            "apiKeyEnv": "EXAMPLE_RAG_KEY",
                            "secretRef": {
                                "backend": "process-env",
                                "service": "actanara",
                                "account": "EXAMPLE_RAG_KEY",
                            },
                        },
                    }
                )
                status = dashboard_settings.get_rag_status(probe_server=False)

            embedding = payload["rag"]["embedding"]
            self.assertNotIn("apiKey", embedding)
            self.assertEqual(embedding["secretRef"]["account"], "EXAMPLE_RAG_KEY")
            self.assertEqual(status["provider"]["cloud"]["apiKeyEnv"], "EXAMPLE_RAG_KEY")
            self.assertTrue(status["provider"]["cloud"]["hasSecretRef"])
            self.assertEqual(status["provider"]["cloud"]["secretRef"]["account"], "EXAMPLE_RAG_KEY")
            self.assertFalse(status["provider"]["cloud"]["storesSecretValue"])

    def test_dashboard_rag_settings_cannot_change_language_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                with self.assertRaisesRegex(ValueError, "rag.languageProfile is immutable"):
                    dashboard_settings.update_rag_settings({"languageProfile": "en"})

    def test_dashboard_rag_server_start_requires_confirmation_and_supports_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            index = root / "Diary" / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text("{}\n", encoding="utf-8")
            write_settings({"rag": {"enabled": True, "mode": "legacy"}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(dashboard_settings, "start_rag_server") as start_server,
            ):
                dry_run = dashboard_settings.rag_operator_action("server-start", {"dryRun": True})
                with self.assertRaises(ValueError):
                    dashboard_settings.rag_operator_action("server-start", {"confirmationText": "wrong"})

            self.assertTrue(dry_run["dryRun"])
            self.assertEqual(dry_run["confirmationTextRequired"], dashboard_settings.RAG_SERVER_START_CONFIRMATION)
            start_server.assert_not_called()

    def test_dashboard_rag_server_start_does_not_double_start_after_launch_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"rag": {"enabled": True, "mode": "v2", "server": {"enabled": True}}},
                paths,
            )
            running_state = {
                "running": True,
                "health": {"healthy": True},
                "status": "healthy",
                "pid": 4321,
            }
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(dashboard_settings, "read_server_process_state", return_value=running_state) as read_state,
                patch.object(dashboard_settings, "start_rag_server") as start_server,
            ):
                result = dashboard_settings.rag_operator_action(
                    "server-start",
                    {"confirmationText": dashboard_settings.RAG_SERVER_START_CONFIRMATION},
                )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["status"], "already-running")
            self.assertEqual(result["lifecycle"], running_state)
            read_state.assert_called()
            start_server.assert_not_called()

    def test_dashboard_rag_server_start_uses_direct_start_without_launch_agent_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"rag": {"enabled": True, "mode": "v2", "server": {"enabled": True}}},
                paths,
            )
            stopped_state = {
                "running": False,
                "health": {"healthy": False},
                "status": "stopped",
                "pid": None,
            }
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(dashboard_settings, "read_server_process_state", return_value=stopped_state),
                patch.object(
                    dashboard_settings,
                    "start_rag_server",
                    return_value={"accepted": True, "status": "starting", "lifecycle": {"pid": 1234}},
                ) as start_server,
            ):
                result = dashboard_settings.rag_operator_action(
                    "server-start",
                    {"confirmationText": dashboard_settings.RAG_SERVER_START_CONFIRMATION},
                )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["lifecycle"], {"pid": 1234})
            start_server.assert_called_once()
            self.assertIsNone(result["launchAgent"])

    def test_dashboard_rag_operator_actions_reject_start_and_index_when_product_switch_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"mode": "disabled"}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(dashboard_settings, "start_rag_server") as start,
                patch.object(dashboard_settings, "sync_v2_production_index") as sync,
            ):
                start_result = dashboard_settings.rag_operator_action("server-start")
                result = dashboard_settings.rag_operator_action("index-run")

            self.assertFalse(start_result["accepted"])
            self.assertEqual(start_result["status"], "rag-disabled")
            self.assertEqual(start_result["action"], "server-start")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "rag-disabled")
            self.assertEqual(result["action"], "index-run")
            self.assertEqual(result["ragStatus"]["freshness"]["status"], "disabled")
            start.assert_not_called()
            sync.assert_not_called()

    def test_dashboard_rag_candidate_refresh_job_uses_unified_sync_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": True, "mode": "v2", "indexing": {"enabled": True}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(
                    rag_index_jobs,
                    "sync_v2_production_index",
                    return_value={
                        "status": "candidate-ready",
                        "build": {
                            "candidateManifest": "/tmp/candidate/manifest.json",
                            "candidatePath": "/tmp/candidate",
                            "manifest": {"chunkCount": 3, "embeddingCount": 3, "skippedCount": 0},
                        },
                        "embeddingSource": "server",
                    },
                ) as sync,
            ):
                queued = rag_index_jobs.queue_candidate_refresh(requested_by="test")
                rag_index_jobs.execute_candidate_refresh(queued["jobId"])
                jobs = rag_index_jobs.list_candidate_refresh_jobs(limit=1)

            sync.assert_called_once()
            _, kwargs = sync.call_args
            self.assertEqual(kwargs["requested_by"], "dashboard-background")
            self.assertFalse(kwargs["promote"])
            self.assertEqual(kwargs["server_wait_timeout_seconds"], 600)
            self.assertEqual(jobs[0]["status"], "candidate-ready")
            self.assertEqual(jobs[0]["embeddingSource"], "server")
            self.assertEqual(jobs[0]["candidateManifest"], "/tmp/candidate/manifest.json")
            self.assertEqual(jobs[0]["chunkCount"], 3)
            self.assertEqual(jobs[0]["embeddingCount"], 3)
            self.assertEqual(jobs[0]["skippedCount"], 0)

    def test_dashboard_rag_production_sync_job_promotes_active_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": True, "mode": "v2", "indexing": {"enabled": True}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(
                    rag_index_jobs,
                    "sync_v2_production_index",
                    return_value={
                        "status": "promoted",
                        "build": {
                            "candidateManifest": "/tmp/candidate/manifest.json",
                            "candidatePath": "/tmp/candidate",
                            "manifest": {"chunkCount": 3, "embeddingCount": 3, "skippedCount": 0},
                        },
                        "promotion": {
                            "activeRunId": "run-1",
                            "activeIndexPath": "/tmp/active/index.jsonl",
                        },
                        "embeddingSource": "server",
                    },
                ) as sync,
            ):
                queued = rag_index_jobs.queue_production_sync(requested_by="test")
                rag_index_jobs.execute_production_sync(queued["jobId"])
                jobs = rag_index_jobs.list_candidate_refresh_jobs(limit=1)

            sync.assert_called_once()
            _, kwargs = sync.call_args
            self.assertEqual(kwargs["requested_by"], "dashboard-production-sync")
            self.assertTrue(kwargs["promote"])
            self.assertEqual(kwargs["server_wait_timeout_seconds"], 600)
            self.assertEqual(jobs[0]["status"], "promoted")
            self.assertEqual(jobs[0]["activeRunId"], "run-1")
            self.assertEqual(jobs[0]["activeIndexPath"], "/tmp/active/index.jsonl")
            self.assertEqual(jobs[0]["chunkCount"], 3)
            self.assertEqual(jobs[0]["embeddingCount"], 3)
            self.assertEqual(jobs[0]["skippedCount"], 0)

    def test_dashboard_rag_profile_migration_job_uses_unified_sync_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": True, "mode": "v2", "indexing": {"enabled": True}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(
                    rag_index_jobs,
                    "sync_v2_production_index",
                    return_value={
                        "status": "candidate-ready",
                        "build": {
                            "candidateManifest": "/tmp/profile/manifest.json",
                            "candidatePath": "/tmp/profile",
                            "manifest": {"chunkCount": 5, "embeddingCount": 5, "skippedCount": 0},
                        },
                        "embeddingSource": "server",
                    },
                ) as sync,
            ):
                queued = rag_index_jobs.queue_profile_migration(
                    {
                        "confirmationText": rag_index_jobs.RAG_PROFILE_MIGRATION_CONFIRMATION,
                        "targetProfile": {
                            "mode": "local",
                            "providerId": "local",
                            "model": "intfloat/multilingual-e5-small",
                            "dimension": 384,
                            "languageProfile": "zh",
                        },
                    },
                    requested_by="test",
                )
                rag_index_jobs.execute_profile_migration(queued["jobId"])
                jobs = rag_index_jobs.list_candidate_refresh_jobs(limit=1)

            sync.assert_called_once()
            args, kwargs = sync.call_args
            self.assertEqual(args[0].embedding_model, "intfloat/multilingual-e5-small")
            self.assertEqual(args[0].embedding_dimension, 384)
            self.assertEqual(kwargs["requested_by"], "dashboard-profile-migration")
            self.assertFalse(kwargs["promote"])
            self.assertEqual(kwargs["server_wait_timeout_seconds"], 600)
            self.assertEqual(jobs[0]["status"], "candidate-ready")
            self.assertEqual(jobs[0]["embeddingSource"], "server")
            self.assertTrue(jobs[0]["promotionRequired"])
            self.assertEqual(jobs[0]["chunkCount"], 5)
            self.assertEqual(jobs[0]["embeddingCount"], 5)
            self.assertEqual(jobs[0]["skippedCount"], 0)

    def test_dashboard_rag_profile_migration_plan_reports_side_effects_without_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": True, "mode": "v2", "indexing": {"enabled": True}}}, paths)
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                plan = rag_index_jobs.plan_profile_migration(
                    {
                        "initMode": True,
                        "autoPromote": True,
                        "targetProfile": {
                            "mode": "local",
                            "providerId": "local",
                            "model": "intfloat/multilingual-e5-small",
                            "dimension": 384,
                            "languageProfile": "zh",
                        },
                    },
                    requested_by="test",
                )
                jobs = rag_index_jobs.list_candidate_refresh_jobs(limit=1)

            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["confirmationTextRequired"], rag_index_jobs.RAG_PROFILE_INITIALIZATION_CONFIRMATION)
            self.assertIn("runtime-settings-write", plan["sideEffects"])
            self.assertIn("rag-active-index-promotion", plan["sideEffects"])
            self.assertTrue(plan["risk"]["settingsMutated"])
            self.assertEqual(jobs, [])

    def test_dashboard_rag_initialization_job_auto_promotes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": True, "mode": "v2", "indexing": {"enabled": True}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(
                    rag_index_jobs,
                    "sync_v2_production_index",
                    return_value={
                        "status": "candidate-ready",
                        "build": {
                            "candidateManifest": "/tmp/profile/run-1/manifest.json",
                            "candidatePath": "/tmp/profile/run-1",
                            "manifest": {"chunkCount": 5, "embeddingCount": 5, "skippedCount": 0, "lastBuildRunId": "run-1"},
                        },
                        "embeddingSource": "server",
                    },
                ),
                patch.object(
                    rag_index_jobs,
                    "promote_v2_candidate",
                    return_value={"accepted": True, "status": "promoted", "runId": "run-1"},
                ) as promote,
                patch.object(rag_index_jobs, "_ensure_local_rag_dependencies") as ensure_dependencies,
                patch.object(
                    rag_index_jobs,
                    "start_rag_server",
                    return_value={"accepted": True, "status": "starting"},
                ) as start_server,
            ):
                queued = rag_index_jobs.queue_profile_migration(
                    {
                        "initMode": True,
                        "confirmationText": rag_index_jobs.RAG_PROFILE_INITIALIZATION_CONFIRMATION,
                        "autoPromote": True,
                        "targetProfile": {
                            "mode": "local",
                            "providerId": "local",
                            "model": "intfloat/multilingual-e5-small",
                            "dimension": 384,
                            "languageProfile": "zh",
                        },
                    },
                    requested_by="test",
                )
                rag_index_jobs.execute_profile_migration(queued["jobId"])
                jobs = rag_index_jobs.list_candidate_refresh_jobs(limit=1)

            self.assertTrue(queued["job"]["initMode"])
            self.assertTrue(queued["job"]["risk"]["settingsMutated"])
            ensure_dependencies.assert_called_once_with(queued["jobId"])
            start_server.assert_called_once()
            self.assertEqual(start_server.call_args.kwargs["requested_by"], "dashboard-profile-initialization")
            promote.assert_called_once()
            _, kwargs = promote.call_args
            self.assertEqual(kwargs["run_id"], "run-1")
            self.assertNotIn("require_legacy_comparison", kwargs)
            self.assertEqual(jobs[0]["status"], "promoted")
            self.assertFalse(jobs[0]["promotionRequired"])
            self.assertEqual(jobs[0]["promotion"]["status"], "promoted")
            self.assertEqual(jobs[0]["chunkCount"], 5)
            self.assertEqual(jobs[0]["embeddingCount"], 5)
            self.assertEqual(jobs[0]["skippedCount"], 0)

    def test_dashboard_rag_initialization_accepts_disabled_product_and_queues_retryable_dependency_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "features": {"rag": False, "embeddingServer": False},
                    "rag": {"enabled": False, "mode": "disabled", "server": {"enabled": False}},
                },
                paths,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                queued = rag_index_jobs.queue_profile_migration(
                    {
                        "initMode": True,
                        "confirmationText": rag_index_jobs.RAG_PROFILE_INITIALIZATION_CONFIRMATION,
                        "autoPromote": True,
                        "targetProfile": {
                            "mode": "local",
                            "providerId": "local",
                            "model": "intfloat/multilingual-e5-small",
                            "dimension": 384,
                            "languageProfile": "zh",
                        },
                    },
                    requested_by="test",
                )

            self.assertTrue(queued["accepted"])
            self.assertEqual(queued["status"], "queued")
            self.assertTrue(queued["job"]["initMode"])

    def test_dashboard_rag_initialization_plan_includes_local_dependency_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"enabled": False, "mode": "disabled"}}, paths)
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                plan = rag_index_jobs.plan_profile_migration(
                    {
                        "initMode": True,
                        "targetProfile": {
                            "mode": "local",
                            "providerId": "local",
                            "model": "intfloat/multilingual-e5-small",
                            "dimension": 384,
                        },
                    }
                )

            self.assertIn("ensure-rag-local-dependencies", [step["id"] for step in plan["steps"]])
            self.assertIn("runtime-python-dependency-install", plan["sideEffects"])

    def test_dashboard_rag_dependency_install_failure_records_terminal_dependency_state(self):
        class FailedModuleProbeInstall:
            returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch(
                    "agentic_rag.rag_server_lifecycle._python_has_modules",
                    side_effect=[False, False],
                ),
                patch.object(
                    rag_index_jobs.subprocess,
                    "run",
                    return_value=FailedModuleProbeInstall(),
                ),
                patch.object(rag_index_jobs, "_append_record") as append_record,
            ):
                with self.assertRaisesRegex(RuntimeError, "local dependency installation failed"):
                    rag_index_jobs._ensure_local_rag_dependencies("rag-dependency-test")

            append_record.assert_any_call(
                {
                    "id": "rag-dependency-test",
                    "dependencyStatus": "failed",
                    "progress": 40,
                }
            )

    def test_dashboard_cloud_rag_initialization_enables_server_without_local_dependency_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"features": {"rag": False, "embeddingServer": False}, "rag": {"enabled": False, "mode": "disabled"}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(rag_index_jobs, "sync_v2_production_index", return_value={"status": "candidate-ready", "build": {"manifest": {}}}),
                patch.object(rag_index_jobs, "_ensure_local_rag_dependencies") as ensure_dependencies,
                patch.object(
                    rag_index_jobs,
                    "start_rag_server",
                    return_value={"accepted": True, "status": "running"},
                ) as start_server,
            ):
                queued = rag_index_jobs.queue_profile_migration(
                    {
                        "initMode": True,
                        "confirmationText": rag_index_jobs.RAG_PROFILE_INITIALIZATION_CONFIRMATION,
                        "targetProfile": {"mode": "cloud", "providerId": "test-cloud", "model": "embed-test", "dimension": 1024},
                    }
                )
                rag_index_jobs.execute_profile_migration(queued["jobId"])
                resolved = rag_index_jobs.resolve_rag_settings(paths)

            ensure_dependencies.assert_not_called()
            start_server.assert_called_once()
            self.assertTrue(resolved.enabled)
            self.assertTrue(resolved.server_enabled)
            self.assertEqual(resolved.embedding_provider, "cloud")

    def test_dashboard_rag_stats_and_search_proxy_are_read_only_and_forward_filters(self):
        status = {
            "searchAvailable": True,
            "settings": {"server_health_path": "/health"},
            "server": {"healthy": True, "url": "http://127.0.0.1:3037/health"},
            "freshness": {"status": "ready"},
        }

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": [], "api": {"readOnly": True}}).encode("utf-8")

        requests = []

        def fake_urlopen(request, timeout=10):
            requests.append(request)
            return Response()

        with (
            patch.object(dashboard_settings, "get_rag_status", return_value=status),
            patch.object(dashboard_settings.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            stats = dashboard_settings.rag_stats()
            search = dashboard_settings.rag_search(
                {"query": "deploy", "topK": 3, "date": "2026-06-05", "role": "codex", "tags": ["coding"]}
            )

        self.assertTrue(stats["available"])
        self.assertTrue(stats["api"]["readOnly"])
        self.assertTrue(search["available"])
        first_url = requests[0] if isinstance(requests[0], str) else requests[0].full_url
        second_url = requests[1] if isinstance(requests[1], str) else requests[1].full_url
        self.assertEqual(str(first_url), "http://127.0.0.1:3037/stats")
        self.assertEqual(str(second_url), "http://127.0.0.1:3037/search")
        forwarded = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(forwarded["top_k"], 3)
        self.assertEqual(forwarded["date"], "2026-06-05")
        self.assertEqual(forwarded["role"], "codex")
        self.assertEqual(forwarded["tags"], ["coding"])

    def test_dashboard_rag_search_defaults_top_k_from_settings(self):
        status = {
            "searchAvailable": True,
            "settings": {"server_health_path": "/health"},
            "server": {"healthy": True, "url": "http://127.0.0.1:3037/health"},
            "freshness": {"status": "ready"},
        }

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": []}).encode("utf-8")

        requests = []

        def fake_urlopen(request, timeout=10):
            requests.append(request)
            return Response()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"retrieval": {"topK": 11}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(dashboard_settings, "get_rag_status", return_value=status),
                patch.object(dashboard_settings.urllib.request, "urlopen", side_effect=fake_urlopen),
            ):
                search = dashboard_settings.rag_search({"query": "deploy"})

        self.assertTrue(search["available"])
        forwarded = json.loads(requests[0].data.decode("utf-8"))
        self.assertEqual(forwarded["top_k"], 11)

    def test_dashboard_rag_v2_promote_uses_guarded_operator_service(self):
        with patch.object(
            dashboard_settings,
            "promote_v2_candidate",
            return_value={"accepted": True, "status": "promoted", "mutationPolicy": {"settingsMutated": False}},
        ) as promote:
            with patch.object(dashboard_settings, "get_rag_status", return_value={"mode": "legacy"}) as status:
                result = dashboard_settings.rag_v2_promote(
                    {
                        "runId": "run-1",
                        "confirm": True,
                        "confirmationText": "PROMOTE RAG V2 run-1",
                        "reason": "validated",
                        "rag": {"mode": "v2"},
                    }
                )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["requiredConfirmation"], "PROMOTE RAG V2 run-1")
        self.assertEqual(result["ragStatus"], {"mode": "legacy"})
        promote.assert_called_once()
        _, kwargs = promote.call_args
        self.assertEqual(kwargs["run_id"], "run-1")
        self.assertTrue(kwargs["confirm"])
        self.assertEqual(kwargs["confirmation_text"], "PROMOTE RAG V2 run-1")
        self.assertEqual(kwargs["requested_by"], "dashboard")
        self.assertEqual(kwargs["reason"], "validated")
        self.assertNotIn("require_legacy_comparison", kwargs)
        self.assertNotIn("rag", kwargs)
        status.assert_called_once_with(probe_server=False)

    def test_dashboard_rag_search_forwards_external_contract_v2_filters(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": []}).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with (
            patch.object(
                dashboard_settings,
                "get_rag_status",
                return_value={
                    "searchAvailable": True,
                    "server": {"url": "http://127.0.0.1:3037/health"},
                    "settings": {"server_health_path": "/health"},
                },
            ),
            patch.object(urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            result = dashboard_settings.rag_search(
                {
                    "query": "RAG current task",
                    "topK": 9,
                    "dateRange": {"from": "2026-06-01", "to": "2026-06-06"},
                    "project": "actanara",
                    "sourceSets": ["task-board-snapshot"],
                    "lifecycle": ["current-state"],
                    "workType": "task",
                    "includeFullText": False,
                    "includeGovernance": True,
                }
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual(captured["payload"]["date_from"], "2026-06-01")
        self.assertEqual(captured["payload"]["date_to"], "2026-06-06")
        self.assertEqual(captured["payload"]["project"], "actanara")
        self.assertEqual(captured["payload"]["source_sets"], ["task-board-snapshot"])
        self.assertEqual(captured["payload"]["lifecycle"], ["current-state"])
        self.assertEqual(captured["payload"]["work_type"], ["task"])
        self.assertEqual(captured["payload"]["latency_budget_ms"], 60000)
        self.assertGreater(captured["timeout"], 64.9)
        self.assertLessEqual(captured["timeout"], 65.0)
        self.assertFalse(captured["payload"]["include_full_text"])

    def test_dashboard_rag_v2_manifest_rollback_uses_guarded_operator_service(self):
        with patch.object(
            dashboard_settings,
            "rollback_v2_manifest",
            return_value={"accepted": True, "status": "rolled-back", "mutationPolicy": {"settingsMutated": False}},
        ) as rollback:
            with patch.object(dashboard_settings, "get_rag_status", return_value={"mode": "legacy"}) as status:
                result = dashboard_settings.rag_v2_manifest_rollback(
                    {
                        "backupName": "20260605-before-promote-run-1.json",
                        "confirm": True,
                        "confirmationText": "ROLLBACK RAG V2 MANIFEST 20260605-before-promote-run-1.json",
                        "reason": "validated rollback",
                        "rag": {"mode": "v2"},
                    }
                )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "rolled-back")
        self.assertEqual(
            result["requiredConfirmation"],
            "ROLLBACK RAG V2 MANIFEST 20260605-before-promote-run-1.json",
        )
        self.assertEqual(result["ragStatus"], {"mode": "legacy"})
        rollback.assert_called_once()
        _, kwargs = rollback.call_args
        self.assertEqual(kwargs["backup_name"], "20260605-before-promote-run-1.json")
        self.assertTrue(kwargs["confirm"])
        self.assertEqual(kwargs["confirmation_text"], "ROLLBACK RAG V2 MANIFEST 20260605-before-promote-run-1.json")
        self.assertEqual(kwargs["requested_by"], "dashboard")
        self.assertEqual(kwargs["reason"], "validated rollback")
        self.assertNotIn("rag", kwargs)
        status.assert_called_once_with(probe_server=False)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_settings_router_round_trips_without_exposing_secret(self):
        from app.routers import settings as settings_router

        with _persistent_secret_store_for_test(), tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings({"runtimeSources": {"dashboardReadSource": "foundation"}}, paths)
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                os.environ.pop("ACTANARA_DATA_FOUNDATION_ENABLED", None)
                os.environ.pop("DASHBOARD_READ_SOURCE", None)
                response = asyncio.run(
                    _dashboard_save_llm_provider_for_test(
                        settings_router,
                        {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M2.7-highspeed",
                            "apiKey": "secret",
                        },
                    )
                )
                self.assertEqual(response["apiKey"], MASKED_SECRET)
                self.assertTrue(response["hasApiKey"])
                settings_payload = asyncio.run(settings_router.api_get_settings())
                self.assertIn("catalog", settings_payload["llmProvider"])
                self.assertIn("agentSchedulePrompt", settings_payload)
                self.assertEqual(settings_payload["runtimeSources"]["dashboardReadSource"], "foundation")
                self.assertEqual(settings_payload["authority"]["runtimeSources"]["DASHBOARD_READ_SOURCE"], "foundation")
                self.assertEqual(settings_payload["runtimePath"]["selected"]["actanaraHome"], str(paths.home))
                self.assertTrue(settings_payload["runtimePath"]["validation"]["initialized"])
                saved = asyncio.run(settings_router.api_update_settings({"schedule": {"dailyPipelineTime": "05:15"}}))
                self.assertEqual(saved["schedule"]["dailyPipelineTime"], "05:15")
                handoff_required = asyncio.run(
                    settings_router.api_update_settings({"schedule": {"enabled": True}})
                )
                self.assertEqual(handoff_required.status_code, 400)
                invalid_llm = asyncio.run(settings_router.api_update_llm_provider({"mode": "custom", "provider": "custom"}))
                self.assertEqual(invalid_llm.status_code, 400)
                rejected = asyncio.run(settings_router.api_update_settings({"llmProvider": {"apiKey": "bad"}}))
                self.assertEqual(rejected.status_code, 400)
                preview = asyncio.run(settings_router.api_scheduler_system_timer_preview())
                self.assertTrue(preview["supported"])

    def test_dashboard_llm_provider_save_rejects_pipeline_unreadable_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                with (
                    patch.object(dashboard_settings, "default_secret_backend", return_value="macos-keychain"),
                    patch.object(
                        dashboard_settings,
                        "write_operator_settings_bundle",
                        return_value={"settingsTransaction": {"id": "test", "status": "committed"}},
                    ) as write_provider,
                    patch.object(dashboard_settings, "read_llm_provider", return_value={"hasApiKey": True}),
                    patch.object(
                        dashboard_settings,
                        "llm_provider_readiness_error",
                        return_value="LLM provider is not ready for pipeline execution: apiKey is not readable.",
                    ) as readiness,
                ):
                    with self.assertRaisesRegex(ValueError, "apiKey is not readable"):
                        dashboard_settings.update_llm_provider(
                            {
                                "mode": "preset",
                                "provider": "minimax-cn",
                                "model": "MiniMax-M2.7-highspeed",
                                "apiKey": "secret",
                            }
                        )

        write_provider.assert_called_once()
        readiness.assert_called_once()
        self.assertTrue(readiness.call_args.kwargs["require_cross_process_secret"])
        self.assertEqual(readiness.call_args.args[0].home, paths.home)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_settings_router_rejects_process_local_memory_provider_secret_for_pipeline(self):
        from app.routers import settings as settings_router

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            read_settings(paths, redact_secrets=False)
            raw_before = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home), "ACTANARA_SECRET_BACKEND": "memory"}):
                response = asyncio.run(
                    settings_router.api_update_llm_provider(
                        {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M2.7-highspeed",
                            "apiKey": "secret",
                        }
                    )
                )
            raw_after = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("process-local memory backend", response.body.decode("utf-8"))
        self.assertEqual(raw_after["llmProvider"], raw_before["llmProvider"])
        self.assertNotIn("minimax-cn", raw_after.get("llmProviderSecrets", {}))

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_settings_router_rejects_existing_memory_provider_secret_before_write(self):
        from app.routers import settings as settings_router

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "memory"}):
                write_llm_provider(
                    {
                        "mode": "preset",
                        "provider": "kimi-code",
                        "model": "kimi-for-coding",
                        "apiKey": "old-process-local-" + "secret",
                    },
                    paths,
                )
            raw_before = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                response = asyncio.run(
                    settings_router.api_update_llm_provider(
                        {
                            "mode": "preset",
                            "provider": "kimi-code",
                            "model": "kimi-for-coding",
                            "apiKey": MASKED_SECRET,
                        }
                    )
                )
            raw_after = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("process-local memory backend", response.body.decode("utf-8"))
        self.assertEqual(raw_after["llmProvider"], raw_before["llmProvider"])

    def test_dashboard_settings_bundle_provider_save_rejects_pipeline_unreadable_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                with (
                    patch.object(dashboard_settings, "default_secret_backend", return_value="macos-keychain"),
                    patch.object(
                        dashboard_settings,
                        "write_operator_settings_bundle",
                        return_value={"llmProvider": {"hasApiKey": True}},
                    ) as write_bundle,
                    patch.object(dashboard_settings, "read_llm_provider", return_value={"hasApiKey": True}) as read_provider,
                    patch.object(
                        dashboard_settings,
                        "llm_provider_readiness_error",
                        return_value="LLM provider is not ready for pipeline execution: apiKey is not readable.",
                    ) as readiness,
                ):
                    with self.assertRaisesRegex(ValueError, "apiKey is not readable"):
                        dashboard_settings.update_settings_bundle(
                            {
                                "llmProvider": {
                                    "mode": "preset",
                                    "provider": "minimax-cn",
                                    "model": "MiniMax-M2.7-highspeed",
                                    "apiKey": "secret",
                                }
                            }
                        )

        write_bundle.assert_called_once()
        read_provider.assert_called_once()
        readiness.assert_called_once()
        self.assertTrue(readiness.call_args.kwargs["require_cross_process_secret"])
        self.assertEqual(readiness.call_args.args[0].home, paths.home)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_settings_router_exposes_readonly_onboarding_status_and_plan(self):
        from app.routers import settings as settings_router

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                status = asyncio.run(settings_router.api_onboarding_status(["nova-rag"]))
                plan = asyncio.run(settings_router.api_onboarding_plan(["dashboard"]))
                rejected = asyncio.run(settings_router.api_onboarding_plan(["bad-profile"]))

        self.assertTrue(status["readOnly"])
        self.assertEqual(status["selectedDependencyProfiles"], ["actanara", "dashboard", "nova-rag", "nova-task"])
        self.assertTrue(plan["planOnly"])
        self.assertEqual(plan["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertEqual(rejected.status_code, 400)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_settings_path_browser_lists_directories_and_files(self):
        from app.routers import settings as settings_router

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "folder").mkdir()
            (root / "file.txt").write_text("ok", encoding="utf-8")
            result = asyncio.run(settings_router.api_path_browser(str(root)))
            names = {entry["name"]: entry["type"] for entry in result["entries"]}
            self.assertEqual(result["current"], str(root))
            self.assertEqual(names["folder"], "directory")
            self.assertEqual(names["file.txt"], "file")

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_runtime_path_router_validates_bad_requests(self):
        from app.routers import settings as settings_router

        missing = asyncio.run(settings_router.api_validate_runtime_path(None))
        self.assertEqual(missing.status_code, 400)
        invalid = asyncio.run(settings_router.api_select_runtime_path({"path": "/tmp/nova", "mode": "bad"}))
        self.assertEqual(invalid.status_code, 400)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "Dashboard runtime dependency fastapi is not installed in this interpreter")
    def test_rag_router_exposes_status_settings_and_control_actions(self):
        from fastapi import BackgroundTasks
        from app.routers import settings as settings_router

        with patch.object(settings_router.settings, "get_rag_status", return_value={"ready": True}) as status:
            response = asyncio.run(settings_router.api_rag_status(False))
        self.assertEqual(response, {"ready": True})
        status.assert_called_once_with(probe_server=False)

        with patch.object(settings_router.settings, "update_rag_settings", return_value={"rag": {"mode": "legacy"}}) as update:
            response = asyncio.run(settings_router.api_update_rag_settings({"mode": "legacy"}))
        self.assertEqual(response["rag"]["mode"], "legacy")
        update.assert_called_once()

        tasks = BackgroundTasks()
        with (
            patch.object(
                settings_router.rag_index_jobs,
                "queue_candidate_refresh",
                return_value={"accepted": True, "status": "queued", "jobId": "rag-index-1"},
            ) as queue,
            patch.object(settings_router.rag_index_jobs, "execute_candidate_refresh") as execute,
        ):
            response = asyncio.run(settings_router.api_rag_index_run(tasks))
        self.assertEqual(response.status_code, 202)
        queue.assert_called_once_with(requested_by="dashboard")
        self.assertEqual(len(tasks.tasks), 1)
        task = tasks.tasks[0]
        task_func = getattr(task, "func", task[0] if isinstance(task, tuple) else None)
        self.assertEqual(task_func, execute)

        tasks = BackgroundTasks()
        with (
            patch.object(
                settings_router.rag_index_jobs,
                "queue_production_sync",
                return_value={"accepted": True, "status": "queued", "jobId": "rag-sync-1"},
            ) as queue_sync,
            patch.object(settings_router.rag_index_jobs, "execute_production_sync") as execute_sync,
        ):
            response = asyncio.run(settings_router.api_rag_sync_run(tasks))
        self.assertEqual(response.status_code, 202)
        queue_sync.assert_called_once_with(requested_by="dashboard")
        self.assertEqual(len(tasks.tasks), 1)
        task = tasks.tasks[0]
        task_func = getattr(task, "func", task[0] if isinstance(task, tuple) else None)
        self.assertEqual(task_func, execute_sync)

        router_source = (ROOT / "src" / "dashboard" / "app" / "routers" / "settings.py").read_text(encoding="utf-8")
        self.assertNotIn('/rag/compare', router_source)
        self.assertNotIn('/rag/switch', router_source)
        self.assertFalse(hasattr(settings_router, "api_rag_compare_latest"))
        self.assertFalse(hasattr(settings_router, "api_rag_switch_apply"))

        with patch.object(settings_router.settings, "rag_coverage", return_value={"status": "ready"}) as coverage:
            response = asyncio.run(settings_router.api_rag_coverage())
        self.assertEqual(response["status"], "ready")
        coverage.assert_called_once_with()

        with patch.object(settings_router.settings, "rag_eval_latest", return_value={"status": "passed"}) as eval_latest:
            response = asyncio.run(settings_router.api_rag_eval_latest())
        self.assertEqual(response["status"], "passed")
        eval_latest.assert_called_once_with()

        with patch.object(settings_router.settings, "rag_v2_promote", return_value={"accepted": True}) as promote:
            response = asyncio.run(
                settings_router.api_rag_v2_promote(
                    {"runId": "run-1", "confirm": True, "confirmationText": "PROMOTE RAG V2 run-1"}
                )
            )
        self.assertTrue(response["accepted"])
        promote.assert_called_once_with({"runId": "run-1", "confirm": True, "confirmationText": "PROMOTE RAG V2 run-1"})

        with patch.object(settings_router.settings, "rag_v2_manifest_rollback", return_value={"accepted": True}) as rollback:
            response = asyncio.run(
                settings_router.api_rag_v2_manifest_rollback(
                    {
                        "backupName": "backup.json",
                        "confirm": True,
                        "confirmationText": "ROLLBACK RAG V2 MANIFEST backup.json",
                    }
                )
            )
        self.assertTrue(response["accepted"])
        rollback.assert_called_once_with(
            {
                "backupName": "backup.json",
                "confirm": True,
                "confirmationText": "ROLLBACK RAG V2 MANIFEST backup.json",
            }
        )

        with patch.object(settings_router.settings, "rag_search", return_value={"available": False, "results": []}) as search:
            from starlette.requests import Request

            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/rag/external/search",
                    "raw_path": b"/api/rag/external/search",
                    "query_string": b"",
                    "headers": [(b"host", b"127.0.0.1:3036")],
                    "client": ("127.0.0.1", 50000),
                    "server": ("127.0.0.1", 3036),
                }
            )
            response = asyncio.run(
                settings_router.api_rag_external_search(
                    request,
                    {"query": "hello", "tags": ["coding"]},
                )
            )
        self.assertFalse(response["available"])
        self.assertTrue(response["externalAgentContract"]["readOnly"])
        self.assertEqual(response["externalAgentContract"]["version"], 2)
        self.assertIn("queryPlan", response)
        self.assertEqual(response["queryPlan"]["query"], "hello")
        self.assertIn("citationPack", response)
        self.assertIn("eventAggregation", response)
        self.assertEqual(response["eventAggregation"]["status"], "unavailable")
        self.assertIn("answerSynthesis", response)
        self.assertEqual(response["answerSynthesis"]["status"], "unavailable")
        self.assertTrue(response["agentic"]["evidenceFieldsStable"])
        self.assertTrue(response["agentic"]["serverSideEventAggregation"])
        search.assert_called_once()

        contract = asyncio.run(settings_router.api_rag_external_contract(request))
        self.assertTrue(contract["readOnly"])
        self.assertIn("usagePrompt", contract)

        with patch.object(settings_router.settings, "rag_operator_action") as action:
            rejected = asyncio.run(settings_router.api_rag_external_reject_mutation())
        self.assertEqual(rejected.status_code, 403)
        action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
