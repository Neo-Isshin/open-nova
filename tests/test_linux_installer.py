import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from install import install_linux


class LinuxInstallerTests(unittest.TestCase):
    def _args(self, *arguments: str) -> argparse.Namespace:
        return install_linux._parser().parse_args(
            ["--source-root", str(ROOT), "--python", sys.executable, *arguments]
        )

    @staticmethod
    def _fake_dependency_marker(venv, *_args, **_kwargs):
        marker = Path(venv) / install_linux.dependency_contract.MARKER_NAME
        marker.write_text('{"fixture":true}\n', encoding="utf-8")
        marker.chmod(0o444)
        return {"status": "written", "path": str(marker)}

    def test_default_plan_uses_shared_dashboard_profile_and_systemd_boundary(self):
        args = self._args()
        plan = install_linux.build_plan(args)

        self.assertEqual(plan.profiles, ("dashboard",))
        self.assertTrue(plan.dashboard_service)
        self.assertTrue(plan.scheduler)
        self.assertFalse(plan.rag_enabled)
        self.assertEqual(plan.rag_embedding_mode, "cloud")
        self.assertEqual(plan.linger_policy, "prompt")

    def test_rag_and_dev_test_profiles_share_linux_installer_code(self):
        args = self._args("--enable-rag", "--enable-dev-test")
        plan = install_linux.build_plan(args)

        self.assertEqual(
            plan.profiles,
            ("dashboard", "dev-test", "rag-local", "rag-server"),
        )
        self.assertEqual(plan.rag_embedding_mode, "local")

    def test_local_rag_selects_both_server_and_local_dependency_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(
                "--runtime",
                str(Path(tmp) / "runtime"),
                "--enable-rag",
                "--rag-embedding-mode",
                "local",
            )
            plan = install_linux.build_plan(args)
            self.assertEqual(plan.profiles, ("dashboard", "rag-local", "rag-server"))

            update = install_linux._runtime_settings_update(plan)

            self.assertEqual(
                update["rag"]["embedding"],
                {
                    "mode": "local",
                    "provider": "local",
                    "providerId": "local",
                    "model": "intfloat/multilingual-e5-small",
                    "dimension": 384,
                    "device": "auto",
                },
            )

    def test_upgrade_inherits_runtime_profiles_services_and_preserves_linger(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            (runtime / "config").mkdir(parents=True)
            settings = {
                "schemaVersion": 1,
                "features": {"rag": True},
                "rag": {
                    "enabled": True,
                    "embedding": {"mode": "local", "provider": "local"},
                    "server": {"enabled": False},
                },
                "dashboard": {
                    "host": "127.0.0.1",
                    "port": 43123,
                    "server": {"enabled": True},
                },
                "schedule": {
                    "enabled": True,
                    "systemTimer": {"provider": "systemd", "registered": True},
                },
            }
            settings_path = runtime / "config" / "settings.json"
            settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")
            settings_path.chmod(0o600)
            args = self._args("--runtime", str(runtime), "--upgrade")
            inherited = {
                "profiles": ["dashboard", "dev-test", "rag-local", "rag-server"],
                "rag": {"enabled": True, "embeddingMode": "local"},
                "evidence": {
                    "settingsSha256": "a" * 64,
                    "activeVenvTarget": str(runtime / "app" / "venvs" / "old"),
                    "activeMarkerStatus": "trusted",
                    "activeMarkerSha256": "b" * 64,
                },
            }
            with patch.object(
                install_linux.dependency_contract,
                "runtime_dependency_profiles",
                return_value=inherited,
            ):
                plan = install_linux.build_plan(args)

            self.assertEqual(plan.update_mode, "upgrade")
            self.assertEqual(plan.profiles, tuple(inherited["profiles"]))
            self.assertEqual(plan.dashboard_port, 43123)
            self.assertTrue(plan.dashboard_service)
            self.assertTrue(plan.scheduler)
            self.assertFalse(plan.rag_enabled)
            self.assertEqual(plan.rag_embedding_mode, "local")
            self.assertEqual(plan.linger_policy, "preserve")

    def test_upgrade_rejects_runtime_setting_changes_disguised_as_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            (runtime / "config").mkdir(parents=True)
            settings_path = runtime / "config" / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "features": {"rag": False},
                        "rag": {"enabled": False},
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": 3036,
                            "server": {"enabled": True},
                        },
                        "schedule": {"enabled": False},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            settings_path.chmod(0o600)
            args = self._args(
                "--runtime",
                str(runtime),
                "--upgrade",
                "--dashboard-port",
                "4040",
            )
            inherited = {
                "profiles": ["dashboard"],
                "rag": {"enabled": False, "embeddingMode": None},
                "evidence": {
                    "settingsSha256": "a" * 64,
                    "activeVenvTarget": str(runtime / ".venv"),
                    "activeMarkerStatus": "missing",
                    "activeMarkerSha256": None,
                },
            }
            with (
                patch.object(
                    install_linux.dependency_contract,
                    "runtime_dependency_profiles",
                    return_value=inherited,
                ),
                self.assertRaisesRegex(install_linux.LinuxInstallError, "--dashboard-port"),
            ):
                install_linux.build_plan(args)

    def test_repair_requires_explicit_yes_and_conflicts_with_upgrade(self):
        with self.assertRaisesRegex(install_linux.LinuxInstallError, "requires --yes"):
            install_linux.build_plan(self._args("--repair-existing"))
        with self.assertRaisesRegex(install_linux.LinuxInstallError, "cannot be combined"):
            install_linux.build_plan(self._args("--repair-existing", "--upgrade", "--yes"))

    def test_force_rebuild_requires_upgrade_and_conflicts_with_source_only(self):
        with self.assertRaisesRegex(install_linux.LinuxInstallError, "requires --upgrade"):
            install_linux._requested_update_mode(self._args("--force-rebuild"))
        with self.assertRaisesRegex(install_linux.LinuxInstallError, "mutually exclusive"):
            install_linux._requested_update_mode(
                self._args("--source-only", "--force-rebuild")
            )
        self.assertEqual(
            install_linux._requested_update_mode(
                self._args("--upgrade", "--force-rebuild")
            ),
            "upgrade",
        )

    def test_force_rebuild_selects_locked_candidate_dependency_plan(self):
        plan = SimpleNamespace(
            update_mode="upgrade",
            force_rebuild=True,
            runtime=Path("/tmp/actanara-update-fixture"),
            offline=False,
        )
        ready = {
            "status": "ready",
            "updateMode": "rebuild-candidate-venv",
            "reason": "explicit-force-rebuild",
        }
        with patch.object(
            install_linux.dependency_contract,
            "plan_update",
            return_value=(ready, 0),
        ) as planner:
            result = install_linux._dependency_update_plan(plan, object())

        self.assertEqual(result, ready)
        self.assertEqual(planner.call_args.kwargs["mode"], "force-rebuild")

    def test_cloud_rag_configuration_does_not_claim_local_model_runtime(self):
        args = self._args("--enable-rag", "--rag-embedding-mode", "cloud")
        plan = install_linux.build_plan(args)

        update = install_linux._runtime_settings_update(plan)

        self.assertEqual(
            update["rag"]["embedding"],
            {"mode": "cloud", "provider": "cloud"},
        )

    def test_fresh_cloud_rag_blocks_before_runtime_or_dependency_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--enable-rag",
                "--rag-embedding-mode",
                "cloud",
            )
            plan = install_linux.build_plan(args)
            with (
                patch.object(install_linux.platform, "system", return_value="Linux"),
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "fresh managed cloud RAG is not available",
                ),
            ):
                install_linux._validate_plan(plan, args)

            self.assertFalse(runtime.exists())

    def test_managed_rag_fresh_install_rejects_source_without_exact_commit_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--enable-rag",
                "--rag-embedding-mode",
                "local",
                "--no-linger-prompt",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(
                environment_id="linux-cpython313-x86-64",
                lock_environment={"architecture": "x86_64"},
            )
            with (
                patch.object(
                    install_linux,
                    "_source_identity",
                    return_value=("actanara-fixture", None),
                ),
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "requires a clean source tree with an exact Git commit identity",
                ),
            ):
                install_linux._install(plan, selection, args)

            self.assertFalse(runtime.exists())

    def test_linux_service_preflight_requires_a_working_user_manager(self):
        args = self._args()
        plan = install_linux.build_plan(args)
        with (
            patch.object(install_linux.shutil, "which", return_value="/usr/bin/systemctl"),
            patch.object(
                install_linux.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "no user bus"),
            ),
            self.assertRaisesRegex(install_linux.LinuxInstallError, "systemd user manager is unavailable"),
        ):
            install_linux._preflight_linux_services(plan)

    def test_linux_service_preflight_rejects_an_occupied_dashboard_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        args = self._args("--dashboard-port", str(port))
        plan = install_linux.build_plan(args)
        with (
            patch.object(install_linux.shutil, "which", return_value="/usr/bin/systemctl"),
            patch.object(
                install_linux.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            self.assertRaisesRegex(install_linux.LinuxInstallError, f"Dashboard port {port} is unavailable"),
        ):
            install_linux._preflight_linux_services(plan)

    def test_linux_fresh_rag_preflight_rejects_an_external_listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 3037))
        listener.listen(1)
        args = self._args(
            "--enable-rag",
            "--rag-embedding-mode",
            "local",
            "--no-dashboard-server",
            "--no-scheduler",
        )
        plan = install_linux.build_plan(args)
        with (
            patch.object(install_linux.shutil, "which", return_value="/usr/bin/systemctl"),
            patch.object(
                install_linux.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            self.assertRaisesRegex(
                install_linux.LinuxInstallError,
                "RAG port 3037 is unavailable",
            ),
        ):
            install_linux._preflight_linux_services(plan)

    def test_linux_fresh_preflight_rejects_dashboard_and_rag_loopback_alias_collision(self):
        args = self._args(
            "--dashboard-host",
            "localhost",
            "--dashboard-port",
            "3037",
            "--enable-rag",
            "--rag-embedding-mode",
            "local",
            "--no-scheduler",
        )
        plan = install_linux.build_plan(args)
        with (
            patch.object(install_linux.shutil, "which", return_value="/usr/bin/systemctl"),
            patch.object(
                install_linux.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            self.assertRaisesRegex(
                install_linux.LinuxInstallError,
                "Dashboard and RAG services cannot use the same loopback port",
            ),
        ):
            install_linux._preflight_linux_services(plan)

    def test_linux_service_preflight_can_be_skipped_for_cli_only_install(self):
        args = self._args("--no-scheduler", "--no-dashboard-server")
        plan = install_linux.build_plan(args)
        with patch.object(install_linux.subprocess, "run") as run:
            install_linux._preflight_linux_services(plan)

        run.assert_not_called()

    def test_cli_only_install_does_not_probe_or_change_linger(self):
        args = self._args("--no-scheduler", "--no-dashboard-server")
        plan = install_linux.build_plan(args)
        with patch("data_foundation.systemd_user.linger_status") as linger_status:
            result = install_linux._prepare_linger(plan)

        self.assertEqual(result["action"], "not-required")
        self.assertFalse(result["sudoInvoked"])
        linger_status.assert_not_called()

    def test_default_linger_prompt_preserves_state_when_declined(self):
        args = self._args()
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            patch.object(install_linux, "_prompt_enable_linger", return_value=False),
            patch("data_foundation.systemd_user.enable_linger") as enable_linger,
        ):
            result = install_linux._prepare_linger(plan)

        self.assertEqual(result["action"], "declined")
        self.assertEqual(result["requestedPolicy"], "prompt")
        self.assertFalse(result["sudoInvoked"])
        enable_linger.assert_not_called()

    def test_default_linger_prompt_enables_only_after_explicit_acceptance(self):
        args = self._args()
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            patch.object(install_linux, "_prompt_enable_linger", return_value=True),
            patch(
                "data_foundation.systemd_user.enable_linger",
                return_value={
                    "status": "enabled",
                    "enabled": True,
                    "changed": True,
                    "action": "enabled",
                    "authorization": "explicit-user-choice",
                },
            ) as enable_linger,
        ):
            result = install_linux._prepare_linger(plan)

        self.assertTrue(result["enabled"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["requestedPolicy"], "prompt")
        self.assertFalse(result["sudoInvoked"])
        enable_linger.assert_called_once_with()

    def test_noninteractive_default_preserves_linger(self):
        args = self._args()
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            patch.object(install_linux, "_prompt_enable_linger", return_value=None),
            patch("data_foundation.systemd_user.enable_linger") as enable_linger,
        ):
            result = install_linux._prepare_linger(plan)

        self.assertEqual(result["action"], "non-interactive-preserved")
        enable_linger.assert_not_called()

    def test_explicit_enable_linger_does_not_depend_on_yes_or_prompt(self):
        args = self._args("--enable-linger", "--yes")
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            patch.object(install_linux, "_prompt_enable_linger") as prompt,
            patch(
                "data_foundation.systemd_user.enable_linger",
                return_value={"status": "enabled", "enabled": True, "changed": True},
            ) as enable_linger,
        ):
            result = install_linux._prepare_linger(plan)

        self.assertEqual(plan.linger_policy, "enable")
        self.assertTrue(result["enabled"])
        prompt.assert_not_called()
        enable_linger.assert_called_once_with()

    def test_require_linger_fails_before_install_when_not_enabled(self):
        args = self._args("--require-linger")
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            self.assertRaisesRegex(install_linux.LinuxInstallError, "linger is required"),
        ):
            install_linux._prepare_linger(plan)

    def test_dry_run_never_prompts_or_changes_linger(self):
        args = self._args("--dry-run", "--enable-linger")
        plan = install_linux.build_plan(args)
        with (
            patch(
                "data_foundation.systemd_user.linger_status",
                return_value={"status": "disabled", "enabled": False, "changed": False},
            ),
            patch.object(install_linux, "_prompt_enable_linger") as prompt,
            patch("data_foundation.systemd_user.enable_linger") as enable_linger,
        ):
            result = install_linux._prepare_linger(plan)

        self.assertEqual(result["action"], "planned-enable")
        self.assertTrue(result["wouldChange"])
        prompt.assert_not_called()
        enable_linger.assert_not_called()

    def test_dry_run_reports_exact_targets_without_writing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args("--runtime", str(runtime), "--dry-run", "--no-scheduler")
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(
                environment_id="linux-cpython313-x86-64",
                lock_environment={"architecture": "x86_64"},
            )

            payload = install_linux._install(plan, selection, args)

        self.assertEqual(payload["status"], "planned")
        self.assertFalse(payload["writes"])
        self.assertEqual(payload["schedulerProvider"], "systemd")
        self.assertFalse(runtime.exists())

    def test_offline_fresh_preflight_blocks_broken_ensurepip_without_runtime_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--offline",
                "--no-scheduler",
                "--no-dashboard-server",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(fingerprint="f" * 64)

            def completed(command, **_kwargs):
                command = list(command)
                if "ensurepip" in command:
                    return subprocess.CompletedProcess(command, 1, b"", b"ensurepip unavailable")
                return subprocess.CompletedProcess(command, 0, "3.13\n", "")

            with (
                patch.object(
                    install_linux.dependency_contract,
                    "dependency_cache_status",
                    return_value={"status": "hit", "usable": True},
                ),
                patch.object(install_linux.subprocess, "run", side_effect=completed),
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "offline.*pip bootstrap",
                ),
            ):
                install_linux._preflight_fresh_dependencies(plan, selection)

            self.assertFalse(runtime.exists())

    def test_offline_fresh_cache_miss_blocks_before_venv_preflight_or_runtime_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--offline",
                "--no-scheduler",
                "--no-dashboard-server",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(fingerprint="f" * 64)
            with (
                patch.object(
                    install_linux.dependency_contract,
                    "dependency_cache_status",
                    return_value={"status": "miss", "usable": False},
                ),
                patch.object(install_linux.subprocess, "run") as run,
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "complete trusted dependency cache",
                ),
            ):
                install_linux._preflight_fresh_dependencies(plan, selection)

            run.assert_not_called()
            self.assertFalse(runtime.exists())

    def test_every_fresh_checkpoint_failure_cleans_generations_and_is_retryable(self):
        checkpoints = (
            "cache-ready",
            "venv-bootstrap-ready",
            "dependencies-ready",
            "source-staged",
            "release-promotion-armed",
            "release-promoted",
            "venv-promotion-armed",
            "venv-promoted",
            "pointers-promotion-armed",
            "source-pointer-promoted",
            "pointers-published",
            "runtime-cli-write-armed",
            "runtime-configuration-armed",
            "location-write-armed",
            "runtime-configured",
            "database-migration-armed",
            "database-migration-running",
            "database-ready",
            "service-settings-armed",
            "services-ready",
            "user-shim-promotion-armed",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = root / "runtime"
                location = root / "location.json"
                original_location = b'{"actanaraHome":"/preserved/runtime"}\n'
                location.write_bytes(original_location)
                location.chmod(0o600)
                arguments = [
                    "--runtime",
                    str(runtime),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ]
                if checkpoint != "user-shim-promotion-armed":
                    arguments.append("--no-shell-path")
                args = self._args(*arguments)
                plan = install_linux.build_plan(args)
                selection = SimpleNamespace(
                    environment_id="linux-cpython313-x86-64",
                    lock_environment={"architecture": "x86_64"},
                )

                def fake_seed(_plan, candidate):
                    (candidate / "bin").mkdir(parents=True)
                    python = candidate / "bin" / "python"
                    python.write_text("#!/bin/sh\n", encoding="utf-8")
                    python.chmod(0o700)
                    return python

                def fake_stage(_plan, candidate, _commit, **_kwargs):
                    candidate.mkdir(parents=True)
                    (candidate / ".actanara-runtime-source.json").write_text(
                        '{}\n', encoding="utf-8"
                    )
                    return {}

                def fake_database(_plan, **_kwargs):
                    worker_started = _kwargs.get("worker_started")
                    if worker_started is not None:
                        worker_started(
                            {
                                "pid": 999_999_999,
                                "processGroup": 999_999_999,
                                "processIdentity": "proc-start-ticks:fixture",
                            }
                        )
                    database = runtime / "data" / "actanara_data.sqlite3"
                    database.parent.mkdir(parents=True, exist_ok=True)
                    database.write_bytes(b"sqlite")
                    return database

                def fake_services(_plan, **kwargs):
                    started = kwargs.get("settings_transaction_started")
                    if started is not None:
                        started(
                            {
                                "id": "3" * 32,
                                "settingsAfterHash": install_linux._fresh_file_hash(
                                    runtime / "config" / "settings.json"
                                ),
                                "runtimeManifestAfterHash": install_linux._fresh_file_hash(
                                    runtime / "config" / "runtime.json"
                                ),
                            }
                        )
                    return {"status": "not-requested", "units": []}

                observed_failures = []

                def fail_at(phase, _transaction_id):
                    if phase == checkpoint:
                        observed_failures.append(phase)
                        raise install_linux.LinuxInstallError(
                            f"synthetic fresh failure at {phase}"
                        )

                dependency_patches = (
                    patch.object(
                        install_linux.dependency_contract,
                        "materialize_dependency_cache",
                        return_value={"status": "hit"},
                    ),
                    patch.object(
                        install_linux.dependency_contract,
                        "install_locked_dependencies",
                        return_value={"status": "installed"},
                    ),
                    patch.object(
                        install_linux.dependency_contract,
                        "write_dependency_marker",
                        side_effect=self._fake_dependency_marker,
                    ),
                    patch.object(
                        install_linux.dependency_contract,
                        "verify_dependency_marker",
                        return_value={},
                    ),
                )
                with ExitStack() as stack:
                    for dependency_patch in dependency_patches:
                        stack.enter_context(dependency_patch)
                    stack.enter_context(
                        patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40))
                    )
                    stack.enter_context(patch.object(install_linux, "_seed_venv_pip", side_effect=fake_seed))
                    stack.enter_context(patch.object(install_linux, "_stage_source", side_effect=fake_stage))
                    stack.enter_context(patch.object(install_linux, "_initialize_database", side_effect=fake_database))
                    stack.enter_context(patch.object(
                        install_linux,
                        "_install_systemd_user_services",
                        side_effect=fake_services,
                    ))
                    stack.enter_context(
                        patch("data_foundation.settings.platform.system", return_value="Linux")
                    )
                    stack.enter_context(
                        patch("data_foundation.systemd_user.platform.system", return_value="Linux")
                    )
                    stack.enter_context(
                        patch.object(install_linux, "fresh_install_checkpoint", side_effect=fail_at)
                    )
                    stack.enter_context(patch.dict(
                        os.environ,
                        {
                            "ACTANARA_LOCATION_FILE": str(location),
                            "HOME": str(root / "home"),
                        },
                        clear=False,
                    ))
                    stack.enter_context(self.assertRaisesRegex(
                        install_linux.LinuxInstallError,
                        "synthetic fresh failure|settings transaction",
                    ))
                    install_linux._install(plan, selection, args)

                self.assertEqual(observed_failures, [checkpoint])

                releases = runtime / "app" / "releases"
                venvs = runtime / "app" / "venvs"
                self.assertEqual(list(releases.iterdir()) if releases.exists() else [], [])
                self.assertEqual(list(venvs.iterdir()) if venvs.exists() else [], [])
                self.assertFalse((runtime / "app" / "source").exists())
                self.assertFalse((runtime / ".venv").exists())
                self.assertFalse((runtime / "config" / "settings.json").exists())
                self.assertFalse((runtime / "config" / "runtime.json").exists())
                self.assertFalse((runtime / "data" / "actanara_data.sqlite3").exists())
                self.assertEqual(location.read_bytes(), original_location)
                self.assertFalse(
                    (root / "home" / ".local" / "bin" / "actanara").exists()
                )

                retry_dependency_patches = (
                        patch.object(
                            install_linux.dependency_contract,
                            "materialize_dependency_cache",
                            return_value={"status": "hit"},
                        ),
                        patch.object(
                            install_linux.dependency_contract,
                            "install_locked_dependencies",
                            return_value={"status": "installed"},
                        ),
                        patch.object(
                            install_linux.dependency_contract,
                            "write_dependency_marker",
                            side_effect=self._fake_dependency_marker,
                        ),
                        patch.object(
                            install_linux.dependency_contract,
                            "verify_dependency_marker",
                            return_value={},
                        ),
                )
                with ExitStack() as stack:
                    for dependency_patch in retry_dependency_patches:
                        stack.enter_context(dependency_patch)
                    stack.enter_context(
                        patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40))
                    )
                    stack.enter_context(patch.object(install_linux, "_seed_venv_pip", side_effect=fake_seed))
                    stack.enter_context(patch.object(install_linux, "_stage_source", side_effect=fake_stage))
                    stack.enter_context(patch.object(install_linux, "_initialize_database", side_effect=fake_database))
                    stack.enter_context(patch.object(
                        install_linux,
                        "_install_systemd_user_services",
                        side_effect=fake_services,
                    ))
                    stack.enter_context(
                        patch("data_foundation.settings.platform.system", return_value="Linux")
                    )
                    stack.enter_context(
                        patch("data_foundation.systemd_user.platform.system", return_value="Linux")
                    )
                    stack.enter_context(patch.dict(
                        os.environ,
                        {
                            "ACTANARA_LOCATION_FILE": str(location),
                            "HOME": str(root / "home"),
                        },
                        clear=False,
                    ))
                    retry = install_linux._install(plan, selection, args)

                self.assertEqual(retry["status"], "installed")

    def test_sigkill_after_fresh_promotion_is_recovered_on_the_next_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            location = root / "location.json"
            original_location = b'{"actanaraHome":"/preserved/runtime"}\n'
            location.write_bytes(original_location)
            location.chmod(0o600)
            child = r'''
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from install import install_linux

root = Path(os.environ["ACTANARA_TEST_FRESH_ROOT"])
runtime = root / "runtime"
args = install_linux._parser().parse_args([
    "--source-root", str(Path.cwd()),
    "--python", sys.executable,
    "--runtime", str(runtime),
    "--no-scheduler", "--no-dashboard-server", "--no-shell-path",
])
plan = install_linux.build_plan(args)
selection = SimpleNamespace(
    environment_id="linux-cpython313-x86-64",
    lock_environment={"architecture": "x86_64"},
)

def seed(_plan, candidate):
    (candidate / "bin").mkdir(parents=True)
    python = candidate / "bin" / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    return python

def stage(_plan, candidate, _commit, **_kwargs):
    candidate.mkdir(parents=True)
    (candidate / ".actanara-runtime-source.json").write_text("{}\n", encoding="utf-8")
    return {}

def marker(venv, *_args, **_kwargs):
    path = Path(venv) / install_linux.dependency_contract.MARKER_NAME
    path.write_text('{"fixture":true}\n', encoding="utf-8")
    path.chmod(0o444)
    return {}

with (
    patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40)),
    patch.object(install_linux, "_seed_venv_pip", side_effect=seed),
    patch.object(install_linux, "_stage_source", side_effect=stage),
    patch.object(install_linux.dependency_contract, "materialize_dependency_cache", return_value={"status": "hit"}),
    patch.object(install_linux.dependency_contract, "install_locked_dependencies", return_value={"status": "installed"}),
    patch.object(install_linux.dependency_contract, "write_dependency_marker", side_effect=marker),
    patch.object(install_linux.dependency_contract, "verify_dependency_marker", return_value={}),
):
    install_linux._install(plan, selection, args)
'''
            environment = {
                **os.environ,
                "ACTANARA_INSTALL_TEST_MODE": "1",
                "ACTANARA_INSTALL_TEST_KILL_PHASE": "pointers-published",
                "ACTANARA_TEST_FRESH_ROOT": str(root),
                "ACTANARA_LOCATION_FILE": str(location),
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
            }
            killed = subprocess.run(
                [sys.executable, "-c", child],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            self.assertTrue((runtime / "app" / "source").is_symlink())
            self.assertTrue((runtime / ".venv").is_symlink())
            with patch.dict(
                os.environ,
                {"ACTANARA_LOCATION_FILE": str(location)},
                clear=False,
            ):
                recovered = install_linux._recover_fresh_install(runtime)

            self.assertEqual(len(recovered), 1)
            self.assertFalse((runtime / "app" / "source").exists())
            self.assertFalse((runtime / ".venv").exists())
            self.assertEqual(list((runtime / "app" / "releases").iterdir()), [])
            self.assertEqual(list((runtime / "app" / "venvs").iterdir()), [])
            self.assertEqual(location.read_bytes(), original_location)

    def test_update_recovery_holds_one_guard_across_all_recovery_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            observed = []

            def transaction_recovery(*_arguments, **_kwargs):
                from data_foundation.runtime_mutation import (
                    current_runtime_mutation_guard_fd,
                )

                observed.append(
                    ("transaction", current_runtime_mutation_guard_fd())
                )
                return subprocess.CompletedProcess([], 0, "", "")

            def settings_recovery(_runtime, *, runtime_guard_held=False):
                from data_foundation.runtime_mutation import (
                    current_runtime_mutation_guard_fd,
                )

                observed.append(
                    (
                        "settings",
                        current_runtime_mutation_guard_fd(),
                        runtime_guard_held,
                    )
                )
                return []

            def systemd_recovery(
                _runtime,
                *,
                runtime_guard_held=False,
                owner_id=None,
            ):
                from data_foundation.runtime_mutation import (
                    current_runtime_mutation_guard_fd,
                )

                observed.append(
                    (
                        "systemd",
                        current_runtime_mutation_guard_fd(),
                        runtime_guard_held,
                        owner_id,
                    )
                )
                return []

            with (
                patch.object(
                    install_linux,
                    "_transaction_command",
                    side_effect=transaction_recovery,
                ),
                patch.object(
                    install_linux,
                    "_recover_settings_transactions_before_update",
                    side_effect=settings_recovery,
                ),
                patch.object(
                    install_linux,
                    "_recover_systemd_transactions_before_update",
                    side_effect=systemd_recovery,
                ),
            ):
                install_linux._recover_update_runtime(runtime)

        descriptors = [entry[1] for entry in observed]
        self.assertEqual([entry[0] for entry in observed], ["transaction", "settings", "systemd"])
        self.assertTrue(all(isinstance(descriptor, int) for descriptor in descriptors))
        self.assertEqual(len(set(descriptors)), 1)
        self.assertTrue(observed[1][2])
        self.assertTrue(observed[2][2])
        self.assertIsNone(observed[2][3])

    def test_update_dry_run_does_not_recover_or_mutate_runtime(self):
        runtime = Path("/tmp/actanara-dry-run-recovery-fixture")
        plan = SimpleNamespace(update_mode="repair")
        with (
            patch.object(install_linux, "_recover_update_runtime") as recover,
            patch.object(install_linux, "build_plan", return_value=plan),
            patch.object(install_linux, "_validate_plan", return_value=SimpleNamespace()),
            patch.object(install_linux, "_prepare_linger", return_value={}),
            patch.object(
                install_linux,
                "_update",
                return_value={
                    "status": "planned",
                    "updateMode": "repair",
                    "writes": False,
                },
            ),
            patch("builtins.print"),
        ):
            status = install_linux.main(
                [
                    "--runtime",
                    str(runtime),
                    "--repair-existing",
                    "--dry-run",
                ]
            )

        self.assertEqual(status, 0)
        recover.assert_not_called()

    def test_fresh_recovery_refuses_a_matching_live_owner_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            transaction_id = install_linux._fresh_install_transaction_id()
            staging = (
                runtime
                / "app"
                / install_linux.FRESH_INSTALL_STAGING_NAME
                / transaction_id
            )
            staging.mkdir(parents=True)
            payload = install_linux._acquire_fresh_install_lock(
                runtime,
                staging,
                transaction_id,
            )
            sentinel = staging / "operator-sentinel"
            sentinel.write_text("preserve\n", encoding="utf-8")

            with self.assertRaisesRegex(
                install_linux.LinuxInstallError,
                "fresh install process is still active",
            ):
                install_linux._recover_fresh_install(runtime)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertTrue(install_linux._fresh_install_lock_path(runtime).exists())
            install_linux._release_fresh_install_lock(runtime, staging, payload)

    def test_fresh_rollback_preserves_a_concurrent_location_edit_and_all_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            location = root / "location.json"
            location.write_text('{"actanaraHome":"/prior"}\n', encoding="utf-8")
            args = self._args(
                "--runtime",
                str(runtime),
                "--no-scheduler",
                "--no-dashboard-server",
                "--no-shell-path",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(
                environment_id="linux-cpython313-x86-64",
                lock_environment={"architecture": "x86_64"},
            )

            def fake_seed(_plan, candidate):
                (candidate / "bin").mkdir(parents=True)
                python = candidate / "bin" / "python"
                python.write_text("#!/bin/sh\n", encoding="utf-8")
                python.chmod(0o700)
                return python

            def fake_stage(_plan, candidate, _commit, **_kwargs):
                candidate.mkdir(parents=True)
                (candidate / ".actanara-runtime-source.json").write_text(
                    '{}\n', encoding="utf-8"
                )
                return {}

            def fake_configure(_plan, **_kwargs):
                (runtime / "config").mkdir(parents=True, exist_ok=True)
                (runtime / "config" / "settings.json").write_text(
                    '{}\n', encoding="utf-8"
                )
                (runtime / "config" / "runtime.json").write_text(
                    '{}\n', encoding="utf-8"
                )
                location.write_text(
                    json.dumps({"actanaraHome": str(runtime)}) + "\n",
                    encoding="utf-8",
                )

            def conflict_after_configuration(phase, _transaction_id):
                if phase == "runtime-configured":
                    location.write_text('{"operator":"concurrent"}\n', encoding="utf-8")
                    raise install_linux.LinuxInstallError("synthetic concurrent edit")

            with (
                patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40)),
                patch.object(install_linux, "_seed_venv_pip", side_effect=fake_seed),
                patch.object(install_linux, "_stage_source", side_effect=fake_stage),
                patch.object(install_linux, "_configure_runtime", side_effect=fake_configure),
                patch.object(install_linux, "fresh_install_checkpoint", side_effect=conflict_after_configuration),
                patch.object(install_linux.dependency_contract, "materialize_dependency_cache"),
                patch.object(install_linux.dependency_contract, "install_locked_dependencies"),
                patch.object(
                    install_linux.dependency_contract,
                    "write_dependency_marker",
                    side_effect=self._fake_dependency_marker,
                ),
                patch.object(install_linux.dependency_contract, "verify_dependency_marker"),
                patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(location)}, clear=False),
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "recovery is incomplete.*concurrent mutable-file change",
                ),
            ):
                install_linux._install(plan, selection, args)

            self.assertEqual(
                location.read_text(encoding="utf-8"),
                '{"operator":"concurrent"}\n',
            )
            self.assertTrue((runtime / "app" / "source").is_symlink())
            self.assertTrue((runtime / ".venv").is_symlink())
            self.assertTrue(install_linux._fresh_install_lock_path(runtime).exists())

    def test_fresh_commit_journal_is_authoritative_across_ack_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            location = root / "location.json"
            location.write_text('{"actanaraHome":"/prior"}\n', encoding="utf-8")
            args = self._args(
                "--runtime",
                str(runtime),
                "--no-scheduler",
                "--no-dashboard-server",
                "--no-shell-path",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(
                environment_id="linux-cpython313-x86-64",
                lock_environment={"architecture": "x86_64"},
            )

            def fake_seed(_plan, candidate):
                (candidate / "bin").mkdir(parents=True)
                python = candidate / "bin" / "python"
                python.write_text("#!/bin/sh\n", encoding="utf-8")
                python.chmod(0o700)
                return python

            def fake_stage(_plan, candidate, _commit, **_kwargs):
                candidate.mkdir(parents=True)
                (candidate / ".actanara-runtime-source.json").write_text(
                    '{}\n', encoding="utf-8"
                )
                return {}

            def fake_configure(_plan, **_kwargs):
                (runtime / "config").mkdir(parents=True, exist_ok=True)
                for name in ("settings.json", "runtime.json"):
                    (runtime / "config" / name).write_text('{}\n', encoding="utf-8")
                location.write_text(
                    json.dumps({"actanaraHome": str(runtime)}) + "\n",
                    encoding="utf-8",
                )

            def fake_database(_plan, **_kwargs):
                worker_started = _kwargs.get("worker_started")
                if worker_started is not None:
                    worker_started(
                        {
                            "pid": 999_999_999,
                            "processGroup": 999_999_999,
                            "processIdentity": "proc-start-ticks:fixture",
                        }
                    )
                database = runtime / "data" / "actanara_data.sqlite3"
                database.parent.mkdir(parents=True, exist_ok=True)
                database.write_bytes(b"sqlite")
                return database

            original_advance = install_linux._advance_fresh_install_journal

            def lose_commit_ack(staging, journal, phase, **updates):
                original_advance(staging, journal, phase, **updates)
                if phase == "committed":
                    raise install_linux.LinuxInstallError("synthetic lost commit ACK")

            with (
                patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40)),
                patch.object(install_linux, "_seed_venv_pip", side_effect=fake_seed),
                patch.object(install_linux, "_stage_source", side_effect=fake_stage),
                patch.object(install_linux, "_configure_runtime", side_effect=fake_configure),
                patch.object(install_linux, "_initialize_database", side_effect=fake_database),
                patch.object(
                    install_linux,
                    "_install_systemd_user_services",
                    return_value={"status": "not-requested", "units": []},
                ),
                patch.object(
                    install_linux,
                    "_advance_fresh_install_journal",
                    side_effect=lose_commit_ack,
                ),
                patch.object(install_linux.dependency_contract, "materialize_dependency_cache"),
                patch.object(install_linux.dependency_contract, "install_locked_dependencies"),
                patch.object(
                    install_linux.dependency_contract,
                    "write_dependency_marker",
                    side_effect=self._fake_dependency_marker,
                ),
                patch.object(install_linux.dependency_contract, "verify_dependency_marker"),
                patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(location)}, clear=False),
            ):
                result = install_linux._install(plan, selection, args)

            self.assertEqual(result["status"], "installed")
            self.assertTrue((runtime / "app" / "source").is_symlink())
            self.assertTrue((runtime / ".venv").is_symlink())
            self.assertFalse(install_linux._fresh_install_lock_path(runtime).exists())
            self.assertFalse(
                (runtime / "app" / install_linux.FRESH_INSTALL_STAGING_NAME).exists()
            )

    def test_candidate_venv_launchers_are_relocated_before_atomic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "staging" / "venv"
            target = root / "runtime" / "app" / "venvs" / "generation"
            (candidate / "bin").mkdir(parents=True)
            pip = candidate / "bin" / "pip"
            activate = candidate / "bin" / "activate"
            pip.write_text(f"#!{candidate}/bin/python\n", encoding="utf-8")
            activate.write_text(f'VIRTUAL_ENV="{candidate}"\n', encoding="utf-8")

            install_linux._relocate_candidate_venv(candidate, target)

            self.assertEqual(pip.read_text(encoding="utf-8"), f"#!{target}/bin/python\n")
            self.assertEqual(
                activate.read_text(encoding="utf-8"),
                f'VIRTUAL_ENV="{target}"\n',
            )

    def test_runtime_directories_ignore_permissive_login_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "runtime"
            previous = os.umask(0o002)
            try:
                install_linux._secure_directory(target)
            finally:
                os.umask(previous)

            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_staged_linux_source_has_release_manifest_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._args("--runtime", str(root / "runtime"), "--dry-run")
            plan = install_linux.build_plan(args)
            release = root / "release"

            manifest = install_linux._stage_source(plan, release, "a" * 40)

            persisted = json.loads(
                (release / ".actanara-runtime-source.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, manifest)
            self.assertEqual(manifest["product"], "actanara")
            self.assertEqual(manifest["deploymentMode"], "release-symlink")
            self.assertEqual(manifest["git"]["commit"], "a" * 40)
            self.assertGreater(manifest["payload"]["fileCount"], 180)
            self.assertFalse(any(path.is_symlink() for path in release.rglob("*")))

    def test_linux_bootstrap_is_posix_truncation_safe_and_never_calls_zsh(self):
        adapter = ROOT / "install" / "bootstrap-linux.sh"
        script = adapter.read_text(encoding="utf-8")

        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("if true; then\nset -eu\numask 077", script)
        self.assertTrue(script.endswith("\nfi\n"))
        self.assertNotIn("/bin/zsh", script)
        syntax = subprocess.run(
            ["sh", "-n", str(adapter)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_systemd_install_selection_respects_linux_service_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args("--runtime", str(runtime), "--no-dashboard-server")
            plan = install_linux.build_plan(args)
            settings = {
                "schedule": {
                    "timezone": "UTC",
                    "dailyPipelineTime": "04:00",
                    "dashboardAggregationTime": "04:30",
                    "systemTimer": {"provider": "systemd", "label": "actanara.daily"},
                },
                "dashboard": {"host": "127.0.0.1", "port": 3036},
            }
            paths = SimpleNamespace(home=runtime)
            installed = {}

            def fake_install(selected_paths, units, **_kwargs):
                installed["paths"] = selected_paths
                installed["names"] = [unit.name for unit in units]
                return {
                    "status": "installed",
                    "transactionId": "fresh-systemd-selection",
                    "linger": {"enabled": False, "changed": False},
                }

            def fake_settings_handoff(
                update,
                _paths,
                *,
                precommit_side_effects,
                postcommit_side_effects=None,
                **_kwargs,
            ):
                installed["update"] = update
                context = {
                    "id": "1" * 32,
                    "settingsBeforeHash": "before-settings",
                    "settingsAfterHash": "after-settings",
                    "runtimeManifestBeforeHash": "before-manifest",
                    "runtimeManifestAfterHash": "after-manifest",
                }
                precommit_side_effects(context)
                if postcommit_side_effects is not None:
                    postcommit_side_effects(context)
                return {"settingsTransaction": {"status": "committed"}}

            with (
                patch("data_foundation.paths.runtime_paths_for_home", return_value=paths),
                patch("data_foundation.settings.read_settings", return_value=settings),
                patch(
                    "data_foundation.settings.write_linux_installer_handoff_settings",
                    side_effect=fake_settings_handoff,
                ),
                patch("data_foundation.systemd_user.install_user_units", side_effect=fake_install),
                patch("data_foundation.systemd_user.finalize_user_unit_transaction"),
            ):
                result = install_linux._install_systemd_user_services(plan)

        self.assertEqual(result["status"], "installed")
        self.assertEqual(
            installed["names"],
            [
                "actanara.daily.pipeline.service",
                "actanara.daily.pipeline.timer",
                "actanara.daily.dashboard-aggregation.service",
                "actanara.daily.dashboard-aggregation.timer",
            ],
        )
        update = installed["update"]
        self.assertTrue(update["schedule"]["systemTimer"]["registered"])
        self.assertEqual(update["schedule"]["systemTimer"]["provider"], "systemd")
        self.assertNotIn("dashboard", update)

    def test_fresh_unregister_only_settings_arms_the_outer_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--no-scheduler",
                "--no-dashboard-server",
            )
            plan = install_linux.build_plan(args)
            settings = {
                "dashboard": {
                    "server": {"enabled": True},
                    "systemdUser": {
                        "registered": True,
                        "units": ["actanara-dashboard.service"],
                    },
                }
            }
            observed = {}

            def fake_settings_handoff(
                update,
                _paths,
                *,
                precommit_side_effects,
                **_kwargs,
            ):
                observed["update"] = update
                context = {
                    "id": "3" * 32,
                    "settingsBeforeHash": "before-settings",
                    "settingsAfterHash": "after-settings",
                    "runtimeManifestBeforeHash": "before-manifest",
                    "runtimeManifestAfterHash": "after-manifest",
                }
                precommit_side_effects(context)
                return {"settingsTransaction": {"status": "committed"}}

            with (
                patch("data_foundation.settings.read_settings", return_value=settings),
                patch.object(
                    install_linux,
                    "_systemd_unit_inventory",
                    return_value=([], ("actanara-dashboard.service",)),
                ),
                patch(
                    "data_foundation.settings.write_linux_installer_handoff_settings",
                    side_effect=fake_settings_handoff,
                ),
            ):
                result = install_linux._install_systemd_user_services(
                    plan,
                    settings_transaction_started=lambda context: observed.update(
                        {"outerContext": context}
                    ),
                    transaction_owner_id="fresh-owner",
                )

        self.assertEqual(result["status"], "not-requested")
        self.assertEqual(observed["outerContext"]["id"], "3" * 32)
        self.assertFalse(observed["update"]["dashboard"]["server"]["enabled"])
        self.assertFalse(
            observed["update"]["dashboard"]["systemdUser"]["registered"]
        )

    def test_fresh_runtime_configuration_commits_rag_and_location_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            location = root / "location.json"
            transaction_id = install_linux._fresh_install_transaction_id()
            staging = (
                runtime
                / "app"
                / install_linux.FRESH_INSTALL_STAGING_NAME
                / transaction_id
            )
            staging.mkdir(parents=True)
            lock_payload = install_linux._acquire_fresh_install_lock(
                runtime,
                staging,
                transaction_id,
            )
            args = self._args(
                "--runtime",
                str(runtime),
                "--enable-rag",
                "--rag-embedding-mode",
                "local",
                "--no-scheduler",
                "--no-dashboard-server",
            )
            plan = install_linux.build_plan(args)
            mutable_paths = install_linux._fresh_mutable_paths(runtime, location)
            journal = {
                "phase": "runtime-cli-written",
                "managedMutableHashes": {
                    key: {
                        "path": str(path),
                        "beforeSha256": install_linux.FRESH_MISSING_HASH,
                        "afterSha256": None,
                    }
                    for key, path in mutable_paths.items()
                },
            }
            install_linux._write_fresh_install_journal(staging, journal)

            try:
                with (
                    patch.dict(
                        os.environ,
                        {"ACTANARA_LOCATION_FILE": str(location)},
                        clear=False,
                    ),
                    patch("data_foundation.settings.platform.system", return_value="Linux"),
                    patch("data_foundation.systemd_user.platform.system", return_value="Linux"),
                ):
                    install_linux._configure_runtime(
                        plan,
                        staging=staging,
                        journal=journal,
                        transaction_id=transaction_id,
                    )
                saved = json.loads(
                    (runtime / "config" / "settings.json").read_text(
                        encoding="utf-8"
                    )
                )
                selected = json.loads(location.read_text(encoding="utf-8"))
            finally:
                install_linux._release_fresh_install_lock(
                    runtime,
                    staging,
                    lock_payload,
                )

        self.assertTrue(saved["rag"]["enabled"])
        self.assertEqual(saved["rag"]["embedding"]["dimension"], 384)
        self.assertEqual(saved["rag"]["languageProfile"], "zh")
        self.assertEqual(selected["actanaraHome"], str(runtime))
        self.assertRegex(journal["configurationSettingsTransactionId"], r"[0-9a-f]{32}")
        self.assertEqual(journal["phase"], "location-write-armed")

    def test_fresh_rag_systemd_install_requires_semantic_readiness_before_settings_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args(
                "--runtime",
                str(runtime),
                "--enable-rag",
                "--rag-embedding-mode",
                "local",
                "--no-dashboard-server",
                "--no-scheduler",
            )
            plan = install_linux.build_plan(args)
            settings = {
                "rag": {
                    "enabled": True,
                    "embedding": {
                        "mode": "local",
                        "provider": "local",
                        "providerId": "local",
                        "model": "intfloat/multilingual-e5-small",
                        "dimension": 384,
                    },
                    "server": {"enabled": True, "host": "127.0.0.1", "port": 3037},
                }
            }
            resolved = object()
            captured = {}

            def fake_install(_paths, units, **kwargs):
                captured["names"] = [unit.name for unit in units]
                captured["readiness"] = kwargs["readiness_verifier"]()
                return {
                    "status": "installed",
                    "units": captured["names"],
                    "transactionId": "systemd-fresh-rag",
                    "readiness": captured["readiness"],
                }

            def fake_settings_handoff(
                update,
                _paths,
                *,
                precommit_side_effects,
                postcommit_side_effects=None,
                **_kwargs,
            ):
                captured["update"] = update
                context = {
                    "id": "2" * 32,
                    "settingsBeforeHash": "before-settings",
                    "settingsAfterHash": "after-settings",
                    "runtimeManifestBeforeHash": "before-manifest",
                    "runtimeManifestAfterHash": "after-manifest",
                }
                precommit_side_effects(context)
                if postcommit_side_effects is not None:
                    postcommit_side_effects(context)
                return {"settingsTransaction": {"status": "committed"}}

            with (
                patch("data_foundation.settings.read_settings", return_value=settings),
                patch(
                    "data_foundation.settings.write_linux_installer_handoff_settings",
                    side_effect=fake_settings_handoff,
                ) as write_settings,
                patch("agentic_rag.rag_settings.resolve_rag_settings", return_value=resolved),
                patch(
                    "agentic_rag.rag_server_lifecycle.require_rag_server_readiness",
                    return_value={"ready": True, "status": "ready"},
                ) as require_ready,
                patch(
                    "data_foundation.systemd_user.install_user_units",
                    side_effect=fake_install,
                ),
                patch("data_foundation.systemd_user.finalize_user_unit_transaction"),
            ):
                result = install_linux._install_systemd_user_services(
                    plan,
                    expected_source_commit="a" * 40,
                )

            self.assertEqual(result["readiness"]["status"], "ready")
            self.assertEqual(captured["names"], ["actanara-rag-server.service"])
            require_ready.assert_called_once()
            self.assertIs(require_ready.call_args.args[0], resolved)
            self.assertEqual(
                require_ready.call_args.kwargs["expected_source_commit"],
                "a" * 40,
            )
            write_settings.assert_called_once()

    def test_fresh_systemd_settings_failure_rolls_back_the_unit_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            args = self._args("--runtime", str(runtime), "--no-scheduler")
            plan = install_linux.build_plan(args)
            settings = {"dashboard": {"host": "127.0.0.1", "port": 3036}}
            transaction_ids = []
            installed = {
                "status": "installed",
                "units": ["actanara-dashboard.service"],
                "transactionId": "fresh-systemd-transaction",
            }

            def fail_settings_handoff(
                _update,
                _paths,
                *,
                precommit_side_effects,
                **_kwargs,
            ):
                cleanup = precommit_side_effects(
                    {
                        "settingsBeforeHash": "before-settings",
                        "settingsAfterHash": "after-settings",
                    }
                )
                cleanup()
                raise RuntimeError("synthetic settings failure")

            with (
                patch("data_foundation.settings.read_settings", return_value=settings),
                patch(
                    "data_foundation.settings.write_linux_installer_handoff_settings",
                    side_effect=fail_settings_handoff,
                ),
                patch(
                    "data_foundation.systemd_user.install_user_units",
                    return_value=installed,
                ) as install_units,
                patch(
                    "data_foundation.systemd_user.rollback_user_unit_transaction"
                ) as rollback,
                patch(
                    "data_foundation.systemd_user.finalize_user_unit_transaction"
                ) as finalize,
                self.assertRaisesRegex(
                    install_linux.LinuxInstallError,
                    "synthetic settings failure",
                ),
            ):
                install_linux._install_systemd_user_services(
                    plan,
                    transaction_started=transaction_ids.append,
                    transaction_owner_id="fresh-install-owner",
                )

            self.assertTrue(install_units.call_args.kwargs["defer_commit"])
            self.assertFalse(install_units.call_args.kwargs["recover_transactions"])
            self.assertEqual(
                install_units.call_args.kwargs["transaction_context"],
                {
                    "settingsBeforeHash": "before-settings",
                    "settingsAfterHash": "after-settings",
                    "ownerId": "fresh-install-owner",
                },
            )
            self.assertEqual(transaction_ids, ["fresh-systemd-transaction"])
            rollback.assert_called_once()
            finalize.assert_not_called()

    def test_fresh_service_transaction_is_journaled_before_settings_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            location = root / "location.json"
            location.write_text('{}\n', encoding="utf-8")
            args = self._args(
                "--runtime",
                str(runtime),
                "--no-scheduler",
                "--no-dashboard-server",
                "--no-shell-path",
            )
            plan = install_linux.build_plan(args)
            selection = SimpleNamespace(
                environment_id="linux-cpython313-x86-64",
                lock_environment={"architecture": "x86_64"},
            )
            observed = {}

            def fake_seed(_plan, candidate):
                (candidate / "bin").mkdir(parents=True)
                python = candidate / "bin" / "python"
                python.write_text("#!/bin/sh\n", encoding="utf-8")
                return python

            def fake_stage(_plan, candidate, _commit, **_kwargs):
                candidate.mkdir(parents=True)
                (candidate / ".actanara-runtime-source.json").write_text(
                    '{}\n', encoding="utf-8"
                )
                return {}

            def fake_configure(_plan, **_kwargs):
                settings = runtime / "config" / "settings.json"
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text('{}\n', encoding="utf-8")
                (runtime / "config" / "runtime.json").write_text(
                    '{}\n', encoding="utf-8"
                )

            def fake_database(_plan, **_kwargs):
                worker_started = _kwargs.get("worker_started")
                if worker_started is not None:
                    worker_started(
                        {
                            "pid": 999_999_999,
                            "processGroup": 999_999_999,
                            "processIdentity": "proc-start-ticks:fixture",
                        }
                    )
                database = runtime / "data" / "actanara_data.sqlite3"
                database.parent.mkdir(parents=True, exist_ok=True)
                database.write_bytes(b"sqlite")
                return database

            def fake_systemd(_plan, **kwargs):
                kwargs["transaction_started"]("fresh-systemd-id")
                staging_root = runtime / "app" / install_linux.FRESH_INSTALL_STAGING_NAME
                journal_path = next(staging_root.iterdir()) / install_linux.FRESH_INSTALL_JOURNAL_NAME
                observed.update(json.loads(journal_path.read_text(encoding="utf-8")))
                observed["transactionOwnerArgument"] = kwargs["transaction_owner_id"]
                raise install_linux.LinuxInstallError("synthetic post-handoff failure")

            with (
                patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40)),
                patch.object(install_linux, "_seed_venv_pip", side_effect=fake_seed),
                patch.object(install_linux, "_stage_source", side_effect=fake_stage),
                patch.object(install_linux, "_configure_runtime", side_effect=fake_configure),
                patch.object(install_linux, "_initialize_database", side_effect=fake_database),
                patch.object(install_linux, "_install_systemd_user_services", side_effect=fake_systemd),
                patch.object(install_linux, "_rollback_fresh_service_transaction"),
                patch.object(install_linux.dependency_contract, "materialize_dependency_cache"),
                patch.object(install_linux.dependency_contract, "install_locked_dependencies"),
                patch.object(
                    install_linux.dependency_contract,
                    "write_dependency_marker",
                    side_effect=self._fake_dependency_marker,
                ),
                patch.object(install_linux.dependency_contract, "verify_dependency_marker"),
                patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(location)}, clear=False),
                self.assertRaisesRegex(install_linux.LinuxInstallError, "post-handoff failure"),
            ):
                install_linux._install(plan, selection, args)

            self.assertEqual(observed["phase"], "service-transaction-started")
            self.assertEqual(observed["serviceTransactionId"], "fresh-systemd-id")
            self.assertEqual(
                observed["transactionOwnerArgument"],
                observed["transactionId"],
            )

    def test_fresh_recovery_finds_a_systemd_transaction_created_before_handoff(self):
        runtime = Path("/tmp/actanara-fresh-systemd-recovery")
        paths = object()
        with (
            patch("data_foundation.paths.runtime_paths_for_home", return_value=paths),
            patch(
                "data_foundation.systemd_user.recover_user_unit_transactions",
                return_value=[
                    {
                        "id": "interrupted-before-handoff",
                        "status": "compensated",
                        "phase": "recovered-prior",
                    }
                ],
            ) as recover,
            patch("data_foundation.systemd_user.rollback_user_unit_transaction") as rollback,
        ):
            install_linux._rollback_fresh_service_transaction(
                runtime,
                None,
                owner_id="fresh-owner-id",
            )

        recover.assert_called_once_with(paths, owner_id="fresh-owner-id")
        rollback.assert_not_called()

    def test_database_initialization_uses_deployed_runtime_and_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            database = runtime / "data" / "actanara_data.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"sqlite")
            database.chmod(0o666)
            args = self._args("--runtime", str(runtime))
            plan = install_linux.build_plan(args)
            commands = []

            def fake_run(command, *, env=None):
                commands.append((list(command), dict(env or {})))

            with patch.object(install_linux, "_run", side_effect=fake_run):
                initialized = install_linux._initialize_database(plan)
            database_mode = database.stat().st_mode & 0o777

        self.assertEqual(initialized, database)
        self.assertEqual(commands[0][0][0], str(runtime / ".venv" / "bin" / "python"))
        self.assertEqual(commands[0][0][1], "-c")
        self.assertIn("data_foundation.db import migrate", commands[0][0][2])
        self.assertEqual(commands[0][1]["ACTANARA_HOME"], str(runtime))
        self.assertIn(str(runtime / "app" / "source" / "src"), commands[0][1]["PYTHONPATH"])
        self.assertEqual(database_mode, 0o600)

    def test_standard_update_rejects_stale_or_drifted_systemd_definitions(self):
        plan = SimpleNamespace(runtime=Path("/tmp/actanara-update-fixture"))
        desired = [SimpleNamespace(name="actanara-dashboard.service", content="managed\n")]

        with self.assertRaisesRegex(install_linux.LinuxInstallError, "inventory is stale"):
            install_linux._validate_existing_systemd_units_for_update(
                plan,
                desired,
                ("actanara-dashboard.service", "actanara-stale.service"),
            )

        with (
            patch(
                "data_foundation.systemd_user.inspect_user_units",
                return_value={
                    "definitionsPresent": True,
                    "definitionsManaged": True,
                    "definitionsAligned": False,
                },
            ),
            self.assertRaisesRegex(install_linux.LinuxInstallError, "have drifted"),
        ):
            install_linux._validate_existing_systemd_units_for_update(
                plan,
                desired,
                ("actanara-dashboard.service",),
            )

    def test_systemd_inventory_requires_an_exact_runtime_environment_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            unit_root = config_home / "systemd" / "user"
            unit_root.mkdir(parents=True)
            unit = unit_root / "actanara-other-runtime.service"
            unit.write_text(
                "# Managed by Actanara. Do not edit by hand.\n"
                "[Service]\n"
                'Environment="ACTANARA_HOME=/x/actanara-old"\n',
                encoding="utf-8",
            )
            plan = SimpleNamespace(
                runtime=Path("/x/actanara"),
                scheduler=False,
                dashboard_service=False,
                rag_enabled=False,
            )
            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(config_home)},
                clear=False,
            ):
                _desired, inventory = install_linux._systemd_unit_inventory(plan, {})

        self.assertNotIn(unit.name, inventory)

    def test_update_alignment_accepts_a_deliberately_stopped_managed_service(self):
        plan = SimpleNamespace(runtime=Path("/tmp/actanara-update-fixture"))
        inspection = {
            "definitionsPresent": True,
            "definitionsManaged": True,
            "definitionsAligned": True,
            "actualEnabled": True,
            "actualActive": False,
            "actualRegistered": False,
        }
        with (
            patch.object(
                install_linux,
                "_desired_systemd_units",
                return_value=[SimpleNamespace(name="actanara-dashboard.service")],
            ),
            patch(
                "data_foundation.systemd_user.inspect_user_units",
                return_value=inspection,
            ),
        ):
            result = install_linux._verify_updated_systemd_units(plan, {})

        self.assertEqual(result, inspection)

    def test_update_health_checks_only_services_active_before_the_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = SimpleNamespace(
                runtime=Path(tmp) / "runtime",
                dashboard_service=True,
                rag_enabled=False,
            )
            settings = {
                "dashboard": {
                    "host": "127.0.0.1",
                    "port": 65534,
                    "systemdUser": {"units": ["actanara-test-dashboard.service"]},
                }
            }
            with patch.object(install_linux.http.client, "HTTPConnection") as connection:
                install_linux._wait_for_update_service_health(
                    plan,
                    settings,
                    active_units=set(),
                )

        connection.assert_not_called()

    def test_update_rag_health_uses_semantic_readiness_and_expected_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = SimpleNamespace(
                runtime=Path(tmp) / "runtime",
                dashboard_service=False,
                rag_enabled=True,
            )
            settings = {
                "rag": {
                    "enabled": True,
                    "embedding": {
                        "provider": "local",
                        "providerId": "local",
                        "model": "intfloat/multilingual-e5-small",
                        "dimension": 384,
                    },
                    "server": {"enabled": True, "host": "127.0.0.1", "port": 3037},
                }
            }
            resolved = object()
            with (
                patch("agentic_rag.rag_settings.resolve_rag_settings", return_value=resolved),
                patch(
                    "agentic_rag.rag_server_lifecycle.require_rag_server_readiness",
                    return_value={"ready": True, "status": "ready"},
                ) as require_ready,
                patch.object(install_linux.http.client, "HTTPConnection") as connection,
            ):
                install_linux._wait_for_update_service_health(
                    plan,
                    settings,
                    expected_source_commit="b" * 40,
                )

            connection.assert_not_called()
            require_ready.assert_called_once()
            self.assertIs(require_ready.call_args.args[0], resolved)
            self.assertEqual(
                require_ready.call_args.kwargs["expected_source_commit"],
                "b" * 40,
            )

    def test_update_doctor_is_captured_as_bounded_machine_readable_evidence(self):
        plan = SimpleNamespace(runtime=Path("/tmp/actanara-update-fixture"))
        completed = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "doctorProfile": "installer",
                    "summary": {
                        "status": "warn",
                        "errors": 0,
                        "warnings": 1,
                        "checks": 6,
                    },
                }
            ),
            "",
        )
        with patch.object(install_linux.subprocess, "run", return_value=completed) as run:
            result = install_linux._run_update_doctor(plan)

        self.assertEqual(
            result,
            {
                "profile": "installer",
                "status": "warn",
                "errors": 0,
                "warnings": 1,
                "checks": 6,
            },
        )
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_committed_repair_clears_pending_marker_only_after_health_and_doctor(self):
        plan = SimpleNamespace(
            runtime=Path("/tmp/actanara-repair-fixture"),
            scheduler=True,
            dashboard_service=True,
            rag_enabled=True,
        )
        journal = plan.runtime / "app" / "update-transactions" / "fixture" / "journal.json"
        events = []

        with (
            patch.object(
                install_linux.dependency_contract,
                "migrate_legacy_runtime_settings",
                side_effect=lambda *_args, **_kwargs: events.append("migrate"),
            ),
            patch.object(install_linux, "_write_cli_shim", side_effect=lambda *_args: events.append("shim")),
            patch.object(
                install_linux,
                "_initialize_database",
                side_effect=lambda *_args: events.append("database") or Path("/tmp/database"),
            ),
            patch.object(
                install_linux,
                "_read_update_settings",
                side_effect=lambda *_args: events.append("settings") or {"schemaVersion": 1},
            ),
            patch.object(
                install_linux,
                "_reconcile_existing_systemd_units",
                side_effect=lambda *_args, **_kwargs: events.append("systemd") or {"units": []},
            ) as reconcile,
            patch.object(
                install_linux,
                "_systemd_unit_inventory",
                return_value=([], ()),
            ),
            patch.object(
                install_linux,
                "_install_systemd_user_services",
                side_effect=lambda *_args, **_kwargs: events.append("handoff")
                or {"status": "installed"},
            ),
            patch.object(
                install_linux,
                "_wait_for_update_service_health",
                side_effect=lambda *_args, **_kwargs: events.append("readiness"),
            ),
            patch.object(
                install_linux,
                "_verify_updated_systemd_units",
                side_effect=lambda *_args: events.append("verify"),
            ),
            patch.object(
                install_linux,
                "_run_update_doctor",
                side_effect=lambda *_args: events.append("doctor") or {"status": "ok"},
            ),
            patch.object(
                install_linux,
                "_transaction_command",
                side_effect=lambda *args, **_kwargs: events.append(args[0]),
            ),
            patch(
                "install.update_transaction._load_state",
                return_value={
                    "txId": "repair-owner",
                    "systemdUnits": [
                        {"name": "actanara-stale-dashboard.service"}
                    ]
                },
            ),
        ):
            database, systemd, doctor = install_linux._finish_committed_repair(
                plan,
                journal=journal,
                source_commit="c" * 40,
            )

        self.assertEqual(database, Path("/tmp/database"))
        self.assertEqual(
            systemd,
            {
                "units": [],
                "settingsHandoff": {"status": "installed"},
                "restoredPriorStateUnits": [],
            },
        )
        self.assertEqual(doctor, {"status": "ok"})
        self.assertEqual(
            reconcile.call_args.kwargs["prior_inventory"],
            ("actanara-stale-dashboard.service",),
        )
        self.assertEqual(
            events,
            [
                "migrate",
                "shim",
                "database",
                "settings",
                "handoff",
                "settings",
                "systemd",
                "readiness",
                "verify",
                "doctor",
                "complete-repair",
            ],
        )

    def test_committed_repair_keeps_pending_marker_when_doctor_fails(self):
        plan = SimpleNamespace(
            runtime=Path("/tmp/actanara-repair-fixture"),
            scheduler=False,
            dashboard_service=False,
            rag_enabled=False,
        )
        completed = []
        with (
            patch.object(install_linux.dependency_contract, "migrate_legacy_runtime_settings"),
            patch.object(install_linux, "_write_cli_shim"),
            patch.object(install_linux, "_initialize_database", return_value=Path("/tmp/database")),
            patch.object(install_linux, "_read_update_settings", return_value={}),
            patch.object(install_linux, "_systemd_unit_inventory", return_value=([], ())),
            patch.object(install_linux, "_install_systemd_user_services", return_value={}),
            patch.object(install_linux, "_reconcile_existing_systemd_units", return_value={}),
            patch.object(install_linux, "_wait_for_update_service_health"),
            patch.object(install_linux, "_verify_updated_systemd_units"),
            patch.object(
                install_linux,
                "_run_update_doctor",
                side_effect=install_linux.LinuxInstallError("doctor failed"),
            ),
            patch.object(
                install_linux,
                "_transaction_command",
                side_effect=lambda *args, **_kwargs: completed.append(args[0]),
            ),
            patch(
                "install.update_transaction._load_state",
                return_value={"txId": "repair-owner", "systemdUnits": []},
            ),
            self.assertRaisesRegex(install_linux.LinuxInstallError, "doctor failed"),
        ):
            install_linux._finish_committed_repair(
                plan,
                journal=Path("/tmp/journal.json"),
                source_commit="d" * 40,
            )

        self.assertNotIn("complete-repair", completed)

    def test_committed_repair_defers_retained_units_then_restores_prior_vector(self):
        runtime = Path("/tmp/actanara-repair-vector-fixture")
        plan = SimpleNamespace(
            runtime=runtime,
            scheduler=False,
            dashboard_service=True,
            rag_enabled=False,
        )
        journal = runtime / "app" / "update-transactions" / "fixture" / "journal.json"
        retained = SimpleNamespace(
            name="actanara-dashboard.service",
            enable_now=True,
        )
        new_unit = SimpleNamespace(
            name="actanara-new.service",
            enable_now=True,
        )
        commands = []
        with (
            patch.object(install_linux.dependency_contract, "migrate_legacy_runtime_settings"),
            patch.object(install_linux, "_write_cli_shim"),
            patch.object(install_linux, "_initialize_database", return_value=Path("/tmp/database")),
            patch.object(install_linux, "_read_update_settings", return_value={}),
            patch.object(
                install_linux,
                "_systemd_unit_inventory",
                return_value=([retained, new_unit], (retained.name, new_unit.name)),
            ),
            patch.object(
                install_linux,
                "_install_systemd_user_services",
                return_value={"status": "installed"},
            ) as install_units,
            patch.object(
                install_linux,
                "_reconcile_existing_systemd_units",
                return_value={"units": [retained.name, new_unit.name]},
            ) as reconcile,
            patch.object(install_linux, "_wait_for_update_service_health") as health,
            patch.object(install_linux, "_verify_updated_systemd_units"),
            patch.object(install_linux, "_run_update_doctor", return_value={"status": "ok"}),
            patch.object(
                install_linux,
                "_transaction_command",
                side_effect=lambda *args, **_kwargs: commands.append(args),
            ),
            patch(
                "install.update_transaction._load_state",
                return_value={
                    "txId": "repair-owner",
                    "systemdUnits": [
                        {
                            "name": retained.name,
                            "definitionExisted": True,
                            "enableState": "enabled-runtime",
                            "activeState": "inactive",
                        }
                    ],
                },
            ),
        ):
            install_linux._finish_committed_repair(
                plan,
                journal=journal,
                source_commit="c" * 40,
            )

        deferred = frozenset({retained.name})
        self.assertEqual(
            install_units.call_args.kwargs["deferred_enable_names"],
            deferred,
        )
        self.assertEqual(
            reconcile.call_args.kwargs["deferred_enable_names"],
            deferred,
        )
        self.assertIn(
            (
                "restore-repair-services",
                "--state",
                str(journal),
                "--unit",
                retained.name,
            ),
            commands,
        )
        self.assertEqual(health.call_args.kwargs["active_units"], {new_unit.name})
        self.assertEqual(commands[-1][0], "complete-repair")

    def test_repair_retry_inherits_stale_unit_inventory_from_pending_journal(self):
        runtime = Path("/tmp/actanara-repair-fixture")
        old_journal = runtime / "app" / "update-transactions" / "old" / "journal.json"
        plan = SimpleNamespace(
            runtime=runtime,
            profile_evidence={"settingsSha256": "a" * 64},
            update_mode="repair",
            dry_run=True,
            profiles=("dashboard",),
            rag_enabled=False,
            force_rebuild=True,
            source_root=ROOT,
        )
        selection = SimpleNamespace()
        args = SimpleNamespace(no_shell_path=True)
        with (
            patch.object(
                install_linux,
                "_repair_transaction_postcondition",
                return_value={
                    "status": "configuration-pending",
                    "journal": str(old_journal),
                },
            ),
            patch(
                "install.update_transaction._load_state",
                return_value={
                    "systemdUnits": [
                        {"name": "actanara-stale-dashboard.service"}
                    ]
                },
            ),
            patch.object(
                install_linux,
                "_dependency_update_plan",
                return_value={
                    "updateMode": "rebuild-candidate-venv",
                    "reason": "repair",
                },
            ),
            patch.object(install_linux, "_read_update_settings", return_value={}),
            patch.object(install_linux, "_source_identity", return_value=("release", "a" * 40)),
            patch(
                "data_foundation.source_identity.loaded_source_commit",
                return_value="a" * 40,
            ),
            patch.object(install_linux, "_systemd_unit_inventory", return_value=([], ())),
        ):
            result = install_linux._update(plan, selection, args)

        self.assertEqual(
            result["managedUnits"],
            ["actanara-stale-dashboard.service"],
        )
        self.assertEqual(result["reason"], "resume-committed-repair-configuration")

    def test_repair_retry_resumes_committed_journal_without_starting_new_update(self):
        runtime = Path("/tmp/actanara-repair-resume-fixture")
        journal = runtime / "app" / "update-transactions" / "old" / "journal.json"
        plan = SimpleNamespace(
            runtime=runtime,
            profile_evidence={"settingsSha256": "a" * 64},
            update_mode="repair",
            dry_run=False,
            profiles=("dashboard",),
            rag_enabled=False,
            force_rebuild=True,
            source_root=ROOT,
        )
        selection = SimpleNamespace()
        args = SimpleNamespace(no_shell_path=True)
        with (
            patch.object(
                install_linux,
                "_repair_transaction_postcondition",
                return_value={
                    "status": "configuration-pending",
                    "journal": str(journal),
                },
            ),
            patch(
                "install.update_transaction._load_state",
                return_value={
                    "txId": "repair-owner",
                    "systemdUnits": [
                        {"name": "actanara-dashboard.service"}
                    ],
                },
            ),
            patch.object(install_linux, "_read_update_settings", return_value={}),
            patch.object(
                install_linux,
                "_source_identity",
                return_value=("release", "b" * 40),
            ),
            patch(
                "data_foundation.source_identity.loaded_source_commit",
                return_value="b" * 40,
            ),
            patch.object(
                install_linux,
                "_systemd_unit_inventory",
                return_value=([], ()),
            ),
            patch.object(install_linux, "_recover_systemd_transactions_before_update"),
            patch.object(install_linux, "_validate_existing_systemd_unit_ownership"),
            patch.object(
                install_linux,
                "_finish_committed_repair",
                return_value=(
                    Path("/tmp/database"),
                    {"units": ["actanara-dashboard.service"]},
                    {"status": "ok"},
                ),
            ) as finish,
            patch.object(install_linux, "_dependency_update_plan") as dependency_plan,
        ):
            result = install_linux._update_guarded(plan, selection, args)

        dependency_plan.assert_not_called()
        finish.assert_called_once_with(
            plan,
            journal=journal,
            source_commit="b" * 40,
        )
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(result["reason"], "resumed-committed-repair-configuration")

    def test_linux_result_envelope_matches_platform_neutral_update_cli_contract(self):
        envelope = install_linux._result_envelope(
            payload={
                "status": "updated",
                "updateMode": "source-only",
                "dependenciesInstalled": False,
                "reusesRuntimeVenv": True,
                "reason": "dependency-fingerprint-match",
                "systemdUser": {"units": [{"name": "actanara-dashboard.service"}]},
            },
            requested_mode="source-only",
        )

        self.assertEqual(envelope["status"], "completed")
        self.assertEqual(envelope["updateMode"], "source-only")
        self.assertTrue(envelope["sourceUpdated"])
        self.assertFalse(envelope["dependenciesInstalled"])
        self.assertFalse(envelope["cacheUsed"])
        self.assertFalse(envelope["plannedDependenciesInstall"])
        self.assertTrue(envelope["reusesRuntimeVenv"])
        self.assertTrue(envelope["servicesStopped"])
        self.assertTrue(envelope["stateCertain"])
        self.assertEqual(len(envelope), 14)

    def test_linux_result_envelope_reports_a_completed_rollback_as_certain(self):
        error = install_linux.LinuxInstallError(
            "candidate health failed",
            rollback_complete=True,
            state_certain=True,
            stage="rollback-complete",
        )
        envelope = install_linux._result_envelope(
            payload=None,
            requested_mode="source-only",
            error=error,
        )

        self.assertEqual(envelope["status"], "failed")
        self.assertFalse(envelope["sourceUpdated"])
        self.assertTrue(envelope["rollbackComplete"])
        self.assertTrue(envelope["stateCertain"])
        self.assertEqual(envelope["stage"], "rollback-complete")

    def test_linux_result_envelope_reports_committed_repair_as_pending_and_retryable(self):
        error = install_linux.LinuxInstallError(
            "repair readiness failed",
            rollback_complete=None,
            state_certain=True,
            stage="repair-configuration-pending",
            source_updated=True,
            dependencies_installed=True,
            reuses_runtime_venv=False,
            services_stopped=True,
        )
        envelope = install_linux._result_envelope(
            payload=None,
            requested_mode="repair",
            error=error,
        )

        self.assertEqual(envelope["status"], "failed")
        self.assertTrue(envelope["sourceUpdated"])
        self.assertTrue(envelope["dependenciesInstalled"])
        self.assertTrue(envelope["cacheUsed"])
        self.assertTrue(envelope["plannedDependenciesInstall"])
        self.assertFalse(envelope["reusesRuntimeVenv"])
        self.assertTrue(envelope["servicesStopped"])
        self.assertIsNone(envelope["rollbackComplete"])
        self.assertTrue(envelope["stateCertain"])
        self.assertEqual(envelope["stage"], "repair-configuration-pending")


if __name__ == "__main__":
    unittest.main()
