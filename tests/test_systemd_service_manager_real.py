import os
import platform
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))


RUN_REAL_SYSTEMD = (
    os.getenv("ACTANARA_RUN_REAL_SYSTEMD_TESTS") == "1" and platform.system() == "Linux"
)


class _SimulatedCrash(BaseException):
    pass


@unittest.skipUnless(
    RUN_REAL_SYSTEMD,
    "set ACTANARA_RUN_REAL_SYSTEMD_TESTS=1 on a disposable systemd-user test host",
)
class RealSystemdServiceManagerTests(unittest.TestCase):
    """Opt-in lifecycle gate that mutates only unique, test-owned user units."""

    def test_service_scheduler_failure_and_recovery_lifecycle(self):
        from app.services import scheduler
        from app.services.service_manager import PlatformServiceManager
        from data_foundation.paths import initialize_home
        from data_foundation.settings import write_settings
        from data_foundation.systemd_user import (
            MANAGED_UNIT_HEADER,
            SystemdUserError,
            UserUnit,
            inspect_user_units,
            install_user_units,
            recover_user_unit_transactions,
            uninstall_user_units,
        )

        prefix = f"actanara-test-sm-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        known_names: set[str] = set()
        unmanaged_path = unit_dir / f"{prefix}-unmanaged.service"

        def service(name: str, description: str, executable: str = "/usr/bin/sleep") -> UserUnit:
            content = "\n".join(
                (
                    MANAGED_UNIT_HEADER,
                    "[Unit]",
                    f"Description={description}",
                    "",
                    "[Service]",
                    "Type=simple",
                    "WorkingDirectory=/tmp",
                    f'ExecStart="{executable}" "3600"' if executable.endswith("sleep") else f'ExecStart="{executable}"',
                    "",
                    "[Install]",
                    "WantedBy=default.target",
                    "",
                )
            )
            unit = UserUnit(name=name, content=content)
            known_names.add(name)
            return unit

        def systemctl(*arguments: str, allow: set[int] | None = None) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["systemctl", "--user", *arguments],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            if result.returncode not in (allow or {0}):
                self.fail(
                    f"systemctl --user {' '.join(arguments)} failed: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
            return result

        systemctl("show-environment")
        with tempfile.TemporaryDirectory(prefix=f"{prefix}-") as raw_root:
            paths = initialize_home(
                Path(raw_root) / "runtime",
                legacy_diary_root=Path(raw_root) / "diary",
            )
            current = [service(f"{prefix}-dashboard.service", "Actanara real gate v1")]
            failing = service(f"{prefix}-failure.service", "Actanara failure gate", "/usr/bin/false")
            interrupted = service(f"{prefix}-recovery.service", "Actanara recovery gate")
            timer_label = f"{prefix}.daily"

            try:
                manager = PlatformServiceManager(paths=paths)
                # This real-systemd gate substitutes unique, test-owned unit
                # names for the product's fixed units.  Keep the transaction's
                # Settings-derived revalidation on the same injected boundary
                # so the safety check validates the selected test definition
                # instead of comparing it with actanara-dashboard.service.
                manager._units = lambda _kind: list(current)
                manager._units_from_settings = lambda _kind, _settings: list(current)
                installed = manager.install(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )
                self.assertEqual(installed["status"], "registered")
                self.assertTrue(manager.status("dashboard")["definitionsAligned"])

                manager.stop(
                    "dashboard",
                    {"confirmationText": "STOP ACTANARA DASHBOARD SERVICE"},
                )
                self.assertFalse(manager.status("dashboard")["actualRunning"])
                manager.start(
                    "dashboard",
                    {"confirmationText": "START ACTANARA DASHBOARD SERVICE"},
                )
                self.assertTrue(manager.status("dashboard")["actualRunning"])

                current[:] = [service(current[0].name, "Actanara real gate v2")]
                updated = manager.update(
                    "dashboard",
                    {"confirmationText": "INSTALL ACTANARA DASHBOARD SERVICE"},
                )
                self.assertEqual(updated["changedUnits"], [current[0].name])
                self.assertEqual(updated["restartedUnits"], [current[0].name])
                manager.restart(
                    "dashboard",
                    {"confirmationText": "RESTART ACTANARA DASHBOARD SERVICE"},
                )

                with self.assertRaises(SystemdUserError):
                    install_user_units(paths, [failing])
                self.assertFalse((unit_dir / failing.name).exists())

                def crash(phase: str, _transaction_id: str) -> None:
                    if phase == "after-definitions-applied":
                        raise _SimulatedCrash()

                with (
                    patch("data_foundation.systemd_user.systemd_transaction_checkpoint", side_effect=crash),
                    self.assertRaises(_SimulatedCrash),
                ):
                    install_user_units(paths, [interrupted])
                # Raising in-process leaves the journal owner alive, so also
                # model the dead durable owner that a real process crash leaves.
                with patch(
                    "data_foundation.systemd_user._same_systemd_transaction_owner",
                    return_value=False,
                ):
                    recovered = recover_user_unit_transactions(paths)
                self.assertTrue(any(item.get("status") == "compensated" for item in recovered))
                self.assertFalse((unit_dir / interrupted.name).exists())

                unit_dir.mkdir(parents=True, exist_ok=True)
                unmanaged_path.write_text("[Unit]\nDescription=operator owned test fixture\n", encoding="utf-8")
                unmanaged = UserUnit(name=unmanaged_path.name, content=current[0].content)
                with self.assertRaisesRegex(SystemdUserError, "unmanaged"):
                    install_user_units(paths, [unmanaged])
                with self.assertRaisesRegex(SystemdUserError, "unmanaged"):
                    uninstall_user_units(paths, [unmanaged])
                self.assertIn("operator owned", unmanaged_path.read_text(encoding="utf-8"))
                unmanaged_path.unlink()
                systemctl("daemon-reload")

                write_settings(
                    {
                        "schedule": {
                            "enabled": False,
                            "mode": "system",
                            "timezone": "UTC",
                            "dailyPipelineTime": "23:51",
                            "dashboardAggregationTime": "23:52",
                            "systemTimer": {
                                "provider": "systemd",
                                "label": timer_label,
                                "registered": False,
                            },
                        }
                    },
                    paths,
                )
                with patch("app.services.scheduler.load_paths", return_value=paths):
                    timer_install = scheduler.install_system_timer(
                        {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION}
                    )
                    timer_names = {
                        name
                        for item in timer_install["installed"]
                        for name in (item["unitName"], item["timerName"])
                    }
                    known_names.update(timer_names)
                    write_settings(
                        {"schedule": {"dailyPipelineTime": "23:53"}},
                        paths,
                    )
                    timer_update = scheduler.install_system_timer(
                        {"confirmationText": scheduler.SCHEDULER_INSTALL_CONFIRMATION}
                    )
                    self.assertEqual(timer_update["handoff"]["status"], "committed")
                    timer_remove = scheduler.uninstall_system_timer(
                        {"confirmationText": scheduler.SCHEDULER_UNINSTALL_CONFIRMATION}
                    )
                    self.assertEqual(timer_remove["handoff"]["status"], "committed")

                removed = manager.uninstall(
                    "dashboard",
                    {"confirmationText": "UNINSTALL ACTANARA DASHBOARD SERVICE"},
                )
                self.assertEqual(removed["status"], "unregistered")
                self.assertFalse((unit_dir / current[0].name).exists())
            finally:
                unmanaged_path.unlink(missing_ok=True)
                managed_units: list[UserUnit] = []
                for name in sorted(known_names):
                    target = unit_dir / name
                    try:
                        content = target.read_text(encoding="utf-8")
                    except (FileNotFoundError, OSError, UnicodeError):
                        continue
                    if content.splitlines()[:1] == [MANAGED_UNIT_HEADER]:
                        managed_units.append(
                            UserUnit(name=name, content=content, enable_now=name.endswith((".service", ".timer")))
                        )
                if managed_units:
                    uninstall_user_units(paths, managed_units)
                if known_names:
                    systemctl("reset-failed", *sorted(known_names), allow={0, 1, 3, 4, 5})
                systemctl("daemon-reload")
                leftovers = [name for name in sorted(known_names) if (unit_dir / name).exists()]
                self.assertEqual(leftovers, [])
                listed = systemctl(
                    "list-units",
                    "--all",
                    f"{prefix}*",
                    "--no-legend",
                    "--no-pager",
                ).stdout.strip()
                self.assertEqual(listed, "")
