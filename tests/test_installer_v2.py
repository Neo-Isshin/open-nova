import hashlib
import http.server
import json
import os
import plistlib
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
INSTALLER = ROOT / "install" / "install.sh"
BOOTSTRAP = ROOT / "install" / "bootstrap.sh"
UPDATE_HELPER = ROOT / "install" / "update_transaction.py"
IMMUTABLE_TEST_COMMIT = "a" * 40

from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings
from install import dependency_contract as runtime_dependency_contract


class InstallerV2Tests(unittest.TestCase):
    def _fresh_bootstrap_env(self, home: Path) -> dict[str, str]:
        env = {
            **os.environ,
            "HOME": str(home),
            "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            "ACTANARA_INSTALL_PLATFORM": "Darwin",
            "ACTANARA_INSTALL_LANGUAGE": "zh-CN",
            "ACTANARA_INSTALL_VERBOSE": "0",
        }
        env.pop("ACTANARA_HOME", None)
        env.pop("ACTANARA_INSTALL_RUNTIME", None)
        return env

    def _start_health_server(self) -> int:
        source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
        ).strip()

        class HealthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health":
                    self.send_error(404)
                    return
                body = json.dumps(
                    {"sourceCommit": source_commit, "status": "ok"},
                    sort_keys=True,
                ).encode("utf-8") + b"\n"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return int(server.server_address[1])

    def _write_stateful_fake_launchctl(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env zsh
set -eu
command_name="${1:-}"
print -r -- "$*" >> "$ACTANARA_TEST_LAUNCHCTL_CALLS"
case "$command_name" in
  print)
    target="${2:-}"
    if [[ "$target" == gui/<-> ]]; then
      print -r -- "state = running"
      exit 0
    fi
    label="${target##*/}"
    state_file="$ACTANARA_TEST_LAUNCHCTL_STATE/$label"
    [[ -f "$state_file" ]] || exit 113
    print -r -- "state = $(<\"$state_file\")"
    ;;
  bootout)
    target="${2:-}"
    label="${target##*/}"
    rm -f "$ACTANARA_TEST_LAUNCHCTL_STATE/$label"
    ;;
  bootstrap)
    plist="${3:-}"
    label="${plist:t:r}"
    state="waiting"
    if [[ "$label" == *dashboard* || "$label" == *rag* ]]; then
      state="running"
    fi
    print -r -- "$state" > "$ACTANARA_TEST_LAUNCHCTL_STATE/$label"
    ;;
  kickstart)
    target="${@: -1}"
    label="${target##*/}"
    print -r -- "running" > "$ACTANARA_TEST_LAUNCHCTL_STATE/$label"
    ;;
  *)
    exit 64
    ;;
