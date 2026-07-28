import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "install" / "setup.sh"
COMMIT = "b" * 40


class PlatformBootstrapTests(unittest.TestCase):
    def _adapter_fixture(self, root: Path) -> Path:
        source = root / "source"
        install = source / "install"
        install.mkdir(parents=True)
        for name in ("bootstrap.sh", "bootstrap-linux.sh"):
            adapter = install / name
            adapter.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$0\" \"$@\" > \"$ACTANARA_TEST_ADAPTER_LOG\"\n"
                "if [ -n \"${ACTANARA_TEST_PUBLIC_ENTRY_LOG:-}\" ]; then\n"
                "  printf '%s\\n' \"${ACTANARA_INSTALL_PUBLIC_ENTRY:-}\" > \"$ACTANARA_TEST_PUBLIC_ENTRY_LOG\"\n"
                "fi\n",
                encoding="utf-8",
            )
            adapter.chmod(0o755)
        return source

    def _run(
        self, platform: str, *arguments: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = self._adapter_fixture(root)
        log = root / "adapter.log"
        public_entry_log = root / "public-entry.log"
        env = {
            **os.environ,
            "ACTANARA_INSTALL_TEST_MODE": "1",
            "ACTANARA_SETUP_PLATFORM": platform,
            "ACTANARA_INSTALL_SOURCE_ROOT": str(source),
            "ACTANARA_TEST_ADAPTER_LOG": str(log),
            "ACTANARA_TEST_PUBLIC_ENTRY_LOG": str(public_entry_log),
        }
        env.pop("ACTANARA_INSTALL_PUBLIC_ENTRY", None)
        if platform == "Darwin":
            # The fixture adapter is POSIX shell, so exercising Darwin
            # dispatch must not require zsh to be installed on a Linux host.
            env["ACTANARA_INSTALL_ZSH"] = "/bin/sh"
        result = subprocess.run(
            ["sh", str(SETUP), *arguments],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        public_entry = public_entry_log.read_text(encoding="utf-8").strip() if public_entry_log.exists() else ""
        return result, lines, public_entry

    def test_hosted_entrypoint_is_posix_and_truncation_safe(self):
        script = SETUP.read_text(encoding="utf-8")

        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("if true; then\nset -eu\numask 077", script)
        self.assertTrue(script.endswith("\nfi\n"))
        truncated = script[:-3]
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["sh", "-c", truncated, "actanara-truncated-setup"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)

    def test_macos_dispatches_to_existing_zsh_adapter(self):
        result, arguments, public_entry = self._run("Darwin", "--dry-run", "--", "--no-scheduler")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(arguments[0].endswith("install/bootstrap.sh"))
        self.assertEqual(arguments[1:], ["--dry-run", "--", "--no-scheduler"])
        self.assertEqual(public_entry, "")

    def test_linux_dispatches_to_posix_linux_adapter(self):
        result, arguments, public_entry = self._run("Linux", "--dry-run", "--", "--no-scheduler")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(arguments[0].endswith("install/bootstrap-linux.sh"))
        self.assertEqual(arguments[1:], ["--dry-run", "--", "--no-scheduler"])
        self.assertEqual(public_entry, "1")

    def test_unknown_platform_fails_without_running_an_adapter(self):
        result, arguments, public_entry = self._run("FreeBSD")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported platform: FreeBSD", result.stderr)
        self.assertEqual(arguments, [])
        self.assertEqual(public_entry, "")

    def test_remote_entrypoint_binds_downloaded_adapter_to_exact_main_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_git = root / "git"
            git_log = root / "git.log"
            adapter_log = root / "adapter.log"
            fake_git.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$ACTANARA_TEST_GIT_LOG\"\n"
                "case \"$*\" in\n"
                "  *ls-remote*) printf '%s\\trefs/heads/main\\n' \"$ACTANARA_TEST_COMMIT\" ;;\n"
                "  *rev-parse*) printf '%s\\n' \"$ACTANARA_TEST_COMMIT\" ;;\n"
                "  *' show '*) printf '%s\\n' '#!/bin/sh' 'printf '\"'\"'%s\\n'\"'\"' \"$@\" > \"$ACTANARA_TEST_ADAPTER_LOG\"' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            env = os.environ.copy()
            for name in (
                "ACTANARA_INSTALL_SOURCE_ROOT",
                "ACTANARA_INSTALL_SOURCE_URL",
                "ACTANARA_INSTALL_REF",
                "ACTANARA_INSTALL_CACHE_ROOT",
            ):
                env.pop(name, None)
            env.update(
                {
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_SETUP_PLATFORM": "Linux",
                    "ACTANARA_INSTALL_GIT": str(fake_git),
                    "ACTANARA_TEST_GIT_LOG": str(git_log),
                    "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                    "ACTANARA_TEST_COMMIT": COMMIT,
                }
            )
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    SETUP.read_text(encoding="utf-8"),
                    "actanara-hosted-setup",
                    "--dry-run",
                    "--",
                    "--no-scheduler",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            calls = git_log.read_text(encoding="utf-8")
            adapter_arguments = adapter_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ls-remote --exit-code", calls)
        self.assertIn(f"fetch --quiet --depth=1 --filter=blob:none origin {COMMIT}", calls)
        self.assertIn(f"show {COMMIT}:install/bootstrap-linux.sh", calls)
        self.assertEqual(
            adapter_arguments,
            [
                "--source-url",
                "https://github.com/Neo-Isshin/actanara.git",
                "--ref",
                COMMIT,
                "--dry-run",
                "--",
                "--no-scheduler",
            ],
        )

    def test_platform_override_is_ignored_outside_test_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._adapter_fixture(root)
            log = root / "adapter.log"
            env = {
                **os.environ,
                "ACTANARA_SETUP_PLATFORM": "Linux",
                "ACTANARA_INSTALL_SOURCE_ROOT": str(source),
                "ACTANARA_TEST_ADAPTER_LOG": str(log),
                "ACTANARA_INSTALL_ZSH": "/bin/sh",
            }
            result = subprocess.run(
                ["sh", str(SETUP), "--dry-run"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            arguments = log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = "bootstrap.sh" if platform.system() == "Darwin" else "bootstrap-linux.sh"
        self.assertTrue(arguments[0].endswith(f"install/{expected}"))


if __name__ == "__main__":
    unittest.main()
