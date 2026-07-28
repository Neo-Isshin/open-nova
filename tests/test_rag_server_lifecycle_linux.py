import json
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentic_rag import rag_server_lifecycle
from agentic_rag.rag_settings import resolve_rag_settings
from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings


SOURCE_COMMIT = "a" * 40


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


class RagServerLinuxLifecycleTests(unittest.TestCase):
    def _settings(self, root: Path, *, provider="local", model="fixture-model", dimension=384):
        paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
        embedding = {
            "mode": provider,
            "provider": provider,
            "providerId": "local" if provider == "local" else "fixture-cloud",
            "model": model,
            "dimension": dimension,
        }
        write_settings(
            {
                "rag": {
                    "enabled": True,
                    "mode": "v2",
                    "embedding": embedding,
                    "server": {"enabled": True, "host": "127.0.0.1", "port": 3037},
                }
            },
            paths,
        )
        return paths, resolve_rag_settings(paths)

    @staticmethod
    def _health_payload(settings, *, status="ok", loaded=True, source_commit=SOURCE_COMMIT):
        profile = {
            "mode": settings.embedding_provider,
            "providerId": settings.embedding_provider_id,
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
        }
        return {
            "status": status,
            "sourceCommit": source_commit,
            "provider": settings.embedding_provider,
            "providerId": settings.embedding_provider_id,
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "embeddingProfile": profile,
            "providerLoaded": loaded,
        }

    def test_health_requires_json_source_commit_provider_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            cases = (
                (b"not-json", "invalid-json"),
                ({"status": "ok"}, "source-commit-missing"),
                (
                    self._health_payload(settings, source_commit="b" * 40),
                    "source-commit-mismatch",
                ),
                (
                    {**self._health_payload(settings), "provider": "cloud"},
                    "provider-mismatch",
                ),
                (
                    {
                        **self._health_payload(settings),
                        "embeddingProfile": {
                            **self._health_payload(settings)["embeddingProfile"],
                            "model": "wrong-model",
                        },
                    },
                    "embedding-profile-mismatch",
                ),
            )
            for payload, reason in cases:
                with self.subTest(reason=reason), patch.object(
                    rag_server_lifecycle.urllib.request,
                    "urlopen",
                    return_value=FakeResponse(payload),
                ):
                    health = rag_server_lifecycle.probe_rag_server_health(
                        settings,
                        expected_source_commit=SOURCE_COMMIT,
                        timeout_seconds=0.1,
                    )
                self.assertFalse(health["ready"])
                self.assertFalse(health["healthy"])
                self.assertEqual(health["reasonCode"], reason)

    def test_health_expresses_local_cold_model_as_starting_not_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            payload = self._health_payload(settings, status="booting", loaded=False)
            with patch.object(
                rag_server_lifecycle.urllib.request,
                "urlopen",
                return_value=FakeResponse(payload),
            ):
                health = rag_server_lifecycle.probe_rag_server_health(
                    settings,
                    expected_source_commit=SOURCE_COMMIT,
                    timeout_seconds=0.1,
                )

        self.assertFalse(health["ready"])
        self.assertFalse(health["healthy"])
        self.assertEqual(health["status"], "starting")
        self.assertEqual(health["phase"], "cold-model-loading")
        self.assertTrue(health["coldModel"])
        self.assertTrue(health["identityMatches"])

    def test_linux_lifecycle_does_not_treat_foreign_http_200_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            foreign = {
                "url": "http://127.0.0.1:3037/health",
                "healthy": True,
                "statusCode": 200,
                "error": None,
                "payload": {"status": "ok"},
            }
            managed = {
                **foreign,
                "payload": self._health_payload(settings, source_commit=None),
            }
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "_probe_health", return_value=foreign),
            ):
                foreign_state = rag_server_lifecycle.read_server_process_state(
                    settings,
                    probe_health=True,
                )
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "_probe_health", return_value=managed),
            ):
                managed_state = rag_server_lifecycle.read_server_process_state(
                    settings,
                    probe_health=True,
                )

        self.assertFalse(foreign_state["running"])
        self.assertFalse(foreign_state["health"]["healthy"])
        self.assertTrue(managed_state["running"])
        self.assertEqual(managed_state["status"], "healthy")

    def test_port_conflict_distinguishes_external_and_managed_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, settings = self._settings(Path(tmp))
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
            starting_health = {
                "ready": False,
                "status": "starting",
                "phase": "cold-model-loading",
                "coldModel": True,
                "identityMatches": False,
                "reasonCode": "connection-unavailable",
                "reachable": False,
            }
            external_listener = {
                "listening": True,
                "pids": [9988],
                "managed": False,
                "managedPid": None,
                "inspectable": True,
            }
            managed_listener = {
                **external_listener,
                "pids": [4321],
                "managed": True,
                "managedPid": 4321,
            }
            with (
                patch.object(rag_server_lifecycle, "probe_rag_server_health", return_value=starting_health),
                patch.object(rag_server_lifecycle, "_state_process_running", return_value=True),
                patch.object(rag_server_lifecycle, "inspect_rag_server_port", return_value=external_listener),
            ):
                external = rag_server_lifecycle.probe_rag_server_readiness(
                    settings,
                    expected_source_commit=SOURCE_COMMIT,
                )
            with (
                patch.object(rag_server_lifecycle, "probe_rag_server_health", return_value=starting_health),
                patch.object(rag_server_lifecycle, "_state_process_running", return_value=True),
                patch.object(rag_server_lifecycle, "inspect_rag_server_port", return_value=managed_listener),
            ):
                managed = rag_server_lifecycle.probe_rag_server_readiness(
                    settings,
                    expected_source_commit=SOURCE_COMMIT,
                )

        self.assertEqual(external["status"], "port-conflict")
        self.assertTrue(external["rollbackRequired"])
        self.assertEqual(managed["status"], "starting")
        self.assertFalse(managed["rollbackRequired"])
        self.assertTrue(managed["listener"]["managed"])

    def test_matching_health_cannot_override_foreign_proc_listener_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, settings = self._settings(Path(tmp))
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
            ready_health = {
                "ready": True,
                "healthy": True,
                "status": "ready",
                "phase": "ready",
                "identityMatches": True,
                "reasonCode": None,
                "reachable": True,
            }
            external_listener = {
                "listening": True,
                "pids": [9988],
                "managed": False,
                "managedPid": None,
                "inspectable": True,
                "basis": "linux-proc-socket-owner",
            }
            with (
                patch.object(
                    rag_server_lifecycle,
                    "probe_rag_server_health",
                    return_value=ready_health,
                ),
                patch.object(
                    rag_server_lifecycle,
                    "_state_process_running",
                    return_value=True,
                ),
                patch.object(
                    rag_server_lifecycle,
                    "inspect_rag_server_port",
                    return_value=external_listener,
                ),
            ):
                readiness = rag_server_lifecycle.probe_rag_server_readiness(
                    settings,
                    expected_source_commit=SOURCE_COMMIT,
                )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "port-conflict")
        self.assertEqual(
            readiness["reasonCode"],
            "rag-port-owned-by-external-process",
        )

    def test_linux_proc_listener_evidence_maps_socket_inode_to_managed_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            (proc_root / "net").mkdir(parents=True)
            (proc_root / "net" / "tcp").write_text(
                "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
                "0: 0100007F:0BDD 00000000:0000 0A 00000000:00000000 "
                "00:00000000 00000000 501 0 12345 1\n",
                encoding="ascii",
            )
            (proc_root / "net" / "tcp6").write_text("header\n", encoding="ascii")
            fd_root = proc_root / "4321" / "fd"
            fd_root.mkdir(parents=True)
            (fd_root / "7").symlink_to("socket:[12345]")
            _paths, settings = self._settings(root)
            state = {"pid": 4321}
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "_state_process_running", return_value=True),
            ):
                listener = rag_server_lifecycle.inspect_rag_server_port(
                    settings,
                    state=state,
                    proc_root=proc_root,
                )

        self.assertTrue(listener["inspectable"])
        self.assertTrue(listener["listening"])
        self.assertEqual(listener["pids"], [4321])
        self.assertTrue(listener["managed"])

    def test_wait_handles_slow_start_and_health_recovery(self):
        starting = {
            "ready": False,
            "status": "starting",
            "phase": "cold-model-loading",
            "coldModel": True,
            "rollbackRequired": False,
        }
        ready = {
            "ready": True,
            "status": "ready",
            "phase": "ready",
            "coldModel": False,
            "rollbackRequired": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            with patch.object(
                rag_server_lifecycle,
                "probe_rag_server_readiness",
                side_effect=[starting, starting, ready],
            ) as probe:
                result = rag_server_lifecycle.wait_for_rag_server_readiness(
                    settings,
                    expected_source_commit=SOURCE_COMMIT,
                    timeout_seconds=1,
                    poll_interval_seconds=0,
                )

        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(probe.call_count, 3)

    def test_model_load_failure_is_terminal_and_requests_rollback(self):
        starting = {
            "ready": False,
            "status": "starting",
            "phase": "cold-model-loading",
            "coldModel": True,
            "rollbackRequired": False,
        }
        failed = {
            "ready": False,
            "status": "failed",
            "phase": "process-exited",
            "reasonCode": "model-process-exited",
            "coldModel": True,
            "rollbackRequired": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            with patch.object(
                rag_server_lifecycle,
                "probe_rag_server_readiness",
                side_effect=[starting, failed],
            ):
                result = rag_server_lifecycle.wait_for_rag_server_readiness(
                    settings,
                    timeout_seconds=1,
                    poll_interval_seconds=0,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reasonCode"], "model-process-exited")
        self.assertTrue(result["rollbackRequired"])

    def test_required_readiness_runs_rollback_callback_on_timeout(self):
        starting = {
            "ready": False,
            "status": "starting",
            "phase": "cold-model-loading",
            "coldModel": True,
            "rollbackRequired": False,
        }
        rollback = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            with patch.object(
                rag_server_lifecycle,
                "probe_rag_server_readiness",
                return_value=starting,
            ):
                with self.assertRaises(rag_server_lifecycle.RagServerReadinessError) as raised:
                    rag_server_lifecycle.require_rag_server_readiness(
                        settings,
                        timeout_seconds=0,
                        poll_interval_seconds=0,
                        rollback=rollback,
                    )

        self.assertEqual(raised.exception.result["status"], "timeout")
        self.assertTrue(raised.exception.result["rollbackRequired"])
        rollback.assert_called_once_with(raised.exception.result)

    def test_linux_process_identity_detects_pid_reuse_by_starttime(self):
        recorded = {
            "pid": 4321,
            "startTimeTicks": 101,
            "exe": "/runtime/python",
            "cmdline": ["/runtime/python", "/runtime/embedding_server.py"],
        }
        reused = {**recorded, "startTimeTicks": 202}
        state = {
            "pid": 4321,
            "processIdentity": recorded,
            "command": recorded["cmdline"],
            "cwd": str(rag_server_lifecycle.ROOT),
        }
        with (
            patch.object(rag_server_lifecycle.sys, "platform", "linux"),
            patch.object(rag_server_lifecycle, "_pid_running", return_value=True),
            patch.object(rag_server_lifecycle, "_read_linux_process_identity", return_value=reused),
        ):
            self.assertFalse(rag_server_lifecycle._state_process_running(state))
            self.assertFalse(rag_server_lifecycle._state_matches_rag_server(state))

    def test_linux_process_identity_reads_proc_stat_exe_cmdline_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp)
            process_root = proc_root / "4321"
            process_root.mkdir()
            fields = ["S", *("0" for _ in range(18)), "98765"]
            (process_root / "stat").write_text(
                f"4321 (model worker ) name) {' '.join(fields)}\n",
                encoding="utf-8",
            )
            executable = proc_root / "runtime-python"
            executable.write_text("", encoding="utf-8")
            (process_root / "exe").symlink_to(executable)
            (process_root / "cmdline").write_bytes(
                b"/runtime/python\0/runtime/embedding_server.py\0"
            )
            cwd = proc_root / "source"
            cwd.mkdir()
            (process_root / "cwd").symlink_to(cwd)

            identity = rag_server_lifecycle._read_linux_process_identity(
                4321,
                proc_root=proc_root,
            )

        self.assertEqual(identity["pid"], 4321)
        self.assertEqual(identity["startTimeTicks"], 98765)
        self.assertEqual(identity["exe"], str(executable))
        self.assertEqual(
            identity["cmdline"],
            ["/runtime/python", "/runtime/embedding_server.py"],
        )
        self.assertEqual(identity["cwd"], str(cwd))

    def test_legacy_linux_state_uses_live_exe_cmdline_and_cwd_evidence(self):
        command = [sys.executable, str(rag_server_lifecycle.SERVER_SCRIPT)]
        state = {
            "schemaVersion": 1,
            "pid": 4321,
            "command": command,
            "cwd": str(rag_server_lifecycle.ROOT),
        }
        live = {
            "pid": 4321,
            "startTimeTicks": 98765,
            "exe": str(Path(sys.executable).resolve()),
            "cmdline": command,
            "cwd": str(rag_server_lifecycle.ROOT),
        }
        with (
            patch.object(rag_server_lifecycle.sys, "platform", "linux"),
            patch.object(rag_server_lifecycle, "_pid_running", return_value=True),
            patch.object(rag_server_lifecycle, "_read_linux_process_identity", return_value=live),
        ):
            self.assertTrue(rag_server_lifecycle._state_process_running(state))
            self.assertTrue(rag_server_lifecycle._state_matches_rag_server(state))

    def test_linux_stop_refuses_reused_pid_without_sending_a_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, settings = self._settings(Path(tmp))
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": 4321,
                        "processIdentity": {
                            "pid": 4321,
                            "startTimeTicks": 101,
                            "exe": "/runtime/python",
                            "cmdline": ["/runtime/python", str(rag_server_lifecycle.SERVER_SCRIPT)],
                        },
                        "command": ["/runtime/python", str(rag_server_lifecycle.SERVER_SCRIPT)],
                        "cwd": str(rag_server_lifecycle.ROOT),
                    }
                ),
                encoding="utf-8",
            )
            reused = {
                "pid": 4321,
                "startTimeTicks": 202,
                "exe": "/usr/bin/unrelated",
                "cmdline": ["/usr/bin/unrelated"],
            }
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "_read_linux_process_identity", return_value=reused),
                patch.object(rag_server_lifecycle, "_pid_running", return_value=True),
                patch.object(rag_server_lifecycle.os, "kill") as kill,
                patch.object(rag_server_lifecycle.os, "killpg") as killpg,
            ):
                result = rag_server_lifecycle.stop_rag_server(settings, wait_timeout_seconds=0)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "refused")
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_linux_stop_escalates_verified_process_group_after_short_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, settings = self._settings(Path(tmp))
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": 4321,
                        "processGroupId": 4321,
                        "command": ["/runtime/python", str(rag_server_lifecycle.SERVER_SCRIPT)],
                        "cwd": str(rag_server_lifecycle.ROOT),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "_state_matches_rag_server", return_value=True),
                patch.object(
                    rag_server_lifecycle,
                    "_state_process_running",
                    side_effect=[True, True, True, True, True, False, False, False],
                ),
                patch.object(rag_server_lifecycle.os, "getpgid", return_value=4321),
                patch.object(rag_server_lifecycle.os, "killpg") as killpg,
                patch.object(rag_server_lifecycle.time, "sleep"),
            ):
                result = rag_server_lifecycle.stop_rag_server(settings, wait_timeout_seconds=0)

        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["forced"])
        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(4321, signal.SIGTERM),
                unittest.mock.call(4321, signal.SIGKILL),
            ],
        )

    def test_canceled_start_never_spawns_the_model_server(self):
        canceled = threading.Event()
        canceled.set()
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            with patch.object(rag_server_lifecycle.subprocess, "Popen") as popen:
                result = rag_server_lifecycle.start_rag_server(
                    settings,
                    requested_by="systemd",
                    cancel_event=canceled,
                )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "canceled")
        popen.assert_not_called()

    def test_cancel_during_cold_start_stops_spawned_child_group(self):
        class FakeProcess:
            pid = 4321

        canceled = threading.Event()
        canceled_readiness = {
            "ready": False,
            "status": "canceled",
            "phase": "canceled",
            "reasonCode": "rag-start-canceled",
            "rollbackRequired": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            _paths, settings = self._settings(Path(tmp))
            with (
                patch.object(rag_server_lifecycle.sys, "platform", "linux"),
                patch.object(rag_server_lifecycle, "probe_rag_server_readiness", return_value={"status": "stopped"}),
                patch.object(rag_server_lifecycle, "_select_server_python", return_value=sys.executable),
                patch.object(rag_server_lifecycle.subprocess, "Popen", return_value=FakeProcess()),
                patch.object(
                    rag_server_lifecycle,
                    "_read_linux_process_identity",
                    return_value={
                        "schemaVersion": 1,
                        "pid": 4321,
                        "startTimeTicks": 98765,
                        "exe": sys.executable,
                        "cmdline": [sys.executable, str(rag_server_lifecycle.SERVER_SCRIPT)],
                        "cwd": str(rag_server_lifecycle.ROOT),
                    },
                ),
                patch.object(rag_server_lifecycle.os, "getpgid", return_value=4321),
                patch.object(
                    rag_server_lifecycle,
                    "wait_for_rag_server_readiness",
                    return_value=canceled_readiness,
                ),
                patch.object(rag_server_lifecycle, "stop_rag_server", return_value={"status": "stopped"}) as stop,
            ):
                result = rag_server_lifecycle.start_rag_server(
                    settings,
                    requested_by="systemd",
                    cancel_event=canceled,
                )

        self.assertEqual(result["status"], "canceled")
        stop.assert_called_once_with(
            settings,
            requested_by="systemd",
            wait_timeout_seconds=2.0,
        )


if __name__ == "__main__":
    unittest.main()