esac
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_runtime_plist(self, path: Path, *, runtime: Path) -> None:
        label = path.stem
        source = runtime / "app" / "releases" / "old-release"
        python = runtime / ".venv" / "bin" / "python"
        environment = {
            "ACTANARA_HOME": str(runtime),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if label.endswith("dashboard.watchdog"):
            payload = {
                "Label": label,
                "ProgramArguments": [
                    str(python),
                    str(source / "advanced" / "dashboard" / "dashboard_launch_agent.py"),
                    "check",
                    "--url",
                    "http://127.0.0.1:3036/health",
                    "--label",
                    label.removesuffix(".watchdog"),
                    "--restart",
                ],
                "EnvironmentVariables": environment,
            }
        elif label.endswith("rag-server"):
            environment["PYTHONPATH"] = f"{source}:{source / 'src'}"
            payload = {
                "Label": label,
                "ProgramArguments": [
                    str(python),
                    str(source / "advanced" / "dashboard" / "rag_server_launch_agent.py"),
                    "run",
                    "--project-root",
                    str(source),
                    "--actanara-home",
                    str(runtime),
                ],
                "EnvironmentVariables": environment,
            }
        elif label.endswith((".pipeline", ".dashboard-aggregation")):
            script = (
                "run_daily_pipeline.py"
                if label.endswith(".pipeline")
                else "run_dashboard_foundation_refresh.py"
            )
            environment["PYTHONPATH"] = f"{source}:{source / 'src'}:{source / 'src' / 'dashboard'}"
            payload = {
                "Label": label,
                "ProgramArguments": [
                    str(python),
                    str(source / "advanced" / "pipeline" / script),
                ],
                "WorkingDirectory": str(source),
                "EnvironmentVariables": environment,
            }
        else:
            environment.update(
                {
                    "ACTANARA_DASHBOARD_PROJECT_ROOT": str(source),
                    "ACTANARA_DASHBOARD_PYTHON": str(python),
                    "PYTHONPATH": f"{source}:{source / 'src'}:{source / 'src' / 'dashboard'}",
                }
            )
            payload = {
                "Label": label,
                "ProgramArguments": [
                    "/bin/zsh",
                    "-lc",
                    f"cd {source} && exec {python} -m uvicorn app.main:app "
                    f"--app-dir {source / 'src' / 'dashboard'} --host 127.0.0.1 --port 3036",
                ],
                "EnvironmentVariables": environment,
            }
        with path.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=False)

    def _write_prior_runtime_source(self, runtime: Path) -> Path:
        release = runtime / "app" / "releases" / "old-release"
        release.mkdir(parents=True, exist_ok=True)
        (release / "pyproject.toml").write_text(
            '[project]\nname="actanara-old-fixture"\nversion="0"\n',
            encoding="utf-8",
        )
        (release / ".actanara-runtime-source.json").write_text(
            '{"fixture":"old-source"}\n',
            encoding="utf-8",
        )
        shutil.copytree(
            ROOT / "src" / "data_foundation" / "migrations",
            release / "src" / "data_foundation" / "migrations",
        )
        (runtime / "app" / "source").symlink_to("releases/old-release")
        return release

    def _locked_distribution_probe_payload(self) -> str:
        lock_path = ROOT / "install" / "runtime-dependencies.lock.json"
        pyproject_path = ROOT / "pyproject.toml"
        dashboard = runtime_dependency_contract.load_contract_selection(
            lock_path,
            pyproject_path,
            ("dashboard",),
            python=sys.executable,
        )
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        supported = lock["environments"][dashboard.environment_id]["supportedProfiles"]
        selection = runtime_dependency_contract.load_contract_selection(
            lock_path,
            pyproject_path,
            supported,
            python=sys.executable,
        )
        return json.dumps(
            {
                "distributions": [
                    {"name": item["name"], "version": item["version"]}
                    for item in selection.distributions
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _write_trusted_runtime_venv(
        self,
        runtime: Path,
        *,
        profiles: tuple[str, ...] = ("dashboard",),
        generation: str = "old-venv",
    ) -> Path:
        selection = runtime_dependency_contract.load_contract_selection(
            ROOT / "install" / "runtime-dependencies.lock.json",
            ROOT / "pyproject.toml",
            profiles,
            python=sys.executable,
        )
        venv = runtime / "app" / "venvs" / generation
        python = venv / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        live_payload = json.dumps(
            {
                "distributions": [
                    {"name": item["name"], "version": item["version"]}
                    for item in selection.distributions
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        python.write_text(
            "#!/usr/bin/env zsh\n"
            "set -eu\n"
            f"if [[ \"${{1:-}}\" == \"-I\" && \"${{2:-}}\" == \"-B\" && \"${{3:-}}\" == \"-c\" && \"${{4:-}}\" == *\"importlib.metadata\"* ]]; then\n"
            f"  print -r -- {json.dumps(live_payload)}\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        marker = venv / runtime_dependency_contract.MARKER_NAME
        marker.write_text(
            json.dumps(selection.marker_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        marker.chmod(0o444)
        pointer = runtime / ".venv"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.symlink_to(Path("app") / "venvs" / generation)
        settings = runtime / "config" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        if not settings.exists():
            rag_enabled = "rag-server" in profiles
            embedding_mode = "local" if "rag-local" in profiles else "cloud"
            settings.write_text(
                json.dumps(
                    {
                        "features": {"rag": rag_enabled},
                        "rag": {
                            "enabled": rag_enabled,
                            **(
                                {"embedding": {"mode": embedding_mode}}
                                if rag_enabled
                                else {}
                            ),
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            settings.chmod(0o600)
        runtime_dependency_contract.verify_dependency_marker(venv, selection)
        runtime_dependency_contract.validate_live_distributions(python, selection)
        return venv

    def _write_fake_python(self, path: Path, log_path: Path) -> None:
        locked_distributions = self._locked_distribution_probe_payload()
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
if [[ "${{1:-}}" == */dependency_contract.py ]]; then
  case "${{2:-}}" in
    runtime-profiles|migrate-legacy-settings|cache-status|write-marker|verify-marker)
      exec {sys.executable!r} "$@"
      ;;
    materialize-cache)
      print -r -- '{{"schemaVersion":1,"status":"materialized"}}'
      exit 0
      ;;
    install)
      print -r -- '{{"schemaVersion":1,"status":"installed"}}'
      exit 0
      ;;
  esac
fi
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-B" && "${{3:-}}" == "-c" ]]; then
  if [[ "${{4:-}}" == *"importlib.metadata"* ]]; then
    print -r -- {json.dumps(locked_distributions)}
    exit 0
  fi
  exec {sys.executable!r} "$@"
elif [[ "${{1:-}}" == "-c" ]]; then
  exec {sys.executable!r} "$@"
elif [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'PYEOF'
#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-B" && "${{3:-}}" == "-c" ]]; then
  if [[ "${{4:-}}" == *"importlib.metadata"* ]]; then
    print -r -- {json.dumps(locked_distributions)}
    exit 0
  fi
  exec {sys.executable!r} "$@"
fi
exit 0
PYEOF
  chmod +x "$3/bin/python"
elif [[ "${{1:-}}" == "-" && -n "${{3:-}}" ]]; then
  if [[ "${{2:-}}" == /* && "${{2:h:t}}" == "releases" && "${{3:-}}" == /* && "${{3:t}}" == "source" ]]; then
    release_target="$2"
    link_path="$3"
    mkdir -p "${{link_path:h}}"
    rm -f "$link_path"
    ln -s "releases/${{release_target:t}}" "$link_path"
  else
    exec {sys.executable!r} "$@"
  fi
fi
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_fake_python_with_dependency_remediation(self, path: Path, log_path: Path, marker_path: Path) -> None:
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'PYEOF'
#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" && "${{3:-}}" == "install" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == "fastapi>=0.110,<1" ]]; then
      print -r -- "installed" > "{marker_path}"
    fi
  done
  exit 0
fi
if [[ "${{1:-}}" == "-" ]]; then
  missing="${{ACTANARA_INSTALL_MISSING_DEPENDENCIES_FILE:-}}"
  if [[ ! -f "{marker_path}" ]]; then
    mkdir -p "${{missing:h}}"
    print -r -- "fastapi>=0.110,<1" > "$missing"
    print -r -- "dependency gate error: Dashboard API dependency import failed: fastapi: fake missing" >&2
    exit 1
  fi
  print -r -- "dependency gate ok: fake remediation passed"
  exit 0
fi
exit 0
PYEOF
  chmod +x "$3/bin/python"
elif [[ "${{1:-}}" == "-" && -n "${{3:-}}" ]]; then
  if [[ "${{2:-}}" == /* && "${{2:h:t}}" == "releases" && "${{3:-}}" == /* && "${{3:t}}" == "source" ]]; then
    release_target="$2"
    link_path="$3"
    mkdir -p "${{link_path:h}}"
    rm -f "$link_path"
    ln -s "releases/${{release_target:t}}" "$link_path"
  else
    exec {sys.executable!r} "$@"
  fi
fi
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_fake_versioned_python(self, path: Path, version: str) -> None:
        major, minor, patch = version.split(".")
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
if [[ "${{1:-}}" == "-" ]]; then
  print -r -- "{version}"
  if (( {major} > 3 || ( {major} == 3 && {minor} >= 11 ) )); then
    exit 0
  fi
  exit 3
fi
if [[ "${{1:-}}" == "-c" && "${{2:-}}" == "import venv" ]]; then
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_fake_git(self, path: Path, log_path: Path) -> None:
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
while [[ "$#" -gt 0 ]]; do
  case "${{1:-}}" in
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
if [[ "${{1:-}}" == "clone" ]]; then
  target=""
  for arg in "$@"; do
    target="$arg"
  done
  mkdir -p "$target/install" "$target/.git" "$target/advanced/cli" "$target/advanced/dashboard" "$target/advanced/pipeline" "$target/src/dashboard/app/static" "$target/src/data_foundation/migrations"
  cp "{INSTALLER}" "$target/install/install.sh"
  cp "{ROOT / 'install' / 'dependency_contract.py'}" "$target/install/dependency_contract.py"
  cp "{ROOT / 'install' / 'runtime-dependencies.lock.json'}" "$target/install/runtime-dependencies.lock.json"
  cp "{ROOT / 'pyproject.toml'}" "$target/pyproject.toml"
  cp "{ROOT / 'MANIFEST.in'}" "$target/MANIFEST.in"
  cp "{ROOT / 'LICENSE'}" "$target/LICENSE"
  cp "{ROOT / 'config.py'}" "$target/config.py"
  cp -R "{ROOT / 'advanced'}"/. "$target/advanced/"
  cp -R "{ROOT / 'src'}"/. "$target/src/"
  chmod +x "$target/install/install.sh"
fi
if [[ "${{1:-}}" == "rev-parse" ]]; then
  print -r -- "{IMMUTABLE_TEST_COMMIT}"
fi
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_bootstrap_command_tripwire(self, path: Path, log_path: Path) -> None:
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
print -r -- "$0 $*" >> "{log_path}"
exit 97
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_bootstrap_installer_probe(self, source_root: Path, log_path: Path) -> None:
        installer = source_root / "install" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text(
            f"""#!/usr/bin/env zsh
set -eu
for argument in "$@"; do
  print -r -- "$argument" >> "{log_path}"
done
""",
            encoding="utf-8",
        )
        installer.chmod(0o755)

    def _legacy_repair_fixture(
        self,
        root: Path,
    ) -> tuple[unittest.TestCase, dict[str, object], dict[str, object]]:
        # Reuse the focused full-upgrade harness so repair exercises the real
        # transaction driver without network access or a real dependency install.
        from tests import test_installer_full_upgrade as full_upgrade_support

        harness = full_upgrade_support.InstallerFullUpgradeTests(
            "test_legacy_concrete_venv_settings_only_upgrade_preserves_protected_state"
        )
        fixture = harness._fixture(root, legacy_settings_only=True)
        runtime = Path(fixture["runtime"])
        source = runtime / "app" / "source"
        source.unlink()
        source.mkdir()
        source_sentinel = source / "legacy-source.txt"
        source_sentinel.write_text("legacy concrete source\n", encoding="utf-8")

        cli = runtime / "bin" / "actanara"
        cli.write_text(
            "#!/usr/bin/env zsh\nprint -r -- 'legacy actanara'\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        user_sentinel = runtime / "user-owned.txt"
        user_sentinel.write_text("preserve user state\n", encoding="utf-8")
        user_sentinel.chmod(0o640)

        fixture["protected_hashes"][user_sentinel] = hashlib.sha256(
            user_sentinel.read_bytes()
        ).hexdigest()
        fixture["protected_bytes"][user_sentinel] = user_sentinel.read_bytes()
        command = fixture["command"]
        command[command.index("--upgrade")] = "--repair-existing"
        command.append("--result-json")

        venv = runtime / ".venv"
        venv_python = venv / "bin" / "python"
        settings = runtime / "config" / "settings.json"
        database = runtime / "data" / "actanara_data.sqlite3"
        prior = {
            "source": source,
            "source_inode": source.stat().st_ino,
            "source_sentinel": source_sentinel,
            "source_sentinel_inode": source_sentinel.stat().st_ino,
            "source_sentinel_bytes": source_sentinel.read_bytes(),
            "venv": venv,
            "venv_inode": venv.stat().st_ino,
            "venv_python": venv_python,
            "venv_python_inode": venv_python.stat().st_ino,
            "venv_python_bytes": venv_python.read_bytes(),
            "cli": cli,
            "cli_inode": cli.stat().st_ino,
            "cli_bytes": cli.read_bytes(),
            "settings": settings,
            "settings_bytes": settings.read_bytes(),
            "database": database,
            "database_bytes": database.read_bytes(),
            "user_sentinel": user_sentinel,
            "user_sentinel_inode": user_sentinel.stat().st_ino,
            "user_sentinel_mode": user_sentinel.stat().st_mode & 0o777,
            "user_sentinel_bytes": user_sentinel.read_bytes(),
        }
        return harness, fixture, prior

    def _write_offline_cache_git(
        self,
        path: Path,
        log_path: Path,
        *,
        source_url: str,
        resolved_commit: str,
    ) -> None:
        path.write_text(
            f"""#!/usr/bin/env zsh
set -eu
print -r -- "GIT_NO_LAZY_FETCH=${{GIT_NO_LAZY_FETCH:-unset}} GIT_ALLOW_PROTOCOL_SET=${{+GIT_ALLOW_PROTOCOL}} GIT_ALLOW_PROTOCOL_VALUE=<${{GIT_ALLOW_PROTOCOL-}}> GIT_TERMINAL_PROMPT=${{GIT_TERMINAL_PROMPT:-unset}} :: $0 $*" >> "{log_path}"
if [[ " $* " == *" remote get-url origin "* ]]; then
  print -r -- "{source_url}"
  exit 0
fi
if [[ " $* " == *" rev-parse --verify "* ]]; then
  print -r -- "{resolved_commit}"
  exit 0
fi
if [[ " $* " == *" ls-tree -r -z --full-tree "* ]]; then
  printf '100755 blob 1111111111111111111111111111111111111111\tinstall/install.sh\\0'
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_fake_lsof(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env zsh
set -eu
for arg in "$@"; do
  if [[ "$arg" == *3036* ]]; then
    exit 0
  fi
done
exit 1
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_installer_script_has_valid_zsh_syntax(self):
        for script in (INSTALLER, BOOTSTRAP):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["zsh", "-n", str(script)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wizard_renders_product_header_with_version(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("render_installer_header", script)
        self.assertIn("installer_version", script)
        self.assertIn("Actanara ${version}", script)
        self.assertIn("installer_text setup_title", script)
        self.assertIn("────────────────────────────────────────", script)
        self.assertIn("TTY_BLUE", script)
        self.assertIn('version = ', script)

    def test_wizard_uses_english_until_language_is_selected(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("LANGUAGE_SELECTED=0", script)
        self.assertIn('text_language="en-US"', script)
        self.assertIn('if [[ "$LANGUAGE_SET" != "1" && "$LANGUAGE_SELECTED" != "1" ]]; then', script)
        self.assertIn("LANGUAGE_SELECTED=1", script)
        self.assertIn("Choose the Actanara language", script)
        self.assertIn("Welcome to Actanara. Dashboard, diaries, daily runs, and Nova-Task are included by default.", script)

    def test_installer_declares_input_data_sensitivity_notice(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("print_installer_data_notice", script)
        self.assertIn("agent/tool history", script)
        self.assertIn("may preserve sensitive information", script)
        self.assertIn("not secret values", script)

    def test_wizard_exposes_only_rag_as_product_choice(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("prompt_rag_choice", script)
        self.assertIn("Dashboard, diaries, daily runs, and Nova-Task are included by default", script)
        self.assertIn("installer_text rag_choice_prompt", script)
        self.assertIn("rag_not_now_label", script)
        self.assertIn("rag_local_label", script)
        self.assertIn("rag_cloud_label", script)
        self.assertNotIn("Enable nova-RAG memory/search subsystem?", script)
        self.assertIn("--enable-dev-test", script)
        self.assertNotIn("prompt_subsystems", script)
        self.assertNotIn("selected_subsystems", script)
        self.assertNotIn("Select subsystems", script)
        self.assertNotIn("Use Up/Down or j/k, Space to toggle optional items", script)
        self.assertNotIn('local ids=("dashboard" "dashboard-server" "scheduler"', script)
        self.assertNotIn('"Dashboard server service"', script)
        self.assertNotIn('"macOS scheduler"', script)
        self.assertNotIn('"LLM diary generation"', script)

    def test_wizard_does_not_prompt_for_runtime_or_generated_diary_paths(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertNotIn('prompt_line "Runtime/install target"', script)
        self.assertNotIn('prompt_line "Generated diary output path"', script)
        self.assertNotIn('prompt_line "Reports output path"', script)
        self.assertNotIn('prompt_line "Dashboard/report snapshots path"', script)
        self.assertNotIn('prompt_line "Archives/intermediate output path"', script)
        self.assertNotIn('prompt_line "Python executable for the runtime venv"', script)
        self.assertNotIn("Create a Desktop shortcut to the generated diary folder?", script)

    def test_wizard_llm_selection_uses_provider_catalog_before_model_and_key(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("llm_provider_catalog_rows", script)
        self.assertIn("llm_model_catalog_rows", script)
        self.assertIn("installer_text llm_provider_prompt", script)
        self.assertIn("installer_text llm_provider_help", script)
        self.assertIn("installer_text llm_model_prompt", script)
        self.assertIn("installer_text custom_input", script)
        self.assertIn("installer_text custom_llm_endpoint", script)
        self.assertIn("installer_text custom_llm_model", script)
        self.assertIn("LLM API key environment variable name", script)
        self.assertIn("installer_text yes_recommended", script)

    def test_wizard_detects_and_selects_external_tools_before_settings_overlay(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("installer_text detecting_tools", script)
        self.assertIn("installer_text detected_tools", script)
        self.assertIn("OpenClaw", script)
        self.assertIn("Claude Code", script)
        self.assertIn("Codex", script)
        self.assertIn("Gemini CLI", script)
        self.assertIn("Hermes", script)
        self.assertIn("manual", script)
        self.assertIn("installer_text manual_tool_name", script)
        self.assertIn("installer_text manual_tool_path", script)
        self.assertIn("installerSelectedTools", script)
        self.assertIn("run_external_rag_skill_registration_apply", script)
        self.assertIn("selected external tools", script)
        self.assertIn('if [[ -z "$row" ]]; then', script)
        self.assertIn('if [[ "${#fields[@]}" -lt 4 ]]; then', script)

    def test_wizard_skill_registration_is_rag_gated_after_rag_choices(self):
        script = INSTALLER.read_text(encoding="utf-8")

        rag_choice = script.index("prompt_rag_choice")
        skill_registration = script.index("ENABLE_SKILL_REGISTRATION=1")
        self.assertGreater(skill_registration, rag_choice)
        self.assertNotIn("Enable Dashboard-controlled nova-RAG memory skill registration for selected tools?", script)
        self.assertIn('if [[ "$ENABLE_RAG" == "1" ]]; then', script)
        self.assertIn('if [[ -n "$SELECTED_EXTERNAL_TOOLS" ]]; then', script)
        self.assertIn("ENABLE_SKILL_REGISTRATION=1", script)
        self.assertIn("installerV2SkillRegistration", script)
        self.assertIn("RAG辅助记忆系统", script)
        self.assertIn('"status": "installer-applied"', script)
        self.assertIn('"supportedNow": True', script)
        self.assertIn('"applyEndpoint": "POST /api/settings/external-tools/rag-skill-registration"', script)
        self.assertIn('"confirmationTextRequired": "INSTALL ACTANARA RAG SKILL"', script)
        self.assertIn("exact unmodified generated versions are backed up and upgraded", script)
        self.assertIn("customized files are preserved unless Dashboard overwrite is explicitly confirmed", script)
        self.assertIn("installer writes missing nova-RAG skills for selected external tools", script)
        self.assertIn("--register-rag-skills", script)
        self.assertIn("queue_rag_skill_registration", script)
        self.assertLess(script.index("apply_installer_settings_overlay\n"), script.index("run_external_rag_skill_registration_apply\n"))
        self.assertIn("enable_rag and enable_skill_registration and selected_external_tools", script)

    def test_wizard_dry_run_uses_summary_only_output(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("SUMMARY_ONLY=0", script)
        self.assertIn('if [[ "$DRY_RUN" == "1" ]]; then\n    SUMMARY_ONLY=1', script)
        self.assertIn('if [[ "$SUMMARY_ONLY" == "1" && "$DRY_RUN" == "1" ]]; then', script)
        self.assertIn('if [[ "$SUMMARY_ONLY" == "1" && -t 1 && -r /dev/tty ]]; then', script)
        self.assertIn("print_install_summary", script)
        self.assertIn("print_useful_commands", script)

    def test_wizard_presents_core_and_rag_dependency_gates(self):
        script = INSTALLER.read_text(encoding="utf-8")
        wizard = script.split("run_wizard() {", 1)[1].split("\n}\n\nwhile [[ $# -gt 0", 1)[0]

        self.assertIn("wizard_core_dependency_gate", script)
        self.assertIn("installer_text core_dependency_title", script)
        self.assertIn("installer_text readiness_dashboard", script)
        self.assertIn("installer_text readiness_components", script)
        self.assertIn("wizard_rag_dependency_gate", script)
        self.assertIn("installer_text rag_dependency_title", script)
        self.assertIn("installer_text readiness_memory_model", script)
        self.assertIn("installer_text readiness_memory_service", script)
        self.assertGreater(wizard.index("wizard_core_dependency_gate"), wizard.index('if [[ "$LANGUAGE_SET" != "1" ]]'))
        self.assertGreater(wizard.index("wizard_rag_dependency_gate"), wizard.index("prompt_rag_local_model"))

    def test_installation_guide_documents_current_workflow(self):
        runbook = (ROOT / "docs" / "new-user-onboarding-runbook.md").read_text(encoding="utf-8")

        for token in (
            "preflight",
            "post-install doctor",
            "actanara update",
            "pyproject.toml",
            "~/.actanara/bin/actanara",
            "--upgrade",
            "https://github.com/Neo-Isshin/actanara",
            "https://github.com/Neo-Isshin/actanara/releases/latest/download/install.sh",
        ):
            with self.subTest(token=token):
                self.assertIn(token, runbook)
        for private_process_term in (
            "Remaining Installer Milestones",
            "LaunchAgent Write Audit",
            "current phase",
            "publication remains",
        ):
            with self.subTest(private_process_term=private_process_term):
                self.assertNotIn(private_process_term, runbook)

    def test_offline_update_docs_require_explicit_cached_source_selection(self):
        documents = [
            (ROOT / "docs" / "local-operations-runbook.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "local-operations-runbook.zh-CN.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "new-user-onboarding-runbook.md").read_text(encoding="utf-8"),
        ]
        unsafe_example = re.compile(r"^actanara update --apply --offline(?:\s+#.*)?$", re.MULTILINE)
        for content in documents:
            with self.subTest():
                self.assertIsNone(unsafe_example.search(content))
                self.assertIn("--offline --ref <full-commit-sha>", content)
                self.assertIn("--offline --source-root /path/to/source", content)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        for content in (readme, readme_zh):
            with self.subTest(readme=True):
                self.assertIn("--source-root PATH", content)
                self.assertIn("--ref", content)

    def test_readmes_document_shell_path_controls(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        for content in (readme, readme_zh):
            with self.subTest():
                self.assertIn("~/.actanara/bin/actanara", content)
                self.assertIn("~/.local/bin/actanara", content)
                self.assertIn("--no-shell-path", content)
                self.assertIn("--shell-path-file /path/to/profile", content)

    def test_readmes_use_isolated_release_suite_command(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        for content in (readme, readme_zh):
            with self.subTest():
                self.assertIn("python tests/run_isolated_release_suite.py", content)
                self.assertNotIn("python -m pytest", content)

    def test_installer_runs_preflight_before_writes(self):
        script = INSTALLER.read_text(encoding="utf-8")

        preflight = script.index("run_installer_preflight")
        mkdir_runtime = script.index('run_cmd mkdir -p "${RUNTIME_HOME}"')
        self.assertLess(preflight, mkdir_runtime)
        self.assertIn("Installer preflight/doctor", script)
        self.assertIn("python-version", script)
        self.assertIn("writable-target", script)
        self.assertIn("launchagent-domain", script)
        self.assertIn("dashboard-port", script)
        self.assertIn("pip-network", script)

    def test_update_stops_managed_services_after_preflight_and_confirmation(self):
        script = INSTALLER.read_text(encoding="utf-8")

        entry = script.index('LOCATION_FILE="${ACTANARA_LOCATION_FILE:-$HOME/.config/actanara/location.json}"')
        port_select = script.index("select_dashboard_port", entry)
        preflight = script.index("run_installer_preflight", entry)
        confirmation = script.index('prompt_yes_no "$(installer_text proceed_upgrade)"', preflight)
        transaction = script.index("run_guarded_update_transaction", confirmation)
        self.assertLess(port_select, preflight)
        self.assertLess(preflight, transaction)
        self.assertLess(confirmation, transaction)
        driver = script.split("run_guarded_update_transaction() {", 1)[1].split("print_useful_commands()", 1)[0]
        self.assertLess(driver.index("stage_runtime_source"), driver.index("update_transaction_command stop"))
        self.assertLess(driver.index('record_update_candidate source'), driver.index("stage_update_candidate_venv"))
        self.assertLess(driver.index("verify-migration-compatibility"), driver.index("stage_update_candidate_venv"))
        self.assertLess(driver.index("stage_update_candidate_venv"), driver.index("update_transaction_command stop"))
        self.assertLess(driver.index("update_transaction_command stop"), driver.index("update_transaction_command promote"))
        self.assertLess(driver.index("update_transaction_command promote"), driver.index("update_transaction_command restore-services"))
        self.assertIn("update_transaction.py", script)
        helper = (ROOT / "install" / "update_transaction.py").read_text(encoding="utf-8")
        for label in (
            "com.actanara.dashboard",
            "com.actanara.dashboard.watchdog",
            "com.actanara.rag-server",
        ):
            self.assertIn(label, helper)
        self.assertIn('timer.get("label") or "actanara.daily"', helper)
        self.assertIn('("scheduler-pipeline", "pipeline")', helper)
        self.assertIn('("scheduler-aggregation", "dashboard-aggregation")', helper)
        self.assertNotIn("defaults = [", helper)

    def test_upgrade_preflight_failure_does_not_stop_managed_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            launch_agents = home / "Library" / "LaunchAgents"
            source = root / "incomplete-source"
            launch_agents.mkdir(parents=True)
            runtime.mkdir(parents=True)
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(runtime)
            source.mkdir()
            (source / "pyproject.toml").write_text(
                '[project]\nname = "audit-fixture"\nversion = "0"\n',
                encoding="utf-8",
            )
            calls = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            fake_launchctl.write_text(
                '#!/usr/bin/env zsh\nprint -r -- "$*" >> "$ACTANARA_TEST_LAUNCHCTL_CALLS"\n',
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            for name in (
                "com.actanara.dashboard.plist",
                "com.actanara.dashboard.watchdog.plist",
                "com.actanara.rag-server.plist",
            ):
                (launch_agents / name).write_text("placeholder", encoding="utf-8")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(source),
                    "--yes",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_INSTALL_LAUNCHCTL": str(fake_launchctl),
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_TEST_LAUNCHCTL_CALLS": str(calls),
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                    "ACTANARA_INSTALL_PYTHON": sys.executable,
                    "ACTANARA_INSTALL_VERBOSE": "1",
                },
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("Installer preflight/doctor", result.stdout + result.stderr)
            self.assertIn("缺少 Actanara 所需文件", result.stdout + result.stderr)
            operations = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            self.assertFalse(any(line.startswith("bootout ") for line in operations), operations)
            self.assertFalse(any(line.startswith("bootstrap ") for line in operations), operations)

    def test_upgrade_requires_managed_service_registration_success(self):
        script = INSTALLER.read_text(encoding="utf-8")
        dashboard = script.split("run_dashboard_service_launch_agent_apply() {", 1)[1].split(
            "run_rag_service_launch_agent_apply() {", 1
        )[0]
        rag = script.split("run_rag_service_launch_agent_apply() {", 1)[1].split(
            "run_external_rag_skill_registration_apply() {", 1
        )[0]
        scheduler = script.split('log "Registering managed Actanara scheduler LaunchAgents"', 1)[1].split(
            'elif [[ "$NO_SCHEDULER" == "1" ]]', 1
        )[0]

        for block in (dashboard, rag, scheduler):
            self.assertIn('if [[ "$UPGRADE" == "1" ]]', block)
            self.assertIn("run_json_cmd", block)
            self.assertIn("run_optional_json_cmd", block)

    def test_source_update_restores_mixed_actual_managed_service_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            launch_agents = home / "Library" / "LaunchAgents"
            state_dir = root / "launchctl-state"
            launch_agents.mkdir(parents=True)
            state_dir.mkdir()
            runtime.mkdir(parents=True)
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(runtime)
            health_port = self._start_health_server()
            (runtime / "config" / "settings.json").write_text(
                json.dumps(
                    {
                        "features": {"rag": False},
                        "rag": {"enabled": False},
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": health_port,
                            "healthPath": "/health",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            calls = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(fake_launchctl)
            for name in (
                "com.actanara.dashboard.plist",
                "com.actanara.dashboard.watchdog.plist",
                "com.actanara.rag-server.plist",
                "actanara.daily.pipeline.plist",
                "actanara.daily.dashboard-aggregation.plist",
            ):
                self._write_runtime_plist(launch_agents / name, runtime=runtime)
            initial_state = {
                "com.actanara.dashboard": "running",
                "com.actanara.dashboard.watchdog": "running",
                "actanara.daily.pipeline": "waiting",
            }
            for label, service_state in initial_state.items():
                (state_dir / label).write_text(service_state + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--source-only",
                    "--runtime",
                    str(runtime),
                    "--python",
                    sys.executable,
                    "--yes",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_INSTALL_LANGUAGE": "zh-CN",
                    "ACTANARA_INSTALL_LAUNCHCTL": str(fake_launchctl),
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_TEST_LAUNCHCTL_CALLS": str(calls),
                    "ACTANARA_TEST_LAUNCHCTL_STATE": str(state_dir),
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                    "ACTANARA_INSTALL_SOURCE_ROOT": str(ROOT),
                },
                text=True,
                capture_output=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Actanara 文件已更新。", result.stdout + result.stderr)
            final_state = {
                path.name: path.read_text(encoding="utf-8").strip()
                for path in state_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(final_state, initial_state)
            source_manifest = json.loads(
                (runtime / "app" / "source" / ".actanara-runtime-source.json").read_text(encoding="utf-8")
            )
            self.assertEqual(source_manifest["cleanScan"]["status"], "passed")
            self.assertEqual(source_manifest["cleanScan"]["findingCount"], 0)
            self.assertEqual(source_manifest["schemaVersion"], 2)
            self.assertIn(source_manifest["sourceLocator"]["kind"], {"login-home-relative", "unavailable"})
            self.assertNotIn("sourceRoot", source_manifest)
            self.assertNotIn("deployedSourceRoot", source_manifest)
            self.assertNotIn("releaseRoot", source_manifest)
            self.assertNotIn(str(Path.home()), json.dumps(source_manifest))
            self.assertGreater(source_manifest["payload"]["fileCount"], 0)
            self.assertEqual(source_manifest["payload"]["fileCount"], len(source_manifest["payload"]["files"]))
            self.assertTrue((runtime / "app" / "source").is_symlink())
            self.assertFalse(Path(os.readlink(runtime / "app" / "source")).is_absolute())
            self.assertEqual(os.readlink(runtime / ".venv"), "app/venvs/old-venv")
            operations = calls.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(line.startswith("bootout gui/") and line.endswith("dashboard.watchdog") for line in operations))
            self.assertTrue(any(line.startswith("bootstrap gui/") and line.endswith("actanara.daily.pipeline.plist") for line in operations))
            self.assertFalse(any(line.startswith("bootstrap gui/") and line.endswith("rag-server.plist") for line in operations))
            self.assertFalse(any(line.startswith("bootstrap gui/") and line.endswith("dashboard-aggregation.plist") for line in operations))

    def test_source_only_legacy_or_unsafe_venv_fails_before_service_stop(self):
        for pointer_kind in ("directory", "absolute", "escaping-relative"):
            with self.subTest(pointer_kind=pointer_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "Home"
                runtime = home / ".actanara"
                old_source = self._write_prior_runtime_source(runtime)
                venv_pointer = runtime / ".venv"
                expected_venv_raw = None
                if pointer_kind == "directory":
                    legacy_root = venv_pointer
                elif pointer_kind == "absolute":
                    legacy_root = runtime / "app" / "venvs" / "legacy-absolute"
                    legacy_root.mkdir(parents=True)
                    expected_venv_raw = str(legacy_root)
                    venv_pointer.symlink_to(expected_venv_raw)
                else:
                    legacy_root = root / "outside-venv"
                    legacy_root.mkdir()
                    expected_venv_raw = os.path.relpath(legacy_root, runtime)
                    venv_pointer.symlink_to(expected_venv_raw)
                legacy_python = legacy_root / "bin" / "python"
                legacy_python.parent.mkdir(parents=True, exist_ok=True)
                legacy_python.write_text("#!/usr/bin/env zsh\nexit 0\n", encoding="utf-8")
                legacy_python.chmod(0o755)
                settings = runtime / "config" / "settings.json"
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text(
                    '{"features":{"rag":false},"rag":{"enabled":false}}\n',
                    encoding="utf-8",
                )
                settings.chmod(0o600)
                protected = {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (
                        old_source / "pyproject.toml",
                        old_source / ".actanara-runtime-source.json",
                        legacy_python,
                        settings,
                    )
                }
                source_raw_target = os.readlink(runtime / "app" / "source")
                state_dir = root / "launchctl-state"
                state_dir.mkdir()
                calls = root / "launchctl-calls.log"
                fake_launchctl = root / "launchctl"
                self._write_stateful_fake_launchctl(fake_launchctl)

                result = subprocess.run(
                    [
                        "zsh",
                        str(INSTALLER),
                        "--source-only",
                        "--runtime",
                        str(runtime),
                        "--source-root",
                        str(ROOT),
                        "--language",
                        "en-US",
                        "--python",
                        sys.executable,
                        "--result-json",
                        "--yes",
                        "--no-scheduler",
                        "--no-dashboard-server",
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(home),
                        "ACTANARA_INSTALL_PLATFORM": "Darwin",
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_LAUNCHCTL": str(fake_launchctl),
                        "ACTANARA_TEST_LAUNCHCTL_CALLS": str(calls),
                        "ACTANARA_TEST_LAUNCHCTL_STATE": str(state_dir),
                        "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                        "ACTANARA_INSTALL_PYTHON": sys.executable,
                    },
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

                output = result.stdout + result.stderr
                envelope_line = next(
                    line
                    for line in output.splitlines()
                    if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
                )
                envelope = json.loads(envelope_line.split("=", 1)[1])
                self.assertEqual(result.returncode, 2, output)
                self.assertIn("Required software could not be prepared or verified.", output)
                self.assertNotIn("Runtime dependency profile", output)
                self.assertEqual(envelope["status"], "failed")
                self.assertEqual(envelope["updateMode"], "not-evaluated")
                self.assertEqual(envelope["reason"], "runtime-dependency-profile-untrusted")
                self.assertEqual(envelope["stage"], "dependency-profile")
                self.assertFalse(envelope["dependenciesInstalled"])
                self.assertFalse(envelope["reusesRuntimeVenv"])
                self.assertFalse(envelope["sourceUpdated"])
                self.assertFalse(envelope["servicesStopped"])
                self.assertEqual((runtime / "app" / "source").resolve(), old_source.resolve())
                self.assertEqual(os.readlink(runtime / "app" / "source"), source_raw_target)
                if expected_venv_raw is None:
                    self.assertTrue(venv_pointer.is_dir())
                    self.assertFalse(venv_pointer.is_symlink())
                else:
                    self.assertTrue(venv_pointer.is_symlink())
                    self.assertEqual(os.readlink(venv_pointer), expected_venv_raw)
                    self.assertEqual(venv_pointer.resolve(), legacy_root.resolve())
                self.assertEqual(
                    {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                    protected,
                )
                journals = list((runtime / "app" / "update-transactions").glob("*/journal.json"))
                self.assertEqual(journals, [])
                self.assertFalse((runtime / "app" / ".update-transaction.lock").exists())
                launchctl_calls = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
                self.assertFalse(
                    any(call.startswith(("bootout ", "bootstrap ", "kickstart ")) for call in launchctl_calls),
                    launchctl_calls,
                )

    def test_source_update_post_stop_failure_matrix_restores_prior_state(self):
        cases = (
            ("services-stopped", "return"),
            ("services-stopped", "conflict"),
            ("source-promoted", "return"),
            ("services-restored", "return"),
            ("candidate-verified", "return"),
            ("services-stopped", "term"),
            ("prior-captured", "kill"),
            ("migration-compatibility-verified", "kill"),
            ("source-staged", "kill"),
            ("payload-scanned", "kill"),
            ("services-stopped", "kill"),
            ("source-promoted", "kill"),
            ("services-restored", "kill"),
            ("candidate-verified", "kill"),
        )
        for phase, failure_kind in cases:
            with self.subTest(phase=phase, failure=failure_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "Home"
                runtime = home / ".actanara"
                app = runtime / "app"
                old_release = app / "releases" / "old-release"
                launch_agents = home / "Library" / "LaunchAgents"
                state_dir = root / "launchctl-state"
                for path in (
                    old_release,
                    runtime / "app" / "venvs" / "old-venv" / "bin",
                    runtime / "config",
                    runtime / "data",
                    launch_agents,
                    state_dir,
                ):
                    path.mkdir(parents=True, exist_ok=True)
                (old_release / "pyproject.toml").write_text('[project]\nname="old"\nversion="0"\n', encoding="utf-8")
                (old_release / ".actanara-runtime-source.json").write_text('{"old": true}\n', encoding="utf-8")
                shutil.copytree(
                    ROOT / "src" / "data_foundation" / "migrations",
                    old_release / "src" / "data_foundation" / "migrations",
                )
                (app / "source").symlink_to("releases/old-release")
                self._write_trusted_runtime_venv(runtime)
                settings = runtime / "config" / "settings.json"
                runtime_manifest = runtime / "config" / "runtime.json"
                database = runtime / "data" / "actanara_data.sqlite3"
                health_port = self._start_health_server()
                settings.write_text(
                    json.dumps(
                        {
                            "features": {"rag": False},
                            "rag": {"enabled": False},
                            "dashboard": {
                                "host": "127.0.0.1",
                                "port": health_port,
                                "healthPath": "/health",
                            }
                        },
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                runtime_manifest.write_text('{"sentinel":"runtime"}\n', encoding="utf-8")
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone(), ("wal",))
                    connection.execute(
                        "CREATE TABLE update_evidence (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE)"
                    )
                    connection.execute(
                        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES ('0001_initial', 'fixture')"
                    )
                    connection.execute("INSERT INTO update_evidence(value) VALUES ('before-update')")
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                protected = (settings, runtime_manifest, database)
                before_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}

                plist_names = (
                    "com.actanara.dashboard.plist",
                    "com.actanara.dashboard.watchdog.plist",
                    "com.actanara.rag-server.plist",
                    "actanara.daily.pipeline.plist",
                    "actanara.daily.dashboard-aggregation.plist",
                )
                for name in plist_names:
                    self._write_runtime_plist(launch_agents / name, runtime=runtime)
                initial_state = {
                    "com.actanara.dashboard": "running",
                    "com.actanara.dashboard.watchdog": "running",
                    "actanara.daily.pipeline": "waiting",
                }
                for label, service_state in initial_state.items():
                    (state_dir / label).write_text(service_state + "\n", encoding="utf-8")
                calls = root / "launchctl-calls.log"
                fake_launchctl = root / "launchctl"
                self._write_stateful_fake_launchctl(fake_launchctl)
                fault_env = {
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_FAIL_PHASE": phase,
                }
                if failure_kind == "term":
                    hook = root / "update-hook"
                    hook.write_text(
                        "#!/usr/bin/env zsh\n"
                        f'if [[ "$1" == "{phase}" ]]; then kill -TERM "$PPID"; fi\n',
                        encoding="utf-8",
                    )
                    hook.chmod(0o755)
                    fault_env = {
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_HOOK": str(hook),
                    }
                elif failure_kind == "kill":
                    hook = root / "update-hook"
                    hook_reached = root / "update-hook-reached"
                    hook.write_text(
                        "#!/usr/bin/env zsh\n"
                        f'if [[ "$1" == "{phase}" ]]; then print -r -- "$1" > "{hook_reached}"; kill -KILL "$PPID"; fi\n',
                        encoding="utf-8",
                    )
                    hook.chmod(0o755)
                    fault_env = {
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_HOOK": str(hook),
                    }
                elif failure_kind == "conflict":
                    hook = root / "update-hook"
                    hook.write_text(
                        "#!/usr/bin/env zsh\n"
                        f'if [[ "$1" == "{phase}" ]]; then '
                        f'print -r -- \'{{"concurrent":"operator-change"}}\' > "{settings}"; '
                        "exit 97; fi\n",
                        encoding="utf-8",
                    )
                    hook.chmod(0o755)
                    fault_env = {
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_HOOK": str(hook),
                    }

                command = [
                    "zsh",
                    str(INSTALLER),
                    "--source-only",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ]
                base_env = {
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_INSTALL_LAUNCHCTL": str(fake_launchctl),
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_TEST_LAUNCHCTL_CALLS": str(calls),
                    "ACTANARA_TEST_LAUNCHCTL_STATE": str(state_dir),
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                }
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env={**base_env, **fault_env},
                    text=True,
                    capture_output=True,
                    timeout=120,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("source-only sync complete", result.stdout + result.stderr)
                journals = list((app / "update-transactions").glob("*/journal.json"))
                self.assertEqual(len(journals), 1)
                if failure_kind == "conflict":
                    self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                    envelope_lines = [
                        line
                        for line in result.stdout.splitlines()
                        if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
                    ]
                    self.assertEqual(len(envelope_lines), 1, result.stdout + result.stderr)
                    envelope = json.loads(envelope_lines[0].split("=", 1)[1])
                    self.assertEqual(envelope["status"], "failed")
                    self.assertEqual(envelope["updateMode"], "reuse-existing-venv")
                    self.assertFalse(envelope["dependenciesInstalled"])
                    self.assertTrue(envelope["reusesRuntimeVenv"])
                    self.assertIsNone(envelope["sourceUpdated"])
                    self.assertFalse(envelope["cacheUsed"])
                    self.assertTrue(envelope["servicesStopped"])
                    self.assertIsNone(envelope["managedServiceDefinitionsNormalized"])
                    self.assertFalse(envelope["rollbackComplete"])
                    self.assertFalse(envelope["stateCertain"])
                    self.assertEqual(envelope["reason"], "update-rollback-incomplete")
                    self.assertEqual(envelope["stage"], "rollback-incomplete")
                    self.assertEqual(os.readlink(app / "source"), "releases/old-release")
                    self.assertEqual(
                        json.loads(settings.read_text(encoding="utf-8")),
                        {"concurrent": "operator-change"},
                    )
                    journal = json.loads(journals[0].read_text(encoding="utf-8"))
                    self.assertEqual(journal["status"], "rollback-failed")
                    self.assertIn("file-concurrent-change:settings", journal["rollbackErrors"])
                    self.assertIn(
                        "services:not-restored-after-pointer-or-control-state-conflict",
                        journal["rollbackErrors"],
                    )
                    self.assertTrue((app / ".update-transaction.lock").exists())
                    final_state = {
                        path.name: path.read_text(encoding="utf-8").strip()
                        for path in state_dir.iterdir()
                        if path.is_file()
                    }
                    self.assertEqual(final_state, {})
                    launchctl_calls = (
                        calls.read_text(encoding="utf-8").splitlines()
                        if calls.exists()
                        else []
                    )
                    self.assertFalse(
                        any(call.startswith(("bootstrap ", "kickstart ")) for call in launchctl_calls),
                        launchctl_calls,
                    )
                    continue
                if failure_kind == "kill":
                    self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                    self.assertEqual(hook_reached.read_text(encoding="utf-8").strip(), phase)
                    interrupted = json.loads(journals[0].read_text(encoding="utf-8"))
                    self.assertNotIn(interrupted["status"], {"committed", "rolled-back"})
                    self.assertTrue((app / ".update-transaction.lock").exists())

                    recovery = subprocess.run(
                        [sys.executable, str(UPDATE_HELPER), "recover", "--runtime", str(runtime)],
                        cwd=ROOT,
                        env=base_env,
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                    self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)
                    self.assertTrue((app / "source").is_symlink())
                    self.assertEqual(os.readlink(app / "source"), "releases/old-release")
                    self.assertEqual(
                        {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                        before_hashes,
                    )
                    recovered_state = {
                        path.name: path.read_text(encoding="utf-8").strip()
                        for path in state_dir.iterdir()
                        if path.is_file()
                    }
                    self.assertEqual(recovered_state, initial_state)
                    recovered = json.loads(journals[0].read_text(encoding="utf-8"))
                    self.assertEqual(recovered["status"], "rolled-back")
                    self.assertEqual(recovered["rollbackErrors"], [])
                    self.assertFalse((app / ".update-transaction.lock").exists())

                    retry = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=base_env,
                        text=True,
                        capture_output=True,
                        timeout=120,
                    )
                    self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                    self.assertTrue((app / "source" / ".actanara-runtime-source.json").is_file())
                    self.assertEqual(
                        {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                        before_hashes,
                    )
                    retry_state = {
                        path.name: path.read_text(encoding="utf-8").strip()
                        for path in state_dir.iterdir()
                        if path.is_file()
                    }
                    self.assertEqual(retry_state, initial_state)
                    statuses = sorted(
                        json.loads(path.read_text(encoding="utf-8"))["status"]
                        for path in (app / "update-transactions").glob("*/journal.json")
                    )
                    self.assertEqual(statuses, ["committed", "rolled-back"])
                    self.assertFalse((app / ".update-transaction.lock").exists())
                    continue

                self.assertTrue((app / "source").is_symlink())
                self.assertEqual(os.readlink(app / "source"), "releases/old-release")
                envelope_lines = [
                    line
                    for line in result.stdout.splitlines()
                    if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
                ]
                self.assertEqual(len(envelope_lines), 1, result.stdout + result.stderr)
                envelope = json.loads(envelope_lines[0].split("=", 1)[1])
                self.assertEqual(envelope["status"], "failed")
                self.assertEqual(envelope["updateMode"], "reuse-existing-venv")
                self.assertFalse(envelope["dependenciesInstalled"])
                self.assertTrue(envelope["reusesRuntimeVenv"])
                self.assertFalse(envelope["sourceUpdated"])
                self.assertFalse(envelope["cacheUsed"])
                self.assertTrue(envelope["servicesStopped"])
                self.assertEqual(envelope["reason"], "update-failed-rolled-back")
                self.assertEqual(envelope["stage"], "rollback-complete")
                self.assertTrue(envelope["rollbackComplete"])
                self.assertTrue(envelope["stateCertain"])
                self.assertEqual(
                    {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                    before_hashes,
                )
                final_state = {
                    path.name: path.read_text(encoding="utf-8").strip()
                    for path in state_dir.iterdir()
                    if path.is_file()
                }
                self.assertEqual(final_state, initial_state)
                journal = json.loads(journals[0].read_text(encoding="utf-8"))
                self.assertEqual(journal["status"], "rolled-back")
                self.assertEqual(journal["rollbackErrors"], [])
                events = [
                    json.loads(line)["event"]
                    for line in (journals[0].parent / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertIn(phase, events)
                self.assertFalse((app / ".update-transaction.lock").exists())

    def test_atomic_update_defers_external_rag_skill_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            codex_skills = root / "codex-skills"
            paths = initialize_home(runtime, legacy_diary_root=root / "Diary")
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(runtime, profiles=("dashboard", "rag-server"))
            write_settings(
                {
                    "features": {"rag": True},
                    "rag": {"enabled": True, "embedding": {"mode": "cloud"}},
                    "externalTools": {
                        "codex": {"skillsRoot": str(codex_skills)},
                        "installerSelectedTools": [{"key": "codex", "name": "Codex", "path": str(root / "codex")}],
                        "installerV2SkillRegistration": {
                            "status": "dashboard-controlled",
                            "supportedNow": True,
                            "selectedTools": [{"key": "codex", "name": "Codex", "path": str(root / "codex")}],
                        },
                    }
                },
                paths,
            )

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--source-root",
                    str(ROOT),
                    "--runtime",
                    str(runtime),
                    "--python",
                    sys.executable,
                    "--yes",
                    "--enable-rag",
                    "--rag-embedding-mode",
                    "cloud",
                    "--register-rag-skills",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Linux",
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                timeout=120,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            skill = codex_skills / "actanara-rag" / "SKILL.md"
            self.assertFalse(skill.exists(), result.stdout + result.stderr)
            saved = (runtime / "config" / "settings.json").read_text(encoding="utf-8")
            self.assertIn('"status": "dashboard-controlled"', saved)
            self.assertNotIn('"status": "installer-applied"', saved)

    def test_installer_copy_uses_semantic_lines_and_display_width_aware_wrapping(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("print_tty_copy()", script)
        self.assertIn("unicodedata.east_asian_width", script)
        self.assertIn('ACTANARA_INSTALL_COPY_WIDTH="$width"', script)
        self.assertIn('print_tty_copy "$prompt"', script)
        self.assertIn("Continue only if you understand", script)
        self.assertIn("请确认你理解：", script)

    def test_detected_external_tools_use_an_affirmative_selected_marker(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('marker="[✓]"', script)
        self.assertNotIn('marker="[x]"', script)
        self.assertNotIn('marker="✅"', script)

    def test_update_reuses_runtime_state_and_venv_without_deleting_user_data(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('VENV_DIR="${RUNTIME_HOME}/.venv"', script)
        self.assertIn('UPDATE_STAGED_VENV="$(update_transaction_command reserve-artifact', script)
        self.assertIn('--kind venv)', script)
        self.assertNotIn('UPDATE_STAGED_VENV="${candidate_root}/${UPDATE_TRANSACTION_ID}"', script)
        self.assertIn('run_update_candidate_cmd candidate-venv-create', script)
        self.assertIn('run_update_candidate_cmd candidate-locked-dependency-install', script)
        self.assertIn('run_update_candidate_cmd candidate-dependency-manifest-write', script)
        self.assertIn('run_update_candidate_cmd candidate-dependency-manifest-verify', script)
        self.assertIn('install_candidate_locked_dependencies "${STAGED_RELEASE_TARGET}" "${UPDATE_STAGED_VENV}"', script)
        self.assertIn('--venv-python "${venv_root}/bin/python"', script)
        self.assertNotIn('"${VENV_PY}" -m pip install', script)
        self.assertIn('run-candidate-command', script)
        self.assertIn('record_update_candidate venv "${UPDATE_STAGED_VENV}"', script)
        self.assertNotIn('rm -rf "${VENV_DIR}"', script)
        self.assertNotIn('rm -rf "${RUNTIME_HOME}"', script)
        self.assertNotIn('rm -rf "${RUNTIME_HOME}/data"', script)
        self.assertIn("legacy Python LaunchAgents may receive cache-suppression environment metadata", script)
        self.assertIn("local profile_command=(", script)
        self.assertIn("runtime-profiles", script)
        self.assertIn("--allow-untrusted-active-venv", script)
        self.assertIn('DEPENDENCY_PROFILE_SOURCE="runtime-settings+active-marker"', script)
        self.assertIn('DEPENDENCY_PROFILE_SOURCE="runtime-settings-recovery"', script)
        self.assertIn('--expected-settings-sha256 "${DEPENDENCY_PROFILE_SETTINGS_SHA256}"', script)
        self.assertIn('--expected-active-venv-target "${DEPENDENCY_PROFILE_ACTIVE_VENV_TARGET}"', script)
        self.assertIn('--expected-active-marker-status "${DEPENDENCY_PROFILE_MARKER_STATUS}"', script)
        self.assertIn("--settings-only-profile-evidence", script)
        self.assertIn("Upgrade dependency selection never requests a Settings rewrite", script)
        self.assertIn("UPDATE_ROLLBACK_COMPLETE=0", script)
        self.assertIn("UPDATE_STATE_CERTAIN=0", script)
        self.assertIn("UPDATE_SOURCE_UPDATED=-1", script)
        self.assertIn("UPDATE_PLISTS_NORMALIZED=-1", script)
        self.assertIn('local source_updated="null"', script)

    def test_install_summary_dashboard_url_uses_runtime_settings_when_available(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("effective_dashboard_url()", script)
        self.assertIn('${RUNTIME_HOME}/config/settings.json', script)
        self.assertIn('dashboard.get("port")', script)
        self.assertIn('dashboard_detail="$(effective_dashboard_url)"', script)
        self.assertIn('summary_line "$summary_status" "$(installer_text label_dashboard)" "$dashboard_detail"', script)

    def test_install_summary_llm_and_tools_use_runtime_settings_when_available(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("effective_llm_summary()", script)
        self.assertIn("effective_external_tools_summary()", script)
        self.assertIn('settings.get("llmProvider")', script)
        self.assertIn('external.get("installerSelectedTools")', script)
        self.assertIn('summary_line "${llm_status:-warn}" "$(installer_text label_ai)" "$llm_detail"', script)
        self.assertIn('connected_tools="$(effective_external_tools_summary)"', script)
        self.assertIn('summary_line "$summary_status" "$(installer_text label_tools)" "$connected_tools"', script)

    def test_install_summary_reads_runtime_settings_during_dry_run_when_available(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertNotIn('if [[ "$DRY_RUN" != "1" && -f "${RUNTIME_HOME}/config/settings.json" ]]; then', script)
        self.assertIn('if [[ -f "${RUNTIME_HOME}/config/settings.json" ]]; then', script)

    def test_installer_runs_post_install_doctor_after_install_actions(self):
        script = INSTALLER.read_text(encoding="utf-8")

        overlay = script.index("apply_installer_settings_overlay")
        post_install = script.index("run_post_install_doctor")
        self.assertGreater(post_install, overlay)
        self.assertIn("Post-install doctor", script)
        self.assertIn("onboarding runtime-status", script)
        self.assertIn("doctor --installer", script)
        self.assertIn("doctor --pipeline", script)
        self.assertIn("doctor --scheduler", script)
        self.assertIn("doctor --rag", script)
        self.assertIn("run_json_cmd", script)
        self.assertIn('run_json_cmd "Runtime status doctor"', script)
        self.assertIn('run_optional_json_cmd "Installer doctor"', script)
        self.assertIn('run_optional_json_cmd "Pipeline doctor"', script)
        self.assertIn('run_optional_json_cmd "Scheduler doctor"', script)
        self.assertIn('INSTALLER_LOG_FILE="${RUNTIME_HOME}/state/logs/installer-v2.log"', script)
        self.assertIn('$(installer_text details_log): ${INSTALLER_LOG_FILE}', script)

    def test_full_upgrade_runs_fatal_candidate_doctor_before_verify(self):
        script = INSTALLER.read_text(encoding="utf-8")
        driver = script.split("run_guarded_update_transaction() {", 1)[1].split(
            "print_useful_commands()", 1
        )[0]
        doctor = script.split("run_update_candidate_doctor() {", 1)[1].split(
            "clean_staged_candidate_build_artifacts()", 1
        )[0]

        self.assertLess(driver.index("restore-services"), driver.index("run_update_candidate_doctor"))
        self.assertLess(driver.index("run_update_candidate_doctor"), driver.index("verify --state"))
        self.assertIn("candidate-doctor-started", driver)
        self.assertIn("candidate-doctor-passed", driver)
        self.assertIn('if [[ "$SOURCE_ONLY" != "1" ]]; then', driver)
        self.assertIn('run_json_cmd "Candidate installer doctor"', doctor)
        self.assertIn("doctor --installer", doctor)
        self.assertNotIn("run_optional_json_cmd", doctor)
        self.assertNotIn("onboarding runtime-status", doctor)

    def test_installer_verifies_runtime_dependencies_after_locked_install(self):
        script = INSTALLER.read_text(encoding="utf-8")
        fresh_flow = script.split('prepare_fresh_dependency_cache "${SOURCE_ROOT}"', 1)[1]
        locked_install = fresh_flow.index("install_fresh_locked_dependencies")
        dependency_gate = fresh_flow.index("run_runtime_dependency_gate")
        promotion = fresh_flow.index("promote_fresh_runtime_artifacts")
        locked_helper = script.split("install_fresh_locked_dependencies() {", 1)[1].split(
            "run_update_candidate_cmd() {", 1
        )[0]
        update_cache_helper = script.split("materialize_update_dependency_cache() {", 1)[1].split(
            "install_candidate_locked_dependencies() {", 1
        )[0]
        fresh_cache_helper = script.split("prepare_fresh_dependency_cache() {", 1)[1].split(
            "install_fresh_locked_dependencies() {", 1
        )[0]

        self.assertLess(locked_install, dependency_gate)
        self.assertLess(dependency_gate, promotion)
        self.assertLess(locked_helper.index("dependency_contract.py\" install"), locked_helper.index("write-marker"))
        self.assertLess(locked_helper.index("write-marker"), locked_helper.index("verify-marker"))
        self.assertIn('dependency_contract.py" materialize-cache', update_cache_helper)
        self.assertIn('if [[ "$OFFLINE" == "1" ]]; then\n    command+=(--offline)', update_cache_helper)
        self.assertIn('dependency_contract.py" materialize-cache', fresh_cache_helper)
        self.assertIn(
            'if [[ "$OFFLINE" == "1" ]]; then\n    materialize_command+=(--offline)',
            fresh_cache_helper,
        )
        self.assertIn("Verifying runtime Dashboard dependency gate", script)
        self.assertIn('("fastapi", "fastapi>=0.110,<1", "Dashboard API")', script)
        self.assertIn('("uvicorn", "uvicorn>=0.29,<1", "Dashboard server")', script)
        self.assertIn('("yaml", "PyYAML>=6,<7", "Dashboard settings YAML")', script)
        self.assertIn('("croniter", "croniter>=2,<7", "Dashboard scheduler")', script)
        self.assertIn('("sentence_transformers", "sentence-transformers>=3,<6", "nova-RAG local embeddings")', script)
        self.assertIn('("torch", "torch>=2,<3", "nova-RAG local embeddings")', script)
        self.assertIn('source_root / "src" / "dashboard" / "app" / "static" / "index.html"', script)
        self.assertIn('importlib.import_module("app.main")', script)
        self.assertIn("installer_text step_failed", script)
        self.assertNotIn('run_cmd "${VENV_PY}" -m pip install "${missing_packages[@]}"', script)

    def test_enabled_cloud_rag_installs_base_server_dependency_extra(self):
        script = INSTALLER.read_text(encoding="utf-8")
        lifecycle = (ROOT / "src" / "agentic_rag" / "rag_server_lifecycle.py").read_text(encoding="utf-8")
        onboarding = (ROOT / "src" / "data_foundation" / "onboarding_plan.py").read_text(encoding="utf-8")
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        extras = metadata["project"]["optional-dependencies"]
        server_dependencies = [item.lower() for item in extras["rag-server"]]

        for module in ("numpy", "pydantic", "fastapi", "uvicorn"):
            self.assertTrue(
                any(item.startswith(module) for item in server_dependencies),
                (module, server_dependencies),
            )
        self.assertIn('if [[ "$ENABLE_RAG" == "1" ]]; then\n  INSTALL_EXTRAS+=("rag-server")', script)
        self.assertIn('if os.environ.get("ACTANARA_INSTALL_ENABLE_RAG") == "1":\n    rag_checks = [', script)
        self.assertIn('if os.environ.get("ACTANARA_INSTALL_RAG_EMBEDDING_MODE") == "local":', script)
        self.assertIn('else "rag-server"', lifecycle)
        self.assertIn("Repair the Actanara rag-server runtime dependencies", lifecycle)
        self.assertIn('"nova-rag-cloud": "rag-server"', onboarding)

    def test_standalone_wheel_declares_synchronized_runtime_config_module(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["tool"]["setuptools"]["py-modules"], ["config"])
        self.assertEqual(metadata["tool"]["setuptools"]["package-dir"]["advanced"], "advanced")
        self.assertIn(".", metadata["tool"]["setuptools"]["packages"]["find"]["where"])
        self.assertIn("advanced*", metadata["tool"]["setuptools"]["packages"]["find"]["include"])
        self.assertEqual((ROOT / "config.py").read_bytes(), (ROOT / "src" / "config.py").read_bytes())

    def test_installer_useful_commands_are_user_facing_cli_commands(self):
        script = INSTALLER.read_text(encoding="utf-8")
        useful = script.split("print_useful_commands() {", 1)[1].split("summary_line()", 1)[0]

        self.assertIn('actanara doctor', useful)
        self.assertIn('actanara update --dry-run', useful)
        self.assertIn('actanara dashboard restart', useful)
        self.assertNotIn("PYTHONPATH", useful)
        self.assertNotIn('"${VENV_PY}" -m data_foundation.cli', useful)

    def test_non_core_permission_failures_are_warning_only(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("run_optional_cmd", script)
        self.assertIn("run_optional_json_cmd", script)
        self.assertIn("not required for core runtime install", script)
        self.assertIn('run_optional_json_cmd "Scheduler LaunchAgent plist write"', script)
        self.assertIn('run_optional_json_cmd "Scheduler LaunchAgent registration"', script)
        self.assertIn('run_optional_json_cmd "SSE server LaunchAgent service registration"', script)
        self.assertIn("launcher.install_dashboard_launch_agent", script)
        self.assertIn("launcher.install_rag_launch_agent", script)
        self.assertIn("continuing without Desktop shortcut", script)

    def test_installer_creates_product_cli_shim(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("create_cli_shim", script)
        self.assertIn("ensure_cli_on_shell_path", script)
        self.assertIn('CLI_SHIM="${RUNTIME_HOME}/bin/actanara"', script)
        self.assertIn('USER_CLI_SHIM="${ACTANARA_INSTALL_USER_CLI_SHIM:-$HOME/.local/bin/actanara}"', script)
        self.assertIn("# >>> actanara installer PATH >>>", script)
        self.assertIn("unset WORKSPACE_DIR DIARY_OUTPUT_DIR TMP_WORKSPACE ACTANARA_DATA_DB_PATH ACTANARA_DATA_EXPORT_DIR TASK_DB_PATH", script)
        self.assertIn('export PYTHONDONTWRITEBYTECODE="1"', script)
        self.assertIn('local shim_tmp="${CLI_SHIM}.tmp.$$"', script)
        self.assertIn('mv -f "${shim_tmp}" "${CLI_SHIM}"', script)
        self.assertNotIn('export DIARY_OUTPUT_DIR="${DIARY_OUTPUT_DIR}"', script)
        self.assertNotIn('export TMP_WORKSPACE="${RUNTIME_HOME}/state/tmp"', script)
        self.assertNotIn('export ACTANARA_DATA_DB_PATH="${RUNTIME_HOME}/data/actanara_data.sqlite3"', script)
        self.assertNotIn('export ACTANARA_DATA_EXPORT_DIR="${SNAPSHOTS_OUTPUT_DIR}"', script)
        self.assertIn("export_runtime_environment", script)
        self.assertIn('exec "${VENV_PY}" -m data_foundation.cli "\\$@"', script)
        self.assertIn("ln -sf", script)
        self.assertIn("deploy_runtime_source", script)
        self.assertIn('DEPLOY_SOURCE_ROOT="${RUNTIME_HOME}/app/source"', script)
        self.assertIn('INSTALL_SPEC="${DEPLOY_SOURCE_ROOT}', script)
        self.assertIn(".actanara-runtime-source.json", script)

    def test_upgrade_recreates_product_cli_shim_after_transaction(self):
        script = INSTALLER.read_text(encoding="utf-8")
        start = script.index('if [[ "$UPGRADE" == "1" ]]; then\n  print_phase phase_installing')
        end = script.index('run_cmd mkdir -p "${RUNTIME_HOME}"', start)
        upgrade_flow = script[start:end]

        self.assertLess(upgrade_flow.index("run_guarded_update_transaction"), upgrade_flow.index("create_cli_shim"))
        self.assertLess(upgrade_flow.index("create_cli_shim"), upgrade_flow.index('if [[ "$SOURCE_ONLY" == "1" ]]'))

    def test_runtime_source_copy_excludes_local_state_and_machine_settings(self):
        script = INSTALLER.read_text(encoding="utf-8")

        for name in (
            '".env"',
            '".git"',
            '".playwright-cli"',
            '"__pycache__"',
            '"artifacts"',
            '"cache"',
            '"data"',
            '"location.json"',
            '"logs"',
            '"runtime.json"',
            '"settings.json"',
            '"snapshots"',
            '"state"',
        ):
            with self.subTest(name=name):
                self.assertIn(name, script)
        for suffix in ('".db"', '".log"', '".sqlite"', '".sqlite3"'):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, script)
        self.assertIn('name.startswith(".env.")', script)

    def test_runtime_source_copy_writes_provenance_manifest(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('"schemaVersion": 2', script)
        self.assertIn("def privacy_safe_source_locator(source_path):", script)
        self.assertIn('"sourceLocator": privacy_safe_source_locator(source)', script)
        self.assertNotIn('"sourceRoot": str(source.resolve())', script)
        self.assertNotIn('"deployedSourceRoot": str(deploy_target.expanduser().absolute())', script)
        self.assertNotIn('"releaseRoot": str(release_target.expanduser().absolute())', script)
        self.assertIn('pwd.getpwuid(os.getuid()).pw_dir', script)
        self.assertIn('"copiedAt": datetime.now().astimezone().isoformat()', script)
        self.assertIn('"pyprojectVersion": None', script)
        self.assertIn('git_value("rev-parse", "HEAD")', script)
        self.assertIn('git_value("rev-parse", "--abbrev-ref", "HEAD")', script)
        self.assertIn('git_optional("config", "--get", "remote.origin.url")', script)
        self.assertIn('git_optional("remote", "get-url", first_remote)', script)
        self.assertIn("def redact_git_remote(value):", script)
        self.assertIn('if parsed.scheme == "file"', script)
        self.assertIn('if not isinstance(remote, str):', script)
        self.assertIn('r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}"', script)
        self.assertIn("scp_remote = re.fullmatch", script)
        self.assertIn('"remote": redact_git_remote(remote)', script)
        self.assertIn('git_value("status", "--porcelain")', script)
        self.assertIn('"policy": contract["policy"]', script)
        self.assertIn('"preCommitWriterContract": contract["preCommitWriterContract"]', script)
        self.assertIn('"migrationSetSha256": migration_set_digest.hexdigest()', script)
        self.assertIn('(target / ".actanara-runtime-source.json").write_text', script)

    def test_runtime_source_deploy_uses_versioned_release_symlink(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('local releases_root="${app_root}/releases"', script)
        self.assertIn('"deploymentMode": "release-symlink"', script)
        self.assertIn('"releaseLocator": {"kind": "runtime-relative"', script)
        self.assertIn("os.symlink(raw_target, pointer)", script)
        self.assertIn('"releases"', script)
        self.assertIn('"app/venvs"', script)
        self.assertNotIn("os.replace(tmp, link)", script)
        self.assertNotIn("os.unlink(tmp)", script)
        self.assertIn("runtime source release switch failed validation; existing files were preserved", script)
        self.assertNotIn('rm -rf "${DEPLOY_SOURCE_ROOT}"', script)

    def test_fresh_runtime_pointer_helper_is_relative_store_confined_and_no_clobber(self):
        script = INSTALLER.read_text(encoding="utf-8")
        function_header = "promote_fresh_runtime_pointer() {"
        function_body = script.split(function_header, 1)[1].split(
            "\npromote_staged_runtime_source() {",
            1,
        )[0]
        harness = "\n".join(
            (
                "set -euo pipefail",
                'PYTHON_BIN="$ACTANARA_TEST_PYTHON"',
                function_header + function_body,
                'promote_fresh_runtime_pointer "$1" "$2" "$3"',
            )
        )

        def promote(candidate: Path, pointer: Path, store_relative: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "zsh",
                    "-c",
                    harness,
                    "fresh-runtime-pointer-test",
                    str(candidate),
                    str(pointer),
                    store_relative,
                ],
                cwd=ROOT,
                env={**os.environ, "ACTANARA_TEST_PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            source_candidate = runtime / "app" / "releases" / "generation"
            venv_candidate = runtime / "app" / "venvs" / "generation"
            source_candidate.mkdir(parents=True)
            venv_candidate.mkdir(parents=True)
            source_pointer = runtime / "app" / "source"
            venv_pointer = runtime / ".venv"

            source_result = promote(source_candidate, source_pointer, "releases")
            venv_result = promote(venv_candidate, venv_pointer, "app/venvs")

            self.assertEqual(source_result.returncode, 0, source_result.stdout + source_result.stderr)
            self.assertEqual(venv_result.returncode, 0, venv_result.stdout + venv_result.stderr)
            self.assertEqual(os.readlink(source_pointer), "releases/generation")
            self.assertEqual(os.readlink(venv_pointer), "app/venvs/generation")
            self.assertEqual(source_pointer.resolve(), source_candidate.resolve())
            self.assertEqual(venv_pointer.resolve(), venv_candidate.resolve())

        for case in (
            "file",
            "directory",
            "dangling",
            "symlinked-parent",
            "symlinked-store",
            "outside-store",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = root / "runtime"
                app = runtime / "app"
                if case == "symlinked-parent":
                    runtime.mkdir()
                    outside_app = root / "outside-app"
                    outside_app.mkdir()
                    app.symlink_to(outside_app, target_is_directory=True)
                else:
                    app.mkdir(parents=True)
                store = app / "releases"
                if case == "symlinked-store":
                    outside_store = root / "outside-releases"
                    outside_store.mkdir()
                    store.symlink_to(outside_store, target_is_directory=True)
                    candidate = store / "generation"
                    candidate.mkdir()
                else:
                    store.mkdir()
                    candidate = (
                        root / "outside" / "generation"
                        if case == "outside-store"
                        else store / "generation"
                    )
                    candidate.mkdir(parents=True)
                pointer = app / "source"
                if case == "file":
                    pointer.write_text("operator-owned\n", encoding="utf-8")
                elif case == "directory":
                    pointer.mkdir()
                    (pointer / "sentinel").write_text("operator-owned\n", encoding="utf-8")
                elif case == "dangling":
                    pointer.symlink_to("missing-generation")

                result = promote(candidate, pointer, "releases")

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(candidate.is_dir())
                if case == "file":
                    self.assertEqual(pointer.read_text(encoding="utf-8"), "operator-owned\n")
                elif case == "directory":
                    self.assertEqual(
                        (pointer / "sentinel").read_text(encoding="utf-8"),
                        "operator-owned\n",
                    )
                elif case == "dangling":
                    self.assertTrue(pointer.is_symlink())
                    self.assertEqual(os.readlink(pointer), "missing-generation")
                else:
                    self.assertFalse(pointer.exists() or pointer.is_symlink())

    def test_runtime_source_deploy_uses_allowlist_not_full_repo_copy(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("allowed_top_level = {", script)
        for name in ('"src"', '"advanced"', '"install"', '"pyproject.toml"', '"LICENSE"', '"MANIFEST.in"', '"config.py"'):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn("for name in sorted(allowed_top_level):", script)
        self.assertNotIn("shutil.copytree(source, target, ignore=ignore, symlinks=True)", script)
        copy_block = script.split("allowed_top_level = {", 1)[1].split("manifest = {", 1)[0]
        self.assertNotIn('"tests"', copy_block)
        self.assertNotIn('"docs"', copy_block)
        self.assertNotIn('"README.md"', copy_block)

    def test_runtime_source_artifacts_are_cleaned_after_doctor(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("cleanup_runtime_source_artifacts()", script)
        self.assertIn('rm -rf "${DEPLOY_SOURCE_ROOT}/build" "${DEPLOY_SOURCE_ROOT}/dist"', script)
        self.assertIn('find -H "${DEPLOY_SOURCE_ROOT}"', script)
        self.assertIn('-name "__pycache__"', script)
        self.assertIn('-name "*.egg-info"', script)
        entry = script.rsplit("run_post_install_doctor", 1)[1]
        self.assertIn("cleanup_runtime_source_artifacts", entry)
        self.assertLess(entry.index("cleanup_runtime_source_artifacts"), entry.index("print_completion"))

    def test_runtime_source_artifact_cleanup_follows_only_active_source_symlink(self):
        script = INSTALLER.read_text(encoding="utf-8")
        function_start = script.index("cleanup_runtime_source_artifacts() {")
        function_end = script.index("\n}\n\nrun_runtime_dependency_check()", function_start) + 2
        cleanup_function = script[function_start:function_end]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "runtime" / "app"
            release = app_root / "releases" / "20260712T000000-test"
            release.mkdir(parents=True)
            source = app_root / "source"
            source.symlink_to(Path("releases") / release.name)

            artifacts = (
                release / "build",
                release / "dist",
                release / "actanara.egg-info",
                release / "src" / "actanara.egg-info",
                release / "src" / "package" / "__pycache__",
            )
            for artifact in artifacts:
                artifact.mkdir(parents=True)
                (artifact / "generated.txt").write_text("generated\n", encoding="utf-8")

            ordinary_file = release / "src" / "package" / "module.py"
            ordinary_file.write_text("VALUE = 1\n", encoding="utf-8")
            outside = root / "outside"
            outside_egg_info = outside / "external.egg-info"
            outside_cache = outside / "__pycache__"
            outside_egg_info.mkdir(parents=True)
            outside_cache.mkdir()
            nested_symlink = release / "linked-tree"
            nested_symlink.symlink_to(outside, target_is_directory=True)

            harness = "\n".join(
                (
                    "set -euo pipefail",
                    "progress_start() { :; }",
                    "progress_ok() { :; }",
                    'DRY_RUN=0',
                    'DEPLOY_SOURCE_ROOT="$1"',
                    cleanup_function,
                    "cleanup_runtime_source_artifacts",
                )
            )
            result = subprocess.run(
                ["zsh", "-c", harness, "cleanup-runtime-source-test", str(source)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(source.is_symlink())
            self.assertEqual(os.readlink(source), str(Path("releases") / release.name))
            self.assertTrue(release.is_dir())
            self.assertTrue(ordinary_file.is_file())
            for artifact in artifacts:
                with self.subTest(artifact=artifact):
                    self.assertFalse(artifact.exists())
            self.assertTrue(nested_symlink.is_symlink())
            self.assertTrue(outside_egg_info.is_dir())
            self.assertTrue(outside_cache.is_dir())

    def test_installer_exports_runtime_environment_before_service_registration(self):
        script = INSTALLER.read_text(encoding="utf-8")

        export_call = script.index("export_runtime_environment")
        scheduler = script.index("Registering managed Actanara scheduler LaunchAgents")
        dashboard = script.index("Installing SSE server LaunchAgent service")
        self.assertLess(export_call, scheduler)
        self.assertLess(export_call, dashboard)
        self.assertIn("unset WORKSPACE_DIR DIARY_OUTPUT_DIR TMP_WORKSPACE ACTANARA_DATA_DB_PATH ACTANARA_DATA_EXPORT_DIR TASK_DB_PATH", script)

    def test_installer_has_guarded_upgrade_mode(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("--upgrade", script)
        self.assertIn("--source-only", script)
        self.assertIn("--sync-runtime-source", script)
        self.assertIn("UPGRADE=1", script)
        self.assertIn("--upgrade requires an existing runtime", script)
        self.assertIn("Proceed with upgrade now?", script)
        self.assertIn("Upgrade preserved Settings, runtime manifest, location pointer", script)
        self.assertIn("installer_text upgrade_complete", script)
        self.assertIn("installer_text update_no_changes", script)
        self.assertIn("installer_text source_update_complete", script)
        self.assertIn('if [[ "$UPGRADE" != "1" || "$LANGUAGE_SET" == "1" ]]; then', script)
        self.assertIn('first_install_or("ACTANARA_INSTALL_LLM_SET") and enable_llm', script)
        self.assertIn('first_install_or("ACTANARA_INSTALL_RAG_SET")', script)
        self.assertIn('first_install_or("ACTANARA_INSTALL_DIARY_OUTPUT_SET")', script)

    def test_guarded_candidate_environment_uses_absolute_env_binary(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertEqual(script.count("/usr/bin/env -i"), 2)
        self.assertEqual(script.count("/usr/bin/env\n      -i"), 2)
        candidate = script.split("run_update_candidate_cmd() {", 1)[1].split(
            "prepare_update_validation_runtime() {", 1
        )[0]
        doctor = script.split("run_update_candidate_doctor() {", 1)[1].split(
            "clean_staged_candidate_build_artifacts() {", 1
        )[0]
        for block in (candidate, doctor):
            self.assertIn("/usr/bin/env -i", block)
            self.assertIn('PIP_CONFIG_FILE=/dev/null', block)
            self.assertIn('PYTHONNOUSERSITE=1', block)
            self.assertIn('PYTHONDONTWRITEBYTECODE=1', block)
        self.assertNotIn("-- env -i", script)
        self.assertNotRegex(script, r"(?m)^\s+env -i(?:\s|\\)")

    def test_installer_llm_provider_keeps_provider_and_api_separate(self):
        script = INSTALLER.read_text(encoding="utf-8")
        llm_provider_case = script.split("--llm-provider)", 1)[1].split("--llm-endpoint)", 1)[0]

        self.assertIn('LLM_PROVIDER="$2"', llm_provider_case)
        self.assertIn('LLM_PROVIDER_MODE="preset"', llm_provider_case)
        self.assertNotIn('LLM_API="$2"', llm_provider_case)
        self.assertIn("ACTANARA_INSTALL_LLM_API", script)

    def test_source_only_dry_run_skips_settings_dependencies_and_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(runtime)
            source_target_before = os.readlink(runtime / "app" / "source")
            venv_target_before = os.readlink(runtime / ".venv")
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_LANGUAGE": "zh-CN",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--source-only",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            source_target_after = os.readlink(runtime / "app" / "source")
            venv_target_after = os.readlink(runtime / ".venv")

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Actanara 文件更新计划已生成。", output)
        self.assertIn("更新计划", output)
        self.assertNotIn("Staging source snapshot", output)
        self.assertNotIn("copy source snapshot", output)
        self.assertNotIn(".actanara-runtime-source.json", output)
        self.assertNotIn("source-only dry-run complete", output)
        self.assertNotIn("-m venv", output)
        self.assertNotIn("-m pip install", output)
        self.assertNotIn("onboarding runtime-apply", output)
        self.assertNotIn("apply runtime bootstrap", output.lower())
        self.assertNotIn("Creating Desktop diary shortcut", output)
        self.assertEqual(source_target_after, source_target_before)
        self.assertEqual(venv_target_after, venv_target_before)
        envelope_line = next(
            line
            for line in output.splitlines()
            if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
        )
        envelope = json.loads(envelope_line.split("=", 1)[1])
        self.assertEqual(envelope["updateMode"], "reuse-existing-venv")
        self.assertTrue(envelope["reusesRuntimeVenv"])
        self.assertFalse(envelope["plannedDependenciesInstall"])
        self.assertFalse(envelope["dependenciesInstalled"])
        self.assertFalse(envelope["sourceUpdated"])
        self.assertFalse(envelope["servicesStopped"])

    def test_upgrade_inherits_rag_dependency_profile_without_rewriting_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(
                runtime,
                profiles=("dashboard", "rag-server", "rag-local"),
            )
            settings = runtime / "config" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(
                json.dumps(
                    {
                        "features": {"rag": True},
                        "rag": {
                            "enabled": True,
                            "embedding": {
                                "mode": "local",
                                "model": "operator-custom-local-model",
                                "dimension": 777,
                            },
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            settings.chmod(0o600)
            settings_before = settings.read_bytes()
            venv_target_before = os.readlink(runtime / ".venv")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    # v1.0.1 forwards these matching preservation flags.  The
                    # candidate installer must accept them without treating
                    # them as a request to rewrite detailed Settings.
                    "--enable-rag",
                    "--rag-embedding-mode",
                    "local",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            settings_after = settings.read_bytes()
            venv_target_after = os.readlink(runtime / ".venv")

        output = result.stdout + result.stderr
        envelope_line = next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
        )
        envelope = json.loads(envelope_line.split("=", 1)[1])
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(envelope["updateMode"], "reuse-existing-venv")
        self.assertTrue(envelope["reusesRuntimeVenv"])
        self.assertFalse(envelope["plannedDependenciesInstall"])
        self.assertEqual(settings_after, settings_before)
        self.assertEqual(venv_target_after, venv_target_before)
        self.assertNotIn("operator-custom-local-model", output)

    def test_upgrade_blocks_untrusted_runtime_profile_before_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            old_source = self._write_prior_runtime_source(runtime)
            old_venv = self._write_trusted_runtime_venv(runtime)
            settings = runtime / "config" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(
                json.dumps(
                    {
                        "features": {"rag": True},
                        "rag": {"enabled": False},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            settings.chmod(0o600)
            protected = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    settings,
                    old_source / ".actanara-runtime-source.json",
                    old_venv / runtime_dependency_contract.MARKER_NAME,
                )
            }
            source_target_before = os.readlink(runtime / "app" / "source")
            venv_target_before = os.readlink(runtime / ".venv")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            output = result.stdout + result.stderr
            envelope_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
            )
            envelope = json.loads(envelope_line.split("=", 1)[1])
            self.assertEqual(result.returncode, 2, output)
            self.assertEqual(envelope["reason"], "runtime-dependency-profile-untrusted")
            self.assertEqual(envelope["stage"], "dependency-profile")
            self.assertFalse(envelope["dependenciesInstalled"])
            self.assertFalse(envelope["sourceUpdated"])
            self.assertFalse(envelope["servicesStopped"])
            self.assertEqual(os.readlink(runtime / "app" / "source"), source_target_before)
            self.assertEqual(os.readlink(runtime / ".venv"), venv_target_before)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                protected,
            )
            self.assertFalse((runtime / "app" / "update-transactions").exists())
            self.assertFalse((runtime / "app" / ".update-transaction.lock").exists())

    def test_upgrade_inherits_dev_test_from_trusted_active_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            self._write_prior_runtime_source(runtime)
            old_venv = self._write_trusted_runtime_venv(
                runtime,
                profiles=("dashboard", "dev-test"),
            )
            marker_before = (
                old_venv / runtime_dependency_contract.MARKER_NAME
            ).read_bytes()
            venv_target_before = os.readlink(runtime / ".venv")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            output = result.stdout + result.stderr
            envelope_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
            )
            envelope = json.loads(envelope_line.split("=", 1)[1])
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(envelope["updateMode"], "reuse-existing-venv")
            self.assertTrue(envelope["reusesRuntimeVenv"])
            self.assertFalse(envelope["plannedDependenciesInstall"])
            self.assertEqual(os.readlink(runtime / ".venv"), venv_target_before)
            self.assertEqual(
                (old_venv / runtime_dependency_contract.MARKER_NAME).read_bytes(),
                marker_before,
            )

    def test_upgrade_rejects_legacy_rag_profile_flags_that_conflict_with_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            self._write_prior_runtime_source(runtime)
            self._write_trusted_runtime_venv(
                runtime,
                profiles=("dashboard", "rag-server", "rag-local"),
            )
            source_before = os.readlink(runtime / "app" / "source")
            venv_before = os.readlink(runtime / ".venv")

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    "--enable-rag",
                    "--rag-embedding-mode",
                    "cloud",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )

            envelope_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
            )
            envelope = json.loads(envelope_line.split("=", 1)[1])
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(envelope["reason"], "runtime-dependency-profile-untrusted")
            self.assertEqual(envelope["stage"], "dependency-profile")
            self.assertFalse(envelope["servicesStopped"])
            self.assertEqual(os.readlink(runtime / "app" / "source"), source_before)
            self.assertEqual(os.readlink(runtime / ".venv"), venv_before)
            self.assertFalse((runtime / "app" / "update-transactions").exists())

    def test_offline_force_rebuild_cache_miss_emits_failure_before_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            prior_source = self._write_prior_runtime_source(runtime)
            prior_venv = self._write_trusted_runtime_venv(runtime)
            source_target_before = os.readlink(runtime / "app" / "source")
            venv_target_before = os.readlink(runtime / ".venv")
            protected = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    prior_source / ".actanara-runtime-source.json",
                    prior_venv / runtime_dependency_contract.MARKER_NAME,
                    prior_venv / "bin" / "python",
                )
            }
            launchctl = root / "launchctl"
            launchctl_calls = root / "launchctl-calls.log"
            launchctl_state = root / "launchctl-state"
            launchctl_state.mkdir()
            self._write_stateful_fake_launchctl(launchctl)

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--force-rebuild",
                    "--offline",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--yes",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                    "ACTANARA_INSTALL_LAUNCHCTL": str(launchctl),
                    "ACTANARA_TEST_LAUNCHCTL_CALLS": str(launchctl_calls),
                    "ACTANARA_TEST_LAUNCHCTL_STATE": str(launchctl_state),
                    "ACTANARA_LOCATION_FILE": str(root / "location.json"),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            output = result.stdout + result.stderr
            envelope_line = next(
                line
                for line in output.splitlines()
                if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
            )
            envelope = json.loads(envelope_line.split("=", 1)[1])
            calls = (
                launchctl_calls.read_text(encoding="utf-8").splitlines()
                if launchctl_calls.exists()
                else []
            )

            self.assertEqual(result.returncode, 3, output)
            self.assertEqual(envelope["status"], "failed")
            self.assertEqual(envelope["updateMode"], "rebuild-candidate-venv")
            self.assertEqual(envelope["reason"], "offline-cache-miss")
            self.assertEqual(envelope["stage"], "dependency-plan")
            self.assertFalse(envelope["plannedDependenciesInstall"])
            self.assertFalse(envelope["dependenciesInstalled"])
            self.assertFalse(envelope["reusesRuntimeVenv"])
            self.assertFalse(envelope["sourceUpdated"])
            self.assertFalse(envelope["cacheUsed"])
            self.assertFalse(envelope["servicesStopped"])
            self.assertEqual(os.readlink(runtime / "app" / "source"), source_target_before)
            self.assertEqual(os.readlink(runtime / ".venv"), venv_target_before)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
                protected,
            )
            self.assertFalse((runtime / "app" / "update-transactions").exists())
            self.assertFalse((runtime / "app" / "dependency-cache").exists())
            self.assertFalse((runtime / "app" / ".update-transaction.lock").exists())
            self.assertFalse(any(call.startswith(("bootout ", "bootstrap ", "kickstart ")) for call in calls), calls)

    def test_installer_persists_distinct_nova_task_feature_flag(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('"novaTask": os.environ["ACTANARA_INSTALL_ENABLE_NOVA_TASK"] == "1"', script)
        self.assertIn('"taskAuditSink": os.environ["ACTANARA_INSTALL_ENABLE_NOVA_TASK"] == "1"', script)

    def test_installer_declares_install_time_language_profile(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("--language LOCALE", script)
        self.assertIn("ACTANARA_INSTALL_LANGUAGE", script)
        self.assertIn("apply_language_profile", script)
        self.assertIn('"locale": os.environ["ACTANARA_INSTALL_LANGUAGE"]', script)
        self.assertIn('"languageProfile": os.environ["ACTANARA_INSTALL_PIPELINE_LANGUAGE_PROFILE"]', script)
        self.assertIn('"englishEnabled": os.environ["ACTANARA_INSTALL_PIPELINE_ENGLISH_ENABLED"] == "1"', script)
        self.assertIn('"diarySchemaVersion": os.environ["ACTANARA_INSTALL_PIPELINE_DIARY_SCHEMA_VERSION"]', script)
        self.assertIn('"promptPayloadProfile": os.environ["ACTANARA_INSTALL_PIPELINE_PROMPT_PAYLOAD_PROFILE"]', script)
        self.assertIn('update.setdefault("rag", {})["languageProfile"] = os.environ["ACTANARA_INSTALL_RAG_LANGUAGE_PROFILE"]', script)
        self.assertIn('RAG_LOCAL_MODEL="all-MiniLM-L6-v2"', script)
        self.assertIn('--language "${INSTALL_LANGUAGE}"', script)

    def test_dry_run_can_select_english_language_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "en-US",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Setup plan", output)
        self.assertIn("Next steps", output)
        self.assertNotIn("language: en-US", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_english_rag_uses_english_local_embedding_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "en-US",
                    "--enable-rag",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Memory and search · local · all-MiniLM-L6-v2", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_english_rag_preserves_explicit_local_embedding_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "en-US",
                    "--enable-rag",
                    "--rag-local-model",
                    "BAAI/bge-large-en-v1.5",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Memory and search · local · BAAI/bge-large-en-v1.5", output)
        self.assertFalse(runtime.exists())

    def test_installer_rejects_unknown_language_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "fr-FR",
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home), "ACTANARA_INSTALL_PLATFORM": "Darwin"},
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 2, output)
        self.assertIn("安装语言请选择 zh-CN 或 en-US", output)
        self.assertNotIn("--language must be", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_defaults_to_dashboard_scheduler_and_base_dashboard_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("准备 Actanara 文件夹", output)
        self.assertEqual(output.count("✓ 准备 Actanara 文件夹"), 1)
        self.assertIn("准备日记与报告文件夹", output)
        self.assertIn("准备 Actanara 文件", output)
        self.assertIn("准备 Python 文件", output)
        self.assertIn("创建 Python 环境", output)
        self.assertIn("安装所需软件", output)
        self.assertIn("保存已安装软件信息", output)
        self.assertIn("确认已安装软件", output)
        self.assertIn("检查所需软件", output)
        self.assertIn("安装计划", output)
        self.assertIn(f"Actanara 文件夹 · {runtime.resolve()}", output)
        self.assertNotIn("mode: install", output)
        self.assertNotIn("preflight ok:", output)
        self.assertNotIn("copy source snapshot", output)
        self.assertNotIn(".actanara-runtime-source.json", output)
        self.assertNotIn("-m venv", output)
        self.assertNotIn("-m pip install", output)
        self.assertNotIn("onboarding runtime-apply", output)
        self.assertNotIn("import-check dashboard dependencies", output)
        self.assertIn("检查 Actanara 文件", output)
        self.assertIn("检查日记创建", output)
        self.assertIn("检查每日自动运行", output)
        self.assertNotIn("--select-active-runtime", output)
        self.assertNotIn("--scheduler-plist-apply", output)
        self.assertNotIn("--scheduler-register-apply", output)
        self.assertNotIn("install_dashboard_launch_agent", output)
        self.assertIn("安装 actanara 命令", output)
        self.assertIn("命令行 · 将会准备", output)
        self.assertNotIn("ln -s", output)
        self.assertNotIn(".zprofile", output)
        self.assertFalse(runtime.exists())

    def test_dashboard_port_auto_falls_back_when_default_port_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            fake_lsof = bin_dir / "lsof"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_lsof(fake_lsof)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "ACTANARA_INSTALL_LSOF": str(fake_lsof),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--no-scheduler",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Dashboard port 3036 is in use; falling back to 8765", output)
        self.assertIn("Dashboard · http://127.0.0.1:8765/dashboard", output)
        self.assertNotIn("preflight ok:", output)
        self.assertNotIn("install_dashboard_launch_agent", output)
        self.assertFalse(runtime.exists())

    def test_dashboard_port_auto_can_be_disabled_for_strict_preflight(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("--no-dashboard-port-auto", script)
        self.assertIn("DASHBOARD_PORT_AUTO=0", script)
        self.assertIn("is already in use and --no-dashboard-port-auto is set", script)
        self.assertIn('preflight_check error error "dashboard-port"', script)

    def test_preflight_blocks_missing_python_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    str(Path(tmp) / "missing-python"),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home), "ACTANARA_INSTALL_PLATFORM": "Darwin"},
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 2, output)
        self.assertIn("无法使用 Python 3.11", output)
        self.assertNotIn("-m venv", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_plans_managed_standalone_python_install_when_default_python_is_too_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            low_python = bin_dir / "python3"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_versioned_python(low_python, "3.9.6")
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_MACHINE": "arm64",
                "ACTANARA_INSTALL_PYTHON_CANDIDATES": str(low_python),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("准备 Python 3.13.14", output)
        self.assertNotIn("cpython-3.13.14%2B20260623-aarch64-apple-darwin-install_only.tar.gz", output)
        self.assertNotIn("verify sha256 804c86c8665b18eb0df5070a79d828229018d145baea38a71a5c74c03f9b11d4", output)
        self.assertNotIn("preflight warn: python-bootstrap", output)
        self.assertNotIn("brew install", output)
        self.assertNotIn("preflight error: python-version", output)
        self.assertFalse(runtime.exists())
        script = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("--silent --show-error", script)
        self.assertNotIn('print -r -- "+ ${CURL_BIN}', script)

    def test_upgrade_dry_run_reports_upgrade_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            old_source = self._write_prior_runtime_source(runtime)
            old_venv = self._write_trusted_runtime_venv(runtime)
            (old_venv / runtime_dependency_contract.MARKER_NAME).unlink()
            source_target_before = os.readlink(runtime / "app" / "source")
            venv_target_before = os.readlink(runtime / ".venv")
            protected = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    old_source / ".actanara-runtime-source.json",
                    old_venv / "bin" / "python",
                    runtime / "config" / "settings.json",
                )
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    sys.executable,
                    "--result-json",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home), "ACTANARA_INSTALL_PLATFORM": "Darwin"},
                text=True,
                capture_output=True,
                check=False,
            )
            source_target_after = os.readlink(runtime / "app" / "source")
            venv_target_after = os.readlink(runtime / ".venv")
            protected_after = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in protected
            }

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Actanara 更新计划已生成", output)
        self.assertNotIn("mode: upgrade", output)
        self.assertNotIn("dry-run only", output)
        self.assertNotIn("Creating Python environment", output)
        self.assertIn("安装 actanara 命令", output)
        self.assertEqual(source_target_after, source_target_before)
        self.assertEqual(venv_target_after, venv_target_before)
        self.assertEqual(protected_after, protected)
        envelope_line = next(
            line
            for line in output.splitlines()
            if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
        )
        envelope = json.loads(envelope_line.split("=", 1)[1])
        self.assertEqual(envelope["updateMode"], "rebuild-candidate-venv")
        self.assertTrue(envelope["plannedDependenciesInstall"])
        self.assertFalse(envelope["dependenciesInstalled"])
        self.assertFalse(envelope["sourceUpdated"])
        self.assertFalse(envelope["servicesStopped"])

    def test_upgrade_dry_run_recovers_legacy_concrete_venv_without_force_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            self._write_prior_runtime_source(runtime)
            managed_venv = self._write_trusted_runtime_venv(runtime)
            pointer = runtime / ".venv"
            pointer.unlink()
            managed_venv.rename(pointer)
            source_target_before = os.readlink(runtime / "app" / "source")
            settings_before = (runtime / "config" / "settings.json").read_bytes()
            python_before = (pointer / "bin" / "python").read_bytes()

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--result-json",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home), "ACTANARA_INSTALL_PLATFORM": "Darwin"},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(os.readlink(runtime / "app" / "source"), source_target_before)
            self.assertTrue(pointer.is_dir())
            self.assertFalse(pointer.is_symlink())
            self.assertEqual((runtime / "config" / "settings.json").read_bytes(), settings_before)
            self.assertEqual((pointer / "bin" / "python").read_bytes(), python_before)

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        envelope_line = next(
            line
            for line in output.splitlines()
            if line.startswith("ACTANARA_UPDATE_RESULT_JSON=")
        )
        envelope = json.loads(envelope_line.split("=", 1)[1])
        self.assertEqual(envelope["updateMode"], "rebuild-candidate-venv")
        self.assertEqual(envelope["reason"], "forced-rebuild")
        self.assertTrue(envelope["plannedDependenciesInstall"])
        self.assertFalse(envelope["dependenciesInstalled"])
        self.assertFalse(envelope["servicesStopped"])

    def test_repair_existing_rejects_explicit_update_mode_flags(self):
        conflicting_flags = ("--upgrade", "--source-only", "--force-rebuild")
        for conflicting_flag in conflicting_flags:
            for ordered_flags in (
                ("--repair-existing", conflicting_flag),
                (conflicting_flag, "--repair-existing"),
            ):
                with self.subTest(flag=conflicting_flag, order=ordered_flags), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    home = root / "Home"
                    runtime = home / ".actanara"
                    home.mkdir()
                    result = subprocess.run(
                        [
                            "zsh",
                            str(INSTALLER),
                            "--language",
                            "en-US",
                            *ordered_flags,
                            "--runtime",
                            str(runtime),
                            "--source-root",
                            str(ROOT),
                            "--dry-run",
                            "--yes",
                        ],
                        cwd=ROOT,
                        env={
                            **os.environ,
                            "HOME": str(home),
                            "ACTANARA_INSTALL_PLATFORM": "Darwin",
                            "ACTANARA_INSTALL_VERBOSE": "1",
                        },
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )

                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 2, output)
                    self.assertIn("--repair-existing", output)
                    self.assertRegex(output, r"mutually exclusive|cannot be combined")
                    self.assertNotIn("Unknown option", output)
                    self.assertFalse(runtime.exists())

    def test_repair_existing_real_apply_requires_legacy_runtime(self):
        for runtime_kind in ("missing", "foreign-directory"):
            with self.subTest(runtime_kind=runtime_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "Home"
                runtime = home / ".actanara"
                home.mkdir()
                sentinel = runtime / "operator-owned.txt"
                if runtime_kind == "foreign-directory":
                    runtime.mkdir()
                    sentinel.write_text("foreign directory\n", encoding="utf-8")
                result = subprocess.run(
                    [
                        "zsh",
                        str(INSTALLER),
                        "--repair-existing",
                        "--runtime",
                        str(runtime),
                        "--source-root",
                        str(ROOT),
                        "--python",
                        sys.executable,
                        "--no-python-auto-install",
                        "--yes",
                        "--no-scheduler",
                        "--no-dashboard-server",
                        "--no-desktop-diary-link",
                        "--no-shell-path",
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(home),
                        "ACTANARA_INSTALL_PLATFORM": "Darwin",
                        "ACTANARA_INSTALL_VERBOSE": "1",
                    },
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )

                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                self.assertIn("--repair-existing requires a legacy Actanara Runtime", output)
                self.assertFalse((runtime / "app" / "update-transactions").exists())
                if runtime_kind == "missing":
                    self.assertFalse(runtime.exists())
                else:
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign directory\n")
                    self.assertEqual(sorted(path.name for path in runtime.iterdir()), [sentinel.name])

    def test_repair_existing_rebuilds_managed_components_and_preserves_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, fixture, prior = self._legacy_repair_fixture(Path(tmp))

            result = harness._run_update(fixture)

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(harness._service_state(fixture), {})
            self.assertFalse((Path(fixture["app"]) / ".update-transaction.lock").exists())
            journals = harness._journal_paths(fixture)
            self.assertEqual(len(journals), 1)
            committed = json.loads(journals[0].read_text(encoding="utf-8"))
            self.assertEqual(committed["status"], "committed")
            self.assertEqual(committed["mode"], "repair")
            self.assertTrue(committed["repairConfigurationComplete"])
            self.assertFalse(
                (Path(fixture["runtime"]) / "app" / ".repair-configuration-pending").exists()
            )
            self.assertEqual(
                Path(committed["repairBackupPath"]),
                journals[0].parent / "backups",
            )
            self.assertTrue(Path(committed["repairBackupPath"]).is_dir())
            source = prior["source"]
            venv = prior["venv"]
            cli = prior["cli"]
            self.assertTrue(source.is_symlink())
            self.assertRegex(os.readlink(source), r"^releases/[^/]+$")
            self.assertTrue(venv.is_symlink())
            self.assertRegex(os.readlink(venv), r"^app/venvs/[^/]+$")
            self.assertTrue((venv / "bin" / "python").is_file())
            self.assertTrue(cli.is_file())
            self.assertTrue(os.access(cli, os.X_OK))
            self.assertNotEqual(cli.read_bytes(), prior["cli_bytes"])
            self.assertNotEqual(cli.stat().st_ino, prior["cli_inode"])
            settings_before = json.loads(prior["settings_bytes"])
            settings_after = json.loads(prior["settings"].read_text(encoding="utf-8"))
            settings_before["schemaVersion"] = 1
            settings_before["features"]["dashboard"] = True
            settings_before["schedule"]["enabled"] = False
            settings_before["dashboard"]["server"] = {"enabled": False}
            settings_before["rag"]["server"]["enabled"] = False
            self.assertEqual(settings_after, settings_before)
            self.assertEqual(prior["database"].read_bytes(), prior["database_bytes"])
            self.assertEqual(prior["user_sentinel"].read_bytes(), prior["user_sentinel_bytes"])
            self.assertEqual(prior["user_sentinel"].stat().st_ino, prior["user_sentinel_inode"])
            self.assertEqual(
                prior["user_sentinel"].stat().st_mode & 0o777,
                prior["user_sentinel_mode"],
            )
            with closing(sqlite3.connect(prior["database"])) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM evidence ORDER BY id").fetchall(),
                    [("before-full-upgrade",)],
                )

    def test_repair_existing_reconciles_conflicting_legacy_rag_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, fixture, prior = self._legacy_repair_fixture(Path(tmp))
            settings = prior["settings"]
            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload["features"]["rag"] = False
            payload["rag"]["enabled"] = True
            payload["rag"]["embedding"] = {
                "provider": "cohere",
                "model": "user-model",
            }
            settings.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            fixture["protected_hashes"][settings] = hashlib.sha256(
                settings.read_bytes()
            ).hexdigest()
            fixture["protected_bytes"][settings] = settings.read_bytes()

            result = harness._run_update(fixture)

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            migrated = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIs(migrated["features"]["rag"], True)
            self.assertIs(migrated["rag"]["enabled"], True)
            self.assertEqual(migrated["rag"]["embedding"]["mode"], "cloud")
            self.assertEqual(migrated["rag"]["embedding"]["providerId"], "cohere")
            self.assertEqual(migrated["rag"]["embedding"]["model"], "user-model")
            self.assertEqual(prior["database"].read_bytes(), prior["database_bytes"])
            self.assertEqual(
                prior["user_sentinel"].read_bytes(), prior["user_sentinel_bytes"]
            )

    def test_repair_service_intent_prefers_explicit_rag_fields(self):
        script = INSTALLER.read_text(encoding="utf-8")
        function = script.split("inherit_repair_service_state() {", 1)[1].split(
            "\n}\n\nrecord_services_stopped_result()", 1
        )[0]
        resolver = function.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        cases = (
            ({"features": {"rag": True}, "rag": {"enabled": False}}, False),
            ({"features": {"rag": False}, "rag": {"enabled": True}}, True),
            (
                {
                    "features": {"rag": False},
                    "rag": {"enabled": True, "server": {"enabled": False}},
                },
                False,
            ),
        )
        for settings_payload, expected in cases:
            with self.subTest(settings=settings_payload), tempfile.TemporaryDirectory() as tmp:
                runtime = Path(tmp) / "runtime"
                settings = runtime / "config" / "settings.json"
                settings.parent.mkdir(parents=True)
                settings.write_text(
                    json.dumps(settings_payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                journal = Path(tmp) / "journal.json"
                journal.write_text(
                    json.dumps(
                        {
                            "mode": "repair",
                            "status": "committed",
                            "runtime": str(runtime),
                            "services": [],
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                result = subprocess.run(
                    [sys.executable, "-", str(journal)],
                    input=resolver,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                fields = result.stdout.strip().split("\t")
                self.assertEqual(len(fields), 3)
                self.assertEqual(fields[2], "1" if expected else "0")

    def test_repair_existing_failure_restores_legacy_components_and_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, fixture, prior = self._legacy_repair_fixture(Path(tmp))

            result = harness._run_update(
                fixture,
                env_overrides={"ACTANARA_INSTALL_TEST_FAIL_PHASE": "venv-promoted"},
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            source = prior["source"]
            venv = prior["venv"]
            cli = prior["cli"]
            self.assertTrue(source.is_dir())
            self.assertFalse(source.is_symlink())
            self.assertEqual(source.stat().st_ino, prior["source_inode"])
            self.assertEqual(
                prior["source_sentinel"].stat().st_ino,
                prior["source_sentinel_inode"],
            )
            self.assertEqual(
                prior["source_sentinel"].read_bytes(),
                prior["source_sentinel_bytes"],
            )
            self.assertTrue(venv.is_dir())
            self.assertFalse(venv.is_symlink())
            self.assertEqual(venv.stat().st_ino, prior["venv_inode"])
            self.assertEqual(prior["venv_python"].stat().st_ino, prior["venv_python_inode"])
            self.assertEqual(prior["venv_python"].read_bytes(), prior["venv_python_bytes"])
            self.assertEqual(cli.stat().st_ino, prior["cli_inode"])
            self.assertEqual(cli.read_bytes(), prior["cli_bytes"])
            self.assertEqual(prior["settings"].read_bytes(), prior["settings_bytes"])
            self.assertEqual(prior["database"].read_bytes(), prior["database_bytes"])
            self.assertEqual(prior["user_sentinel"].read_bytes(), prior["user_sentinel_bytes"])
            self.assertEqual(prior["user_sentinel"].stat().st_ino, prior["user_sentinel_inode"])
            self.assertEqual(
                prior["user_sentinel"].stat().st_mode & 0o777,
                prior["user_sentinel_mode"],
            )
            harness._assert_protected_unchanged(fixture)
            journals = harness._journal_paths(fixture)
            self.assertEqual(len(journals), 1)
            journal = json.loads(journals[0].read_text(encoding="utf-8"))
            self.assertEqual(journal["status"], "rolled-back")
            self.assertEqual(journal["rollbackErrors"], [])
            self.assertIn("venv-promoted", harness._journal_events(journals[0]))
            self.assertFalse((Path(fixture["app"]) / ".update-transaction.lock").exists())

    def test_repair_existing_post_commit_failure_is_completed_by_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, fixture, prior = self._legacy_repair_fixture(Path(tmp))
            pending = Path(fixture["runtime"]) / "app" / ".repair-configuration-pending"
            fixture["command"].remove("--no-scheduler")

            failed = harness._run_update(
                fixture,
                env_overrides={
                    "ACTANARA_FULL_UPGRADE_FAULT_PHASE": "runtime-apply",
                    "ACTANARA_FULL_UPGRADE_FAULT_KIND": "return",
                },
            )

            failed_output = failed.stdout + failed.stderr
            self.assertNotEqual(failed.returncode, 0, failed_output)
            self.assertIn("请重新运行 one-liner", failed_output)
            self.assertNotIn("--repair-existing", failed_output)
            self.assertTrue(pending.is_file())
            self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
            first_journals = harness._journal_paths(fixture)
            self.assertEqual(len(first_journals), 1)
            first_state = json.loads(first_journals[0].read_text(encoding="utf-8"))
            self.assertEqual(first_state["status"], "committed")
            self.assertFalse(first_state["repairConfigurationComplete"])
            self.assertEqual(pending.read_text(encoding="ascii"), f"{first_state['txId']}\n")
            self.assertEqual(prior["database"].read_bytes(), prior["database_bytes"])
            self.assertEqual(prior["user_sentinel"].read_bytes(), prior["user_sentinel_bytes"])
            first_settings = json.loads(prior["settings"].read_text(encoding="utf-8"))
            self.assertIs(first_settings["schedule"]["enabled"], True)
            self.assertIs(first_settings["features"]["dashboard"], True)
            self.assertIs(first_settings["dashboard"]["server"]["enabled"], False)

            retried = harness._run_update(fixture)

            retried_output = retried.stdout + retried.stderr
            self.assertEqual(retried.returncode, 0, retried_output)
            self.assertFalse(pending.exists())
            journals = harness._journal_paths(fixture)
            self.assertEqual(len(journals), 2)
            states = [json.loads(path.read_text(encoding="utf-8")) for path in journals]
            self.assertEqual(
                sorted(state["repairConfigurationComplete"] for state in states),
                [False, True],
            )
            self.assertEqual(prior["database"].read_bytes(), prior["database_bytes"])
            self.assertEqual(prior["user_sentinel"].read_bytes(), prior["user_sentinel_bytes"])
            calls = [
                json.loads(line)
                for line in Path(fixture["python_log"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            argument_lines = [" ".join(call.get("argv") or []) for call in calls]
            self.assertTrue(
                any("--scheduler-register-apply" in line for line in argument_lines)
            )
            self.assertFalse(
                any("install_dashboard_launch_agent" in line for line in argument_lines)
            )

    def test_repair_existing_restores_services_requested_by_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, fixture, prior = self._legacy_repair_fixture(Path(tmp))
            command = fixture["command"]
            command.remove("--no-scheduler")
            command.remove("--no-dashboard-server")
            settings = prior["settings"]
            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload["features"]["dashboard"] = True
            payload["dashboard"]["server"] = {"enabled": True}
            payload["schedule"]["enabled"] = True
            settings.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            fixture["protected_hashes"][settings] = hashlib.sha256(
                settings.read_bytes()
            ).hexdigest()
            fixture["protected_bytes"][settings] = settings.read_bytes()

            result = harness._run_update(fixture)

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            calls = [
                json.loads(line)
                for line in Path(fixture["python_log"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            argument_lines = [" ".join(call.get("argv") or []) for call in calls]
            self.assertTrue(
                any("--scheduler-plist-apply" in line for line in argument_lines)
            )
            self.assertTrue(
                any("--scheduler-register-apply" in line for line in argument_lines)
            )
            self.assertTrue(
                any("install_dashboard_launch_agent" in line for line in argument_lines)
            )
            self.assertFalse(
                any("install_rag_launch_agent" in line for line in argument_lines)
            )
            self.assertFalse(
                (Path(fixture["runtime"]) / "app" / ".repair-configuration-pending").exists()
            )
            migrated = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIs(migrated["schedule"]["enabled"], True)
            self.assertIs(migrated["features"]["dashboard"], True)
            self.assertIs(migrated["dashboard"]["server"]["enabled"], True)
            self.assertIs(migrated["rag"]["server"]["enabled"], False)

    def test_upgrade_requires_existing_runtime_for_real_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            fake_python = Path(tmp) / "python3"
            log_path = Path(tmp) / "commands.log"
            home.mkdir()
            self._write_fake_python(fake_python, log_path)
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--upgrade",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "en-US",
                    "--python",
                    str(fake_python),
                    "--no-scheduler",
                    "--no-dashboard-server",
                    "--yes",
                ],
                cwd=ROOT,
                env={**os.environ, "HOME": str(home), "ACTANARA_INSTALL_PLATFORM": "Darwin"},
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 2, output)
        self.assertIn("No existing Actanara installation was found", output)
        self.assertNotIn("--upgrade requires an existing runtime", output)
        self.assertNotIn("-m venv", log)

    def test_fresh_apply_rejects_existing_runtime_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            old_release = runtime / "app" / "releases" / "old"
            fake_python = Path(tmp) / "python3"
            log_path = Path(tmp) / "commands.log"
            old_release.mkdir(parents=True)
            (runtime / "app" / "source").symlink_to("releases/old")
            sentinel = old_release / "operator-owned.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            installer_log = runtime / "state" / "logs" / "installer-v2.log"
            installer_log.parent.mkdir(parents=True)
            installer_log.write_text("operator-log\n", encoding="utf-8")
            self._write_fake_python(fake_python, log_path)

            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--language",
                    "en-US",
                    "--python",
                    str(fake_python),
                    "--no-scheduler",
                    "--no-dashboard-server",
                    "--yes",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "ACTANARA_INSTALL_PLATFORM": "Darwin",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("Actanara is already installed in this folder", output)
            self.assertNotIn("existing Actanara Runtime state", output)
            self.assertEqual(os.readlink(runtime / "app" / "source"), "releases/old")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertEqual(
                installer_log.read_text(encoding="utf-8"),
                "operator-log\n",
            )
            self.assertNotIn("-m venv", log)

    def test_dry_run_can_disable_desktop_diary_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--no-desktop-diary-link",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Desktop diary shortcut:", output)
        self.assertNotIn("Desktop diary shortcut skipped", output)
        self.assertNotIn("Actanara'", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_can_disable_wizard_with_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_WIZARD": "false",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("dry-run only", output)
        self.assertIn("Actanara 安装计划已生成", output)
        self.assertIn("actanara doctor", output)
        self.assertNotIn("guided setup", output)

    def test_dry_run_summary_only_hides_command_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_WIZARD": "false",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--summary-only",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("安装计划", output)
        self.assertIn("接下来", output)
        self.assertIn("Actanara 安装计划已生成", output)
        self.assertIn("actanara doctor", output)
        self.assertNotIn("+ mkdir", output)
        self.assertNotIn("Installer preflight", output)
        self.assertFalse(runtime.exists())

    def test_summary_only_uses_selected_english_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_WIZARD": "false",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--summary-only",
                    "--language",
                    "en-US",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Setup plan", output)
        self.assertIn("Next steps", output)
        self.assertNotIn("安装计划", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_can_disable_scheduler_and_dashboard_server_without_disabling_nova_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--summary-only",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("SSE server disabled", output)
        self.assertNotIn("Static snapshot pages such as AI Assets", output)
        self.assertNotIn("已按你的选择关闭 Dashboard 后台服务", output)
        self.assertNotIn("检查系统环境\n", output)
        self.assertNotIn("--scheduler-register-apply", output)
        self.assertNotIn("install_dashboard_launch_agent", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_reports_output_paths_and_non_secret_llm_provider_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            diary = home / "DiaryOut"
            reports = home / "ReportsOut"
            snapshots = home / "SnapshotsOut"
            archives = home / "ArchivesOut"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--diary-output",
                    str(diary),
                    "--reports-output",
                    str(reports),
                    "--snapshots-output",
                    str(snapshots),
                    "--archives-output",
                    str(archives),
                    "--llm-provider",
                    "openai-compatible",
                    "--llm-endpoint",
                    "https://llm.example.invalid/v1",
                    "--llm-model",
                    "example-model",
                    "--llm-api-key-env",
                    "ACTANARA_TEST_LLM_KEY",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"日记 · {diary.resolve()}", output)
        self.assertNotIn(f"reports output: {reports.resolve()}", output)
        self.assertNotIn(f"snapshots output: {snapshots.resolve()}", output)
        self.assertNotIn(f"archives/intermediate output: {archives.resolve()}", output)
        self.assertIn("AI 生成 · example-model", output)
        self.assertNotIn("api key env:", output)
        self.assertNotIn("no secret values", output)
        self.assertFalse(runtime.exists())
        self.assertFalse(diary.exists())

    def test_installer_rejects_secret_like_llm_api_key_env_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            secret_like = "sk-test-value-that-should-not-be-echoed"
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_INSTALL_LANGUAGE": "zh-CN",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--llm-provider",
                    "openai-compatible",
                    "--llm-endpoint",
                    "https://llm.example.invalid/v1",
                    "--llm-model",
                    "example-model",
                    "--llm-api-key-env",
                    secret_like,
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 2, output)
        self.assertIn("AI 密钥设置无效", output)
        self.assertIn("请在 Dashboard 设置中保存密钥", output)
        self.assertNotIn("environment variable", output)
        self.assertNotIn(secret_like, output)
        self.assertFalse(runtime.exists())

    def test_no_dashboard_is_rejected_because_dashboard_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--no-dashboard",
                    "--no-scheduler",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("Dashboard is included with Actanara", output)
        self.assertNotIn("--no-dashboard is no longer supported", output)
        self.assertIn("--no-dashboard-server", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_enable_dev_test_adds_dev_test_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-dev-test",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        deployed = runtime.resolve() / "app" / "source"

        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"Actanara 文件夹 · {runtime.resolve()}", output)
        self.assertNotIn("install dependency spec:", output)
        self.assertNotIn("dev-test: enabled", output)
        self.assertNotIn(f"-m pip install {deployed}[dashboard,dev-test]", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_rag_embedding_server_deployment_is_background_and_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-rag",
                    "--deploy-embedding-server",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        deployed = runtime.resolve() / "app" / "source"

        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"Actanara 文件夹 · {runtime.resolve()}", output)
        self.assertNotIn("install dependency spec:", output)
        self.assertNotIn(f"-m pip install {deployed}[dashboard,rag-local]", output)
        self.assertIn("准备记忆与搜索", output)
        self.assertIn("启动记忆与搜索", output)
        self.assertNotIn("install_rag_launch_agent", output)
        self.assertNotIn("deploy-embedding-server.sh", output)
        self.assertNotIn("nohup", output)
        self.assertNotIn("embedding-server-deploy.log", output)
        self.assertNotIn(f"-m pip install {deployed}[rag-local]", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_rag_local_defaults_to_embedding_server_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-rag",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("启动记忆与搜索", output)
        self.assertNotIn("install_rag_launch_agent", output)
        self.assertIn("准备记忆与搜索", output)
        self.assertNotIn("deploy-embedding-server.sh", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_rag_local_embedding_server_deployment_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-rag",
                    "--no-deploy-embedding-server",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("启动记忆与搜索", output)
        self.assertNotIn("install_rag_launch_agent", output)
        self.assertNotIn("Queueing background embedding server deployment", output)
        self.assertNotIn("deploy-embedding-server.sh", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_rag_cloud_mode_does_not_queue_local_embedding_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-rag",
                    "--rag-embedding-mode",
                    "cloud",
                    "--rag-cloud-provider",
                    "example-cloud",
                    "--rag-cloud-endpoint",
                    "https://embed.example.invalid/v1",
                    "--rag-cloud-model",
                    "embed-example",
                    "--rag-cloud-dimension",
                    "1024",
                    "--rag-cloud-api-key-env",
                    "ACTANARA_TEST_EMBED_KEY",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("记忆与搜索 · 云端 · embed-example", output)
        self.assertNotIn("nova-RAG embedding mode: cloud", output)
        self.assertNotIn("api key env=ACTANARA_TEST_EMBED_KEY", output)
        self.assertNotIn("Queueing background embedding server deployment", output)
        self.assertNotIn("install_rag_launch_agent", output)
        self.assertNotIn("nohup", output)
        self.assertFalse(runtime.exists())

    def test_dry_run_rag_local_model_can_select_384_dimension(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--dry-run",
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--enable-rag",
                    "--rag-local-model",
                    "intfloat/multilingual-e5-small",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        deployed = runtime.resolve() / "app" / "source"

        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"Actanara 文件夹 · {runtime.resolve()}", output)
        self.assertIn("记忆与搜索 · 本地 · intfloat/multilingual-e5-small", output)
        self.assertNotIn("nova-RAG embedding mode: local", output)
        self.assertFalse(runtime.exists())

    def test_installer_rag_cloud_schema_separates_mode_and_provider_id(self):
        content = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('"mode": embedding_mode', content)
        self.assertIn('"provider": embedding_mode', content)
        self.assertIn('"providerId": "local" if embedding_mode == "local" else os.environ["ACTANARA_INSTALL_RAG_CLOUD_PROVIDER"]', content)
        self.assertIn('os.environ["ACTANARA_INSTALL_RAG_LOCAL_MODEL"]', content)
        self.assertIn('os.environ["ACTANARA_INSTALL_RAG_LOCAL_DIMENSION"]', content)

    def test_fake_python_helpers_execute_manifest_validator_without_cwd_symlink(self):
        for helper_name in ("base", "dependency-remediation"):
            with self.subTest(helper=helper_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fake_python = root / "bin" / "python3"
                log_path = root / "commands.log"
                fake_python.parent.mkdir()
                if helper_name == "base":
                    self._write_fake_python(fake_python, log_path)
                else:
                    self._write_fake_python_with_dependency_remediation(
                        fake_python,
                        log_path,
                        root / "dependency-installed.marker",
                    )

                release_id = "20260712T160253-75981-7548"
                releases = root / "runtime" / "app" / "releases"
                staging = releases / f".tmp-{release_id}"
                staging.mkdir(parents=True)
                manifest = staging / ".actanara-runtime-source.json"
                manifest.write_text('{"schemaVersion": 2}\n', encoding="utf-8")
                validator_marker = staging / "validator-ran.txt"
                validator_script = (
                    "import sys\n"
                    "from pathlib import Path\n"
                    "manifest = Path(sys.argv[1])\n"
                    "release_id = sys.argv[2]\n"
                    "assert manifest.is_file()\n"
                    "(manifest.parent / 'validator-ran.txt').write_text(release_id, encoding='utf-8')\n"
                )
                validated = subprocess.run(
                    [str(fake_python), "-", str(manifest), release_id],
                    cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    input=validator_script,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
                self.assertEqual(validator_marker.read_text(encoding="utf-8"), release_id)
                self.assertFalse(os.path.lexists(root / release_id))

                release_target = releases / release_id
                release_target.mkdir()
                source_pointer = root / "runtime" / "app" / "source"
                promoted = subprocess.run(
                    [str(fake_python), "-", str(release_target), str(source_pointer)],
                    cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    input="raise SystemExit('promotion should be simulated')\n",
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
                self.assertTrue(source_pointer.is_symlink())
                self.assertEqual(os.readlink(source_pointer), str(Path("releases") / release_target.name))

    def test_fake_python_smoke_executes_real_installer_path_without_real_pip(self):
        from advanced.dashboard import dashboard_launch_agent as dashboard_launcher
        from advanced.dashboard import rag_server_launch_agent as rag_launcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_python = bin_dir / "python3"
            home.mkdir()
            bin_dir.mkdir()
            (runtime / "app").mkdir(parents=True)
            self._write_fake_python(fake_python, log_path)
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
                "ACTANARA_INSTALL_LANGUAGE": "zh-CN",
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    str(fake_python),
                    "--summary-only",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            runtime_exists = runtime.resolve().exists()
            source_pointer = runtime / "app" / "source"
            venv_pointer = runtime / ".venv"
            source_raw_target = os.readlink(source_pointer)
            venv_raw_target = os.readlink(venv_pointer)
            source_resolved = source_pointer.resolve()
            venv_resolved = venv_pointer.resolve()
            dependency_marker = runtime_dependency_contract.read_dependency_marker(venv_resolved)
            dashboard_launcher._require_runtime_pointers(runtime.resolve())
            rag_launcher._require_runtime_pointers(runtime.resolve())

        output = result.stdout + result.stderr
        deployed = runtime.resolve() / "app" / "source"

        self.assertEqual(result.returncode, 0, output)
        self.assertTrue(runtime_exists)
        self.assertEqual(Path(source_raw_target).parts[:1], ("releases",))
        self.assertEqual(Path(venv_raw_target).parts[:2], ("app", "venvs"))
        self.assertFalse(Path(source_raw_target).is_absolute())
        self.assertFalse(Path(venv_raw_target).is_absolute())
        self.assertEqual(source_resolved.parent.name, "releases")
        self.assertEqual(venv_resolved.parent.name, "venvs")
        self.assertEqual(source_resolved.name, venv_resolved.name)
        self.assertIn("-m venv", log)
        self.assertNotIn("-m pip install", log)
        self.assertIn("dependency_contract.py cache-status", log)
        self.assertIn("dependency_contract.py materialize-cache", log)
        self.assertIn("dependency_contract.py install", log)
        self.assertIn("dependency_contract.py write-marker", log)
        self.assertIn("dependency_contract.py verify-marker", log)
        self.assertEqual(dependency_marker["profiles"], ["dashboard"])
        self.assertIn("onboarding runtime-apply", log)
        self.assertIn("onboarding runtime-status", log)
        self.assertIn("doctor --installer", log)
        self.assertIn("doctor --pipeline", log)
        self.assertIn("doctor --scheduler", log)
        self.assertNotIn("--scheduler-register-apply", log)
        self.assertNotIn("install_dashboard_launch_agent", log)
        self.assertIn("安装摘要", output)
        self.assertIn("接下来", output)
        self.assertNotIn("安装助手", output)
        self.assertNotIn("────────────────────────────────────────", output)
        self.assertNotIn("检查系统环境\n", output)
        self.assertNotIn("准备 Actanara\n", output)
        self.assertNotIn("安装 Actanara\n", output)

    def test_installer_stores_wizard_llm_api_key_via_stdin_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_python = bin_dir / "python3"
            secret_value = "sk-test-value-that-should-not-be-echoed"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_python(fake_python, log_path)
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
                "ACTANARA_INSTALL_LLM_API_KEY_VALUE": secret_value,
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    str(fake_python),
                    "--llm-provider",
                    "openai-compatible",
                    "--llm-endpoint",
                    "https://llm.example.invalid/v1",
                    "--llm-model",
                    "example-model",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            installer_log = (runtime / "state" / "logs" / "installer-v2.log").read_text(encoding="utf-8")

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("安全保存 AI 密钥", output)
        self.assertIn("model key --value-stdin", log)
        self.assertIn("actanara model key --value-stdin", installer_log)
        self.assertNotIn(secret_value, output)
        self.assertNotIn(secret_value, log)
        self.assertNotIn(secret_value, installer_log)

    def test_dependency_gate_missing_package_fails_closed_without_remediation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            marker_path = root / "fastapi-installed.marker"
            fake_python = bin_dir / "python3"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_python_with_dependency_remediation(fake_python, log_path, marker_path)
            env = {
                **os.environ,
                "HOME": str(home),
                "ACTANARA_INSTALL_PLATFORM": "Darwin",
                "ACTANARA_LOCATION_FILE": str(home / ".config" / "actanara" / "location.json"),
            }
            result = subprocess.run(
                [
                    "zsh",
                    str(INSTALLER),
                    "--runtime",
                    str(runtime),
                    "--source-root",
                    str(ROOT),
                    "--python",
                    str(fake_python),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            marker_exists = marker_path.exists()

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 1, output)
        self.assertNotIn("Installing missing runtime dependencies detected by dependency gate: fastapi>=0.110,<1", output)
        self.assertNotIn("dependency gate ok: fake remediation passed", output)
        self.assertIn("这个步骤未能完成", output)
        self.assertNotIn("-m pip install fastapi>=0.110,<1", log)
        self.assertFalse(marker_exists)

    def test_bootstrap_missing_option_values_use_localized_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            home.mkdir()
            env = self._fresh_bootstrap_env(home)
            commands = (
                ["zsh", str(BOOTSTRAP), "--source-root"],
                ["zsh", str(BOOTSTRAP), "--dry-run", "--source-root", str(ROOT), "--", "--runtime"],
            )

            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 2, output)
                    self.assertIn("一个安装选项缺少内容", output)
                    self.assertNotIn("requires a value", output)

    def test_bootstrap_dry_run_uses_local_source_root_and_forwards_installer_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            runtime = home / ".actanara"
            home.mkdir()
            env = self._fresh_bootstrap_env(home)
            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--dry-run",
                    "--source-root",
                    str(ROOT),
                    "--",
                    "--runtime",
                    str(runtime),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("启动 Actanara 安装", output)
        self.assertNotIn("install/install.sh --source-root", output)
        self.assertNotIn("--no-scheduler --no-dashboard-server", output)
        self.assertNotIn("dry-run only", output)
        self.assertNotIn("-m pip install", output)
        self.assertNotIn("Scheduler registration skipped by --no-scheduler", output)
        self.assertNotIn("SSE server disabled", output)
        self.assertFalse(runtime.exists())

    def test_bootstrap_offline_local_source_never_uses_network_and_forwards_flag_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            source = root / "source"
            bin_dir = root / "bin"
            git_log = root / "git.log"
            curl_log = root / "curl.log"
            installer_log = root / "installer-args.log"
            fake_git = bin_dir / "git"
            fake_curl = bin_dir / "curl"
            home.mkdir()
            bin_dir.mkdir()
            self._write_bootstrap_command_tripwire(fake_git, git_log)
            self._write_bootstrap_command_tripwire(fake_curl, curl_log)
            self._write_bootstrap_installer_probe(source, installer_log)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_INSTALL_CURL"] = str(fake_curl)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-root",
                    str(source),
                    "--git",
                    str(fake_git),
                    "--",
                    "--offline",
                    "--yes",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            installer_args = installer_log.read_text(encoding="utf-8").splitlines()

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertFalse(git_log.exists(), output)
        self.assertFalse(curl_log.exists(), output)
        self.assertEqual(installer_args.count("--offline"), 1)
        self.assertEqual(installer_args[0], "--source-root")
        self.assertEqual(Path(installer_args[1]).resolve(), source.resolve())
        self.assertIn("--yes", installer_args)

    def test_bootstrap_offline_flag_forwarding_uses_exact_array_elements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            source = root / "source"
            installer_log = root / "installer-args.log"
            home.mkdir()
            self._write_bootstrap_installer_probe(source, installer_log)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-root",
                    str(source),
                    "--",
                    "--llm-model",
                    "model name containing --offline as data",
                    "--yes",
                ],
                cwd=root,
                env=self._fresh_bootstrap_env(home),
                text=True,
                capture_output=True,
                check=False,
            )
            installer_args = installer_log.read_text(encoding="utf-8").splitlines()

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(installer_args.count("--offline"), 1)
        self.assertIn("model name containing --offline as data", installer_args)

    def test_bootstrap_offline_remote_without_ref_fails_before_cache_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            git_log = root / "git.log"
            curl_log = root / "curl.log"
            fake_git = bin_dir / "git"
            fake_curl = bin_dir / "curl"
            home.mkdir()
            bin_dir.mkdir()
            self._write_bootstrap_command_tripwire(fake_git, git_log)
            self._write_bootstrap_command_tripwire(fake_curl, curl_log)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_INSTALL_CURL"] = str(fake_curl)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    "https://github.com/Neo-Isshin/actanara.git",
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--runtime",
                    str(home / ".actanara"),
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("离线状态下缺少所需 Actanara 文件", output)
        self.assertFalse(cache.exists())
        self.assertFalse(git_log.exists(), output)
        self.assertFalse(curl_log.exists(), output)

    def test_bootstrap_offline_remote_cache_miss_fails_without_network_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            git_log = root / "git.log"
            curl_log = root / "curl.log"
            fake_git = bin_dir / "git"
            fake_curl = bin_dir / "curl"
            home.mkdir()
            bin_dir.mkdir()
            self._write_bootstrap_command_tripwire(fake_git, git_log)
            self._write_bootstrap_command_tripwire(fake_curl, curl_log)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_INSTALL_CURL"] = str(fake_curl)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    "https://github.com/Neo-Isshin/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--runtime",
                    str(home / ".actanara"),
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("离线状态下缺少所需 Actanara 文件", output)
        self.assertNotIn(str((cache / "source").resolve()), output)
        self.assertFalse(cache.exists())
        self.assertFalse(git_log.exists(), output)
        self.assertFalse(curl_log.exists(), output)

    def test_bootstrap_offline_cached_ref_verifies_object_without_network_operations(self):
        source_url = "https://example.invalid/actanara.git"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            source = cache / "source"
            bin_dir = root / "bin"
            git_log = root / "git.log"
            curl_log = root / "curl.log"
            installer_log = root / "installer-args.log"
            fake_git = bin_dir / "git"
            fake_curl = bin_dir / "curl"
            home.mkdir()
            bin_dir.mkdir()
            (source / ".git").mkdir(parents=True)
            self._write_bootstrap_installer_probe(source, installer_log)
            self._write_offline_cache_git(
                fake_git,
                git_log,
                source_url=source_url,
                resolved_commit=IMMUTABLE_TEST_COMMIT,
            )
            self._write_bootstrap_command_tripwire(fake_curl, curl_log)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_INSTALL_CURL"] = str(fake_curl)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    source_url,
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--yes",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            git_calls = git_log.read_text(encoding="utf-8").splitlines()
            installer_args = installer_log.read_text(encoding="utf-8").splitlines()

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("已准备此前下载的文件", output)
        self.assertFalse(curl_log.exists(), output)
        self.assertFalse(
            any(re.search(r"(^|\s)(fetch|clone|ls-remote)(\s|$)", call) for call in git_calls),
            git_calls,
        )
        self.assertTrue(
            any(
                f"rev-parse --verify {IMMUTABLE_TEST_COMMIT}^{{commit}}" in call
                for call in git_calls
            ),
            git_calls,
        )
        self.assertTrue(any("remote get-url origin" in call for call in git_calls), git_calls)
        self.assertTrue(any(" ls-tree -r -z --full-tree " in call for call in git_calls), git_calls)
        self.assertTrue(any(" cat-file -e " in call for call in git_calls), git_calls)
        self.assertTrue(
            any(" cat-file blob 1111111111111111111111111111111111111111" in call for call in git_calls),
            git_calls,
        )
        self.assertTrue(
            all(
                "GIT_NO_LAZY_FETCH=1" in call
                and "GIT_ALLOW_PROTOCOL_SET=1" in call
                and "GIT_ALLOW_PROTOCOL_VALUE=<>" in call
                and "GIT_TERMINAL_PROMPT=0" in call
                for call in git_calls
            ),
            git_calls,
        )
        self.assertEqual(installer_args.count("--offline"), 1)

    def test_bootstrap_offline_partial_clone_missing_blob_blocks_lazy_fetch_before_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            origin_work = root / "origin-work"
            origin_bare = root / "origin.git"
            cache = root / "Cache"
            source = cache / "source"
            tripwire_marker = root / "lazy-fetch-invoked"
            tripwire = root / "promisor-tripwire.sh"
            home.mkdir()
            origin_work.mkdir()

            def git(*arguments: str, cwd: Path = root) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            git("init", "--quiet", cwd=origin_work)
            git("config", "user.name", "Actanara Test", cwd=origin_work)
            git("config", "user.email", "actanara-test@example.invalid", cwd=origin_work)
            payloads = {
                "pyproject.toml": "[project]\nname='offline-fixture'\nversion='1.0.0'\n",
                "MANIFEST.in": "include LICENSE\n",
                "LICENSE": "fixture\n",
                "config.py": "# fixture\n",
                "install/install.sh": "#!/usr/bin/env zsh\nexit 0\n",
                "advanced/placeholder.txt": "advanced\n",
                "src/placeholder.txt": "src\n",
            }
            for relative, content in payloads.items():
                path = origin_work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            (origin_work / "install" / "install.sh").chmod(0o755)
            git("add", ".", cwd=origin_work)
            git("commit", "--quiet", "-m", "offline partial clone fixture", cwd=origin_work)
            commit = git("rev-parse", "HEAD", cwd=origin_work).stdout.strip()
            git("clone", "--quiet", "--bare", str(origin_work), str(origin_bare))
            git("config", "uploadpack.allowFilter", "true", cwd=origin_bare)
            git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin_bare)
            git(
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--sparse",
                "--no-checkout",
                origin_bare.as_uri(),
                str(source),
            )
            missing_probe_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
            missing_probe = subprocess.run(
                ["git", "-C", str(source), "cat-file", "-e", f"{commit}:install/install.sh"],
                env=missing_probe_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(missing_probe.returncode, 0, "fixture must omit the installer blob")

            tripwire.write_text(
                "#!/bin/sh\n"
                f"touch {str(tripwire_marker)!r}\n"
                "exit 97\n",
                encoding="utf-8",
            )
            tripwire.chmod(0o755)
            source_url = f"ext::{tripwire} %S"
            git("remote", "set-url", "origin", source_url, cwd=source)
            git("config", "protocol.ext.allow", "always", cwd=source)
            config_before = (source / ".git" / "config").read_bytes()

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    source_url,
                    "--ref",
                    commit,
                    "--cache-root",
                    str(cache),
                    "--",
                    "--runtime",
                    str(home / ".actanara"),
                ],
                cwd=root,
                env=self._fresh_bootstrap_env(home),
                text=True,
                capture_output=True,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)
            self.assertIn("离线状态下缺少所需 Actanara 文件", output)
            self.assertFalse(tripwire_marker.exists(), output)
            self.assertEqual((source / ".git" / "config").read_bytes(), config_before)
            self.assertFalse((source / "install" / "install.sh").exists())

    def test_bootstrap_offline_partial_clone_accepts_complete_sparse_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            origin_work = root / "origin-work"
            origin_bare = root / "origin.git"
            cache = root / "Cache"
            source = cache / "source"
            installer_log = root / "installer.log"
            tripwire_marker = root / "lazy-fetch-invoked"
            tripwire = root / "promisor-tripwire.sh"
            home.mkdir()
            origin_work.mkdir()

            def git(*arguments: str, cwd: Path = root) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            git("init", "--quiet", cwd=origin_work)
            git("config", "user.name", "Actanara Test", cwd=origin_work)
            git("config", "user.email", "actanara-test@example.invalid", cwd=origin_work)
            payloads = {
                "pyproject.toml": "[project]\nname='offline-fixture'\nversion='1.0.0'\n",
                "MANIFEST.in": "include LICENSE\n",
                "LICENSE": "fixture\n",
                "config.py": "# fixture\n",
                "install/install.sh": (
                    "#!/usr/bin/env zsh\n"
                    "set -eu\n"
                    "print -r -- \"$@\" > \"${ACTANARA_TEST_INSTALLER_LOG:?}\"\n"
                ),
                "advanced/placeholder.txt": "advanced\n",
                "src/placeholder.txt": "src\n",
                "docs/unneeded.txt": "public-only blob must remain absent\n",
            }
            for relative, content in payloads.items():
                path = origin_work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            (origin_work / "install" / "install.sh").chmod(0o755)
            git("add", ".", cwd=origin_work)
            git("commit", "--quiet", "-m", "offline sparse success fixture", cwd=origin_work)
            commit = git("rev-parse", "HEAD", cwd=origin_work).stdout.strip()
            git("clone", "--quiet", "--bare", str(origin_work), str(origin_bare))
            git("config", "uploadpack.allowFilter", "true", cwd=origin_bare)
            git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin_bare)
            git(
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--sparse",
                "--no-checkout",
                origin_bare.as_uri(),
                str(source),
            )
            git("sparse-checkout", "init", "--no-cone", cwd=source)
            git(
                "sparse-checkout",
                "set",
                "/pyproject.toml",
                "/MANIFEST.in",
                "/LICENSE",
                "/config.py",
                "/install",
                "/advanced",
                "/src",
                cwd=source,
            )
            git("checkout", "--detach", commit, cwd=source)

            no_fetch_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
            required_probe = subprocess.run(
                ["git", "-C", str(source), "cat-file", "-e", f"{commit}:install/install.sh"],
                env=no_fetch_env,
                text=True,
                capture_output=True,
                check=False,
            )
            unneeded_probe = subprocess.run(
                ["git", "-C", str(source), "cat-file", "-e", f"{commit}:docs/unneeded.txt"],
                env=no_fetch_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(required_probe.returncode, 0, required_probe.stderr)
            self.assertNotEqual(unneeded_probe.returncode, 0, "fixture must omit public-only blob")

            tripwire.write_text(
                "#!/bin/sh\n"
                f"touch {str(tripwire_marker)!r}\n"
                "exit 97\n",
                encoding="utf-8",
            )
            tripwire.chmod(0o755)
            source_url = f"ext::{tripwire} %S"
            git("remote", "set-url", "origin", source_url, cwd=source)
            git("config", "protocol.ext.allow", "always", cwd=source)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_TEST_INSTALLER_LOG"] = str(installer_log)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    source_url,
                    "--ref",
                    commit,
                    "--cache-root",
                    str(cache),
                    "--",
                    "--runtime",
                    str(home / ".actanara"),
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("已准备此前下载的文件", output)
            self.assertFalse(tripwire_marker.exists(), output)
            self.assertIn("--offline", installer_log.read_text(encoding="utf-8"))
            self.assertEqual(git("rev-parse", "HEAD", cwd=source).stdout.strip(), commit)
            still_unneeded = subprocess.run(
                ["git", "-C", str(source), "cat-file", "-e", f"{commit}:docs/unneeded.txt"],
                env=no_fetch_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(still_unneeded.returncode, 0, output)
            self.assertFalse(tripwire_marker.exists(), output)

    def test_bootstrap_offline_cached_ref_rejects_mismatched_resolved_object(self):
        source_url = "https://example.invalid/actanara.git"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            source = cache / "source"
            bin_dir = root / "bin"
            git_log = root / "git.log"
            curl_log = root / "curl.log"
            installer_log = root / "installer-args.log"
            fake_git = bin_dir / "git"
            fake_curl = bin_dir / "curl"
            home.mkdir()
            bin_dir.mkdir()
            (source / ".git").mkdir(parents=True)
            self._write_bootstrap_installer_probe(source, installer_log)
            self._write_offline_cache_git(
                fake_git,
                git_log,
                source_url=source_url,
                resolved_commit="b" * 40,
            )
            self._write_bootstrap_command_tripwire(fake_curl, curl_log)
            env = self._fresh_bootstrap_env(home)
            env["ACTANARA_INSTALL_CURL"] = str(fake_curl)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--offline",
                    "--source-url",
                    source_url,
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--yes",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            git_calls = git_log.read_text(encoding="utf-8").splitlines()

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("未能确认所选 Actanara 版本", output)
        self.assertFalse(installer_log.exists())
        self.assertFalse(curl_log.exists(), output)
        self.assertFalse(
            any(re.search(r"(^|\s)(fetch|clone|ls-remote)(\s|$)", call) for call in git_calls),
            git_calls,
        )

    def test_bootstrap_dry_run_source_url_prints_clone_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            cache = Path(tmp) / "Cache"
            home.mkdir()
            env = self._fresh_bootstrap_env(home)
            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--dry-run",
                    "--source-url",
                    "https://example.invalid/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=Path(tmp),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("下载 Actanara", output)
        self.assertIn("准备安装文件", output)
        self.assertIn("已生成不可变源码计划", output)
        self.assertNotIn("启动 Actanara 安装", output)
        self.assertNotIn("sparse-checkout", output)
        self.assertNotIn(f"checkout --detach {IMMUTABLE_TEST_COMMIT}", output)
        self.assertNotIn("install/install.sh --source-root", output)
        self.assertFalse(cache.exists())

    def test_bootstrap_preserves_download_failure_status_without_raw_command_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            fake_git = bin_dir / "git"
            git_log = root / "git.log"
            home.mkdir()
            bin_dir.mkdir()
            self._write_bootstrap_command_tripwire(fake_git, git_log)

            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--source-url",
                    "https://credential@example.invalid/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--dry-run",
                ],
                cwd=root,
                env=self._fresh_bootstrap_env(home),
                text=True,
                capture_output=True,
                check=False,
            )
            bootstrap_log = (cache / "bootstrap.log").read_text(encoding="utf-8")

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 97, output)
        self.assertIn("未能准备 Actanara", output)
        self.assertNotIn("credential@example.invalid", output)
        self.assertNotIn("credential@example.invalid", bootstrap_log)

    def test_bootstrap_fake_git_smoke_acquires_source_and_runs_installer_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_git = bin_dir / "git"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_git(fake_git, log_path)
            env = self._fresh_bootstrap_env(home)
            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--source-url",
                    "https://example.invalid/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--dry-run",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            bootstrap_log_path = cache / "bootstrap.log"
            bootstrap_log = bootstrap_log_path.read_text(encoding="utf-8")
            bootstrap_log_mode = bootstrap_log_path.stat().st_mode & 0o777

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("clone --filter=blob:none --sparse --no-checkout https://example.invalid/actanara.git", log)
        self.assertIn("sparse-checkout init --no-cone", log)
        self.assertIn("sparse-checkout set /pyproject.toml /MANIFEST.in /LICENSE /config.py /install /advanced /src", log)
        self.assertIn("--git-dir=", log)
        self.assertIn("--work-tree=", log)
        self.assertIn(f"checkout --detach {IMMUTABLE_TEST_COMMIT}", log)
        self.assertIn(f"reset --hard {IMMUTABLE_TEST_COMMIT}", log)
        self.assertNotIn("dry-run only", output)
        self.assertEqual(bootstrap_log_mode, 0o600)
        self.assertNotIn("https://example.invalid/actanara.git", bootstrap_log)

    def test_bootstrap_stdin_style_with_source_url_does_not_depend_on_script_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_git = bin_dir / "git"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_git(fake_git, log_path)
            script = BOOTSTRAP.read_text(encoding="utf-8")
            env = self._fresh_bootstrap_env(home)
            env.update(
                {
                    "ACTANARA_INSTALL_SOURCE_URL": "https://example.invalid/actanara.git",
                    "ACTANARA_INSTALL_REF": IMMUTABLE_TEST_COMMIT,
                    "ACTANARA_INSTALL_CACHE_ROOT": str(cache),
                    "ACTANARA_INSTALL_GIT": str(fake_git),
                }
            )
            result = subprocess.run(
                [
                    "zsh",
                    "-c",
                    script,
                    "actanara-bootstrap",
                    "--",
                    "--dry-run",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("clone --filter=blob:none --sparse --no-checkout https://example.invalid/actanara.git", log)
        self.assertIn("sparse-checkout init --no-cone", log)
        self.assertIn("sparse-checkout set /pyproject.toml /MANIFEST.in /LICENSE /config.py /install /advanced /src", log)
        self.assertIn(f"checkout --detach {IMMUTABLE_TEST_COMMIT}", log)
        self.assertIn(f"reset --hard {IMMUTABLE_TEST_COMMIT}", log)
        self.assertNotIn("dry-run only", output)

    def test_bootstrap_stdin_style_uses_hosted_default_source_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_git = bin_dir / "git"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_git(fake_git, log_path)
            script = BOOTSTRAP.read_text(encoding="utf-8")
            env = self._fresh_bootstrap_env(home)
            env.update(
                {
                    "ACTANARA_INSTALL_CACHE_ROOT": str(cache),
                    "ACTANARA_INSTALL_GIT": str(fake_git),
                    "ACTANARA_INSTALL_REF": IMMUTABLE_TEST_COMMIT,
                }
            )
            result = subprocess.run(
                [
                    "zsh",
                    "-c",
                    script,
                    "actanara-bootstrap",
                    "--",
                    "--dry-run",
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("clone --filter=blob:none --sparse --no-checkout https://github.com/Neo-Isshin/actanara.git", log)
        self.assertIn("sparse-checkout init --no-cone", log)
        self.assertIn("sparse-checkout set /pyproject.toml /MANIFEST.in /LICENSE /config.py /install /advanced /src", log)
        self.assertIn(f"checkout --detach {IMMUTABLE_TEST_COMMIT}", log)
        self.assertNotIn("dry-run only", output)

    def test_bootstrap_clean_home_fake_git_fake_python_non_dry_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            cache = root / "Cache"
            runtime = home / ".actanara"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_git = bin_dir / "git"
            fake_python = bin_dir / "python3"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_git(fake_git, log_path)
            self._write_fake_python(fake_python, log_path)
            env = self._fresh_bootstrap_env(home)
            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--source-url",
                    "https://example.invalid/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--runtime",
                    str(runtime),
                    "--python",
                    str(fake_python),
                    "--no-scheduler",
                    "--no-dashboard-server",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            runtime_exists = runtime.resolve().exists()
            profile_text = (home / ".zprofile").read_text(encoding="utf-8")

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertTrue(runtime_exists)
        self.assertIn("clone --filter=blob:none --sparse --no-checkout https://example.invalid/actanara.git", log)
        self.assertIn("sparse-checkout init --no-cone", log)
        self.assertIn("sparse-checkout set /pyproject.toml /MANIFEST.in /LICENSE /config.py /install /advanced /src", log)
        self.assertIn(f"checkout --detach {IMMUTABLE_TEST_COMMIT}", log)
        self.assertIn(f"reset --hard {IMMUTABLE_TEST_COMMIT}", log)
        self.assertIn("-m venv", log)
        self.assertNotIn("-m pip install", log)
        self.assertIn("dependency_contract.py materialize-cache", log)
        self.assertIn("dependency_contract.py install", log)
        self.assertIn("dependency_contract.py write-marker", log)
        self.assertIn("dependency_contract.py verify-marker", log)
        self.assertIn("onboarding runtime-apply", log)
        self.assertNotIn("--scheduler-register-apply", log)
        self.assertIn("# >>> actanara installer PATH >>>", profile_text)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', profile_text)

    def test_installer_shell_path_update_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Home"
            runtime = home / ".actanara"
            cache = root / "Cache"
            bin_dir = root / "bin"
            log_path = root / "commands.log"
            fake_git = bin_dir / "git"
            fake_python = bin_dir / "python3"
            home.mkdir()
            bin_dir.mkdir()
            self._write_fake_git(fake_git, log_path)
            self._write_fake_python(fake_python, log_path)
            env = self._fresh_bootstrap_env(home)
            result = subprocess.run(
                [
                    "zsh",
                    str(BOOTSTRAP),
                    "--source-url",
                    "https://example.invalid/actanara.git",
                    "--ref",
                    IMMUTABLE_TEST_COMMIT,
                    "--cache-root",
                    str(cache),
                    "--git",
                    str(fake_git),
                    "--",
                    "--runtime",
                    str(runtime),
                    "--python",
                    str(fake_python),
                    "--no-scheduler",
                    "--no-dashboard-server",
                    "--no-shell-path",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            profile_exists = (home / ".zprofile").exists()

        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertFalse(profile_exists)
        self.assertNotIn("Shell PATH update skipped by --no-shell-path", output)


if __name__ == "__main__":
    unittest.main()
