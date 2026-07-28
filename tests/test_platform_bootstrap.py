import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "install" / "setup.sh"
COMMIT = "b" * 40
DEFAULT_SOURCE_URL = "https://github.com/Neo-Isshin/actanara.git"
SOURCE_REF_ANCHOR = 'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"'


def _release_entrypoint(commit: str) -> str:
    payload = SETUP.read_text(encoding="utf-8")
    if payload.count(SOURCE_REF_ANCHOR) != 1:
        raise AssertionError("setup entrypoint must expose one release-ref anchor")
    return payload.replace(
        SOURCE_REF_ANCHOR,
        f'SOURCE_REF="${{ACTANARA_INSTALL_REF:-{commit}}}"',
        1,
    )


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

    def _committed_cached_adapter(
        self,
        source: Path,
        *,
        adapter_log: Path,
        origin: str = DEFAULT_SOURCE_URL,
    ) -> tuple[str, Path]:
        install = source / "install"
        install.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@actanara.invalid"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Actanara Tests"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=source,
            check=True,
        )
        adapter = install / "bootstrap-linux.sh"
        adapter.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' committed \"$0\" \"$@\" > "
            "\"$ACTANARA_TEST_ADAPTER_LOG\"\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "install/bootstrap-linux.sh"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "fixture"],
            cwd=source,
            check=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        return commit, adapter

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
            "ACTANARA_TEST_ADAPTER_LOG": str(log),
            "ACTANARA_TEST_PUBLIC_ENTRY_LOG": str(public_entry_log),
        }
        env.pop("ACTANARA_INSTALL_SOURCE_ROOT", None)
        env.pop("ACTANARA_INSTALL_PUBLIC_ENTRY", None)
        if platform == "Darwin":
            # The fixture adapter is POSIX shell, so exercising Darwin
            # dispatch must not require zsh to be installed on a Linux host.
            env["ACTANARA_INSTALL_ZSH"] = "/bin/sh"
        result = subprocess.run(
            ["sh", str(SETUP), "--source-root", str(source), *arguments],
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
        self.assertEqual(arguments[1], "--source-root")
        self.assertTrue(arguments[2].endswith("/source"))
        self.assertEqual(arguments[3:], ["--dry-run", "--", "--no-scheduler"])
        self.assertEqual(public_entry, "")

    def test_linux_dispatches_to_posix_linux_adapter(self):
        result, arguments, public_entry = self._run("Linux", "--dry-run", "--", "--no-scheduler")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(arguments[0].endswith("install/bootstrap-linux.sh"))
        self.assertEqual(arguments[1], "--source-root")
        self.assertTrue(arguments[2].endswith("/source"))
        self.assertEqual(arguments[3:], ["--dry-run", "--", "--no-scheduler"])
        self.assertEqual(public_entry, "1")

    def test_unknown_platform_fails_without_running_an_adapter(self):
        result, arguments, public_entry = self._run("FreeBSD")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported platform: FreeBSD", result.stderr)
        self.assertEqual(arguments, [])
        self.assertEqual(public_entry, "")

    def test_release_entrypoint_binds_both_platform_adapters_to_its_pinned_commit(self):
        cases = (
            ("Darwin", "install/bootstrap.sh"),
            ("Linux", "install/bootstrap-linux.sh"),
        )
        for selected_platform, adapter_path in cases:
            with self.subTest(platform=selected_platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fake_git = root / "git"
                git_log = root / "git.log"
                adapter_log = root / "adapter.log"
                fake_git.write_text(
                    "#!/bin/sh\n"
                    "printf '%s\\n' \"$*\" >> \"$ACTANARA_TEST_GIT_LOG\"\n"
                    "case \"$*\" in\n"
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
                        "ACTANARA_SETUP_PLATFORM": selected_platform,
                        "ACTANARA_INSTALL_GIT": str(fake_git),
                        "ACTANARA_INSTALL_ZSH": "/bin/sh",
                        "ACTANARA_TEST_GIT_LOG": str(git_log),
                        "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                        "ACTANARA_TEST_COMMIT": COMMIT,
                    }
                )
                result = subprocess.run(
                    [
                        "sh",
                        "-c",
                        _release_entrypoint(COMMIT),
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
            self.assertNotIn("ls-remote", calls)
            self.assertNotIn("refs/heads/main", calls)
            self.assertIn(
                f"fetch --quiet --depth=1 --filter=blob:none origin {COMMIT}",
                calls,
            )
            self.assertIn(f"show {COMMIT}:{adapter_path}", calls)
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

    def test_unpinned_network_entrypoint_fails_without_following_a_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            implicit_source = self._adapter_fixture(root)
            adapter_log = root / "adapter.log"
            fake_git = root / "git"
            git_log = root / "git.log"
            fake_git.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$ACTANARA_TEST_GIT_LOG\"\n",
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
                    "ACTANARA_INSTALL_SOURCE_ROOT": str(implicit_source),
                    "ACTANARA_TEST_GIT_LOG": str(git_log),
                    "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                }
            )
            result = subprocess.run(
                ["sh", "-c", SETUP.read_text(encoding="utf-8"), "actanara-hosted-setup"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Release-pinned install.sh", result.stderr)
        self.assertFalse(git_log.exists())
        self.assertFalse(adapter_log.exists())

    def test_pinned_entrypoint_ignores_adjacent_and_environment_source_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adjacent_source = self._adapter_fixture(root)
            adjacent_setup = adjacent_source / "install" / "setup.sh"
            adjacent_setup.write_text(_release_entrypoint(COMMIT), encoding="utf-8")
            adjacent_adapter = adjacent_source / "install" / "bootstrap-linux.sh"
            adjacent_adapter.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' adjacent > \"$ACTANARA_TEST_ADAPTER_LOG\"\n"
                "exit 91\n",
                encoding="utf-8",
            )
            fake_git = root / "git"
            git_log = root / "git.log"
            adapter_log = root / "adapter.log"
            fake_git.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$ACTANARA_TEST_GIT_LOG\"\n"
                "case \"$*\" in\n"
                "  *rev-parse*) printf '%s\\n' \"$ACTANARA_TEST_COMMIT\" ;;\n"
                "  *' show '*) printf '%s\\n' '#!/bin/sh' 'printf '\"'\"'%s\\n'\"'\"' downloaded \"$@\" > \"$ACTANARA_TEST_ADAPTER_LOG\"' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            env = {
                **os.environ,
                "ACTANARA_INSTALL_TEST_MODE": "1",
                "ACTANARA_SETUP_PLATFORM": "Linux",
                "ACTANARA_INSTALL_GIT": str(fake_git),
                "ACTANARA_INSTALL_SOURCE_ROOT": str(adjacent_source),
                "ACTANARA_TEST_GIT_LOG": str(git_log),
                "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                "ACTANARA_TEST_COMMIT": COMMIT,
            }
            result = subprocess.run(
                ["sh", str(adjacent_setup), "--dry-run"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            calls = git_log.read_text(encoding="utf-8")
            adapter_lines = adapter_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"fetch --quiet --depth=1 --filter=blob:none origin {COMMIT}", calls)
        self.assertEqual(adapter_lines[0], "downloaded")
        self.assertNotIn("adjacent", adapter_lines)

    def test_offline_pin_extracts_committed_adapter_instead_of_cache_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_source = root / "cache" / "source"
            adapter_log = root / "adapter.log"
            commit, adapter = self._committed_cached_adapter(
                cache_source,
                adapter_log=adapter_log,
            )
            adapter.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' tampered > \"$ACTANARA_TEST_ADAPTER_LOG\"\n"
                "exit 91\n",
                encoding="utf-8",
            )
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
                    "ACTANARA_INSTALL_REF": commit,
                    "ACTANARA_INSTALL_CACHE_ROOT": str(root / "cache"),
                    "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                }
            )
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    SETUP.read_text(encoding="utf-8"),
                    "actanara-hosted-setup",
                    "--offline",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            adapter_lines = adapter_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(adapter_lines[0], "committed")
        self.assertNotIn(str(cache_source / "install" / "bootstrap-linux.sh"), adapter_lines)

    def test_offline_pin_uses_isolated_official_cache_after_primary_origin_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "cache"
            primary = cache_root / "source"
            primary.mkdir(parents=True)
            subprocess.run(["git", "init", "--quiet"], cwd=primary, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://example.invalid/legacy.git",
                ],
                cwd=primary,
                check=True,
            )
            isolated = cache_root / "official-release" / "source"
            adapter_log = root / "adapter.log"
            commit, _adapter = self._committed_cached_adapter(
                isolated,
                adapter_log=adapter_log,
            )
            env = {
                **os.environ,
                "ACTANARA_INSTALL_TEST_MODE": "1",
                "ACTANARA_SETUP_PLATFORM": "Linux",
                "ACTANARA_INSTALL_REF": commit,
                "ACTANARA_INSTALL_CACHE_ROOT": str(cache_root),
                "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
            }
            for name in (
                "ACTANARA_INSTALL_SOURCE_ROOT",
                "ACTANARA_INSTALL_SOURCE_URL",
            ):
                env.pop(name, None)
            result = subprocess.run(
                ["sh", str(SETUP), "--offline"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            adapter_lines = adapter_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(adapter_lines[0], "committed")

    def test_offline_pin_rejects_cache_that_resolves_to_a_different_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cache" / "source" / ".git").mkdir(parents=True)
            fake_git = root / "git"
            git_log = root / "git.log"
            adapter_log = root / "adapter.log"
            fake_git.write_text(
                "#!/bin/sh\n"
                "printf 'lazy=%s allow_set=%s allow=%s args=%s\\n' "
                "\"${GIT_NO_LAZY_FETCH:-}\" \"${GIT_ALLOW_PROTOCOL+x}\" "
                "\"${GIT_ALLOW_PROTOCOL:-}\" \"$*\" >> \"$ACTANARA_TEST_GIT_LOG\"\n"
                "case \"$*\" in\n"
                "  *' remote get-url origin'*) printf '%s\\n' "
                f"'{DEFAULT_SOURCE_URL}' ;;\n"
                "  *rev-parse*) printf '%s\\n' \"$ACTANARA_TEST_OTHER_COMMIT\" ;;\n"
                "  *cat-file*) printf '%s\\n' '#!/bin/sh' 'exit 99' ;;\n"
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
                    "ACTANARA_INSTALL_REF": COMMIT,
                    "ACTANARA_INSTALL_CACHE_ROOT": str(root / "cache"),
                    "ACTANARA_TEST_GIT_LOG": str(git_log),
                    "ACTANARA_TEST_ADAPTER_LOG": str(adapter_log),
                    "ACTANARA_TEST_OTHER_COMMIT": "c" * 40,
                }
            )
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    SETUP.read_text(encoding="utf-8"),
                    "actanara-hosted-setup",
                    "--offline",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            calls = git_log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 2)
        self.assertIn("selected commit in a matching installer cache", result.stderr)
        self.assertIn("lazy=1 allow_set=x allow=", calls)
        self.assertIn("-c protocol.allow=never", calls)
        self.assertNotIn("cat-file", calls)
        self.assertFalse(adapter_log.exists())

    def test_platform_override_is_ignored_outside_test_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._adapter_fixture(root)
            log = root / "adapter.log"
            env = {
                **os.environ,
                "ACTANARA_SETUP_PLATFORM": "Linux",
                "ACTANARA_TEST_ADAPTER_LOG": str(log),
                "ACTANARA_INSTALL_ZSH": "/bin/sh",
            }
            env.pop("ACTANARA_INSTALL_SOURCE_ROOT", None)
            result = subprocess.run(
                ["sh", str(SETUP), "--source-root", str(source), "--dry-run"],
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
