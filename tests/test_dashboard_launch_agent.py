import plistlib
import os
import subprocess
import sys
import tempfile
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from data_foundation.paths import initialize_home
from data_foundation.settings import read_settings, write_settings
from app.services import launcher
from advanced.dashboard import dashboard_launch_agent as agent
from advanced.dashboard import rag_server_launch_agent as rag_agent


class DashboardLaunchAgentTests(unittest.TestCase):
    def _write_runtime_pointers(self, runtime: Path) -> None:
        release = runtime / "app" / "releases" / "fixture-release"
        venv = runtime / "app" / "venvs" / "fixture-venv"
        release.mkdir(parents=True)
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (runtime / "app" / "source").symlink_to(Path("releases") / release.name)
        (runtime / ".venv").symlink_to(Path("app") / "venvs" / venv.name)

    def test_service_plist_uses_keepalive_and_foundation_env(self):
        plist = agent.build_service_plist(
            label="com.example.dashboard",
            python=Path("/tmp/venv/bin/python"),
            project_root=Path("/repo"),
            actanara_home=Path("/nova"),
            host="127.0.0.1",
            port=3036,
            foundation=True,
            logs_dir=Path("/tmp/logs"),
        )

        args = plist["ProgramArguments"]
        env = plist["EnvironmentVariables"]
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(args[:2], ["/bin/zsh", "-lc"])
        self.assertIn("/tmp/venv/bin/python -m uvicorn app.main:app", args[2])
        self.assertIn("/repo/src/dashboard", args[2])
        self.assertIn("--host 127.0.0.1", args[2])
        self.assertIn("--port 3036", args[2])
        self.assertEqual(env["ACTANARA_DASHBOARD_PYTHON"], "/tmp/venv/bin/python")
        self.assertEqual(env["ACTANARA_DASHBOARD_PORT"], "3036")
        self.assertEqual(env["ACTANARA_HOME"], "/nova")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("WORKSPACE_DIR", env)
        self.assertNotIn("DIARY_OUTPUT_DIR", env)
        self.assertNotIn("TMP_WORKSPACE", env)
        self.assertNotIn("ACTANARA_DATA_DB_PATH", env)
        self.assertNotIn("ACTANARA_DATA_EXPORT_DIR", env)
        self.assertEqual(env["ACTANARA_DATA_FOUNDATION_ENABLED"], "true")
        self.assertEqual(env["DASHBOARD_READ_SOURCE"], "foundation")
        self.assertEqual(plist["StandardOutPath"], "/tmp/logs/dashboard-server.out.log")

    def test_watchdog_plist_restarts_service_on_health_failure(self):
        plist = agent.build_watchdog_plist(
            label="com.example.dashboard.watchdog",
            service_label="com.example.dashboard",
            python=Path("/tmp/venv/bin/python"),
            script=Path("/repo/advanced/dashboard/dashboard_launch_agent.py"),
            url="http://127.0.0.1:3036/health",
            interval=60,
            actanara_home=Path("/nova"),
            logs_dir=Path("/tmp/logs"),
        )

        args = plist["ProgramArguments"]
        self.assertEqual(plist["StartInterval"], 60)
        self.assertIn("check", args)
        self.assertIn("--restart", args)
        self.assertIn("com.example.dashboard", args)
        self.assertEqual(plist["EnvironmentVariables"]["ACTANARA_HOME"], "/nova")
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/logs/dashboard-watchdog.err.log")

    def test_launch_defaults_read_dashboard_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            self._write_runtime_pointers(paths.home)
            write_settings(
                {
                    "dashboard": {
                        "projectRoot": str(root / "project"),
                        "pythonExecutable": str(root / "venv" / "bin" / "python"),
                        "host": "0.0.0.0",
                        "port": 4545,
                        "healthPath": "/healthz",
                        "logsDir": str(root / "logs"),
                        "serviceLabel": "com.example.dashboard",
                        "watchdogLabel": "com.example.dashboard.watchdog",
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                defaults = agent.dashboard_launch_defaults()

        self.assertEqual(defaults["project_root"], paths.home / "app" / "source")
        self.assertEqual(defaults["python"], paths.home / ".venv" / "bin" / "python")
        self.assertNotEqual(defaults["project_root"], root / "project")
        self.assertNotEqual(defaults["python"], root / "venv" / "bin" / "python")
        self.assertEqual(defaults["host"], "0.0.0.0")
        self.assertEqual(defaults["port"], 4545)
        self.assertEqual(defaults["url"], "http://0.0.0.0:4545/healthz")
        self.assertEqual(defaults["logs_dir"], root / "logs")
        self.assertEqual(defaults["label"], "com.example.dashboard")
        self.assertEqual(defaults["watchdog_label"], "com.example.dashboard.watchdog")

    def test_rag_launch_defaults_use_stable_runtime_source_and_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            self._write_runtime_pointers(paths.home)
            write_settings(
                {
                    "dashboard": {
                        "projectRoot": str(root / "resolved-release"),
                        "pythonExecutable": str(root / "resolved-venv" / "bin" / "python"),
                        "logsDir": str(root / "logs"),
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                defaults = rag_agent.rag_launch_defaults()

        self.assertEqual(defaults["project_root"], paths.home / "app" / "source")
        self.assertEqual(defaults["python"], paths.home / ".venv" / "bin" / "python")
        self.assertEqual(defaults["logs_dir"], root / "logs")

    def test_explicit_runtime_load_failure_never_falls_back_to_login_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected_home = root / "SelectedRuntime"
            login_home = root / "LoginHome"
            login_home.mkdir()
            with (
                patch.dict(
                    os.environ,
                    {"HOME": str(login_home), "ACTANARA_HOME": str(selected_home)},
                    clear=False,
                ),
                patch(
                    "data_foundation.paths.load_paths",
                    side_effect=RuntimeError("synthetic load failure"),
                ),
            ):
                for defaults in (agent.dashboard_launch_defaults, rag_agent.rag_launch_defaults):
                    with self.subTest(defaults=defaults.__name__):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "selected Runtime",
                        ) as raised:
                            defaults()
                        self.assertNotIn(str(login_home / ".actanara"), str(raised.exception))

            self.assertFalse((login_home / ".actanara").exists())

    def test_malformed_selected_runtime_settings_fail_closed_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "SelectedRuntime", legacy_diary_root=root / "Diary")
            settings_path = paths.config_dir / "settings.json"
            settings_path.write_bytes(b"{not-json\n")
            before = settings_path.read_bytes()

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                for defaults in (agent.dashboard_launch_defaults, rag_agent.rag_launch_defaults):
                    with self.subTest(defaults=defaults.__name__):
                        with self.assertRaisesRegex(RuntimeError, "settings"):
                            defaults()

            self.assertEqual(settings_path.read_bytes(), before)

    def test_explicit_runtime_mismatch_from_loader_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = initialize_home(root / "SelectedRuntime", legacy_diary_root=root / "Diary")
            different = initialize_home(root / "DifferentRuntime", legacy_diary_root=root / "OtherDiary")
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(selected.home)}, clear=False),
                patch("data_foundation.paths.load_paths", return_value=different),
            ):
                for defaults in (agent.dashboard_launch_defaults, rag_agent.rag_launch_defaults):
                    with self.subTest(defaults=defaults.__name__):
                        with self.assertRaisesRegex(RuntimeError, "explicit ACTANARA_HOME"):
                            defaults()

    def test_selected_runtime_rejects_absolute_pointer_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "SelectedRuntime", legacy_diary_root=root / "Diary")
            self._write_runtime_pointers(paths.home)
            write_settings({}, paths)
            source_pointer = paths.home / "app" / "source"
            concrete_release = source_pointer.resolve(strict=True)
            source_pointer.unlink()
            source_pointer.symlink_to(concrete_release)

            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False):
                for defaults in (agent.dashboard_launch_defaults, rag_agent.rag_launch_defaults):
                    with self.subTest(defaults=defaults.__name__):
                        with self.assertRaisesRegex(RuntimeError, "must be relative"):
                            defaults()

    def test_direct_writers_preserve_stable_runtime_symlinks_in_managed_plists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Runtime"
            release = runtime / "app" / "releases" / "commit-a"
            venv = runtime / "app" / "venvs" / "commit-a"
            (release / "advanced" / "dashboard").mkdir(parents=True)
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (runtime / "app" / "source").symlink_to(Path("releases") / release.name)
            (runtime / ".venv").symlink_to(Path("app") / "venvs" / venv.name)

            stable_source = runtime / "app" / "source"
            stable_python = runtime / ".venv" / "bin" / "python"
            logs = runtime / "logs"
            dashboard_plist = root / "dashboard.plist"
            watchdog_plist = root / "watchdog.plist"
            rag_plist = root / "rag.plist"
            common = {
                "python": stable_python,
                "project_root": stable_source,
                "actanara_home": runtime,
                "logs_dir": logs,
            }
            dashboard_args = SimpleNamespace(
                **common,
                label="com.example.dashboard",
                watchdog_label="com.example.dashboard.watchdog",
                host="127.0.0.1",
                port=3036,
                url=None,
                interval=60,
                foundation=True,
            )
            rag_args = SimpleNamespace(**common, label="com.example.rag-server")

            with (
                patch.object(agent, "service_plist_path", return_value=dashboard_plist),
                patch.object(agent, "watchdog_plist_path", return_value=watchdog_plist),
            ):
                agent.write_agents(dashboard_args)
            with patch.object(rag_agent, "service_plist_path", return_value=rag_plist):
                rag_agent.write_agent(rag_args)

            dashboard = plistlib.loads(dashboard_plist.read_bytes())
            watchdog = plistlib.loads(watchdog_plist.read_bytes())
            rag = plistlib.loads(rag_plist.read_bytes())

        self.assertEqual(
            dashboard["EnvironmentVariables"]["ACTANARA_DASHBOARD_PROJECT_ROOT"],
            str(stable_source),
        )
        self.assertEqual(
            dashboard["EnvironmentVariables"]["ACTANARA_DASHBOARD_PYTHON"],
            str(stable_python),
        )
        self.assertIn(f"cd {stable_source}", dashboard["ProgramArguments"][2])
        self.assertNotIn(str(release), dashboard["ProgramArguments"][2])
        self.assertEqual(
            watchdog["ProgramArguments"][:2],
            [
                str(stable_python),
                str(stable_source / "advanced" / "dashboard" / "dashboard_launch_agent.py"),
            ],
        )
        self.assertEqual(
            rag["ProgramArguments"][:2],
            [
                str(stable_python),
                str(stable_source / "advanced" / "dashboard" / "rag_server_launch_agent.py"),
            ],
        )
        self.assertEqual(
            rag["ProgramArguments"][rag["ProgramArguments"].index("--project-root") + 1],
            str(stable_source),
        )
        self.assertNotIn(str(release), "\n".join(rag["ProgramArguments"]))

    def test_direct_writers_reject_concrete_release_and_venv_paths_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Runtime"
            release = runtime / "app" / "releases" / "commit-a"
            venv = runtime / "app" / "venvs" / "commit-a"
            (release / "advanced" / "dashboard").mkdir(parents=True)
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (runtime / "app" / "source").symlink_to(Path("releases") / release.name)
            (runtime / ".venv").symlink_to(Path("app") / "venvs" / venv.name)
            logs = runtime / "logs"
            common = {
                "python": venv / "bin" / "python",
                "project_root": release,
                "actanara_home": runtime,
                "logs_dir": logs,
            }
            dashboard_args = SimpleNamespace(
                **common,
                label="com.example.dashboard",
                watchdog_label="com.example.dashboard.watchdog",
                host="127.0.0.1",
                port=3036,
                url=None,
                interval=60,
                foundation=True,
            )
            rag_args = SimpleNamespace(**common, label="com.example.rag-server")

            with self.assertRaisesRegex(RuntimeError, "stable Runtime"):
                agent.write_agents(dashboard_args)
            with self.assertRaisesRegex(RuntimeError, "stable Runtime"):
                rag_agent.write_agent(rag_args)

            self.assertFalse(logs.exists())

    def test_write_plist_round_trips(self):
        payload = {"Label": "com.example.dashboard", "RunAtLoad": True}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.plist"
            agent.write_plist(path, payload)
            self.assertEqual(plistlib.loads(path.read_bytes()), payload)

    def test_check_health_returns_false_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(agent.check_health("http://127.0.0.1:3036/health", timeout=0.1))

    def test_rag_service_plist_runs_lifecycle_wrapper(self):
        plist = rag_agent.build_service_plist(
            label="com.example.rag-server",
            python=Path("/tmp/venv/bin/python"),
            project_root=Path("/repo"),
            actanara_home=Path("/nova"),
            script=Path("/repo/advanced/dashboard/rag_server_launch_agent.py"),
            logs_dir=Path("/tmp/logs"),
        )

        args = plist["ProgramArguments"]
        env = plist["EnvironmentVariables"]
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(args[:3], ["/tmp/venv/bin/python", "/repo/advanced/dashboard/rag_server_launch_agent.py", "run"])
        self.assertIn("--project-root", args)
        self.assertIn("--actanara-home", args)
        self.assertEqual(env["ACTANARA_HOME"], "/nova")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIn("/repo/src", env["PYTHONPATH"])
        self.assertEqual(plist["StandardOutPath"], "/tmp/logs/rag-server-launchagent.out.log")

    def test_linux_rag_wrapper_turns_sigterm_into_cancelable_child_shutdown(self):
        args = SimpleNamespace(
            actanara_home=Path("/nova"),
            project_root=ROOT,
        )
        registered = {}
        settings = object()

        def register(signum, handler):
            registered[signum] = handler

        def start(_settings, **kwargs):
            registered[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(kwargs["cancel_event"].is_set())
            return {"accepted": False, "status": "canceled", "reason": "canceled"}

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(rag_agent.sys, "platform", "linux"),
            patch.object(rag_agent.signal, "signal", side_effect=register),
            patch("agentic_rag.rag_settings.resolve_rag_settings", return_value=settings),
            patch("agentic_rag.rag_server_lifecycle.start_rag_server", side_effect=start),
            patch("agentic_rag.rag_server_lifecycle.stop_rag_server") as stop,
        ):
            result = rag_agent.run_server(args)

        self.assertEqual(result, 0)
        stop.assert_called_once()

    def test_rag_launcher_does_not_restart_a_run_at_load_manager_after_bootstrap(self):
        with patch.object(rag_agent, "rag_launch_defaults") as defaults:
            defaults.return_value = {
                "label": "com.example.rag-server",
                "python": Path("/tmp/venv/bin/python"),
                "project_root": Path("/repo"),
                "actanara_home": Path("/nova"),
                "logs_dir": Path("/tmp/logs"),
            }
            jobs = launcher._jobs("rag")

        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0]["kickstart"])

    def test_managed_python_wrappers_disable_source_bytecode_before_project_imports(self):
        wrappers = (
            ROOT / "advanced" / "dashboard" / "dashboard_launch_agent.py",
            ROOT / "advanced" / "dashboard" / "rag_server_launch_agent.py",
            ROOT / "advanced" / "pipeline" / "run_daily_pipeline.py",
            ROOT / "advanced" / "pipeline" / "run_dashboard_foundation_refresh.py",
        )
        for path in wrappers:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn("sys.dont_write_bytecode = True", source)

    def test_launcher_install_requires_confirmation_and_supports_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            plist_path = root / "LaunchAgents" / "com.actanara.rag-server.plist"
            job = {
                "kind": "rag-server",
                "label": "com.actanara.rag-server",
                "plistPath": str(plist_path),
                "plist": {"Label": "com.actanara.rag-server", "ProgramArguments": ["python", "rag"]},
            }
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(launcher, "_jobs", return_value=[job]),
                patch.object(launcher, "_launchctl") as launchctl,
            ):
                dry_run = launcher.install_rag_launch_agent({"dryRun": True})
                with self.assertRaisesRegex(ValueError, "confirmationText"):
                    launcher.install_rag_launch_agent({"confirmationText": "wrong"})

            self.assertEqual(dry_run["confirmationTextRequired"], launcher.RAG_INSTALL_CONFIRMATION)
            self.assertFalse(plist_path.exists())
            launchctl.assert_not_called()

    def test_launcher_uses_stable_import_paths_for_managed_scripts(self):
        source = Path(launcher.__file__).read_text(encoding="utf-8")

        self.assertIn('script=defaults["project_root"] / "advanced" / "dashboard" / "dashboard_launch_agent.py"', source)
        self.assertIn('script=defaults["project_root"] / "advanced" / "dashboard" / "rag_server_launch_agent.py"', source)
        self.assertNotIn("script=Path(dashboard_launch_agent.__file__)", source)
        self.assertNotIn("script=Path(rag_server_launch_agent.__file__)", source)
        self.assertNotIn("script=Path(dashboard_launch_agent.__file__).resolve()", source)
        self.assertNotIn("script=Path(rag_server_launch_agent.__file__).resolve()", source)

    def test_launcher_preview_reports_actual_launchd_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            plist_path = root / "LaunchAgents" / "com.actanara.rag-server.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_text("placeholder", encoding="utf-8")
            job = {
                "kind": "rag-server",
                "label": "com.actanara.rag-server",
                "plistPath": str(plist_path),
                "plist": {"Label": "com.actanara.rag-server", "ProgramArguments": ["python", "rag"]},
            }
            commands = []

            def loaded_runner(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "state = running", "")

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(launcher, "_jobs", return_value=[job]),
                patch.object(launcher.platform, "system", return_value="Darwin"),
                patch.object(launcher.os, "getuid", return_value=501),
            ):
                preview = launcher.preview_rag_launch_agent(launchctl_runner=loaded_runner)

        self.assertTrue(preview["registered"])
        self.assertTrue(preview["actualRegistered"])
        self.assertFalse(preview["configuredRegistered"])
        self.assertTrue(preview["registrationMismatch"])
        self.assertEqual(preview["registrationSource"], "launchd-probe")
        self.assertEqual(preview["runtimeProbe"]["status"], "loaded")
        self.assertEqual(preview["runtimeProbe"]["loadedJobs"], 1)
        self.assertEqual(preview["jobs"][0]["runtimeStatus"]["status"], "loaded")
        self.assertEqual(commands, [["launchctl", "print", "gui/501/com.actanara.rag-server"]])

    def test_launcher_install_backs_up_existing_plist_and_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            plist_path = root / "LaunchAgents" / "com.actanara.rag-server.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_text("old", encoding="utf-8")
            job = {
                "kind": "rag-server",
                "label": "com.actanara.rag-server",
                "plistPath": str(plist_path),
                "plist": {"Label": "com.actanara.rag-server", "ProgramArguments": ["python", "rag"]},
            }
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(launcher, "_jobs", return_value=[job]),
                patch.object(launcher, "_launchctl") as launchctl,
            ):
                result = launcher.install_rag_launch_agent({"confirmationText": launcher.RAG_INSTALL_CONFIRMATION})
                settings = read_settings(paths)

            self.assertEqual(result["status"], "registered")
            self.assertTrue(plist_path.exists())
            self.assertEqual(plistlib.loads(plist_path.read_bytes())["Label"], "com.actanara.rag-server")
            self.assertTrue(Path(result["backupDir"]).joinpath(plist_path.name).exists())
            self.assertTrue(settings["rag"]["server"]["launchAgent"]["registered"])
            self.assertEqual(settings["rag"]["server"]["launchAgent"]["registrationManagedBy"], "dashboard")
            self.assertGreaterEqual(launchctl.call_count, 2)

    def test_launcher_install_failure_writes_failed_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            plist_path = root / "LaunchAgents" / "com.actanara.rag-server.plist"
            job = {
                "kind": "rag-server",
                "label": "com.actanara.rag-server",
                "plistPath": str(plist_path),
                "plist": {"Label": "com.actanara.rag-server", "ProgramArguments": ["python", "rag"]},
            }

            def fail_bootstrap(action, label, plist_path, allow_failure=False):
                if action == "bootstrap":
                    raise RuntimeError("bootstrap failed")
                return subprocess.CompletedProcess(["launchctl"], 0, "", "")

            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(launcher, "_jobs", return_value=[job]),
                patch.object(launcher, "_launchctl", side_effect=fail_bootstrap),
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    launcher.install_rag_launch_agent({"confirmationText": launcher.RAG_INSTALL_CONFIRMATION})
                settings = read_settings(paths)

            audit = settings["rag"]["server"]["launchAgent"]
            self.assertFalse(audit["registered"])
            self.assertEqual(audit["lastAction"], "install")
            self.assertEqual(audit["lastActionStatus"], "failed")
            self.assertIn("bootstrap failed", audit["lastError"])
            self.assertTrue(audit["operationResults"])
            self.assertIn("rollbackHint", audit)

    def test_launcher_uninstall_moves_plist_to_backup_and_marks_unregistered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            plist_path = root / "LaunchAgents" / "com.actanara.dashboard.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_text("old", encoding="utf-8")
            job = {
                "kind": "dashboard-service",
                "label": "com.actanara.dashboard",
                "plistPath": str(plist_path),
                "plist": {"Label": "com.actanara.dashboard", "ProgramArguments": ["python", "dashboard"]},
            }
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(launcher, "_jobs", return_value=[job]),
                patch.object(launcher, "_launchctl") as launchctl,
            ):
                result = launcher.uninstall_dashboard_launch_agent({"confirmationText": launcher.DASHBOARD_UNINSTALL_CONFIRMATION})
                settings = read_settings(paths)

            self.assertEqual(result["status"], "unregistered")
            self.assertFalse(plist_path.exists())
            self.assertTrue(Path(result["backupDir"]).joinpath(plist_path.name).exists())
            self.assertFalse(settings["dashboard"]["launchAgent"]["registered"])
            launchctl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
