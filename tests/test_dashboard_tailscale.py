import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import tailscale
from app.services.dashboard_security import is_protected_path


def completed(argv, *, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class QueueRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class DashboardTailscaleTests(unittest.TestCase):
    def test_not_installed_is_read_only_and_actionable(self):
        status = tailscale.tailscale_status(which=lambda _name: None)

        self.assertFalse(status["installed"])
        self.assertEqual(status["loginState"], "not-installed")
        self.assertFalse(status["canEnableServe"])
        self.assertEqual(status["errors"][0]["code"], "tailscale-not-installed")
        self.assertFalse(status["funnel"]["available"])

    def test_logged_out_does_not_probe_ip_or_serve(self):
        raw = {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.test"}
        runner = QueueRunner([completed([], stdout=json.dumps(raw))])

        status = tailscale.tailscale_status(runner=runner, which=lambda _name: "/usr/bin/tailscale")

        self.assertEqual(status["loginState"], "logged-out")
        self.assertFalse(status["connected"])
        self.assertEqual([call[0] for call in runner.calls], [["/usr/bin/tailscale", "status", "--json"]])

    def test_connected_status_detects_ip_magic_dns_reachability_and_managed_serve(self):
        raw_status = {
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.8", "fd7a:115c:a1e0::8"],
            "Self": {"Online": True, "DNSName": "actanara.example.ts.net."},
            "CurrentTailnet": {"MagicDNSSuffix": "example.ts.net"},
        }
        raw_serve = {
            "Web": {
                "actanara.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:3036"}}
                }
            }
        }
        runner = QueueRunner(
            [completed([], stdout=json.dumps(raw_status)), completed([], stdout=json.dumps(raw_serve))]
        )

        status = tailscale.tailscale_status(runner=runner, which=lambda _name: "/opt/bin/tailscale")

        self.assertTrue(status["connected"])
        self.assertTrue(status["reachable"])
        self.assertEqual(status["reachability"]["basis"], "tailscale-status-self-online-and-ip")
        self.assertFalse(status["reachability"]["httpServeProbed"])
        self.assertEqual(status["ips"], {"ipv4": "100.64.0.8", "ipv6": "fd7a:115c:a1e0::8"})
        self.assertEqual(status["dns"]["origin"], "https://actanara.example.ts.net")
        self.assertTrue(status["serve"]["exclusiveManaged"])
        self.assertFalse(status["serve"]["exposesNovaRag"])
        self.assertTrue(status["canDisableServe"])

    def test_unparsed_existing_serve_configuration_fails_closed_as_conflict(self):
        raw_status = {
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.8", "fd7a:115c:a1e0::8"],
            "Self": {"Online": True, "DNSName": "actanara.example.ts.net."},
        }
        raw_serve = {"TCP": {"22": {"TCPForward": "127.0.0.1:22"}}}
        runner = QueueRunner(
            [completed([], stdout=json.dumps(raw_status)), completed([], stdout=json.dumps(raw_serve))]
        )

        status = tailscale.tailscale_status(runner=runner, which=lambda _name: "/opt/bin/tailscale")

        self.assertTrue(status["serve"]["enabled"])
        self.assertTrue(status["serve"]["conflict"])
        self.assertFalse(status["canEnableServe"])
        self.assertFalse(status["canDisableServe"])

    def test_command_failure_is_reported_without_mutation(self):
        runner = QueueRunner([completed([], stderr="daemon unavailable", returncode=1)])

        status = tailscale.tailscale_status(runner=runner, which=lambda _name: "/usr/bin/tailscale")

        self.assertEqual(status["loginState"], "error")
        self.assertEqual(status["errors"][0]["code"], "tailscale-command-failed")
        self.assertEqual(len(runner.calls), 1)

    def test_nonzero_logged_out_message_is_classified_without_followup_commands(self):
        runner = QueueRunner([completed([], stderr="Logged out.", returncode=1)])

        status = tailscale.tailscale_status(runner=runner, which=lambda _name: "/usr/bin/tailscale")

        self.assertEqual(status["loginState"], "logged-out")
        self.assertEqual(status["errors"][0]["code"], "tailscale-not-logged-in")
        self.assertEqual(len(runner.calls), 1)

    def test_enable_uses_fixed_argv_and_never_shell(self):
        runner = QueueRunner([completed([])])
        status = self._connected_status(enabled=False)

        result = tailscale.set_dashboard_serve(
            True,
            {"confirmationText": tailscale.ENABLE_CONFIRMATION},
            observed_status=status,
            runner=runner,
            which=lambda _name: "/usr/bin/tailscale",
        )

        self.assertTrue(result["changed"])
        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv,
            ["/usr/bin/tailscale", "serve", "--yes", "--bg", "--https=443", "http://127.0.0.1:3036"],
        )
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], tailscale.COMMAND_TIMEOUT_SECONDS)
        self.assertNotIn("funnel", " ".join(argv).lower())
        self.assertNotIn("3037", " ".join(argv))

    def test_serve_uses_configured_dashboard_port_for_status_and_mutation(self):
        raw_status = {
            "BackendState": "Running",
            "TailscaleIPs": ["100.64.0.8", "fd7a:115c:a1e0::8"],
            "Self": {"Online": True, "DNSName": "actanara.example.ts.net."},
        }
        raw_serve = {
            "Web": {
                "actanara.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:4545"}}
                }
            }
        }
        status_runner = QueueRunner(
            [completed([], stdout=json.dumps(raw_status)), completed([], stdout=json.dumps(raw_serve))]
        )
        with patch.object(tailscale, "_configured_dashboard_port", return_value=4545):
            status = tailscale.tailscale_status(
                runner=status_runner,
                which=lambda _name: "/usr/bin/tailscale",
            )

        self.assertTrue(status["serve"]["exclusiveManaged"])
        self.assertEqual(status["serve"]["target"], "http://127.0.0.1:4545")

        action_runner = QueueRunner([completed([])])
        observed = self._connected_status(enabled=False)
        with patch.object(tailscale, "_configured_dashboard_port", return_value=4545):
            result = tailscale.set_dashboard_serve(
                True,
                {"confirmationText": tailscale.ENABLE_CONFIRMATION},
                observed_status=observed,
                runner=action_runner,
                which=lambda _name: "/usr/bin/tailscale",
            )

        self.assertEqual(action_runner.calls[0][0][-1], "http://127.0.0.1:4545")
        self.assertEqual(result["target"], "http://127.0.0.1:4545")

    def test_disable_only_removes_exclusive_actanara_mapping(self):
        runner = QueueRunner([completed([])])
        status = self._connected_status(enabled=True, exclusive=True)

        tailscale.set_dashboard_serve(
            False,
            {"confirmationText": tailscale.DISABLE_CONFIRMATION},
            observed_status=status,
            runner=runner,
            which=lambda _name: "/usr/bin/tailscale",
        )

        self.assertEqual(runner.calls[0][0], ["/usr/bin/tailscale", "serve", "--https=443", "off"])

    def test_existing_non_owned_serve_configuration_is_preserved(self):
        runner = QueueRunner([])
        status = self._connected_status(enabled=True, exclusive=False)

        with self.assertRaisesRegex(tailscale.TailscalePolicyError, "preserved"):
            tailscale.set_dashboard_serve(
                True,
                {"confirmationText": tailscale.ENABLE_CONFIRMATION},
                observed_status=status,
                runner=runner,
                which=lambda _name: "/usr/bin/tailscale",
            )

        self.assertEqual(runner.calls, [])

    def test_confirmation_and_connection_are_fail_closed(self):
        runner = QueueRunner([])
        with self.assertRaises(tailscale.TailscalePolicyError) as mismatch:
            tailscale.set_dashboard_serve(
                True,
                {"confirmationText": "yes"},
                observed_status=self._connected_status(enabled=False),
                runner=runner,
                which=lambda _name: "/usr/bin/tailscale",
            )
        self.assertEqual(mismatch.exception.code, "confirmation-mismatch")

        disconnected = self._connected_status(enabled=False)
        disconnected["connected"] = False
        with self.assertRaises(tailscale.TailscalePolicyError) as not_connected:
            tailscale.set_dashboard_serve(
                True,
                {"confirmationText": tailscale.ENABLE_CONFIRMATION},
                observed_status=disconnected,
                runner=runner,
                which=lambda _name: "/usr/bin/tailscale",
            )
        self.assertEqual(not_connected.exception.code, "tailscale-not-connected")
        self.assertEqual(runner.calls, [])

    def test_magic_dns_origin_must_be_explicitly_allowlisted(self):
        status = self._connected_status(enabled=False)
        status["dns"]["origin"] = "https://actanara.example.ts.net"

        blocked = tailscale.dashboard_access_status(status, ["http://127.0.0.1:3036"])
        ready = tailscale.dashboard_access_status(status, ["https://actanara.example.ts.net"])

        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["origin"], "https://actanara.example.ts.net")
        self.assertTrue(ready["ready"])

    def test_funnel_has_no_executable_path(self):
        source = (ROOT / "src" / "dashboard" / "app" / "services" / "tailscale.py").read_text(encoding="utf-8")
        router = (ROOT / "src" / "dashboard" / "app" / "routers" / "settings.py").read_text(encoding="utf-8")

        self.assertNotIn('"funnel",', source.lower())
        self.assertNotIn("/tailscale/funnel", router.lower())
        self.assertIn('"available": False', source)

    def test_dashboard_ui_has_bilingual_async_states_and_explicit_tailnet_controls(self):
        script = (ROOT / "src" / "dashboard" / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertGreaterEqual(script.count("tailscaleLoading:"), 2)
        self.assertGreaterEqual(script.count("tailscaleActionSuccess:"), 2)
        self.assertGreaterEqual(script.count("tailscaleStatusError:"), 2)
        self.assertIn("function loadTailscaleStatus", script)
        self.assertIn("function tailscaleServeAction", script)
        self.assertIn("/api/settings/tailscale/serve/enable", script)
        self.assertIn("/api/settings/tailscale/serve/disable", script)
        self.assertNotIn("/api/settings/tailscale/funnel", script.lower())
        self.assertIn("not independent user identity authentication", script)

    def test_all_tailscale_api_paths_remain_inside_dashboard_session_and_csrf_boundary(self):
        paths = [
            "/api/settings/tailscale/status",
            "/api/settings/tailscale/serve/enable",
            "/api/settings/tailscale/serve/disable",
        ]

        self.assertTrue(all(is_protected_path(path) for path in paths))

    def test_router_reports_required_origin_and_refuses_unconfigured_enable(self):
        from app.routers import settings as settings_router

        status = self._connected_status(enabled=False)
        status.update({"canEnableServe": True, "canDisableServe": False})
        status["dns"]["origin"] = "https://actanara.example.ts.net"
        with (
            patch.object(settings_router.tailscale, "tailscale_status", return_value=status),
            patch.object(
                settings_router,
                "dashboard_security_config",
                return_value={"allowedOrigins": ["http://127.0.0.1:3036"]},
            ),
            patch.object(settings_router.tailscale, "set_dashboard_serve") as apply_serve,
        ):
            payload = settings_router._tailscale_status_with_access()
            with self.assertRaises(settings_router.tailscale.TailscalePolicyError) as blocked:
                settings_router._tailscale_serve_action(
                    True,
                    {"confirmationText": tailscale.ENABLE_CONFIRMATION},
                )

        self.assertEqual(payload["requiredOrigin"], "https://actanara.example.ts.net")
        self.assertFalse(payload["configurationReady"])
        self.assertFalse(payload["canEnableServe"])
        self.assertEqual(blocked.exception.code, "dashboard-origin-not-allowlisted")
        apply_serve.assert_not_called()

    @staticmethod
    def _connected_status(*, enabled, exclusive=False):
        return {
            "connected": True,
            "dns": {"origin": None},
            "serve": {
                "supported": True,
                "enabled": enabled,
                "exclusiveManaged": exclusive,
            },
        }


if __name__ == "__main__":
    unittest.main()
