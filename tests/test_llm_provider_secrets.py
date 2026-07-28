import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["ACTANARA_SECRET_BACKEND"] = "memory"

from data_foundation.paths import initialize_home
from data_foundation.secret_store import (
    SecretRef,
    _store_macos_keychain_secret,
    default_secret_backend,
    delete_secret,
    llm_api_key_ref,
    rag_embedding_api_key_ref,
    read_secret,
    store_secret,
)
from data_foundation.settings import MASKED_SECRET, read_llm_provider, read_settings, resolve_llm_provider, write_llm_api_key_secret, write_llm_provider, write_settings
from data_foundation.llm_provider_test import check_llm_provider_availability
from data_foundation.settings_status import actanara_settings_status


class LlmProviderSecretIsolationTests(unittest.TestCase):
    def test_runtime_file_is_default_and_ref_does_not_persist_runtime_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACTANARA_SECRET_BACKEND", None)
            ref = llm_api_key_ref(str(Path(tmp) / "Private Runtime"), name="llm-provider-api-key-glm")
            self.assertEqual(default_secret_backend(), "runtime-file")

        self.assertEqual(ref.backend, "runtime-file")
        self.assertEqual(ref.account, "llm-provider-api-key-glm")
        self.assertNotIn(tmp, json.dumps(ref.as_dict()))

    def test_runtime_file_crud_permissions_and_cross_runtime_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            first = initialize_home(Path(tmp) / "First")
            second = initialize_home(Path(tmp) / "Second")
            first_ref = llm_api_key_ref(str(first.home), name="llm-provider-api-key-glm")
            second_ref = llm_api_key_ref(str(second.home), name="llm-provider-api-key-glm")
            self.assertEqual(first_ref, second_ref)

            store_secret(first_ref, "first-runtime-value", runtime_home=first.home)
            store_secret(second_ref, "second-runtime-value", runtime_home=second.home)

            self.assertEqual(read_secret(first_ref, runtime_home=first.home), "first-runtime-value")
            self.assertEqual(read_secret(second_ref, runtime_home=second.home), "second-runtime-value")
            first_root = first.state_dir / "secrets"
            second_root = second.state_dir / "secrets"
            self.assertEqual(first_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(second_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(list(first_root.glob("*.secret"))), 1)
            self.assertEqual(len(list(second_root.glob("*.secret"))), 1)
            self.assertEqual(next(first_root.glob("*.secret")).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("first-runtime-value", next(first_root.glob("*.secret")).name)
            self.assertTrue(delete_secret(first_ref, runtime_home=first.home))
            self.assertEqual(read_secret(first_ref, runtime_home=first.home), "")
            self.assertEqual(read_secret(second_ref, runtime_home=second.home), "second-runtime-value")

    def test_runtime_file_concurrent_writes_remain_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            ref = llm_api_key_ref(str(paths.home), name="llm-provider-api-key-glm")
            values = [f"value-{index}-" + (str(index) * 2048) for index in range(8)]
            errors: list[Exception] = []

            def write(value: str) -> None:
                try:
                    store_secret(ref, value, runtime_home=paths.home)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(value,)) for value in values]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            stored = read_secret(ref, runtime_home=paths.home)
            self.assertEqual(errors, [])
            self.assertIn(stored, values)
            self.assertFalse(any((paths.state_dir / "secrets").glob("*.tmp")))

    def test_runtime_file_rejects_wide_mode_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            ref = llm_api_key_ref(str(paths.home), name="llm-provider-api-key-glm")
            store_secret(ref, "synthetic-value", runtime_home=paths.home)
            secret_root = paths.state_dir / "secrets"
            secret_path = next(secret_root.glob("*.secret"))

            secret_path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "mode, ownership, or link"):
                read_secret(ref, runtime_home=paths.home)
            secret_path.chmod(0o600)

            hardlink = secret_root / "hardlink-evidence"
            os.link(secret_path, hardlink)
            with self.assertRaisesRegex(RuntimeError, "mode, ownership, or link"):
                read_secret(ref, runtime_home=paths.home)
            hardlink.unlink()

            backing = secret_root / "backing"
            secret_path.replace(backing)
            secret_path.symlink_to(backing)
            with self.assertRaisesRegex(RuntimeError, "opened safely"):
                read_secret(ref, runtime_home=paths.home)
            secret_path.unlink()
            backing.unlink()

            secret_root.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "mode, ownership, or link"):
                store_secret(ref, "replacement", runtime_home=paths.home)

    def test_cloud_embedding_api_key_uses_runtime_file_without_settings_leak(self):
        value = "synthetic-cloud-embedding-value"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            saved = write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embedding.invalid",
                            "apiKey": value,
                        }
                    }
                },
                paths,
            )
            raw_text = (paths.config_dir / "settings.json").read_text(encoding="utf-8")
            secret_ref = saved["rag"]["embedding"]["secretRef"]

            self.assertEqual(secret_ref["backend"], "runtime-file")
            canonical_ref = rag_embedding_api_key_ref(
                str(paths.home),
                provider_id="example-cloud",
            ).as_dict()
            if secret_ref != canonical_ref:
                transaction_id = saved["settingsTransaction"]["id"]
                self.assertTrue(secret_ref["account"].startswith("settings-tx-"))
                self.assertTrue(secret_ref["account"].endswith(transaction_id))
            else:
                self.assertEqual(secret_ref, canonical_ref)
            self.assertNotIn(str(paths.home), json.dumps(secret_ref))
            self.assertNotIn(value, raw_text)
            self.assertNotIn("apiKey", json.loads(raw_text)["rag"]["embedding"])
            self.assertEqual(read_secret(secret_ref, runtime_home=paths.home), value)

    def test_linux_cloud_embedding_transaction_ref_is_journal_owned(self):
        value = "synthetic-linux-cloud-embedding-value"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ), patch("data_foundation.settings.platform.system", return_value="Linux"):
            paths = initialize_home(Path(tmp) / "Runtime")
            saved = write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embedding.invalid",
                            "apiKey": value,
                        }
                    }
                },
                paths,
            )
            transaction_id = saved["settingsTransaction"]["id"]
            secret_ref = saved["rag"]["embedding"]["secretRef"]
            journal = json.loads(
                (
                    paths.state_dir
                    / "settings-transactions"
                    / transaction_id
                    / "journal.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(saved["settingsTransaction"]["status"], "committed")
            self.assertTrue(secret_ref["account"].startswith("settings-tx-"))
            self.assertTrue(secret_ref["account"].endswith(transaction_id))
            self.assertIn(secret_ref, journal["ownedSecretRefs"])
            self.assertEqual(read_secret(secret_ref, runtime_home=paths.home), value)

    def test_readable_legacy_keychain_ref_migrates_without_delete(self):
        value = "synthetic-legacy-keychain-value"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            read_settings(paths, redact_secrets=False)
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            legacy_ref = {
                "backend": "macos-keychain",
                "service": "actanara",
                "account": f"{paths.home}:llm-provider-api-key-glm",
            }
            raw["llmProvider"].update({"provider": "glm", "model": "glm-5.1", "secretRef": legacy_ref})
            raw["llmProviderSecrets"] = {"glm": legacy_ref}
            settings_path.write_text(json.dumps(raw), encoding="utf-8")
            original_read = read_secret

            def migration_read(ref, **kwargs):
                if ref.get("backend") == "macos-keychain":
                    return value
                return original_read(ref, **kwargs)

            before = settings_path.read_bytes()
            with patch("data_foundation.settings.read_secret", side_effect=migration_read) as secret_read:
                read_only = read_settings(paths, redact_secrets=False)
                redacted = resolve_llm_provider(paths, redact_secrets=True)
                self.assertEqual(secret_read.call_count, 0)
                self.assertEqual(settings_path.read_bytes(), before)
                resolved = resolve_llm_provider(paths, redact_secrets=False)
                migrated = read_settings(paths, redact_secrets=False)

            self.assertEqual(read_only["llmProvider"]["secretRef"], legacy_ref)
            self.assertEqual(redacted["apiKey"], "")
            self.assertEqual(resolved["apiKey"], value)
            migrated_ref = migrated["llmProvider"]["secretRef"]
            self.assertEqual(migrated_ref["backend"], "runtime-file")
            self.assertEqual(migrated_ref["account"], "llm-provider-api-key-glm")
            self.assertEqual(read_secret(migrated_ref, runtime_home=paths.home), value)
            self.assertNotIn(str(paths.home), json.dumps(migrated_ref))
            self.assertEqual(legacy_ref["backend"], "macos-keychain")

    def test_unreadable_legacy_keychain_ref_is_retained_for_reentry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            read_settings(paths, redact_secrets=False)
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            legacy_ref = {
                "backend": "macos-keychain",
                "service": "actanara",
                "account": f"{paths.home}:llm-provider-api-key-glm",
            }
            raw["llmProvider"].update({"provider": "glm", "model": "glm-5.1", "secretRef": legacy_ref})
            raw["llmProviderSecrets"] = {"glm": legacy_ref}
            settings_path.write_text(json.dumps(raw), encoding="utf-8")

            with patch("data_foundation.settings.read_secret", return_value="") as keychain_read:
                retained = read_settings(paths)
                retained_again = read_settings(paths)
                redacted = resolve_llm_provider(paths, redact_secrets=True)
                self.assertEqual(keychain_read.call_count, 0)
                resolved = resolve_llm_provider(paths, redact_secrets=False)
                resolved_again = resolve_llm_provider(paths, redact_secrets=False)
                retained_after_attempt = read_settings(paths)

            self.assertEqual(retained["llmProvider"]["secretRef"], legacy_ref)
            self.assertEqual(retained["llmProviderSecrets"]["glm"], legacy_ref)
            self.assertEqual(retained_again["llmProvider"]["secretRef"], legacy_ref)
            self.assertTrue(retained["llmProvider"]["secretMigrationRequired"])
            self.assertFalse(retained["llmProvider"]["secretReadable"])
            self.assertEqual(redacted["apiKey"], "")
            self.assertEqual(resolved["apiKey"], "")
            self.assertEqual(resolved_again["apiKey"], "")
            self.assertEqual(keychain_read.call_count, 1)
            attempts = retained_after_attempt["secretMigration"]["attempts"]
            self.assertEqual(len(attempts), 1)
            self.assertEqual(next(iter(attempts.values()))["status"], "reentry-required")

    def test_installer_doctor_does_not_migrate_or_probe_legacy_keychain_refs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            read_settings(paths, redact_secrets=False)
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            legacy_ref = {
                "backend": "macos-keychain",
                "service": "actanara",
                "account": f"{paths.home}:llm-provider-api-key-minimax-cn",
            }
            raw["llmProvider"].update(
                {
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "secretRef": legacy_ref,
                }
            )
            raw["llmProviderSecrets"] = {"minimax-cn": legacy_ref}
            settings_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
            before = settings_path.read_bytes()

            with patch("data_foundation.settings.read_secret") as secret_read:
                status = actanara_settings_status(paths, doctor_profile="installer")

            self.assertTrue(status["readOnly"])
            self.assertEqual(status["summary"]["errors"], 0)
            self.assertEqual(settings_path.read_bytes(), before)
            secret_read.assert_not_called()

    def test_provider_switch_migrates_legacy_saved_ref_before_composing_update(self):
        value = "synthetic-provider-switch-migration"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
        ):
            paths = initialize_home(Path(tmp) / "Runtime")
            read_settings(paths, redact_secrets=False)
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            legacy_ref = {
                "backend": "macos-keychain",
                "service": "actanara",
                "account": f"{paths.home}:llm-provider-api-key-minimax-cn",
            }
            raw["llmProviderSecrets"] = {"minimax-cn": legacy_ref}
            settings_path.write_text(json.dumps(raw), encoding="utf-8")
            original_read = read_secret

            def migration_read(ref, **kwargs):
                if ref.get("backend") == "macos-keychain":
                    return value
                return original_read(ref, **kwargs)

            with patch("data_foundation.settings.read_secret", side_effect=migration_read):
                write_llm_provider(
                    {
                        "mode": "preset",
                        "provider": "minimax-cn",
                        "model": "MiniMax-M2.5",
                        "apiKey": MASKED_SECRET,
                    },
                    paths,
                )

            saved = read_settings(paths, redact_secrets=False)
            saved_ref = saved["llmProviderSecrets"]["minimax-cn"]
            self.assertEqual(saved_ref["backend"], "runtime-file")
            self.assertEqual(saved["llmProvider"]["secretRef"], saved_ref)
            self.assertEqual(read_secret(saved_ref, runtime_home=paths.home), value)

    def test_macos_keychain_store_keeps_secret_out_of_process_arguments(self):
        secret = "synthetic-" + "key-that-must-not-enter-argv"
        ref = SecretRef(backend="macos-keychain", service="actanara", account="test-account")
        with (
            patch("data_foundation.secret_store._store_macos_keychain_secret") as secure_store,
            patch("data_foundation.secret_store.subprocess.run") as run,
        ):
            store_secret(ref, secret)

        secure_store.assert_called_once_with("actanara", "test-account", secret)
        run.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain PTY behavior")
    def test_macos_keychain_store_uses_tty_prompt_without_secret_argv(self):
        expected = "synthetic-pty-value"
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-security"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import getpass\n"
                "import sys\n"
                f"expected = {expected!r}\n"
                "if expected in sys.argv:\n"
                "    raise SystemExit(8)\n"
                "received = getpass.getpass('password data for new item: ')\n"
                "raise SystemExit(0 if received == expected else 9)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

            _store_macos_keychain_secret(
                "actanara",
                "test-account",
                expected,
                executable=str(executable),
                timeout_seconds=5,
            )

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain PTY behavior")
    def test_macos_keychain_store_confirms_retype_prompt_without_secret_argv(self):
        expected = "synthetic-pty-confirmed-value"
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-security-retype"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import getpass\n"
                "import sys\n"
                f"expected = {expected!r}\n"
                "if expected in sys.argv:\n"
                "    raise SystemExit(8)\n"
                "received = getpass.getpass('password data for new item: ')\n"
                "confirmed = getpass.getpass('retype password for new item: ')\n"
                "raise SystemExit(0 if received == expected and confirmed == expected else 9)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

            _store_macos_keychain_secret(
                "actanara",
                "test-account",
                expected,
                executable=str(executable),
                timeout_seconds=5,
            )

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain PTY behavior")
    def test_macos_keychain_store_sanitizes_child_failure_and_timeout(self):
        secret = "synthetic-" + "pty-failure-value"
        fixtures = {
            "child-failure": (
                "#!/usr/bin/env python3\n"
                "import getpass\n"
                "received = getpass.getpass('password data for new item: ')\n"
                "print(received)\n"
                "raise SystemExit(9)\n",
                RuntimeError,
                3,
            ),
            "prompt-timeout": (
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import signal\n"
                "import time\n"
                "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                "with open(os.environ['ACTANARA_TEST_CHILD_PID_FILE'], 'w', encoding='utf-8') as handle:\n"
                "    handle.write(str(os.getpid()))\n"
                "time.sleep(10)\n",
                TimeoutError,
                0.2,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (source, error_type, timeout) in fixtures.items():
                with self.subTest(name=name):
                    executable = Path(tmp) / name
                    executable.write_text(source, encoding="utf-8")
                    executable.chmod(0o755)
                    child_pid_file = Path(tmp) / f"{name}.pid"

                    with patch.dict(os.environ, {"ACTANARA_TEST_CHILD_PID_FILE": str(child_pid_file)}):
                        with self.assertRaises(error_type) as raised:
                            _store_macos_keychain_secret(
                                "actanara",
                                "test-account",
                                secret,
                                executable=str(executable),
                                timeout_seconds=timeout,
                            )

                    self.assertNotIn(secret, str(raised.exception))
                    if child_pid_file.exists():
                        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                        for _ in range(20):
                            try:
                                os.kill(child_pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.01)
                        else:
                            self.fail(f"synthetic Keychain child {child_pid} survived timeout cleanup")

    def test_macos_keychain_store_rejects_terminal_controls_and_oversized_values(self):
        for value, message in (("bad\x03value", "control characters"), ("x" * 1024, "too large")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _store_macos_keychain_secret("actanara", "test-account", value)

    def test_macos_keychain_read_and_delete_use_fixed_binary_and_sanitized_timeout(self):
        ref = SecretRef(backend="macos-keychain", service="actanara", account="test-account")
        leaked_output = "synthetic-value-that-must-not-escape"
        timeout = subprocess.TimeoutExpired(
            ["/usr/bin/security"],
            30,
            output=leaked_output,
            stderr=leaked_output,
        )
        with patch("data_foundation.secret_store.subprocess.run", side_effect=timeout) as run:
            self.assertEqual(read_secret(ref), "")
            self.assertFalse(delete_secret(ref))

        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[0], "/usr/bin/security")
            self.assertNotIn(leaked_output, command)
            self.assertEqual(call.kwargs["timeout"], 30.0)

    def test_switching_provider_without_key_does_not_reuse_previous_provider_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": "glm-secret"}, paths)

            saved = write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": ""}, paths)
            raw = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            resolved = resolve_llm_provider(paths, redact_secrets=False)

        self.assertEqual(saved["provider"], "minimax-cn")
        self.assertFalse(saved["hasApiKey"])
        self.assertNotIn("secretRef", raw["llmProvider"])
        self.assertIn("glm", raw["llmProviderSecrets"])
        self.assertNotIn("minimax-cn", raw["llmProviderSecrets"])
        self.assertEqual(resolved["provider"], "minimax-cn")
        self.assertEqual(resolved["apiKey"], "")

    def test_provider_switch_restores_that_provider_saved_secret_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": "mini-secret"}, paths)
            write_llm_provider({"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": "glm-secret"}, paths)

            glm = resolve_llm_provider(paths, redact_secrets=False)
            saved_keys = read_llm_provider(paths).get("savedProviderKeys", {})
            write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": MASKED_SECRET}, paths)
            minimax = resolve_llm_provider(paths, redact_secrets=False)

        self.assertEqual(glm["provider"], "glm")
        self.assertEqual(glm["apiKey"], "glm-secret")
        self.assertNotIn("glm", saved_keys)
        self.assertNotIn("minimax-cn", saved_keys)
        self.assertEqual(minimax["provider"], "minimax-cn")
        self.assertEqual(minimax["apiKey"], "mini-secret")

    def test_model_key_secret_uses_active_provider_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": ""}, paths)
            write_llm_api_key_secret("glm-secret", paths)
            raw = read_settings(paths, redact_secrets=False)
            resolved = resolve_llm_provider(paths, redact_secrets=False)

        self.assertIn("glm", raw["llmProviderSecrets"])
        self.assertEqual(raw["llmProvider"]["secretRef"], raw["llmProviderSecrets"]["glm"])
        self.assertIn("llm-provider-api-key-glm", raw["llmProvider"]["secretRef"]["account"])
        self.assertEqual(resolved["apiKey"], "glm-secret")

    def test_legacy_global_secret_ref_is_migrated_to_active_provider_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": "mini-secret"}, paths)
            raw_path = paths.config_dir / "settings.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            legacy_ref = store_secret(llm_api_key_ref(str(paths.home)), "legacy-mini-secret")
            raw["llmProvider"]["secretRef"] = legacy_ref
            raw["llmProviderSecrets"] = {"minimax-cn": legacy_ref}
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            resolve_llm_provider(paths, redact_secrets=False)
            settings = read_settings(paths, redact_secrets=False)

        self.assertIn("llm-provider-api-key-minimax-cn", settings["llmProvider"]["secretRef"]["account"])
        self.assertEqual(settings["llmProviderSecrets"]["minimax-cn"], settings["llmProvider"]["secretRef"])

    def test_candidate_probe_uses_candidate_provider_saved_secret_not_active_provider_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": "mini-secret"}, paths)
            write_llm_provider({"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": "glm-secret"}, paths)
            write_llm_provider({"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": MASKED_SECRET}, paths)
            calls = []

            def fake_anthropic(**kwargs):
                calls.append(kwargs)
                return "OK"

            result = check_llm_provider_availability(
                paths,
                candidate={"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": MASKED_SECRET},
                anthropic_sender=fake_anthropic,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "glm")
        self.assertEqual(calls[0]["api_key"], "glm-secret")

    def test_candidate_probe_does_not_reuse_active_secret_for_unsaved_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_llm_provider({"mode": "preset", "provider": "glm", "model": "glm-5.1", "apiKey": "glm-secret"}, paths)

            result = check_llm_provider_availability(
                paths,
                candidate={"mode": "preset", "provider": "minimax-cn", "model": "MiniMax-M3", "apiKey": ""},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["provider"], "minimax-cn")
        self.assertIn("apiKey", result["missing"])


if __name__ == "__main__":
    unittest.main()
