import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))
os.environ["ACTANARA_SECRET_BACKEND"] = "memory"

from data_foundation.paths import initialize_home, runtime_paths_for_home
from data_foundation.secret_store import read_secret
from data_foundation.settings import (
    ensure_settings,
    read_settings,
    write_llm_provider,
    write_operator_settings_bundle,
)
from data_foundation import settings_transaction
from data_foundation.settings_transaction import (
    SettingsTransactionError,
    recover_settings_transactions,
)


class SyntheticSettingsCrash(BaseException):
    pass


class SettingsTransactionTests(unittest.TestCase):
    def _runtime(self, root: Path):
        paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
        ensure_settings(paths)
        return paths

    def _bytes(self, paths):
        return (
            (paths.config_dir / "settings.json").read_bytes(),
            (paths.config_dir / "runtime.json").read_bytes(),
        )

    def _journal(self, paths, transaction_id: str) -> dict:
        path = paths.state_dir / "settings-transactions" / transaction_id / "journal.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_runtime_file_transaction_uses_explicit_runtime_not_active_actanara_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active_paths = self._runtime(root / "active")
            target_paths = self._runtime(root / "target")
            value = "SYNTHETIC_EXPLICIT_RUNTIME_VALUE"
            with patch.dict(
                os.environ,
                {
                    "ACTANARA_SECRET_BACKEND": "runtime-file",
                    "ACTANARA_HOME": str(active_paths.home),
                },
            ):
                saved = write_operator_settings_bundle(
                    {
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "glm",
                            "model": "glm-5.1",
                            "apiKey": value,
                        }
                    },
                    target_paths,
                    readiness_verifier=lambda: None,
                )
                ref = saved["llmProvider"]["secretRef"]

                self.assertEqual(ref["backend"], "runtime-file")
                self.assertEqual(read_secret(ref, runtime_home=target_paths.home), value)
                self.assertEqual(read_secret(ref, runtime_home=active_paths.home), "")
                self.assertEqual(len(list((target_paths.state_dir / "secrets").glob("*.secret"))), 1)
                self.assertFalse((active_paths.state_dir / "secrets").exists())

    def test_manifest_failure_restores_byte_preimages_and_reports_compensation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            before = self._bytes(paths)

            def fail_before_manifest(phase, transaction_id):
                if phase == "before-runtime-manifest-commit":
                    raise OSError("synthetic manifest failure")

            with (
                patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=fail_before_manifest),
                self.assertRaises(SettingsTransactionError) as raised,
            ):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "04:20"}}},
                    paths,
                )

            after = self._bytes(paths)
            journal = self._journal(paths, raised.exception.summary["id"])

        self.assertEqual(after, before)
        self.assertEqual(raised.exception.summary["compensation"]["status"], "compensated")
        self.assertEqual(journal["status"], "compensated")
        self.assertNotIn("synthetic manifest failure", json.dumps(journal).lower())
        self.assertNotIn(str(paths.home), json.dumps(journal))

    def test_readiness_failure_restores_files_deletes_only_new_ref_and_keeps_old_ref(self):
        value_before = "SYNTHETIC_OLD_VALUE"
        value_after = "SYNTHETIC_NEW_VALUE"
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            with patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "memory"}):
                write_llm_provider(
                    {
                        "mode": "preset",
                        "provider": "minimax-cn",
                        "model": "MiniMax-M3",
                        "apiKey": value_before,
                    },
                    paths,
                )
            raw_before = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            old_ref = raw_before["llmProvider"]["secretRef"]
            before = self._bytes(paths)

            with self.assertRaises(SettingsTransactionError) as raised:
                write_operator_settings_bundle(
                    {
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M3",
                            "apiKey": value_after,
                        }
                    },
                    paths,
                    readiness_verifier=lambda: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
                )

            journal = self._journal(paths, raised.exception.summary["id"])
            new_ref = journal["ownedSecretRefs"][0]
            transaction_tree = paths.state_dir / "settings-transactions" / raised.exception.summary["id"]
            persisted_transaction_bytes = b"".join(
                path.read_bytes()
                for path in transaction_tree.iterdir()
                if path.is_file()
            )
            after = self._bytes(paths)
            old_value_after = read_secret(old_ref)
            new_value_after = read_secret(new_ref)

        self.assertEqual(after, before)
        self.assertEqual(old_value_after, value_before)
        self.assertEqual(new_value_after, "")
        self.assertEqual(raised.exception.summary["compensation"]["secretCleanup"], "deleted-or-absent")
        self.assertNotIn(value_after.encode("utf-8"), persisted_transaction_bytes)
        self.assertNotIn("provider unavailable", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_success_uses_unique_ref_and_retains_old_ref_as_gc_candidate(self):
        value_before = "SYNTHETIC_RETAINED_VALUE"
        value_after = "SYNTHETIC_COMMITTED_VALUE"
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            with patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "memory"}):
                write_llm_provider(
                    {
                        "mode": "preset",
                        "provider": "minimax-cn",
                        "model": "MiniMax-M3",
                        "apiKey": value_before,
                    },
                    paths,
                )
                old_ref = json.loads(
                    (paths.config_dir / "settings.json").read_text(encoding="utf-8")
                )["llmProvider"]["secretRef"]
                saved = write_operator_settings_bundle(
                    {
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M3",
                            "apiKey": value_after,
                        }
                    },
                    paths,
                    readiness_verifier=lambda: None,
                )
            raw_after = json.loads((paths.config_dir / "settings.json").read_text(encoding="utf-8"))
            new_ref = raw_after["llmProvider"]["secretRef"]
            retained_value = read_secret(old_ref)
            committed_value = read_secret(new_ref)

        self.assertNotEqual(new_ref, old_ref)
        self.assertTrue(new_ref["account"].startswith("settings-tx-"))
        self.assertTrue(new_ref["account"].endswith(saved["settingsTransaction"]["id"]))
        self.assertEqual(retained_value, value_before)
        self.assertEqual(committed_value, value_after)
        self.assertEqual(len(saved["settingsTransaction"]["garbageCollectionCandidateIds"]), 1)

    def test_crash_after_settings_replace_is_recovered_idempotently(self):
        original_advance = settings_transaction._advance_journal

        def crash_before_settings_phase_write(transaction_dir, journal, phase):
            if phase == "settings-committed":
                raise SyntheticSettingsCrash()
            return original_advance(transaction_dir, journal, phase)

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            before = self._bytes(paths)
            with (
                patch.object(settings_transaction, "_advance_journal", side_effect=crash_before_settings_phase_write),
                self.assertRaises(SyntheticSettingsCrash),
            ):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "05:10"}}},
                    paths,
                )
            self.assertNotEqual(self._bytes(paths)[0], before[0])

            recovered = recover_settings_transactions(paths)
            recovered_again = recover_settings_transactions(paths)
            after_recovery = self._bytes(paths)

        self.assertEqual(after_recovery, before)
        self.assertEqual(recovered[0]["status"], "compensated")
        self.assertEqual(recovered_again, [])

    def test_next_save_recovers_stale_transaction_before_applying_new_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))

            def crash_after_settings(phase, transaction_id):
                if phase == "after-settings-commit":
                    raise SyntheticSettingsCrash()

            with (
                patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=crash_after_settings,
                ),
                self.assertRaises(SyntheticSettingsCrash),
            ):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "05:15"}}},
                    paths,
                )

            saved = write_operator_settings_bundle(
                {"settings": {"schedule": {"dailyPipelineTime": "05:20"}}},
                paths,
            )

        self.assertEqual(saved["schedule"]["dailyPipelineTime"], "05:20")
        self.assertEqual(len(saved["settingsTransaction"]["recoveredTransactions"]), 1)

    def test_crash_after_manifest_replace_recovers_both_resources(self):
        original_advance = settings_transaction._advance_journal

        def crash_before_manifest_phase_write(transaction_dir, journal, phase):
            if phase == "runtime-manifest-committed":
                raise SyntheticSettingsCrash()
            return original_advance(transaction_dir, journal, phase)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            before = self._bytes(paths)
            with (
                patch.object(settings_transaction, "_advance_journal", side_effect=crash_before_manifest_phase_write),
                self.assertRaises(SyntheticSettingsCrash),
            ):
                write_operator_settings_bundle(
                    {
                        "settings": {
                            "paths": {
                                "runtime": {"database": str(root / "custom" / "nova.sqlite3")}
                            }
                        }
                    },
                    paths,
                )
            changed = self._bytes(paths)
            self.assertNotEqual(changed[0], before[0])
            self.assertNotEqual(changed[1], before[1])

            recovered = recover_settings_transactions(paths)
            after_recovery = self._bytes(paths)

        self.assertEqual(after_recovery, before)
        self.assertEqual(recovered[0]["status"], "compensated")

    def test_every_pre_finalize_checkpoint_crash_recovers_file_preimages(self):
        phases = (
            "after-journal-created",
            "after-files-staged",
            "after-secrets-created",
            "before-settings-commit",
            "after-settings-commit",
            "before-runtime-manifest-commit",
            "after-runtime-manifest-commit",
            "after-verified",
            "before-finalize",
        )
        for crash_phase in phases:
            with self.subTest(phase=crash_phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._runtime(root)
                before = self._bytes(paths)

                def crash_at_checkpoint(phase, transaction_id):
                    if phase == crash_phase:
                        raise SyntheticSettingsCrash()

                with (
                    patch.object(
                        settings_transaction,
                        "settings_transaction_checkpoint",
                        side_effect=crash_at_checkpoint,
                    ),
                    self.assertRaises(SyntheticSettingsCrash),
                ):
                    write_operator_settings_bundle(
                        {
                            "settings": {
                                "schedule": {"dailyPipelineTime": "05:45"},
                                "paths": {
                                    "runtime": {
                                        "database": str(root / "checkpoint" / "nova.sqlite3")
                                    }
                                },
                            }
                        },
                        paths,
                    )
                recovered = recover_settings_transactions(paths)

                self.assertEqual(self._bytes(paths), before)
                self.assertEqual(recovered[0]["status"], "compensated")

    def test_real_sigkill_after_file_promotions_is_recovered(self):
        for crash_phase in ("after-settings-commit", "after-runtime-manifest-commit"):
            with self.subTest(phase=crash_phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._runtime(root)
                before = self._bytes(paths)
                script = """
import os
from pathlib import Path
from unittest.mock import patch
from data_foundation.paths import runtime_paths_for_home
from data_foundation.settings import write_operator_settings_bundle
from data_foundation import settings_transaction

paths = runtime_paths_for_home(Path(os.environ["ACTANARA_HOME"]))
phase_to_kill = os.environ["ACTANARA_TEST_SETTINGS_KILL_PHASE"]
def checkpoint(phase, transaction_id):
    if phase == phase_to_kill:
        os.kill(os.getpid(), 9)
with patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=checkpoint):
    write_operator_settings_bundle(
        {
            "settings": {
                "schedule": {"dailyPipelineTime": "05:50"},
                "paths": {"runtime": {"database": os.environ["ACTANARA_TEST_DATABASE"]}},
            }
        },
        paths,
    )
"""
                env = dict(os.environ)
                env.update(
                    {
                        "ACTANARA_HOME": str(paths.home),
                        "ACTANARA_SECRET_BACKEND": "memory",
                        "ACTANARA_TEST_SETTINGS_KILL_PHASE": crash_phase,
                        "ACTANARA_TEST_DATABASE": str(root / "sigkill" / "nova.sqlite3"),
                        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
                    }
                )
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
                recovered = recover_settings_transactions(paths)

                self.assertEqual(result.returncode, -9, msg=result.stderr)
                self.assertEqual(self._bytes(paths), before)
                self.assertEqual(recovered[0]["status"], "compensated")

    def test_real_sigkill_after_finalize_keeps_committed_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            script = """
import os
from pathlib import Path
from unittest.mock import patch
from data_foundation.paths import runtime_paths_for_home
from data_foundation.settings import write_operator_settings_bundle
from data_foundation import settings_transaction

paths = runtime_paths_for_home(Path(os.environ["ACTANARA_HOME"]))
def checkpoint(phase, transaction_id):
    if phase == "after-finalize":
        os.kill(os.getpid(), 9)
with patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=checkpoint):
    write_operator_settings_bundle(
        {"settings": {"schedule": {"dailyPipelineTime": "05:55"}}},
        paths,
    )
"""
            env = dict(os.environ)
            env.update(
                {
                    "ACTANARA_HOME": str(paths.home),
                    "ACTANARA_SECRET_BACKEND": "memory",
                    "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            recovered = recover_settings_transactions(paths)
            saved = read_settings(paths, redact_secrets=False)
            journals = [
                json.loads((path / "journal.json").read_text(encoding="utf-8"))
                for path in (paths.state_dir / "settings-transactions").iterdir()
                if path.is_dir()
            ]

        self.assertEqual(result.returncode, -9, msg=result.stderr)
        self.assertEqual(recovered, [])
        self.assertEqual(saved["schedule"]["dailyPipelineTime"], "05:55")
        self.assertEqual(journals[0]["status"], "committed")

    def test_crash_after_secret_store_before_phase_write_removes_unreferenced_ref(self):
        original_advance = settings_transaction._advance_journal
        value = "SYNTHETIC_CRASH_VALUE"

        def crash_before_secret_phase_write(transaction_dir, journal, phase):
            if phase == "secrets-created":
                raise SyntheticSettingsCrash()
            return original_advance(transaction_dir, journal, phase)

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            before = self._bytes(paths)
            with (
                patch.object(settings_transaction, "_advance_journal", side_effect=crash_before_secret_phase_write),
                self.assertRaises(SyntheticSettingsCrash),
            ):
                write_operator_settings_bundle(
                    {
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M3",
                            "apiKey": value,
                        }
                    },
                    paths,
                )
            transaction_dir = next(
                path
                for path in (paths.state_dir / "settings-transactions").iterdir()
                if path.is_dir()
            )
            journal = json.loads((transaction_dir / "journal.json").read_text(encoding="utf-8"))
            owned_ref = journal["ownedSecretRefs"][0]
            self.assertEqual(read_secret(owned_ref), value)

            recovered = recover_settings_transactions(paths)
            after_recovery = self._bytes(paths)
            value_after_recovery = read_secret(owned_ref)

        self.assertEqual(after_recovery, before)
        self.assertEqual(value_after_recovery, "")
        self.assertEqual(recovered[0]["compensation"]["secretCleanup"], "deleted-or-absent")

    def test_concurrent_external_write_causes_conflict_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            settings_path = paths.config_dir / "settings.json"

            def concurrent_write_then_fail(phase, transaction_id):
                if phase != "after-settings-commit":
                    return
                current = json.loads(settings_path.read_text(encoding="utf-8"))
                current["schedule"]["dailyPipelineTime"] = "06:45"
                foundation_bytes = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                settings_transaction._atomic_replace_bytes(settings_path, foundation_bytes)
                raise OSError("synthetic post-concurrent failure")

            with (
                patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=concurrent_write_then_fail),
                self.assertRaises(SettingsTransactionError) as raised,
            ):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "04:20"}}},
                    paths,
                )
            current = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertEqual(current["schedule"]["dailyPipelineTime"], "06:45")
        self.assertTrue(raised.exception.summary["conflict"])
        self.assertEqual(raised.exception.summary["compensation"]["settings"], "not-overwritten")

    def test_conflicting_write_that_references_new_secret_prevents_secret_deletion(self):
        value_before = "SYNTHETIC_CONFLICT_OLD"
        value_after = "SYNTHETIC_CONFLICT_NEW"
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            write_llm_provider(
                {
                    "mode": "preset",
                    "provider": "minimax-cn",
                    "model": "MiniMax-M3",
                    "apiKey": value_before,
                },
                paths,
            )
            settings_path = paths.config_dir / "settings.json"
            old_ref = json.loads(settings_path.read_text(encoding="utf-8"))["llmProvider"]["secretRef"]

            def retain_ref_in_concurrent_write(phase, transaction_id):
                if phase != "after-settings-commit":
                    return
                current = json.loads(settings_path.read_text(encoding="utf-8"))
                current["schedule"]["dailyPipelineTime"] = "06:50"
                content = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                settings_transaction._atomic_replace_bytes(settings_path, content)
                raise OSError("synthetic conflict after secret commit")

            with (
                patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=retain_ref_in_concurrent_write,
                ),
                self.assertRaises(SettingsTransactionError) as raised,
            ):
                write_operator_settings_bundle(
                    {
                        "llmProvider": {
                            "mode": "preset",
                            "provider": "minimax-cn",
                            "model": "MiniMax-M3",
                            "apiKey": value_after,
                        }
                    },
                    paths,
                )
            current = json.loads(settings_path.read_text(encoding="utf-8"))
            new_ref = current["llmProvider"]["secretRef"]
            old_value = read_secret(old_ref)
            new_value = read_secret(new_ref)

        self.assertTrue(raised.exception.summary["conflict"])
        self.assertEqual(raised.exception.summary["compensation"]["secretCleanup"], "not-attempted")
        self.assertEqual(current["schedule"]["dailyPipelineTime"], "06:50")
        self.assertEqual(old_value, value_before)
        self.assertEqual(new_value, value_after)

    def test_staging_failure_is_structured_and_immediately_compensated(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            before = self._bytes(paths)

            def fail_after_journal(phase, transaction_id):
                if phase == "after-journal-created":
                    raise OSError("synthetic staging failure")

            with (
                patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=fail_after_journal),
                self.assertRaises(SettingsTransactionError) as raised,
            ):
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "06:55"}}},
                    paths,
                )
            after = self._bytes(paths)
            journal = self._journal(paths, raised.exception.summary["id"])

        self.assertEqual(after, before)
        self.assertEqual(raised.exception.summary["compensation"]["status"], "compensated")
        self.assertEqual(journal["status"], "compensated")

    def test_two_operator_transactions_serialize_without_lost_updates(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "data_foundation.settings.platform.system",
            return_value="Linux",
        ):
            paths = self._runtime(Path(tmp))
            barrier = threading.Barrier(3)
            errors = []

            def worker(payload):
                try:
                    barrier.wait(timeout=5)
                    write_operator_settings_bundle({"settings": payload}, paths)
                except Exception as error:
                    errors.append(error)

            first = threading.Thread(
                target=worker,
                args=({"schedule": {"dailyPipelineTime": "07:10"}},),
            )
            second = threading.Thread(
                target=worker,
                args=({"dashboard": {"port": 18765}},),
            )
            first.start()
            second.start()
            barrier.wait(timeout=5)
            first.join(timeout=10)
            second.join(timeout=10)
            saved = read_settings(paths, redact_secrets=False)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(saved["schedule"]["dailyPipelineTime"], "07:10")
        self.assertEqual(saved["dashboard"]["port"], 18765)

    def test_linux_operator_transaction_rejects_a_durable_update_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "data_foundation.settings.platform.system",
            return_value="Linux",
        ):
            paths = self._runtime(Path(tmp))
            owner_path = paths.home / "app" / "owner.json"
            owner_path.write_text(
                json.dumps({"txId": "active-update-owner"}) + "\n",
                encoding="utf-8",
            )
            owner_path.chmod(0o600)
            os.link(owner_path, paths.home / "app" / ".update-transaction.lock")

            with self.assertRaisesRegex(
                RuntimeError,
                "install, update, or repair transaction is active",
            ):
                write_operator_settings_bundle(
                    {"settings": {"dashboard": {"port": 18766}}},
                    paths,
                )

    def test_secret_store_failures_leave_files_unchanged(self):
        for failure in (TimeoutError("timeout"), RuntimeError("locked"), OSError("unavailable")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                paths = self._runtime(Path(tmp))
                before = self._bytes(paths)
                value = "SYNTHETIC_FAILURE_VALUE"
                with (
                    patch.object(settings_transaction, "store_secret", side_effect=failure),
                    self.assertRaises(SettingsTransactionError) as raised,
                ):
                    write_operator_settings_bundle(
                        {
                            "llmProvider": {
                                "mode": "preset",
                                "provider": "minimax-cn",
                                "model": "MiniMax-M3",
                                "apiKey": value,
                            }
                        },
                        paths,
                    )
                self.assertEqual(self._bytes(paths), before)
                self.assertEqual(raised.exception.summary["compensation"]["status"], "compensated")

    def test_unreadable_stale_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            transaction_dir = paths.state_dir / "settings-transactions" / ("a" * 32)
            transaction_dir.mkdir(parents=True)
            (transaction_dir / "settings.before").write_text("evidence", encoding="utf-8")
            (transaction_dir / "journal.json").write_text("{broken", encoding="utf-8")

            with self.assertRaises(SettingsTransactionError) as raised:
                write_operator_settings_bundle(
                    {"settings": {"schedule": {"dailyPipelineTime": "08:20"}}},
                    paths,
                )

        self.assertEqual(raised.exception.summary["status"], "recovery-blocked")
        self.assertTrue(raised.exception.summary["conflict"])

    def test_dashboard_api_adds_success_and_failure_transaction_summaries(self):
        from app.routers import settings as settings_router

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                success = asyncio.run(
                    settings_router.api_update_settings(
                        {"schedule": {"dailyPipelineTime": "09:10"}}
                    )
                )

                def fail_before_manifest(phase, transaction_id):
                    if phase == "before-runtime-manifest-commit":
                        raise OSError("synthetic api failure")

                with patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=fail_before_manifest,
                ):
                    failure = asyncio.run(
                        settings_router.api_update_settings(
                            {"schedule": {"dailyPipelineTime": "09:20"}}
                        )
                    )
            failure_payload = json.loads(failure.body)

        self.assertEqual(success["settingsTransaction"]["status"], "committed")
        self.assertEqual(failure.status_code, 500)
        self.assertEqual(failure_payload["settingsTransaction"]["compensation"]["status"], "compensated")
        self.assertNotIn("synthetic api failure", failure_payload["error"])

    def test_path_commit_keeps_manifest_authority_and_creates_expected_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            database = root / "custom" / "db" / "nova.sqlite3"
            reports = root / "custom" / "reports"
            saved = write_operator_settings_bundle(
                {
                    "settings": {
                        "paths": {
                            "runtime": {"database": str(database)},
                            "diary": {"reports": str(reports)},
                        }
                    }
                },
                paths,
            )
            resolved = runtime_paths_for_home(paths.home)
            database_parent_exists = database.parent.is_dir()
            reports_weekly_exists = (reports / "weekly").is_dir()

        self.assertEqual(saved["settingsTransaction"]["status"], "committed")
        self.assertEqual(resolved.db_path, database)
        self.assertEqual(resolved.reports_dir, reports)
        self.assertTrue(database_parent_exists)
        self.assertTrue(reports_weekly_exists)


if __name__ == "__main__":
    unittest.main()
