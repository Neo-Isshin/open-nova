import errno
import json
import os
import pty
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "install" / "bootstrap-linux.sh"
SETUP = ROOT / "install" / "setup.sh"
DEFAULT_SOURCE_URL = "https://github.com/Neo-Isshin/actanara.git"


class LinuxUpdateBootstrapTests(unittest.TestCase):
    def test_bilingual_public_docs_publish_the_managed_runtime_and_remote_source_contracts(self):
        documents = (
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "docs" / "local-operations-runbook.md",
            ROOT / "docs" / "local-operations-runbook.zh-CN.md",
        )
        for document in documents:
            with self.subTest(document=document.name):
                content = document.read_text(encoding="utf-8")
                self.assertIn("actanara update --dry-run", content)
                self.assertIn("actanara update --apply", content)
                self.assertIn("--source-url", content)
                self.assertIn("`origin`", content)
                self.assertTrue("status 2" in content or "状态码 2" in content)

    def _git(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )

    def _origin_fixture(self, root: Path) -> tuple[Path, str, str]:
        work = root / "origin-work"
        bare = root / "origin.git"
        (work / "install").mkdir(parents=True)
        (work / "install" / "install_linux.py").write_text(
            "# fixture Linux installer\n",
            encoding="utf-8",
        )
        self._git("init", "--quiet", cwd=work)
        self._git("config", "user.name", "Actanara Test", cwd=work)
        self._git("config", "user.email", "actanara-test@example.invalid", cwd=work)
        self._git("add", ".", cwd=work)
        self._git("commit", "--quiet", "-m", "first", cwd=work)
        first = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        (work / "selected-commit.txt").write_text("second\n", encoding="utf-8")
        self._git("add", ".", cwd=work)
        self._git("commit", "--quiet", "-m", "second", cwd=work)
        second = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        self._git("clone", "--quiet", "--bare", str(work), str(bare), cwd=root)
        self._git("config", "uploadpack.allowFilter", "true", cwd=bare)
        self._git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
        return bare, first, second

    def _fake_python(self, root: Path) -> tuple[Path, Path]:
        executable = root / "python3.13"
        log = root / "python.log"
        executable.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
            "{\n"
            "  printf '%s\\n' BEGIN\n"
            "  for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done\n"
            "  printf '%s\\n' END\n"
            "} >> \"$ACTANARA_TEST_PYTHON_LOG\"\n"
            "printf '%s\\n' '{\"schemaVersion\":1,\"status\":\"planned\"}'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable, log

    def _fake_gnu_stat(self, root: Path, *, identity: str) -> tuple[Path, Path]:
        executable = root / "gnu-stat"
        log = root / "stat.log"
        executable.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$ACTANARA_TEST_STAT_LOG\"\n"
            f"printf '%s\\n' '{identity}'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable, log

    def _environment(self, root: Path, python_log: Path, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "ACTANARA_HOME",
            "ACTANARA_INSTALL_CACHE_ROOT",
            "ACTANARA_LOCATION_FILE",
            "ACTANARA_INSTALL_OFFLINE",
            "ACTANARA_INSTALL_PUBLIC_ENTRY",
            "ACTANARA_INSTALL_REF",
            "ACTANARA_INSTALL_RUNTIME",
            "ACTANARA_INSTALL_SOURCE_ROOT",
            "ACTANARA_INSTALL_SOURCE_URL",
        ):
            env.pop(name, None)
        env.update(
            {
                "HOME": str(root / "Home"),
                "ACTANARA_INSTALL_TEST_MODE": "1",
                "ACTANARA_TEST_PYTHON_LOG": str(python_log),
            }
        )
        env.update(overrides)
        return env

    def _run(
        self,
        root: Path,
        *arguments: str,
        env: dict[str, str],
        streamed: bool = False,
        start_new_session: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = (
            ["sh", "-c", BOOTSTRAP.read_text(encoding="utf-8"), "actanara-bootstrap", *arguments]
            if streamed
            else ["sh", str(BOOTSTRAP), *arguments]
        )
        return subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            start_new_session=start_new_session,
        )

    def _python_invocations(self, log: Path) -> list[list[str]]:
        if not log.exists():
            return []
        invocations: list[list[str]] = []
        current: list[str] | None = None
        for line in log.read_text(encoding="utf-8").splitlines():
            if line == "BEGIN":
                current = []
            elif line == "END":
                if current is not None:
                    invocations.append(current)
                current = None
            elif current is not None:
                current.append(line)
        return invocations

    def _managed_runtime(self, root: Path, name: str = "Managed Runtime") -> Path:
        runtime = root / name
        release = runtime / "app" / "releases" / "installed"
        (runtime / "config").mkdir(parents=True)
        (runtime / "bin").mkdir(parents=True)
        release.mkdir(parents=True)
        (runtime / ".venv").mkdir()
        (runtime / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
        (runtime / "config" / "runtime.json").write_text(
            '{"runtime":"actanara"}\n',
            encoding="utf-8",
        )
        (release / ".actanara-runtime-source.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "product": "actanara",
                    "deploymentMode": "release-symlink",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (runtime / "app" / "source").symlink_to("releases/installed")
        shim = runtime / "bin" / "actanara"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        return runtime

    def test_explicit_remote_selection_never_adopts_the_bootstrap_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            checkout = root / "adjacent-checkout"
            (checkout / "install").mkdir(parents=True)
            shutil.copy2(BOOTSTRAP, checkout / "install" / "bootstrap-linux.sh")
            (checkout / "install" / "install_linux.py").write_text(
                "# adjacent installer must not be selected\n",
                encoding="utf-8",
            )
            cache = root / "Cache"
            env = self._environment(root, python_log)

            result = subprocess.run(
                [
                    "sh",
                    str(checkout / "install" / "bootstrap-linux.sh"),
                    "--source-url",
                    origin.as_uri(),
                    "--ref",
                    first,
                    "--cache-root",
                    str(cache),
                    "--python",
                    str(python),
                    "--",
                    "--dry-run",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(
            invocations[0][0],
            str((cache / "source" / "install" / "install_linux.py").resolve()),
        )
        self.assertNotIn(str(checkout / "install" / "install_linux.py"), invocations[0])

    def test_installer_arguments_cannot_override_the_verified_remote_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            alternate = root / "alternate-source"
            (alternate / "install").mkdir(parents=True)
            (alternate / "install" / "install_linux.py").write_text(
                "# unverified alternate installer\n",
                encoding="utf-8",
            )
            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(root / "Cache"),
                "--python",
                str(python),
                "--",
                "--source-root",
                str(alternate),
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must appear before --", result.stdout + result.stderr)
        self.assertEqual(invocations, [])

    def test_explicit_ref_alone_uses_canonical_official_cache_instead_of_adjacent_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            cache = root / "Cache"
            self._git("clone", "--quiet", origin.as_uri(), str(cache / "source"), cwd=root)
            self._git(
                "remote",
                "set-url",
                "origin",
                "https://github.com/Neo-Isshin/actanara/",
                cwd=cache / "source",
            )
            checkout = root / "adjacent-checkout"
            (checkout / "install").mkdir(parents=True)
            shutil.copy2(BOOTSTRAP, checkout / "install" / "bootstrap-linux.sh")
            (checkout / "install" / "install_linux.py").write_text(
                "# adjacent installer must not be selected\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "sh",
                    str(checkout / "install" / "bootstrap-linux.sh"),
                    "--offline",
                    "--ref",
                    first,
                    "--cache-root",
                    str(cache),
                    "--python",
                    str(python),
                    "--",
                    "--dry-run",
                ],
                cwd=root,
                env=self._environment(root, python_log),
                text=True,
                capture_output=True,
                check=False,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(
            invocations[0][0],
            str((cache / "source" / "install" / "install_linux.py").resolve()),
        )

    def test_fake_remote_ref_is_rejected_before_the_installer_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, _first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                "f" * 40,
                "--cache-root",
                str(root / "Cache"),
                "--python",
                str(python),
                "--",
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("ref is unavailable", result.stdout + result.stderr)
        self.assertEqual(invocations, [])

    def test_fake_remote_url_is_rejected_before_the_installer_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            _origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            result = self._run(
                root,
                "--source-url",
                (root / "missing-origin.git").as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(root / "Cache"),
                "--python",
                str(python),
                "--",
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("could not clone", result.stdout + result.stderr)
        self.assertEqual(invocations, [])

    def test_offline_cache_rejects_a_missing_exact_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, _first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            cache = root / "Cache"
            self._git("clone", "--quiet", origin.as_uri(), str(cache / "source"), cwd=root)
            result = self._run(
                root,
                "--offline",
                "--source-url",
                origin.as_uri(),
                "--ref",
                "f" * 40,
                "--cache-root",
                str(cache),
                "--python",
                str(python),
                "--",
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cached source does not match", result.stdout + result.stderr)
        self.assertEqual(invocations, [])

    def test_cached_source_origin_mismatch_is_rejected_online_and_offline(self):
        for offline in (False, True):
            with self.subTest(offline=offline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "Home").mkdir()
                origin, first, _second = self._origin_fixture(root)
                python, python_log = self._fake_python(root)
                cache = root / "Cache"
                self._git("clone", "--quiet", origin.as_uri(), str(cache / "source"), cwd=root)
                arguments = [
                    "--source-url",
                    "https://example.invalid/not-the-cache-origin.git",
                    "--ref",
                    first,
                    "--cache-root",
                    str(cache),
                    "--python",
                    str(python),
                ]
                if offline:
                    arguments.append("--offline")
                arguments.extend(["--", "--dry-run"])

                result = self._run(
                    root,
                    *arguments,
                    env=self._environment(root, python_log),
                    streamed=True,
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("cache", (result.stdout + result.stderr).lower())
                self.assertIn("origin", (result.stdout + result.stderr).lower())
                self.assertEqual(self._python_invocations(python_log), [])

    def test_valid_remote_ref_deploys_the_exact_requested_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            cache = root / "Cache"
            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(cache),
                "--python",
                str(python),
                "--",
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            deployed = self._git("rev-parse", "HEAD", cwd=cache / "source").stdout.strip()
            invocations = self._python_invocations(python_log)

        self.assertNotEqual(first, second)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(deployed, first)
        self.assertEqual(len(invocations), 1)

    def test_verified_cache_removes_untracked_payload_before_exact_ref_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            cache = root / "Cache"
            self._git("clone", "--quiet", origin.as_uri(), str(cache / "source"), cwd=root)
            injected = cache / "source" / "install" / "untracked-injected.py"
            injected.write_text("raise RuntimeError('must not deploy')\n", encoding="utf-8")

            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(cache),
                "--python",
                str(python),
                "--",
                "--dry-run",
                env=self._environment(root, python_log),
                streamed=True,
            )
            injected_exists = injected.exists()
            invocations = self._python_invocations(python_log)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(injected_exists)
        self.assertEqual(len(invocations), 1)

    def test_noninteractive_public_entry_prints_pinned_upgrade_commands_without_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root, "Managed ' Runtime")
            cache = root / "Cache"
            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(cache),
                "--python",
                str(python),
                "--",
                "--runtime",
                str(runtime),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("existing managed Runtime", output)
        self.assertIn("update --dry-run", output)
        self.assertIn("update --apply", output)
        self.assertIn("--source-url", output)
        self.assertIn(origin.as_uri(), output)
        self.assertIn(f"--ref '{first}'", output)
        self.assertIn(str(runtime), output)
        self.assertEqual(invocations, [])
        commands = [line for line in output.splitlines() if " update --" in line]
        self.assertEqual(len(commands), 2)
        for command in commands:
            syntax = subprocess.run(
                ["sh", "-n", "-c", command],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_setup_entrypoint_propagates_noninteractive_managed_runtime_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            result = subprocess.run(
                [
                    "sh",
                    str(SETUP),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    str(python),
                    "--",
                    "--runtime",
                    str(runtime),
                ],
                cwd=root,
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_SETUP_PLATFORM="Linux",
                ),
                text=True,
                capture_output=True,
                check=False,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("existing managed Runtime", output)
        self.assertIn("update --dry-run", output)
        self.assertIn("update --apply", output)
        self.assertIn("--source-root", output)
        self.assertEqual(invocations, [])

    def test_public_entry_finds_managed_runtime_from_active_location_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            (home / ".config" / "actanara").mkdir(parents=True)
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            (home / ".config" / "actanara" / "location.json").write_text(
                json.dumps({"actanaraHome": str(runtime)}) + "\n",
                encoding="utf-8",
            )
            result = self._run(
                root,
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(root / "Cache"),
                "--python",
                str(python),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("existing managed Runtime", output)
        self.assertIn(str(runtime.name), output)
        self.assertIn("update --apply", output)
        self.assertEqual(invocations, [])

    def test_public_entry_recovers_fresh_staging_before_managed_runtime_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            (runtime / "app" / "install-staging" / "interrupted").mkdir(
                parents=True
            )

            result = self._run(
                root,
                "--source-root",
                str(ROOT),
                "--python",
                str(python),
                "--",
                "--runtime",
                str(runtime),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("recovering an interrupted fresh install", output)
        self.assertEqual(len(invocations), 1)
        self.assertIn("--runtime", invocations[0])
        self.assertIn(str(runtime), invocations[0])
        self.assertNotIn("--upgrade", invocations[0])
        self.assertNotIn("--repair-existing", invocations[0])

    def test_public_entry_reports_an_exact_retry_for_a_pending_linux_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            pending = runtime / "app" / ".repair-configuration-pending"
            pending.write_text("20260722T120000-1234-deadbeef\n", encoding="ascii")
            pending.chmod(0o600)
            stat, stat_log = self._fake_gnu_stat(
                root,
                identity=f"1:600:{os.getuid()}",
            )
            result = self._run(
                root,
                "--source-root",
                str(ROOT),
                "--python",
                str(python),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_HOME=str(runtime),
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                    ACTANARA_INSTALL_STAT=str(stat),
                    ACTANARA_TEST_STAT_LOG=str(stat_log),
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)
            stat_arguments = stat_log.read_text(encoding="utf-8")

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("committed Runtime repair is pending", output)
        self.assertIn("--repair-existing --yes --dry-run", output)
        self.assertIn("--repair-existing --yes", output)
        self.assertNotIn("update --apply", output)
        self.assertEqual(invocations, [])
        self.assertIn("-c %h:%a:%u", stat_arguments)

    def test_public_entry_rejects_an_unsafe_pending_linux_repair_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            pending = runtime / "app" / ".repair-configuration-pending"
            pending.write_text("20260722T120000-1234-deadbeef\n", encoding="ascii")
            pending.chmod(0o600)
            stat, stat_log = self._fake_gnu_stat(
                root,
                identity=f"2:600:{os.getuid()}",
            )
            result = self._run(
                root,
                "--source-root",
                str(ROOT),
                "--python",
                str(python),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_HOME=str(runtime),
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                    ACTANARA_INSTALL_STAT=str(stat),
                    ACTANARA_TEST_STAT_LOG=str(stat_log),
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("repair marker is unsafe", output)
        self.assertEqual(invocations, [])

    def test_public_managed_runtime_dry_run_previews_upgrade_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            result = self._run(
                root,
                "--dry-run",
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(root / "Cache"),
                "--python",
                str(python),
                "--",
                "--runtime",
                str(runtime),
                env=self._environment(
                    root,
                    python_log,
                    ACTANARA_INSTALL_PUBLIC_ENTRY="1",
                ),
                streamed=True,
                start_new_session=True,
            )
            invocations = self._python_invocations(python_log)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("previewing the existing managed Runtime upgrade", output)
        self.assertEqual(len(invocations), 1)
        self.assertIn("--upgrade", invocations[0])
        self.assertIn("--dry-run", invocations[0])
        self.assertNotIn("--yes", invocations[0])

    def test_interactive_public_entry_previews_then_confirms_managed_runtime_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Home").mkdir()
            origin, first, _second = self._origin_fixture(root)
            python, python_log = self._fake_python(root)
            runtime = self._managed_runtime(root)
            cache = root / "Cache"
            arguments = [
                "sh",
                "-c",
                BOOTSTRAP.read_text(encoding="utf-8"),
                "actanara-bootstrap",
                "--source-url",
                origin.as_uri(),
                "--ref",
                first,
                "--cache-root",
                str(cache),
                "--python",
                str(python),
                "--",
                "--runtime",
                str(runtime),
            ]
            env = self._environment(
                root,
                python_log,
                ACTANARA_INSTALL_PUBLIC_ENTRY="1",
            )
            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                os.chdir(root)
                try:
                    os.execvpe(arguments[0], arguments, env)
                except OSError:
                    os._exit(127)
            os.write(master_fd, b"y\n")
            output = bytearray()
            try:
                while True:
                    block = os.read(master_fd, 4096)
                    if not block:
                        break
                    output.extend(block)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
            finally:
                os.close(master_fd)
            _waited, wait_status = os.waitpid(child_pid, 0)
            returncode = os.waitstatus_to_exitcode(wait_status)
            invocations = self._python_invocations(python_log)

        rendered = output.decode("utf-8", errors="replace")
        self.assertEqual(returncode, 0, rendered)
        self.assertIn("Upgrade this managed Runtime", rendered)
        self.assertEqual(len(invocations), 2)
        self.assertIn("--upgrade", invocations[0])
        self.assertIn("--dry-run", invocations[0])
        self.assertNotIn("--yes", invocations[0])
        self.assertIn("--upgrade", invocations[1])
        self.assertIn("--yes", invocations[1])
        self.assertEqual(
            invocations[0][invocations[0].index("--runtime") + 1],
            str(runtime.resolve()),
        )
        self.assertEqual(
            invocations[1][invocations[1].index("--runtime") + 1],
            str(runtime.resolve()),
        )


if __name__ == "__main__":
    unittest.main()
