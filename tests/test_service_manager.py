import json
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import scheduler, service_manager
from data_foundation.paths import initialize_home
from data_foundation.runtime_mutation import (
    durable_runtime_mutation_owner,
    RuntimeMutationBusy,
    RuntimeMutationUnsafe,
    require_runtime_mutation_owner,
    runtime_mutation_guard,
)
from data_foundation.settings import read_settings, write_operator_settings, write_settings
from data_foundation.settings_transaction import (
    SettingsTransactionError,
    SettingsTransactionPlan,
    execute_settings_transaction,
    recover_settings_transactions,
)
from data_foundation import settings_transaction, systemd_user
from data_foundation.systemd_user import (
    SystemdUserError,
    control_user_units,
    dashboard_unit,
    install_user_units,
    recover_user_unit_transactions,
)


class SyntheticSystemdCrash(BaseException):
    pass


class StatefulSystemctl:
    def __init__(self):
        self.states = {}
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        verb = command[2]
        if verb == "is-enabled":
            state = self.states.get(command[3], {"enabled": False, "active": False})
            return subprocess.CompletedProcess(command, 0 if state["enabled"] else 4, "", "")
        if verb == "is-active":
            state = self.states.get(command[3], {"enabled": False, "active": False})
            return subprocess.CompletedProcess(command, 0 if state["active"] else 4, "", "")
        if verb in {"enable", "disable"}:
            now = len(command) > 3 and command[3] == "--now"
            names = command[4:] if now else command[3:]
            for name in names:
                state = self.states.setdefault(name, {"enabled": False, "active": False})
                state["enabled"] = verb == "enable"
                if now:
                    state["active"] = verb == "enable"
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb in {"start", "restart", "stop"}:
            for name in command[3:]:
                state = self.states.setdefault(name, {"enabled": False, "active": False})
                state["active"] = verb != "stop"
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class ServiceManagerTests(unittest.TestCase):
    def _runtime(self, root: Path):
        paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
        write_settings({}, paths)
        return paths

    def _linux(self):
        return (
            patch("app.services.service_manager.platform.system", return_value="Linux"),
            patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
            patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
        )

    def test_runtime_mutation_guard_serializes_two_user_runtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "runtime-a"
            second = root / "runtime-b"
            acquired = threading.Event()
            release = threading.Event()

            def hold_first_runtime():
                with runtime_mutation_guard(first):
                    acquired.set()
                    release.wait(timeout=5)

            worker = threading.Thread(target=hold_first_runtime)
            worker.start()
            self.assertTrue(acquired.wait(timeout=5))
            try:
                with self.assertRaises(RuntimeMutationBusy):
                    with runtime_mutation_guard(second, blocking=False):
                        pass
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())

    def test_pending_repair_marker_is_a_durable_mutation_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            app = runtime / "app"
            app.mkdir(parents=True)
            marker = app / ".repair-configuration-pending"
            marker.write_text("repair-owner\n", encoding="ascii")
            marker.chmod(0o600)

            self.assertEqual(durable_runtime_mutation_owner(runtime), "repair-owner")
            require_runtime_mutation_owner(runtime, owner_id="repair-owner")
            with self.assertRaises(RuntimeMutationBusy):
                require_runtime_mutation_owner(runtime, owner_id=None)
            with self.assertRaises(RuntimeMutationBusy):
                require_runtime_mutation_owner(runtime, owner_id="other-owner")

    def test_pending_repair_owner_validation_fails_closed(self):
        for defect in ("bad-mode", "symlink", "malformed", "owner-mismatch"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                runtime = Path(tmp) / "runtime"
                app = runtime / "app"
                app.mkdir(parents=True)
                marker = app / ".repair-configuration-pending"
                marker.write_text("repair-owner\n", encoding="ascii")
                marker.chmod(0o600)
                if defect == "bad-mode":
                    marker.chmod(0o644)
                elif defect == "symlink":
                    target = app / "marker-target"
                    marker.rename(target)
                    marker.symlink_to(target)
                elif defect == "malformed":
                    marker.write_text("../escape\n", encoding="ascii")
                    marker.chmod(0o600)
                else:
                    owner = app / "owner.json"
                    owner.write_text(
                        json.dumps({"txId": "update-owner"}) + "\n",
                        encoding="utf-8",
                    )
                    owner.chmod(0o600)
                    os.link(owner, app / ".update-transaction.lock")

                with self.assertRaises(RuntimeMutationUnsafe):
                    durable_runtime_mutation_owner(runtime)

    def test_linux_service_lifecycle_reconciles_definition_and_controls_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=runner,
                unit_dir=unit_dir,
            )
            with (
                self._linux()[0],
                self._linux()[1],
                self._linux()[2],
                patch("data_foundation.settings.platform.system", return_value="Linux"),
            ):
                before = manager.preview("dashboard")
                installed = manager.install(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )
                running = manager.preview("dashboard")
                write_settings({"dashboard": {"port": 4040}}, paths)
                updated = manager.update(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )
                stopped = manager.stop(
                    "dashboard",
                    {"confirmationText": "STOP ACTANARA DASHBOARD SERVICE"},
                )
                started = manager.start(
                    "dashboard",
                    {"confirmationText": "START ACTANARA DASHBOARD SERVICE"},
                )
                restarted = manager.restart(
                    "dashboard",
                    {"confirmationText": "RESTART ACTANARA DASHBOARD SERVICE"},
                )
                removed = manager.uninstall(
                    "dashboard",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )

            unit_path = unit_dir / "actanara-dashboard.service"
            unit_exists_after_uninstall = unit_path.exists()
            settings = read_settings(paths)

        self.assertEqual(before["provider"], "systemd-user")
        self.assertFalse(before["registered"])
        self.assertEqual(installed["status"], "registered")
        self.assertTrue(running["registered"])
        self.assertTrue(running["actualRunning"])
        self.assertTrue(running["definitionsAligned"])
        self.assertIn("actanara-dashboard.service", updated["restartedUnits"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(started["status"], "running")
        self.assertEqual(restarted["status"], "running")
        self.assertEqual(removed["status"], "unregistered")
        self.assertFalse(unit_exists_after_uninstall)
        self.assertFalse(settings["dashboard"]["systemdUser"]["registered"])
        self.assertFalse(settings["dashboard"]["server"]["enabled"])
        self.assertFalse(any("sudo" in item for command in runner.commands for item in command))

    def test_linux_rag_uninstall_disables_server_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=runner,
                unit_dir=unit_dir,
            )
            with self._linux()[0], self._linux()[1], self._linux()[2]:
                manager.install(
                    "rag",
                    {"confirmationText": "INSTALL ACTANARA RAG SERVICE"},
                )
                manager.uninstall(
                    "rag",
                    {"confirmationText": "UNINSTALL ACTANARA RAG SERVICE"},
                )
            saved = read_settings(paths)

        self.assertFalse(saved["rag"]["server"]["enabled"])
        self.assertFalse(saved["rag"]["server"]["systemdUser"]["registered"])

    def test_linux_settings_write_is_blocked_by_a_durable_update_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            settings_path = paths.config_dir / "settings.json"
            before = settings_path.read_bytes()
            (paths.home / "app").mkdir(exist_ok=True)
            owner = paths.home / "app" / "update-owner.json"
            owner.write_text(
                json.dumps({"txId": "update-owner-fixture"}) + "\n",
                encoding="utf-8",
            )
            owner.chmod(0o600)
            os.link(owner, paths.home / "app" / ".update-transaction.lock")

            with (
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                self.assertRaisesRegex(RuntimeError, "transaction is active"),
            ):
                write_settings({"dashboard": {"port": 4040}}, paths)

            after = settings_path.read_bytes()

        self.assertEqual(after, before)

    def test_linux_read_does_not_persist_additive_defaults_during_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            raw["dashboard"].pop("port", None)
            settings_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            before = settings_path.read_bytes()
            (paths.home / "app").mkdir(exist_ok=True)
            owner = paths.home / "app" / "update-owner.json"
            owner.write_text(
                json.dumps({"txId": "update-owner-fixture"}) + "\n",
                encoding="utf-8",
            )
            owner.chmod(0o600)
            os.link(owner, paths.home / "app" / ".update-transaction.lock")

            with patch("data_foundation.settings.platform.system", return_value="Linux"):
                observed = read_settings(paths)
            after = settings_path.read_bytes()

        self.assertEqual(observed["dashboard"]["port"], 3036)
        self.assertEqual(after, before)

    def test_next_linux_settings_commit_finalizes_a_crash_after_settings_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=runner,
                unit_dir=unit_dir,
            )

            def interrupt(phase, _transaction_id):
                if phase == "after-finalize":
                    raise SyntheticSystemdCrash()

            with (
                patch("app.services.service_manager.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=interrupt,
                ),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                manager.install(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )

            transaction_root = paths.state_dir / "systemd-transactions"
            journal_path = next(
                path / "journal.json"
                for path in transaction_root.iterdir()
                if path.is_dir()
            )
            interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch(
                    "data_foundation.systemd_user._run_systemctl",
                    side_effect=lambda arguments, **_kwargs: runner(
                        ["/usr/bin/systemctl", "--user", *arguments]
                    ),
                ),
                patch("data_foundation.settings.platform.system", return_value="Linux"),
            ):
                write_settings({"dashboard": {"port": 4040}}, paths)
            recovered = json.loads(journal_path.read_text(encoding="utf-8"))

        self.assertEqual(interrupted["status"], "active")
        self.assertEqual(recovered["status"], "committed")

    def test_settings_recovery_precedes_systemd_after_precommit_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=runner,
                unit_dir=unit_dir,
            )

            def interrupt(phase, _transaction_id):
                if phase == "after-settings-commit":
                    raise SyntheticSystemdCrash()

            with (
                patch("app.services.service_manager.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=interrupt,
                ),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                manager.install(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )

            transaction_root = paths.state_dir / "systemd-transactions"
            journal_path = next(
                path / "journal.json"
                for path in transaction_root.iterdir()
                if path.is_dir()
            )
            interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(interrupted.get("settingsTransactionId"))
            self.assertTrue(runner.states["actanara-dashboard.service"]["active"])

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch(
                    "data_foundation.systemd_user._run_systemctl",
                    side_effect=lambda arguments, **_kwargs: runner(
                        ["/usr/bin/systemctl", "--user", *arguments]
                    ),
                ),
                patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ),
                patch("data_foundation.settings.platform.system", return_value="Linux"),
            ):
                write_settings({"dashboard": {"port": 4040}}, paths)

            recovered = json.loads(journal_path.read_text(encoding="utf-8"))
            saved = read_settings(paths, persist_defaults=False)

        self.assertEqual(recovered["status"], "compensated")
        self.assertFalse(runner.states["actanara-dashboard.service"]["enabled"])
        self.assertFalse(runner.states["actanara-dashboard.service"]["active"])
        self.assertFalse(
            saved["dashboard"].get("systemdUser", {}).get("registered", False)
        )
        self.assertEqual(saved["dashboard"]["port"], 4040)

    def test_noop_settings_recovery_still_compensates_coupled_systemd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            settings_before = (paths.config_dir / "settings.json").read_bytes()
            manifest_before = (paths.config_dir / "runtime.json").read_bytes()
            holder = {}

            def prepare(_transaction_id, _settings_before, _manifest_before):
                return SettingsTransactionPlan(
                    settings_bytes=settings_before,
                    manifest_bytes=manifest_before,
                    secret_writes=[],
                    garbage_collection_candidates=[],
                )

            def precommit(context):
                holder["result"] = install_user_units(
                    paths,
                    [unit],
                    unit_dir=unit_dir,
                    runner=runner,
                    defer_commit=True,
                    transaction_context=context,
                )

            def interrupt(phase, _transaction_id):
                if phase == "after-settings-commit":
                    raise SyntheticSystemdCrash()

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch(
                    "data_foundation.systemd_user._systemctl_binary",
                    return_value="/usr/bin/systemctl",
                ),
                patch.object(
                    settings_transaction,
                    "settings_transaction_checkpoint",
                    side_effect=interrupt,
                ),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                execute_settings_transaction(
                    paths,
                    prepare,
                    precommit_side_effects=precommit,
                )

            systemd_journal_path = (
                paths.state_dir
                / "systemd-transactions"
                / holder["result"]["transactionId"]
                / "journal.json"
            )
            interrupted = json.loads(
                systemd_journal_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                interrupted["settingsBeforeHash"],
                interrupted["settingsAfterHash"],
            )

            settings_recovery = recover_settings_transactions(paths)
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch(
                    "data_foundation.systemd_user._systemctl_binary",
                    return_value="/usr/bin/systemctl",
                ),
                patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ),
            ):
                systemd_recovery = recover_user_unit_transactions(
                    paths,
                    runner=runner,
                )
            recovered = json.loads(
                systemd_journal_path.read_text(encoding="utf-8")
            )

        self.assertEqual(settings_recovery[0]["status"], "compensated")
        self.assertEqual(systemd_recovery[0]["status"], "compensated")
        self.assertEqual(recovered["status"], "compensated")
        self.assertFalse((unit_dir / unit.name).exists())
        self.assertEqual(
            runner.states[unit.name],
            {"enabled": False, "active": False},
        )

    def test_linux_dashboard_self_uninstall_queues_transient_job_after_settings_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            unit = dashboard_unit(paths, {"server": {"enabled": True}})
            (unit_dir / unit.name).write_text(unit.content, encoding="utf-8")
            write_settings(
                {
                    "dashboard": {
                        "server": {"enabled": True},
                        "systemdUser": {
                            "registered": True,
                            "units": [unit.name],
                        },
                    }
                },
                paths,
            )
            submitted = []

            def systemd_run(command, **kwargs):
                committed = read_settings(paths)
                self.assertFalse(committed["dashboard"]["server"]["enabled"])
                self.assertFalse(committed["dashboard"]["systemdUser"]["registered"])
                submitted.append(list(command))
                return subprocess.CompletedProcess(command, 0, "", "")

            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=StatefulSystemctl(),
                systemd_run_runner=systemd_run,
                unit_dir=unit_dir,
            )
            with (
                self._linux()[0],
                self._linux()[1],
                self._linux()[2],
                patch("data_foundation.systemd_user._systemd_run_binary", return_value="/usr/bin/systemd-run"),
            ):
                result = manager.enqueue(
                    "dashboard",
                    "uninstall",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )

            saved = read_settings(paths)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "queued")
        self.assertIn("settingsTransaction", result)
        self.assertEqual(len(submitted), 1)
        command = submitted[0]
        self.assertEqual(command[:2], ["/usr/bin/systemd-run", "--user"])
        self.assertIn("--no-block", command)
        self.assertIn("--collect", command)
        self.assertIn("--on-active=1s", command)
        self.assertIn("--timer-property=AccuracySec=1s", command)
        self.assertIn("--property=Restart=on-failure", command)
        self.assertIn("--property=RestartSec=5s", command)
        self.assertIn("--property=RestartPreventExitStatus=1", command)
        self.assertIn("--property=StartLimitIntervalSec=0", command)
        self.assertIn("data_foundation.systemd_user", command)
        self.assertIn("service-action", command)
        self.assertFalse(saved["dashboard"]["server"]["enabled"])
        self.assertFalse(saved["dashboard"]["systemdUser"]["registered"])
        self.assertEqual(
            saved["dashboard"]["systemdUser"]["pendingJobUnit"],
            result["job"]["unitName"],
        )
        self.assertEqual(
            saved["dashboard"]["systemdUser"]["pendingRequestId"],
            result["job"]["requestId"],
        )

    def test_linux_service_api_returns_202_for_self_affecting_action(self):
        try:
            from app.routers import settings as settings_router
        except ModuleNotFoundError as exc:
            if exc.name == "fastapi":
                self.skipTest("FastAPI is not installed")
            raise
        queued = {
            "accepted": True,
            "status": "queued",
            "provider": "systemd-user",
            "job": {"unitName": "actanara-service-control-test.service"},
        }
        with (
            patch.object(service_manager, "service_action_requires_async", return_value=True),
            patch.object(service_manager, "enqueue_service_action", return_value=queued) as enqueue,
            patch.object(service_manager, "uninstall_service") as synchronous,
        ):
            response = asyncio.run(
                settings_router.api_service_manager_action(
                    "dashboard",
                    "uninstall",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body), queued)
        enqueue.assert_called_once()
        synchronous.assert_not_called()

    def test_only_linux_mutating_self_actions_require_transient_jobs(self):
        with patch.object(service_manager.platform, "system", return_value="Linux"):
            for action in ("install", "uninstall", "stop", "restart"):
                with self.subTest(action=action):
                    self.assertTrue(
                        service_manager.service_action_requires_async("dashboard", action)
                    )
            self.assertFalse(service_manager.service_action_requires_async("dashboard", "start"))
            self.assertFalse(
                service_manager.service_action_requires_async(
                    "dashboard",
                    "restart",
                    {"dryRun": True},
                )
            )
            self.assertFalse(service_manager.service_action_requires_async("rag", "uninstall"))
        with patch.object(service_manager.platform, "system", return_value="Darwin"):
            for action in ("install", "uninstall", "stop", "restart"):
                with self.subTest(platform="Darwin", action=action):
                    self.assertFalse(
                        service_manager.service_action_requires_async("dashboard", action)
                    )

    def test_linux_enqueue_does_not_submit_job_when_settings_transaction_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            submitted = []

            def systemd_run(command, **kwargs):
                submitted.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemd_run_runner=systemd_run,
            )
            failure = SettingsTransactionError(
                {
                    "id": "settings-failure",
                    "phase": "commit",
                    "status": "failed",
                    "compensation": {"status": "complete"},
                }
            )
            with (
                self._linux()[0],
                patch.object(service_manager, "write_service_manager_settings", side_effect=failure),
                self.assertRaises(SettingsTransactionError),
            ):
                manager.enqueue(
                    "dashboard",
                    "uninstall",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )

        self.assertEqual(submitted, [])

    def test_linux_enqueue_rejects_a_stale_unit_render_before_job_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            submitted = []
            original_writer = service_manager.write_service_manager_settings
            changed = False

            def change_settings_before_prepare(*args, **kwargs):
                nonlocal changed
                if not changed:
                    changed = True
                    write_settings({"dashboard": {"port": 4545}}, paths)
                return original_writer(*args, **kwargs)

            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemd_run_runner=lambda command, **kwargs: (
                    submitted.append(command)
                    or subprocess.CompletedProcess(command, 0, "", "")
                ),
            )
            with (
                self._linux()[0],
                self._linux()[1],
                self._linux()[2],
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                patch.object(
                    service_manager,
                    "write_service_manager_settings",
                    side_effect=change_settings_before_prepare,
                ),
                self.assertRaisesRegex(
                    service_manager.ServiceManagerError,
                    "Settings changed while the unit handoff was prepared",
                ),
            ):
                manager.enqueue(
                    "dashboard",
                    "install",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )

            saved = read_settings(paths)

        self.assertEqual(submitted, [])
        self.assertEqual(saved["dashboard"]["port"], 4545)
        self.assertIsNone(
            saved["dashboard"].get("systemdUser", {}).get("pendingRequestId")
        )

    def test_linux_enqueue_submission_failure_restores_previous_desired_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            write_settings(
                {
                    "dashboard": {
                        "server": {"enabled": True},
                        "systemdUser": {
                            "registered": True,
                            "units": ["actanara-dashboard.service"],
                        },
                    }
                },
                paths,
            )

            def systemd_run(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "", "manager unavailable")

            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemd_run_runner=systemd_run,
            )
            with (
                self._linux()[0],
                self._linux()[1],
                self._linux()[2],
                patch("data_foundation.systemd_user._systemd_run_binary", return_value="/usr/bin/systemd-run"),
                self.assertRaisesRegex(service_manager.ServiceManagerError, "manager unavailable"),
            ):
                manager.enqueue(
                    "dashboard",
                    "uninstall",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )
            saved = read_settings(paths)

        self.assertTrue(saved["dashboard"]["server"]["enabled"])
        registration = saved["dashboard"]["systemdUser"]
        self.assertTrue(registration["registered"])
        self.assertEqual(registration["lastActionStatus"], "failed")
        self.assertIsNone(registration["pendingAction"])

    def test_transient_helper_failure_restores_queued_dashboard_state(self):
        for action in ("install", "uninstall"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._runtime(root)
                unit_dir = root / "units"
                unit_dir.mkdir()
                previously_registered = action == "uninstall"
                previous_units = (
                    ["actanara-dashboard.service"] if previously_registered else []
                )
                write_settings(
                    {
                        "dashboard": {
                            "server": {"enabled": previously_registered},
                            "systemdUser": {
                                "registered": previously_registered,
                                "units": previous_units,
                            },
                        }
                    },
                    paths,
                )
                unit = dashboard_unit(paths, {"server": {"enabled": True}})
                runner = StatefulSystemctl()
                if previously_registered:
                    (unit_dir / unit.name).write_text(unit.content, encoding="utf-8")
                    runner.states[unit.name] = {"enabled": True, "active": True}

                rejected_verb = "disable" if action == "uninstall" else "enable"

                def failing_runner(command, **kwargs):
                    if command[2] == rejected_verb:
                        return subprocess.CompletedProcess(command, 2, "", "synthetic helper failure")
                    return runner(command, **kwargs)

                manager = service_manager.PlatformServiceManager(
                    paths=paths,
                    systemd_run_runner=lambda command, **kwargs: subprocess.CompletedProcess(
                        command, 0, "", ""
                    ),
                    unit_dir=unit_dir,
                )
                confirmation = (
                    "INSTALL ACTANARA DASHBOARD SERVICE"
                    if action == "install"
                    else "UNINSTALL ACTANARA DASHBOARD SERVICE"
                )
                with (
                    self._linux()[0],
                    self._linux()[1],
                    self._linux()[2],
                    patch(
                        "data_foundation.systemd_user._systemd_run_binary",
                        return_value="/usr/bin/systemd-run",
                    ),
                ):
                    queued = manager.enqueue(
                        "dashboard",
                        action,
                        {"confirmationText": confirmation},
                    )
                    queued_settings = read_settings(paths)
                    queued_registration = queued_settings["dashboard"]["systemdUser"]
                    request_id = queued["job"]["requestId"]
                    self.assertEqual(
                        queued_registration["pendingPreviousState"],
                        {
                            "serverEnabled": previously_registered,
                            "registered": previously_registered,
                            "units": previous_units,
                        },
                    )
                    with self.assertRaisesRegex(SystemdUserError, "compensation=compensated"):
                        systemd_user.execute_queued_user_unit_action(
                            paths,
                            kind="dashboard",
                            action=action,
                            request_id=request_id,
                            unit_dir=unit_dir,
                            runner=failing_runner,
                        )
                saved = read_settings(paths)

                self.assertIs(
                    saved["dashboard"]["server"]["enabled"],
                    previously_registered,
                )
                registration = saved["dashboard"]["systemdUser"]
                self.assertIs(registration["registered"], previously_registered)
                self.assertEqual(registration["units"], previous_units)
                self.assertEqual(registration["lastActionStatus"], "failed")
                self.assertIn("compensation=compensated", registration["lastError"])
                self.assertIsNone(registration["pendingAction"])
                self.assertIsNone(registration["pendingRequestId"])
                self.assertIsNone(registration["pendingPreviousState"])

    def test_transient_helper_finishes_queued_uninstall_and_audit(self):
        request_id = "1" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            dashboard = {
                "server": {"enabled": False},
                "systemdUser": {
                    "registered": False,
                    "units": ["actanara-dashboard.service"],
                    "pendingAction": "uninstall",
                    "pendingRequestId": request_id,
                    "lastActionStatus": "queued",
                    "pendingPreviousState": {
                        "serverEnabled": True,
                        "registered": True,
                        "units": ["actanara-dashboard.service"],
                    },
                },
            }
            unit = dashboard_unit(paths, dashboard)
            (unit_dir / unit.name).write_text(unit.content, encoding="utf-8")
            write_settings({"dashboard": dashboard}, paths)
            runner = StatefulSystemctl()
            runner.states[unit.name] = {"enabled": True, "active": True}

            with self._linux()[1], self._linux()[2]:
                result = systemd_user.execute_queued_user_unit_action(
                    paths,
                    kind="dashboard",
                    action="uninstall",
                    request_id=request_id,
                    unit_dir=unit_dir,
                    runner=runner,
                )
            saved = read_settings(paths)
            journals = list((paths.state_dir / "systemd-transactions").glob("*/journal.json"))
            journal = json.loads(journals[0].read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse((unit_dir / unit.name).exists())
        registration = saved["dashboard"]["systemdUser"]
        self.assertFalse(saved["dashboard"]["server"]["enabled"])
        self.assertFalse(registration["registered"])
        self.assertEqual(registration["lastActionStatus"], "success")
        self.assertIsNone(registration["pendingAction"])
        self.assertIsNone(registration["pendingRequestId"])
        self.assertIsNone(registration["pendingPreviousState"])
        self.assertEqual(journal["status"], "committed")
        self.assertTrue(journal["settingsBeforeHash"])
        self.assertTrue(journal["settingsAfterHash"])
        self.assertNotEqual(journal["settingsBeforeHash"], journal["settingsAfterHash"])

    def test_transient_helper_rejects_settings_changed_after_unit_render(self):
        request_id = "2" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            write_settings(
                {
                    "dashboard": {
                        "port": 3036,
                        "server": {"enabled": False},
                        "systemdUser": {
                            "registered": False,
                            "units": [],
                            "pendingAction": "install",
                            "pendingRequestId": request_id,
                            "lastActionStatus": "queued",
                            "pendingPreviousState": {
                                "serverEnabled": False,
                                "registered": False,
                                "units": [],
                            },
                        },
                    }
                },
                paths,
            )
            original_record = systemd_user._record_queued_registration_result
            changed = False

            def change_settings_before_prepare(*args, **kwargs):
                nonlocal changed
                if kwargs.get("error") is None and not changed:
                    changed = True
                    write_settings({"dashboard": {"port": 4545}}, paths)
                return original_record(*args, **kwargs)

            runner = StatefulSystemctl()
            with (
                self._linux()[1],
                self._linux()[2],
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                patch.object(
                    systemd_user,
                    "_record_queued_registration_result",
                    side_effect=change_settings_before_prepare,
                ),
                self.assertRaisesRegex(SystemdUserError, "service action is stale"),
            ):
                systemd_user.execute_queued_user_unit_action(
                    paths,
                    kind="dashboard",
                    action="install",
                    request_id=request_id,
                    unit_dir=unit_dir,
                    runner=runner,
                )

            saved = read_settings(paths)

        self.assertEqual(runner.commands, [])
        self.assertEqual(saved["dashboard"]["port"], 4545)
        self.assertEqual(
            saved["dashboard"]["systemdUser"]["lastActionStatus"],
            "failed",
        )
        self.assertIsNone(
            saved["dashboard"]["systemdUser"]["pendingRequestId"]
        )

    def test_transient_helper_defers_while_a_durable_update_owner_remains(self):
        request_id = "4" * 32
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            write_settings(
                {
                    "dashboard": {
                        "systemdUser": {
                            "registered": False,
                            "units": [],
                            "pendingAction": "install",
                            "pendingRequestId": request_id,
                            "lastActionStatus": "queued",
                            "pendingPreviousState": {
                                "serverEnabled": False,
                                "registered": False,
                                "units": [],
                            },
                        }
                    }
                },
                paths,
            )
            (paths.home / "app").mkdir(exist_ok=True)
            owner = paths.home / "app" / "update-owner.json"
            owner.write_text(
                json.dumps({"txId": "stale-update-owner"}) + "\n",
                encoding="utf-8",
            )
            owner.chmod(0o600)
            os.link(owner, paths.home / "app" / ".update-transaction.lock")

            with (
                self._linux()[1],
                self.assertRaisesRegex(
                    systemd_user._QueuedActionRetryable,
                    "transaction is active",
                ),
            ):
                systemd_user.execute_queued_user_unit_action(
                    paths,
                    kind="dashboard",
                    action="install",
                    request_id=request_id,
                    unit_dir=Path(tmp) / "units",
                    runner=StatefulSystemctl(),
                )
            saved = read_settings(paths)

        self.assertEqual(
            saved["dashboard"]["systemdUser"]["pendingRequestId"],
            request_id,
        )
        self.assertEqual(
            saved["dashboard"]["systemdUser"]["lastActionStatus"],
            "queued",
        )

    def test_transient_helper_reloads_after_runtime_generation_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            old_source = root / "release-old"
            current_source = root / "release-current"
            old_source.mkdir()
            current_source.mkdir()
            (paths.home / "app").mkdir(exist_ok=True)
            source_pointer = paths.home / "app" / "source"
            source_pointer.symlink_to(current_source)

            with (
                self._linux()[1],
                self.assertRaisesRegex(
                    systemd_user._QueuedHelperReloadRequired,
                    "reload the current Runtime generation",
                ),
            ):
                systemd_user.execute_queued_user_unit_action(
                    paths,
                    kind="dashboard",
                    action="restart",
                    request_id="5" * 32,
                    unit_dir=root / "units",
                    runner=StatefulSystemctl(),
                    loaded_source_root=old_source,
                )

    def test_transient_control_helper_rejects_settings_changed_after_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            runner = StatefulSystemctl()
            settings = read_settings(paths, redact_secrets=False)
            dashboard = settings.get("dashboard", {})
            accepted_units = [dashboard_unit(paths, dashboard)]
            expected_unit_sha256 = systemd_user.user_unit_set_sha256(
                accepted_units
            )
            write_settings({"dashboard": {"port": 4545}}, paths)

            with (
                self._linux()[1],
                self.assertRaisesRegex(
                    systemd_user._QueuedActionStale,
                    "Settings changed after the action was accepted",
                ),
            ):
                systemd_user.execute_queued_user_unit_action(
                    paths,
                    kind="dashboard",
                    action="restart",
                    request_id="7" * 32,
                    unit_dir=root / "units",
                    runner=runner,
                    expected_unit_sha256=expected_unit_sha256,
                )

        self.assertEqual(runner.commands, [])

    def test_transient_helper_retryable_exit_requests_systemd_restart(self):
        argv = [
            "service-action",
            "--runtime-home",
            "/tmp/actanara-retry-fixture",
            "--kind",
            "dashboard",
            "--action",
            "restart",
            "--request-id",
            "6" * 32,
            "--source-generation",
            "/tmp/actanara-retry-source",
            "--expected-unit-sha256",
            "a" * 64,
            "--unit-dir",
            "/tmp/actanara-retry-units",
        ]
        with patch.object(
            systemd_user,
            "execute_queued_user_unit_action",
            side_effect=systemd_user._QueuedActionRetryable("update recovery pending"),
        ):
            status = systemd_user._service_action_main(argv)

        self.assertEqual(status, 75)

    def test_linux_service_settings_failure_compensates_unit_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            manager = service_manager.PlatformServiceManager(
                paths=paths,
                systemctl_runner=runner,
                unit_dir=unit_dir,
            )

            def fail_after_external(phase, transaction_id):
                if phase == "after-precommit-side-effects":
                    raise OSError("synthetic settings failure")

            with (
                self._linux()[0],
                self._linux()[1],
                self._linux()[2],
                patch.object(settings_transaction, "settings_transaction_checkpoint", side_effect=fail_after_external),
                self.assertRaises(SettingsTransactionError),
            ):
                manager.install(
                    "rag",
                    {"confirmationText": "INSTALL ACTANARA RAG SERVICE"},
                )

            unit_exists = (unit_dir / "actanara-rag-server.service").exists()
            settings = read_settings(paths)

        self.assertFalse(unit_exists)
        self.assertFalse(((settings.get("rag") or {}).get("server") or {}).get("systemdUser", {}).get("registered", False))

    def test_systemd_interruption_recovers_prior_definition_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            runner = StatefulSystemctl()
            before_unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            after_unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 4040})
            target = unit_dir / before_unit.name
            target.write_text(before_unit.content, encoding="utf-8")
            runner.states[before_unit.name] = {"enabled": True, "active": True}

            def interrupt(phase, transaction_id):
                if phase == "after-definitions-applied":
                    raise SyntheticSystemdCrash()

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch.object(systemd_user, "systemd_transaction_checkpoint", side_effect=interrupt),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                install_user_units(paths, [after_unit], unit_dir=unit_dir, runner=runner)

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ),
            ):
                recovered = recover_user_unit_transactions(paths, runner=runner)
                recovered_again = recover_user_unit_transactions(paths, runner=runner)
            content = target.read_text(encoding="utf-8")

        self.assertEqual(content, before_unit.content)
        self.assertEqual(recovered[0]["status"], "compensated")
        self.assertEqual(recovered_again, [])
        self.assertEqual(runner.states[before_unit.name], {"enabled": True, "active": True})

    def test_systemd_compensation_is_restartable_after_each_owned_mutation(self):
        checkpoints = (
            "after-compensation-definition:actanara-dashboard.service",
            "after-compensation-daemon-reload",
            "after-compensation-disable",
            "after-compensation-state:actanara-dashboard.service",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._runtime(root)
                unit_dir = root / "units"
                unit_dir.mkdir()
                runner = StatefulSystemctl()
                unit = dashboard_unit(
                    paths,
                    {"host": "127.0.0.1", "port": 3036},
                )

                def interrupt(phase, _transaction_id):
                    if phase == checkpoint:
                        raise SyntheticSystemdCrash()

                with (
                    patch(
                        "data_foundation.systemd_user.platform.system",
                        return_value="Linux",
                    ),
                    patch(
                        "data_foundation.systemd_user._systemctl_binary",
                        return_value="/usr/bin/systemctl",
                    ),
                    patch.object(
                        systemd_user,
                        "systemd_transaction_checkpoint",
                        side_effect=interrupt,
                    ),
                    self.assertRaises(SyntheticSystemdCrash),
                ):
                    install_user_units(
                        paths,
                        [unit],
                        unit_dir=unit_dir,
                        runner=runner,
                        readiness_verifier=lambda: (_ for _ in ()).throw(
                            RuntimeError("synthetic readiness failure")
                        ),
                    )

                journal_path = next(
                    (paths.state_dir / "systemd-transactions").glob(
                        "*/journal.json"
                    )
                )
                interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(interrupted["phase"], "compensation-armed")
                with (
                    patch(
                        "data_foundation.systemd_user.platform.system",
                        return_value="Linux",
                    ),
                    patch(
                        "data_foundation.systemd_user._systemctl_binary",
                        return_value="/usr/bin/systemctl",
                    ),
                    patch(
                        "data_foundation.systemd_user._same_systemd_transaction_owner",
                        return_value=False,
                    ),
                ):
                    recovery = recover_user_unit_transactions(
                        paths,
                        runner=runner,
                    )
                recovered = json.loads(journal_path.read_text(encoding="utf-8"))

                self.assertEqual(recovery[0]["status"], "compensated")
                self.assertEqual(recovered["status"], "compensated")
                self.assertFalse((unit_dir / unit.name).exists())
                self.assertEqual(
                    runner.states[unit.name],
                    {"enabled": False, "active": False},
                )

    def test_systemd_transaction_is_published_only_after_complete_staging(self):
        for failure_kind in ("interruption", "write-failure"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._runtime(root)
                unit_dir = root / "units"
                unit_dir.mkdir()
                before_unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
                after_unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 4040})
                target = unit_dir / before_unit.name
                target.write_text(before_unit.content, encoding="utf-8")
                runner = StatefulSystemctl()

                def interrupt(phase, _transaction_id):
                    if phase == "before-transaction-publish":
                        raise SyntheticSystemdCrash()

                failure_patch = (
                    patch.object(
                        systemd_user,
                        "systemd_transaction_checkpoint",
                        side_effect=interrupt,
                    )
                    if failure_kind == "interruption"
                    else patch.object(
                        systemd_user,
                        "_write_systemd_journal",
                        side_effect=OSError("synthetic journal write failure"),
                    )
                )
                expected_error = SyntheticSystemdCrash if failure_kind == "interruption" else OSError
                with (
                    patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                    patch(
                        "data_foundation.systemd_user._systemctl_binary",
                        return_value="/usr/bin/systemctl",
                    ),
                    failure_patch,
                    self.assertRaises(expected_error),
                ):
                    install_user_units(
                        paths,
                        [after_unit],
                        unit_dir=unit_dir,
                        runner=runner,
                    )

                transaction_root = paths.state_dir / "systemd-transactions"
                published = [path for path in transaction_root.iterdir() if path.is_dir()]
                with (
                    patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                    patch(
                        "data_foundation.systemd_user._systemctl_binary",
                        return_value="/usr/bin/systemctl",
                    ),
                ):
                    recovered = recover_user_unit_transactions(paths, runner=runner)

                self.assertEqual(published, [])
                self.assertEqual(recovered, [])
                self.assertEqual(target.read_text(encoding="utf-8"), before_unit.content)

    def test_scoped_systemd_recovery_touches_only_matching_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            runner = StatefulSystemctl()
            first = dashboard_unit(
                paths,
                {
                    "host": "127.0.0.1",
                    "port": 3036,
                    "systemdUser": {"units": ["actanara-owner-a.service"]},
                },
            )
            second = dashboard_unit(
                paths,
                {
                    "host": "127.0.0.1",
                    "port": 3037,
                    "systemdUser": {"units": ["actanara-owner-b.service"]},
                },
            )
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch(
                    "data_foundation.systemd_user._systemctl_binary",
                    return_value="/usr/bin/systemctl",
                ),
            ):
                first_result = install_user_units(
                    paths,
                    [first],
                    unit_dir=unit_dir,
                    runner=runner,
                    defer_commit=True,
                    recover_transactions=False,
                    transaction_context={"ownerId": "fresh-owner-a"},
                )
                second_result = install_user_units(
                    paths,
                    [second],
                    unit_dir=unit_dir,
                    runner=runner,
                    defer_commit=True,
                    recover_transactions=False,
                    transaction_context={"ownerId": "fresh-owner-b"},
                )
                transaction_root = paths.state_dir / "systemd-transactions"
                journal_less = transaction_root / ("f" * 32)
                journal_less.mkdir()
                recovered = recover_user_unit_transactions(
                    paths,
                    runner=runner,
                    owner_id="fresh-owner-a",
                )

            first_journal = json.loads(
                (
                    transaction_root
                    / first_result["transactionId"]
                    / "journal.json"
                ).read_text(encoding="utf-8")
            )
            second_journal = json.loads(
                (
                    transaction_root
                    / second_result["transactionId"]
                    / "journal.json"
                ).read_text(encoding="utf-8")
            )
            first_definition_exists = (unit_dir / first.name).exists()
            second_definition_exists = (unit_dir / second.name).exists()
            journal_less_exists = journal_less.is_dir()

        self.assertEqual(
            recovered,
            [
                {
                    "id": first_result["transactionId"],
                    "status": "compensated",
                    "phase": "recovered-prior",
                }
            ],
        )
        self.assertEqual(first_journal["ownerId"], "fresh-owner-a")
        self.assertEqual(first_journal["status"], "compensated")
        self.assertEqual(second_journal["ownerId"], "fresh-owner-b")
        self.assertEqual(second_journal["status"], "active")
        self.assertFalse(first_definition_exists)
        self.assertTrue(second_definition_exists)
        self.assertTrue(journal_less_exists)

    def test_unscoped_recovery_does_not_compensate_a_live_systemd_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            runner = StatefulSystemctl()
            unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
            ):
                result = install_user_units(
                    paths,
                    [unit],
                    unit_dir=unit_dir,
                    runner=runner,
                    defer_commit=True,
                    recover_transactions=False,
                )
                recovered = recover_user_unit_transactions(paths, runner=runner)
                command_count = len(runner.commands)
                with self.assertRaisesRegex(
                    SystemdUserError,
                    "active transaction",
                ):
                    control_user_units(
                        paths,
                        [unit],
                        "stop",
                        unit_dir=unit_dir,
                        runner=runner,
                    )
                commands_after_block = runner.commands[command_count:]

            journal_path = (
                paths.state_dir
                / "systemd-transactions"
                / result["transactionId"]
                / "journal.json"
            )
            journal = json.loads(journal_path.read_text(encoding="utf-8"))

        self.assertEqual(
            recovered,
            [
                {
                    "id": result["transactionId"],
                    "status": "active",
                    "phase": "external-verified",
                }
            ],
        )
        self.assertEqual(journal["status"], "active")
        self.assertTrue(journal["ownerProcessIdentity"])
        self.assertEqual(commands_after_block, [])

    def test_interrupted_systemd_recovery_preserves_concurrent_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            runner = StatefulSystemctl()
            unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            target = unit_dir / unit.name

            def interrupt(phase, transaction_id):
                if phase == "after-definitions-applied":
                    raise SyntheticSystemdCrash()

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch.object(systemd_user, "systemd_transaction_checkpoint", side_effect=interrupt),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                install_user_units(paths, [unit], unit_dir=unit_dir, runner=runner)
            target.write_text("# Managed by Actanara. Do not edit by hand.\n# concurrent\n", encoding="utf-8")
            concurrent = target.read_text(encoding="utf-8")
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ),
            ):
                recovered = recover_user_unit_transactions(paths, runner=runner)
            final_content = target.read_text(encoding="utf-8")

        self.assertEqual(recovered[0]["status"], "conflict")
        self.assertEqual(final_content, concurrent)

    def test_interrupted_systemd_recovery_preserves_a_third_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            runner = StatefulSystemctl()
            unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            target = unit_dir / unit.name

            def interrupt(phase, _transaction_id):
                if phase == "after-definitions-applied":
                    raise SyntheticSystemdCrash()

            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch.object(systemd_user, "systemd_transaction_checkpoint", side_effect=interrupt),
                self.assertRaises(SyntheticSystemdCrash),
            ):
                install_user_units(paths, [unit], unit_dir=unit_dir, runner=runner)

            runner.states[unit.name] = {"enabled": False, "active": True}
            command_count = len(runner.commands)
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ),
            ):
                recovered = recover_user_unit_transactions(paths, runner=runner)

            journal_root = paths.state_dir / "systemd-transactions"
            journal_path = next(
                path / "journal.json"
                for path in journal_root.iterdir()
                if path.is_dir()
            )
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            recovery_commands = runner.commands[command_count:]
            final_content = target.read_text(encoding="utf-8")
            final_state = dict(runner.states[unit.name])

        self.assertEqual(recovered[0]["phase"], "runtime-state-conflict")
        self.assertEqual(journal["status"], "conflict")
        self.assertEqual(final_content, unit.content)
        self.assertEqual(final_state, {"enabled": False, "active": True})
        self.assertTrue(recovery_commands)
        self.assertTrue(
            all(command[2] in {"is-enabled", "is-active"} for command in recovery_commands)
        )

    def test_install_refuses_unmanaged_definition_before_systemctl_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            unit = dashboard_unit(paths, {"host": "127.0.0.1", "port": 3036})
            target = unit_dir / unit.name
            target.write_text("[Unit]\nDescription=operator owned\n", encoding="utf-8")
            runner = StatefulSystemctl()
            with (
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
                self.assertRaisesRegex(SystemdUserError, "unmanaged"),
            ):
                install_user_units(paths, [unit], unit_dir=unit_dir, runner=runner)

        self.assertEqual(runner.commands, [])

    def test_macos_backend_delegates_existing_launchd_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._runtime(Path(tmp))
            manager = service_manager.PlatformServiceManager(paths=paths)
            with (
                patch("app.services.service_manager.platform.system", return_value="Darwin"),
                patch.object(service_manager.launcher, "install_dashboard_launch_agent", return_value={"status": "registered"}) as install,
            ):
                result = manager.install("dashboard", {"confirmationText": "existing launchd phrase"})

        self.assertEqual(result["serviceManager"], "launchd-user")
        install.assert_called_once_with({"confirmationText": "existing launchd phrase"})

    def test_linux_scheduler_installs_reconciles_old_label_and_safely_uninstalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            config_home = root / "config"
            unit_dir = config_home / "systemd" / "user"
            runner = StatefulSystemctl()
            write_settings(
                {
                    "schedule": {
                        "enabled": False,
                        "mode": "system",
                        "timezone": "UTC",
                        "dailyPipelineTime": "04:00",
                        "dashboardAggregationTime": "04:30",
                        "systemTimer": {
                            "provider": "systemd",
                            "label": "actanara.test-old",
                            "registered": False,
                        },
                    }
                },
                paths,
            )
            environment = {
                "ACTANARA_HOME": str(paths.home),
                "XDG_CONFIG_HOME": str(config_home),
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("app.services.scheduler.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch("data_foundation.scheduler_preview.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user._systemctl_binary", return_value="/usr/bin/systemctl"),
            ):
                installed = scheduler.install_system_timer(
                    {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION},
                    systemctl_runner=runner,
                    unit_dir=unit_dir,
                )
                preview = scheduler.preview_system_timer(paths, systemctl_runner=runner)
                write_settings(
                    {
                        "schedule": {
                            "dailyPipelineTime": "05:10",
                            "dashboardAggregationTime": "05:40",
                            "systemTimer": {"label": "actanara.test-new"},
                        }
                    },
                    paths,
                )
                updated = scheduler.install_system_timer(
                    {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION},
                    systemctl_runner=runner,
                    unit_dir=unit_dir,
                )
                old_units_exist = any(unit_dir.glob("actanara.test-old.*"))
                new_timer = unit_dir / "actanara.test-new.pipeline.timer"
                new_timer_content = new_timer.read_text(encoding="utf-8")
                removed = scheduler.uninstall_system_timer(
                    {"confirmationText": scheduler.SCHEDULER_UNINSTALL_CONFIRMATION},
                    systemctl_runner=runner,
                    unit_dir=unit_dir,
                )
                remaining = list(unit_dir.glob("actanara.test-*"))
                saved = read_settings(paths)

        self.assertEqual(len(installed["installed"]), 2)
        self.assertTrue(preview["actualRegistered"])
        self.assertTrue(all(job["runtimeStatus"]["definitionsAligned"] for job in preview["jobs"]))
        self.assertEqual(len(updated["handoff"]["transactions"]), 2)
        self.assertFalse(old_units_exist)
        self.assertIn("OnCalendar=*-*-* 05:10:00 UTC", new_timer_content)
        self.assertEqual(len(removed["removed"]), 2)
        self.assertEqual(remaining, [])
        self.assertFalse(saved["schedule"]["systemTimer"]["registered"])
        self.assertFalse(any("sudo" in item for command in runner.commands for item in command))

    def test_linux_scheduler_rejects_settings_changed_after_unit_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            unit_dir = root / "units"
            runner = StatefulSystemctl()
            write_settings(
                {
                    "schedule": {
                        "enabled": False,
                        "mode": "system",
                        "timezone": "UTC",
                        "dailyPipelineTime": "04:00",
                        "dashboardAggregationTime": "04:30",
                        "systemTimer": {
                            "provider": "systemd",
                            "label": "actanara.stale",
                            "registered": False,
                        },
                    }
                },
                paths,
            )
            stale_schedule = read_settings(paths, redact_secrets=False)["schedule"]
            stale_timer = stale_schedule["systemTimer"]
            write_settings(
                {"schedule": {"dailyPipelineTime": "05:55"}},
                paths,
            )

            with (
                patch("data_foundation.settings.platform.system", return_value="Linux"),
                patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                patch(
                    "data_foundation.systemd_user._systemctl_binary",
                    return_value="/usr/bin/systemctl",
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "scheduler Settings changed while the systemd handoff was prepared",
                ),
            ):
                scheduler._execute_systemd_scheduler_handoff(
                    paths,
                    schedule=stale_schedule,
                    timer=stale_timer,
                    action="install",
                    systemctl_runner=runner,
                    unit_dir=unit_dir,
                )

            saved = read_settings(paths)

        self.assertEqual(runner.commands, [])
        self.assertEqual(saved["schedule"]["dailyPipelineTime"], "05:55")
        self.assertFalse(unit_dir.exists())

    def test_linux_scheduler_rejects_an_active_runtime_update_before_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._runtime(root)
            write_settings(
                {
                    "schedule": {
                        "systemTimer": {
                            "provider": "systemd",
                            "label": "actanara.blocked",
                        }
                    }
                },
                paths,
            )
            (paths.home / "app").mkdir(exist_ok=True)
            owner = paths.home / "app" / "update-owner.json"
            owner.write_text(
                json.dumps({"txId": "active-update-owner"}) + "\n",
                encoding="utf-8",
            )
            owner.chmod(0o600)
            os.link(owner, paths.home / "app" / ".update-transaction.lock")
            runner = StatefulSystemctl()

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch("app.services.scheduler.platform.system", return_value="Linux"),
                self.assertRaisesRegex(RuntimeError, "transaction is active"),
            ):
                scheduler.install_system_timer(
                    {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION},
                    systemctl_runner=runner,
                    unit_dir=root / "units",
                )

        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()
