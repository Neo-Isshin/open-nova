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
BOOTSTRAP = ROOT / "install" / "bootstrap.sh"
DEFAULT_SOURCE_URL = "https://github.com/Neo-Isshin/actanara.git"
COMMIT = "b" * 40
OTHER_COMMIT = "c" * 40


class UpdateBootstrapSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "Home"
        self.bin_dir = self.root / "bin"
        self.cache = self.root / "Cache"
        self.git_log = self.root / "git.log"
        self.install_log = self.root / "install.log"
        self.location = self.root / "location.json"
        self.fake_git = self.bin_dir / "git"
        self.fake_installer = self.root / "install.sh"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self._write_fakes()

    def _write_fakes(self) -> None:
        self.fake_installer.write_text(
            """#!/usr/bin/env zsh
set -eu
print -r -- "$*" >> "$ACTANARA_TEST_INSTALL_LOG"
""",
            encoding="utf-8",
        )
        self.fake_installer.chmod(0o755)
        self.fake_git.write_text(
            """#!/usr/bin/env zsh
set -eu
print -r -- "$*" >> "$ACTANARA_TEST_GIT_LOG"
while [[ "$#" -gt 0 ]]; do
  case "${1:-}" in
    -c)
      shift 2
      ;;
    --git-dir=*|--work-tree=*)
      shift
      ;;
    *)
      break
      ;;
  esac
done
if [[ "${1:-}" == "clone" ]]; then
  target="${@: -1}"
  mkdir -p "$target/.git" "$target/install"
  cp "$ACTANARA_TEST_INSTALLER" "$target/install/install.sh"
  chmod +x "$target/install/install.sh"
  exit 0
fi
if [[ "${1:-}" == "remote" && "${2:-}" == "get-url" ]]; then
  print -r -- "${ACTANARA_TEST_SOURCE_URL}"
  exit 0
fi
if [[ "${1:-}" == "rev-parse" ]]; then
  if [[ "${ACTANARA_TEST_REV_PARSE_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  print -r -- "${ACTANARA_TEST_REV_PARSE_COMMIT:-${ACTANARA_TEST_COMMIT}}"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        self.fake_git.chmod(0o755)

    def _git(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shutil.which("git") or "git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )

    def _origin_fixture(self) -> tuple[Path, str, str]:
        work = self.root / "origin-work"
        origin = self.root / "origin.git"
        (work / "install").mkdir(parents=True)
        installer = work / "install" / "install.sh"
        installer.write_text(
            "#!/usr/bin/env zsh\n"
            'print -r -- "SAFE $*" >> "$ACTANARA_TEST_INSTALL_LOG"\n',
            encoding="utf-8",
        )
        installer.chmod(0o755)
        for name, value in (
            ("user.name", "Actanara Test"),
            ("user.email", "actanara-test@example.invalid"),
        ):
            if name == "user.name":
                self._git("init", "--quiet", cwd=work)
            self._git("config", name, value, cwd=work)
        self._git("add", ".", cwd=work)
        self._git("commit", "--quiet", "-m", "safe", cwd=work)
        safe = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        installer.write_text(
            "#!/usr/bin/env zsh\n"
            'print -r -- "EVIL $*" >> "$ACTANARA_TEST_INSTALL_LOG"\n',
            encoding="utf-8",
        )
        self._git("add", ".", cwd=work)
        self._git("commit", "--quiet", "-m", "evil", cwd=work)
        evil = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        self._git("clone", "--quiet", "--bare", str(work), str(origin), cwd=self.root)
        return origin, safe, evil

    def _environment(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "ACTANARA_HOME",
            "ACTANARA_INSTALL_RUNTIME",
            "ACTANARA_INSTALL_SOURCE_ROOT",
            "ACTANARA_INSTALL_SOURCE_URL",
            "ACTANARA_INSTALL_REF",
            "ACTANARA_INSTALL_CACHE_ROOT",
            "ACTANARA_INSTALL_GIT",
            "ACTANARA_INSTALL_PLUTIL",
        ):
            env.pop(name, None)
        env.update(
            {
                "HOME": str(self.home),
                "ACTANARA_LOCATION_FILE": str(self.location),
                "ACTANARA_TEST_GIT_LOG": str(self.git_log),
                "ACTANARA_TEST_INSTALL_LOG": str(self.install_log),
                "ACTANARA_TEST_INSTALLER": str(self.fake_installer),
                "ACTANARA_TEST_SOURCE_URL": DEFAULT_SOURCE_URL,
                "ACTANARA_INSTALL_VERBOSE": "1",
                "ACTANARA_TEST_COMMIT": COMMIT,
            }
        )
        env.update(overrides)
        return env

    def _run(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(BOOTSTRAP), *arguments],
            cwd=self.root,
            env=env or self._environment(),
            text=True,
            capture_output=True,
            check=False,
            start_new_session=True,
        )

    def _run_with_tty(
        self,
        *arguments: str,
        response: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["zsh", str(BOOTSTRAP), *arguments]
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            os.chdir(self.root)
            try:
                os.execvpe(command[0], command, env or self._environment())
            except OSError:
                # Never let an exec failure fall back into unittest execution
                # in the forked child; that would recursively run the suite.
                os._exit(127)
        os.write(master_fd, response.encode("utf-8"))
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
        _, wait_status = os.waitpid(child_pid, 0)
        return subprocess.CompletedProcess(
            command,
            os.waitstatus_to_exitcode(wait_status),
            output.decode("utf-8", errors="replace"),
            "",
        )

    def test_tty_exec_failure_returns_without_forking_another_test_runner(self):
        env = self._environment(PATH=str(self.bin_dir))

        result = self._run_with_tty("--help", response="\n", env=env)

        self.assertEqual(result.returncode, 127)

    def _remote_arguments(self, *, source_url: str = DEFAULT_SOURCE_URL, ref: str | None = None) -> list[str]:
        arguments = [
            "--source-url",
            source_url,
            "--cache-root",
            str(self.cache),
            "--git",
            str(self.fake_git),
        ]
        if ref is not None:
            arguments.extend(["--ref", ref])
        arguments.extend(["--", "--runtime", str(self.root / "runtime"), "--no-scheduler", "--no-dashboard-server"])
        return arguments

    def _output(self, result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def _write_marker(self, runtime: Path, relative: str = "config/settings.json") -> None:
        marker = runtime / relative
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("marker\n", encoding="utf-8")

    def _write_updateable_runtime(self, runtime: Path) -> None:
        config = runtime / "config"
        release = runtime / "app" / "releases" / "installed"
        config.mkdir(parents=True, exist_ok=True)
        release.mkdir(parents=True, exist_ok=True)
        (config / "settings.json").write_text(
            '{"features":{"rag":false},"rag":{"enabled":false}}\n',
            encoding="utf-8",
        )
        (config / "runtime.json").write_text(
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

    def test_github_defaults_are_canonical_and_do_not_reference_legacy_host(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn(f'DEFAULT_SOURCE_URL="{DEFAULT_SOURCE_URL}"', script)
        self.assertIn("Release-pinned installer or an exact version ID", script)
        self.assertNotIn("refs/heads/main", script)
        self.assertNotIn("refs/remotes/origin/main", script)
        self.assertNotIn("releases/latest", script)
        self.assertNotIn("git" + "ea", script.lower())

    def test_hosted_stream_is_one_compound_command_and_truncation_executes_nothing(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertTrue(script.startswith("#!/usr/bin/env zsh\n"))
        self.assertIn("if true; then\nset -euo pipefail", script)
        self.assertTrue(script.endswith("\nfi\n"))

        truncated = script[:-3]
        result = subprocess.run(
            ["zsh", "-c", truncated, "actanara-truncated-bootstrap"],
            cwd=self.root,
            env=self._environment(),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0, self._output(result))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.install_log.exists())

    def test_explicit_remote_commit_is_checked_out_detached(self) -> None:
        result = self._run(*self._remote_arguments(ref=COMMIT))
        output = self._output(result)
        git_log = self.git_log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("clone --filter=blob:none --sparse --no-checkout", git_log)
        self.assertIn(f"rev-parse --verify {COMMIT}^{{commit}}", git_log)
        self.assertIn(f"checkout --detach {COMMIT}", git_log)
        self.assertIn(f"reset --hard {COMMIT}", git_log)
        self.assertNotIn("origin/HEAD", git_log)
        self.assertNotIn("refs/heads/main", git_log)
        self.assertNotIn("refs/remotes/origin/main", git_log)
        self.assertNotIn("refs/tags/", git_log)
        self.assertTrue(self.install_log.is_file())
        installer_args = self.install_log.read_text(encoding="utf-8").split()
        self.assertNotIn("--upgrade", installer_args)
        self.assertNotIn("--yes", installer_args)

    def test_remote_without_release_pin_fails_before_cache_or_git(self) -> None:
        result = self._run(*self._remote_arguments())

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn(
            "network setup requires a Release-pinned installer or an exact version ID",
            self._output(result),
        )
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.install_log.exists())

    def test_explicit_remote_dry_run_uses_commit_without_creating_cache(self) -> None:
        arguments = self._remote_arguments(ref=COMMIT)
        arguments.insert(0, "--dry-run")

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertIn(
            f"Dry-run planned immutable source commit {COMMIT}",
            self._output(result),
        )
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.install_log.exists())

    def test_remote_dry_run_never_executes_a_warm_cache_worktree(self) -> None:
        source = self.cache / "source"
        (source / ".git").mkdir(parents=True)
        (source / "install").mkdir()
        tampered_marker = self.root / "tampered-ran"
        (source / "install" / "install.sh").write_text(
            "#!/usr/bin/env zsh\n"
            f"print -r -- tampered > {tampered_marker}\n"
            "exit 91\n",
            encoding="utf-8",
        )
        arguments = self._remote_arguments(ref=OTHER_COMMIT)
        arguments.insert(0, "--dry-run")

        result = self._run(
            *arguments,
            env=self._environment(ACTANARA_TEST_COMMIT=COMMIT),
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertIn(
            f"Dry-run planned immutable source commit {OTHER_COMMIT}",
            self._output(result),
        )
        self.assertFalse(tampered_marker.exists())
        self.assertFalse(self.install_log.exists())

    def test_hosted_stdin_bootstrap_never_adopts_the_current_checkout(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        env = self._environment(
            ACTANARA_INSTALL_CACHE_ROOT=str(self.cache),
            ACTANARA_INSTALL_GIT=str(self.fake_git),
        )

        result = subprocess.run(
            [
                "zsh",
                "-c",
                script,
                "actanara-hosted-bootstrap",
                "--ref",
                COMMIT,
                "--",
                "--runtime",
                str(self.root / "runtime"),
                "--no-scheduler",
                "--no-dashboard-server",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        git_log = self.git_log.read_text(encoding="utf-8")
        self.assertIn("clone --filter=blob:none --sparse --no-checkout", git_log)
        self.assertIn(DEFAULT_SOURCE_URL, git_log)
        self.assertIn(f"rev-parse --verify {COMMIT}^{{commit}}", git_log)
        self.assertIn(f"checkout --detach {COMMIT}", git_log)
        self.assertNotIn("refs/heads/main", git_log)
        self.assertNotIn("refs/remotes/origin/main", git_log)
        self.assertNotIn(str(ROOT / "install" / "install.sh"), self.install_log.read_text(encoding="utf-8"))

    def test_offline_without_ref_still_fails_before_cache_write(self) -> None:
        arguments = self._remote_arguments()
        arguments.insert(arguments.index("--"), "--offline")

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn("offline setup requires a local source or exact cached version", self._output(result))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.git_log.exists())

    def test_official_url_forms_without_ref_fail_before_source_writes(self) -> None:
        source_urls = (
            "https://github.com/Neo-Isshin/actanara",
            "https://github.com/Neo-Isshin/actanara/",
            DEFAULT_SOURCE_URL,
        )
        for index, source_url in enumerate(source_urls):
            with self.subTest(source_url=source_url):
                cache = self.root / f"official-release-cache-{index}"
                arguments = self._remote_arguments(source_url=source_url)
                arguments[arguments.index(str(self.cache))] = str(cache)
                self.git_log.unlink(missing_ok=True)
                self.install_log.unlink(missing_ok=True)

                result = self._run(*arguments)

                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn(
                    "network setup requires a Release-pinned installer or an exact version ID",
                    self._output(result),
                )
                self.assertFalse(cache.exists())
                self.assertFalse(self.git_log.exists())
                self.assertFalse(self.install_log.exists())

    def test_custom_remote_without_commit_and_symbolic_remote_ref_fail_closed(self) -> None:
        cases = (
            ("https://example.invalid/actanara.git", None, "custom source URL"),
            (DEFAULT_SOURCE_URL, "main", "full 40- or 64-character commit ID"),
            (DEFAULT_SOURCE_URL, "v1.2.3", "full 40- or 64-character commit ID"),
        )
        for index, (source_url, ref, expected) in enumerate(cases):
            with self.subTest(index=index):
                cache = self.root / f"Cache-{index}"
                arguments = self._remote_arguments(source_url=source_url, ref=ref)
                arguments[arguments.index(str(self.cache))] = str(cache)
                result = self._run(*arguments)
                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn(expected, self._output(result))
                self.assertFalse(cache.exists())

    def test_custom_remote_with_full_commit_is_detached_and_never_uses_head(self) -> None:
        source_url = "https://example.invalid/actanara.git"
        env = self._environment(ACTANARA_TEST_SOURCE_URL=source_url)
        result = self._run(*self._remote_arguments(source_url=source_url, ref=COMMIT), env=env)
        git_log = self.git_log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertIn(f"checkout --detach {COMMIT}", git_log)
        self.assertNotIn("refs/remotes/origin/main", git_log)
        self.assertNotIn("origin/HEAD", git_log)

    def test_official_https_cache_urls_with_or_without_dot_git_are_equivalent(self) -> None:
        source = self.cache / "source"
        (source / ".git").mkdir(parents=True)
        (source / "install").mkdir()
        shutil_installer = source / "install" / "install.sh"
        shutil_installer.write_bytes(self.fake_installer.read_bytes())
        shutil_installer.chmod(0o755)

        result = self._run(
            *self._remote_arguments(ref=COMMIT),
            env=self._environment(
                ACTANARA_TEST_SOURCE_URL="https://github.com/Neo-Isshin/actanara"
            ),
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        git_log = self.git_log.read_text(encoding="utf-8")
        self.assertIn(f"fetch --force origin {COMMIT}", git_log)
        self.assertIn("rev-parse --verify FETCH_HEAD^{commit}", git_log)
        self.assertIn(f"rev-parse --verify {COMMIT}^{{commit}}", git_log)
        self.assertIn(f"checkout --detach {COMMIT}", git_log)
        self.assertIn("clean -fdx", git_log)
        self.assertNotIn("fetch --all", git_log)
        self.assertNotIn("refs/heads/main", git_log)
        self.assertNotIn("refs/remotes/origin/main", git_log)
        self.assertTrue(self.install_log.is_file())

    def test_cache_git_isolation_ignores_replace_worktree_fsmonitor_and_ambient_git(self) -> None:
        origin, safe, evil = self._origin_fixture()
        source = self.cache / "source"
        self._git("clone", "--quiet", origin.as_uri(), str(source), cwd=self.root)
        external_worktree = self.root / "external-worktree"
        external_worktree.mkdir()
        sentinel = external_worktree / "operator-owned.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")
        fsmonitor_marker = self.root / "fsmonitor-ran"
        fsmonitor = self.root / "fsmonitor"
        fsmonitor.write_text(
            "#!/bin/sh\n"
            f"printf ran > {fsmonitor_marker}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fsmonitor.chmod(0o755)
        self._git("config", "core.worktree", str(external_worktree), cwd=source)
        self._git("config", "core.fsmonitor", str(fsmonitor), cwd=source)
        self._git("replace", safe, evil, cwd=source)
        ambient_git_dir = self.root / "ambient.git"
        self._git(
            "init",
            "--bare",
            "--quiet",
            str(ambient_git_dir),
            cwd=self.root,
        )
        arguments = self._remote_arguments(source_url=origin.as_uri(), ref=safe)
        arguments[arguments.index(str(self.fake_git))] = shutil.which("git") or "git"
        result = self._run(
            *arguments,
            env=self._environment(
                ACTANARA_TEST_SOURCE_URL=origin.as_uri(),
                GIT_DIR=str(ambient_git_dir),
                GIT_WORK_TREE=str(external_worktree),
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="protocol.ext.allow",
                GIT_CONFIG_VALUE_0="always",
            ),
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(fsmonitor_marker.exists())
        self.assertTrue(self.install_log.is_file())
        self.assertTrue(self.install_log.read_text(encoding="utf-8").startswith("SAFE "))

    def test_symlinked_cache_git_directory_is_rejected_before_git_or_installer(self) -> None:
        source = self.cache / "source"
        source.mkdir(parents=True)
        external_git = self.root / "external.git"
        external_git.mkdir()
        (source / ".git").symlink_to(external_git, target_is_directory=True)

        result = self._run(*self._remote_arguments(ref=COMMIT))

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn("cache layout is unsafe", self._output(result))
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.install_log.exists())

    def test_truly_different_cache_source_still_fails_without_installer_writes(self) -> None:
        source = self.cache / "source"
        (source / ".git").mkdir(parents=True)
        sentinel = source / "operator-owned.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")

        result = self._run(
            *self._remote_arguments(ref=COMMIT),
            env=self._environment(
                ACTANARA_TEST_SOURCE_URL="https://github.com/other/actanara.git",
                ACTANARA_INSTALL_VERBOSE="1",
            ),
        )

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn("download cache source does not match", self._output(result))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(self.install_log.exists())

    def test_implicit_default_cache_mismatch_uses_isolated_official_cache(self) -> None:
        legacy_source = self.home / ".cache" / "actanara" / "installer" / "source"
        (legacy_source / ".git").mkdir(parents=True)
        sentinel = legacy_source / "legacy-cache.txt"
        sentinel.write_text("keep legacy cache\n", encoding="utf-8")
        arguments = self._remote_arguments(ref=COMMIT)
        cache_index = arguments.index("--cache-root")
        del arguments[cache_index : cache_index + 2]

        result = self._run(
            *arguments,
            env=self._environment(
                ACTANARA_TEST_SOURCE_URL="https://legacy.invalid/actanara.git"
            ),
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep legacy cache\n")
        git_log = self.git_log.read_text(encoding="utf-8")
        isolated_source = (
            self.home
            / ".cache"
            / "actanara"
            / "installer"
            / "official-release"
            / "source"
        )
        self.assertIn(str(isolated_source), git_log)
        self.assertTrue(self.install_log.is_file())

    def test_full_sha256_commit_is_accepted(self) -> None:
        commit = "d" * 64
        source_url = "https://example.invalid/actanara.git"
        result = self._run(
            *self._remote_arguments(source_url=source_url, ref=commit),
            env=self._environment(
                ACTANARA_TEST_SOURCE_URL=source_url,
                ACTANARA_TEST_COMMIT=commit,
                ACTANARA_TEST_REV_PARSE_COMMIT=commit,
            ),
        )

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertIn(f"checkout --detach {commit}", self.git_log.read_text(encoding="utf-8"))

    def test_source_root_and_ref_are_rejected_without_touching_user_checkout(self) -> None:
        source = self.root / "user-source"
        installer = source / "install" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("user-owned\n", encoding="utf-8")
        before = installer.read_bytes()

        result = self._run(
            "--source-root",
            str(source),
            "--ref",
            COMMIT,
            "--git",
            str(self.fake_git),
            "--",
            "--runtime",
            str(self.root / "runtime"),
        )

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn("cannot be combined", self._output(result))
        self.assertEqual(installer.read_bytes(), before)
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.cache.exists())

    def test_oneliner_auto_updates_target_actanara_home_default_and_pointer_runtimes(self) -> None:
        for index, name in enumerate(("target", "actanara-home", "default", "pointer")):
            with self.subTest(name=name):
                case_home = self.root / f"Home-{index}"
                case_home.mkdir()
                case_location = self.root / f"location-{index}.json"
                case_runtime = self.root / f"existing-{name}-{index}"
                env_values = {
                    "HOME": str(case_home),
                    "ACTANARA_LOCATION_FILE": str(case_location),
                    "ACTANARA_INSTALL_PLUTIL": "/no/such/plutil",
                }
                if name == "actanara-home":
                    env_values["ACTANARA_HOME"] = str(case_runtime)
                elif name == "default":
                    case_runtime = case_home / ".actanara"
                elif name == "pointer":
                    case_location.write_text(
                        json.dumps({"actanaraHome": str(case_runtime)}),
                        encoding="utf-8",
                    )
                self._write_updateable_runtime(case_runtime)
                cache = self.root / f"runtime-guard-cache-{index}"
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                runtime_index = arguments.index(str(self.root / "runtime"))
                if name == "target":
                    arguments[runtime_index] = str(case_runtime)
                else:
                    del arguments[runtime_index - 1 : runtime_index + 1]
                self.install_log.unlink(missing_ok=True)
                result = self._run(*arguments, env=self._environment(**env_values))
                self.assertEqual(result.returncode, 0, self._output(result))
                installer_args = self.install_log.read_text(encoding="utf-8").split()
                self.assertEqual(installer_args.count("--upgrade"), 1)
                self.assertEqual(installer_args.count("--yes"), 1)
                self.assertNotIn("--force-rebuild", installer_args)
                runtime_arg = installer_args.index("--runtime")
                self.assertEqual(Path(installer_args[runtime_arg + 1]), case_runtime)
                self.assertTrue(cache.exists())

    def test_pending_repair_configuration_routes_modern_runtime_to_repair(self) -> None:
        runtime = self.root / "runtime"
        self._write_updateable_runtime(runtime)
        pending = runtime / "app" / ".repair-configuration-pending"
        pending.write_text("20260716T120000-12345-6789\n", encoding="utf-8")
        pending.chmod(0o600)
        arguments = self._remote_arguments(ref=COMMIT)
        arguments.append("--yes")

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 0, self._output(result))
        installer_args = self.install_log.read_text(encoding="utf-8").split()
        self.assertEqual(installer_args.count("--repair-existing"), 1)
        self.assertEqual(installer_args.count("--upgrade"), 0)
        self.assertEqual(installer_args.count("--yes"), 1)
        runtime_index = installer_args.index("--runtime")
        self.assertEqual(Path(installer_args[runtime_index + 1]), runtime)
        self.assertEqual(
            pending.read_text(encoding="utf-8"),
            "20260716T120000-12345-6789\n",
        )
        self.assertTrue(self.cache.exists())

    def test_unsafe_pending_repair_marker_fails_before_source_writes(self) -> None:
        unsafe_payloads = (
            "",
            "../escape\n",
            "fixture-tx\nsecond-line\n",
            "fixture-tx\n\n",
            f"{'a' * 129}\n",
            " leading-space\n",
        )
        for index, payload in enumerate(unsafe_payloads):
            with self.subTest(payload=payload):
                runtime = self.root / f"unsafe-pending-runtime-{index}"
                cache = self.root / f"unsafe-pending-cache-{index}"
                self._write_updateable_runtime(runtime)
                pending = runtime / "app" / ".repair-configuration-pending"
                pending.write_text(payload, encoding="utf-8")
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                arguments[arguments.index(str(self.root / "runtime"))] = str(runtime)
                arguments.append("--yes")
                self.git_log.unlink(missing_ok=True)
                self.install_log.unlink(missing_ok=True)

                result = self._run(*arguments)

                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertFalse(cache.exists())
                self.assertFalse(self.git_log.exists())
                self.assertFalse(self.install_log.exists())

        runtime = self.root / "linked-pending-runtime"
        cache = self.root / "linked-pending-cache"
        self._write_updateable_runtime(runtime)
        pending_target = self.root / "pending-target"
        pending_target.write_text("fixture-tx\n", encoding="utf-8")
        (runtime / "app" / ".repair-configuration-pending").symlink_to(pending_target)
        arguments = self._remote_arguments(ref=COMMIT)
        arguments[arguments.index(str(self.cache))] = str(cache)
        arguments[arguments.index(str(self.root / "runtime"))] = str(runtime)
        arguments.append("--yes")
        self.git_log.unlink(missing_ok=True)
        self.install_log.unlink(missing_ok=True)

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertFalse(cache.exists())
        self.assertFalse(self.git_log.exists())
        self.assertFalse(self.install_log.exists())

    def test_partial_runtime_without_tty_or_yes_fails_before_source_writes(self) -> None:
        runtime = self.root / "runtime"
        self._write_marker(runtime)
        for index, suffix in enumerate(((), ("--llm-model", "--yes"))):
            with self.subTest(suffix=suffix):
                cache = self.root / f"confirmation-cache-{index}"
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                arguments.extend(suffix)
                result = self._run(
                    *arguments,
                    env=self._environment(ACTANARA_INSTALL_VERBOSE="0"),
                )

                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn("请添加 --yes 后重试", self._output(result))
                self.assertFalse(cache.exists())
                self.assertFalse(self.install_log.exists())

    def test_partial_runtime_yes_routes_once_to_guarded_repair(self) -> None:
        runtime = self.root / "runtime"
        self._write_marker(runtime)
        settings_before = (runtime / "config" / "settings.json").read_bytes()
        arguments = self._remote_arguments(ref=COMMIT)
        arguments.extend(("--repair-existing", "--repair-existing", "--yes", "--yes"))

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 0, self._output(result))
        installer_args = self.install_log.read_text(encoding="utf-8").split()
        self.assertEqual(installer_args.count("--repair-existing"), 1)
        self.assertEqual(installer_args.count("--yes"), 1)
        self.assertNotIn("--upgrade", installer_args)
        runtime_index = installer_args.index("--runtime")
        self.assertEqual(Path(installer_args[runtime_index + 1]), runtime)
        self.assertEqual((runtime / "config" / "settings.json").read_bytes(), settings_before)
        self.assertTrue(self.cache.exists())
        git_log = self.git_log.read_text(encoding="utf-8")
        self.assertIn(f"rev-parse --verify {COMMIT}^{{commit}}", git_log)
        self.assertIn(f"checkout --detach {COMMIT}", git_log)
        self.assertNotIn("refs/heads/main", git_log)
        self.assertNotIn("refs/remotes/origin/main", git_log)

    def test_partial_runtime_tty_prompt_accepts_default_and_decline_is_noop(self) -> None:
        prompt = "当前 Actanara 不能直接升级，是否进行覆盖安装？现有数据与设置不会丢失，只会重建运行环境与依赖。 [Y/n]"
        for index, (response, accepted) in enumerate((("\n", True), ("n\n", False))):
            with self.subTest(response=response):
                runtime = self.root / f"runtime-{index}"
                cache = self.root / f"tty-cache-{index}"
                self._write_marker(runtime)
                marker = runtime / "config" / "settings.json"
                before = marker.read_bytes()
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                arguments[arguments.index(str(self.root / "runtime"))] = str(runtime)
                self.install_log.unlink(missing_ok=True)

                result = self._run_with_tty(*arguments, response=response)

                self.assertEqual(result.returncode, 0, self._output(result))
                self.assertIn(prompt, self._output(result))
                self.assertEqual(marker.read_bytes(), before)
                if accepted:
                    installer_args = self.install_log.read_text(encoding="utf-8").split()
                    self.assertEqual(installer_args.count("--repair-existing"), 1)
                    self.assertEqual(installer_args.count("--yes"), 1)
                    self.assertTrue(cache.exists())
                else:
                    self.assertIn("已取消 Actanara 恢复", self._output(result))
                    self.assertFalse(cache.exists())
                    self.assertFalse(self.install_log.exists())

    def test_foreign_runtime_manifest_still_fails_before_clone(self) -> None:
        runtime = self.root / "runtime"
        self._write_updateable_runtime(runtime)
        manifest = runtime / "app" / "source" / ".actanara-runtime-source.json"
        manifest.write_text(
            '{"product":"other","deploymentMode":"release-symlink"}\n',
            encoding="utf-8",
        )

        result = self._run(*self._remote_arguments(ref=COMMIT))

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.install_log.exists())

    def test_symlink_or_non_directory_runtime_root_fails_before_clone(self) -> None:
        actual = self.root / "actual-runtime"
        self._write_marker(actual)
        linked = self.root / "linked-runtime"
        linked.symlink_to(actual, target_is_directory=True)
        occupied = self.root / "occupied-runtime"
        occupied.write_text("operator-owned\n", encoding="utf-8")

        for index, runtime in enumerate((linked, occupied)):
            with self.subTest(runtime=runtime):
                cache = self.root / f"unsafe-runtime-cache-{index}"
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                arguments[arguments.index(str(self.root / "runtime"))] = str(runtime)
                arguments.append("--yes")
                result = self._run(*arguments)

                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn("symlink or non-directory", self._output(result))
                self.assertFalse(cache.exists())
                self.assertFalse(self.install_log.exists())

    def test_symlinked_location_pointer_fails_before_clone(self) -> None:
        actual = self.root / "actual-location.json"
        actual.write_text(
            json.dumps({"actanaraHome": str(self.root / "runtime")}),
            encoding="utf-8",
        )
        location = self.root / "linked-location.json"
        location.symlink_to(actual)
        arguments = self._remote_arguments(ref=COMMIT)
        runtime_index = arguments.index(str(self.root / "runtime"))
        del arguments[runtime_index - 1 : runtime_index + 1]

        result = self._run(
            *arguments,
            env=self._environment(ACTANARA_LOCATION_FILE=str(location)),
        )

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.install_log.exists())

    def test_malformed_or_unsafe_location_pointer_fails_before_clone(self) -> None:
        cases = ("not-json", json.dumps({"actanaraHome": "relative/runtime"}))
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                location = self.root / f"unsafe-location-{index}.json"
                location.write_text(payload, encoding="utf-8")
                cache = self.root / f"unsafe-pointer-cache-{index}"
                arguments = self._remote_arguments(ref=COMMIT)
                arguments[arguments.index(str(self.cache))] = str(cache)
                runtime_index = arguments.index(str(self.root / "runtime"))
                del arguments[runtime_index - 1 : runtime_index + 1]
                result = self._run(
                    *arguments,
                    env=self._environment(
                        ACTANARA_LOCATION_FILE=str(location),
                        ACTANARA_INSTALL_PLUTIL="/no/such/plutil",
                    ),
                )
                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn("Actanara", self._output(result))
                self.assertFalse(cache.exists())

    def test_existing_actanara_launch_agent_fails_before_clone(self) -> None:
        launch_agent = self.home / "Library" / "LaunchAgents" / "com.actanara.dashboard.plist"
        launch_agent.parent.mkdir(parents=True)
        launch_agent.write_text("plist\n", encoding="utf-8")

        result = self._run(*self._remote_arguments(ref=COMMIT))

        self.assertEqual(result.returncode, 2, self._output(result))
        self.assertIn("Actanara", self._output(result))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.git_log.exists())

    def test_explicit_upgrade_is_converted_to_legacy_repair(self) -> None:
        runtime = self.root / "runtime"
        self._write_marker(runtime)
        launch_agent = self.home / "Library" / "LaunchAgents" / "com.actanara.dashboard.plist"
        launch_agent.parent.mkdir(parents=True)
        launch_agent.write_text("plist\n", encoding="utf-8")
        arguments = self._remote_arguments(ref=COMMIT)
        arguments.extend(("--upgrade", "--yes"))

        result = self._run(*arguments)

        self.assertEqual(result.returncode, 0, self._output(result))
        self.assertTrue(self.install_log.is_file())
        self.assertEqual(
            self.install_log.read_text(encoding="utf-8").split().count("--upgrade"),
            0,
        )
        self.assertEqual(
            self.install_log.read_text(encoding="utf-8").split().count("--repair-existing"),
            1,
        )

    def test_apply_rejects_non_commit_or_nonmatching_object_without_head_fallback(self) -> None:
        cases = (
            {"ACTANARA_TEST_REV_PARSE_FAIL": "1"},
            {"ACTANARA_TEST_REV_PARSE_COMMIT": OTHER_COMMIT},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(index=index):
                self.git_log.unlink(missing_ok=True)
                self.install_log.unlink(missing_ok=True)
                result = self._run(
                    *self._remote_arguments(ref=COMMIT),
                    env=self._environment(**overrides),
                )
                git_log = self.git_log.read_text(encoding="utf-8")

                self.assertEqual(result.returncode, 2, self._output(result))
                self.assertIn("does not match required commit", self._output(result))
                self.assertNotIn("origin/HEAD", git_log)
                self.assertNotIn("checkout --detach", git_log)
                self.assertNotIn("reset --hard", git_log)
                self.assertFalse(self.install_log.exists())


if __name__ == "__main__":
    unittest.main()
