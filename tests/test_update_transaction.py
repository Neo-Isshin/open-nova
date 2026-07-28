import copy
import hashlib
import http.server
import json
import os
import plistlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "install" / "update_transaction.py"


class UpdateTransactionTests(unittest.TestCase):
    BASE_MIGRATION_VERSION = "0001_initial"
    BASE_MIGRATION_BODY = "CREATE TABLE fixture_migration (id INTEGER PRIMARY KEY);\n"
    MIGRATION_POLICY = "rollback-compatible-additive-only"
    PRE_COMMIT_WRITER_CONTRACT = "prior-reader-compatible-v1"

    def _run(
        self,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(HELPER), *args],
            env={**os.environ, **(env or {})},
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if check and result.returncode != 0:
            self.fail(result.stdout + result.stderr)
        return result

    def _migration_set_sha256(self, records: list[dict[str, str]]) -> str:
        digest = hashlib.sha256()
        for record in records:
            digest.update(record["version"].encode("ascii"))
            digest.update(b"\0")
            digest.update(record["sha256"].encode("ascii"))
            digest.update(b"\0")
            digest.update(record["rollbackClass"].encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _dependency_marker_payload(self) -> dict[str, object]:
        environment = {
            "implementation": "cpython",
            "pythonMajorMinor": "3.12",
            "abi": "cpython-312",
            "platformFamily": "linux",
            "architecture": "x86_64",
            "minimumMacOS": None,
        }
        # environmentId is the stable lock selection key, not a mechanical
        # concatenation of the environment identity fields.
        environment_id = "linux-cpython312-x86-64"
        profiles = ["runtime"]
        direct_dependencies = [
            {"profile": "runtime", "requirements": ["fastapi==0.116.1"]}
        ]
        distributions = [
            {
                "name": "fastapi",
                "version": "0.116.1",
                "hashes": ["sha256:" + "b" * 64],
            }
        ]
        lock_sha256 = "a" * 64
        fingerprint_payload = {
            "schemaVersion": 1,
            "algorithm": "actanara-runtime-dependencies-v1",
            "runtimeEnvironment": {
                "implementation": environment["implementation"],
                "pythonMajorMinor": environment["pythonMajorMinor"],
                "abi": environment["abi"],
                "platformFamily": environment["platformFamily"],
                "architecture": environment["architecture"],
                "environmentId": environment_id,
            },
            "lockEnvironment": environment,
            "profiles": profiles,
            "directDependencies": direct_dependencies,
            "runtimeLockSha256": lock_sha256,
            "resolvedDistributions": distributions,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schemaVersion": 1,
            "product": "actanara",
            "fingerprintAlgorithm": "actanara-runtime-dependencies-v1",
            "dependencyFingerprint": fingerprint,
            "lockSha256": lock_sha256,
            "environmentId": environment_id,
            "lockEnvironment": environment,
            "profiles": profiles,
            "directDependencies": direct_dependencies,
            "distributions": distributions,
        }

    def _write_dependency_marker(
        self,
        venv: Path,
        *,
        payload: dict[str, object] | None = None,
        raw: bytes | None = None,
        mode: int = 0o444,
    ) -> Path:
        marker = venv / ".actanara-dependencies.json"
        marker.unlink(missing_ok=True)
        if raw is None:
            raw = (
                json.dumps(payload or self._dependency_marker_payload(), sort_keys=True)
                + "\n"
            ).encode("utf-8")
        marker.write_bytes(raw)
        marker.chmod(mode)
        return marker

    def _write_prior_migrations(
        self,
        source: Path,
        migrations: list[tuple[str, str]],
    ) -> None:
        migrations_root = source / "src" / "data_foundation" / "migrations"
        migrations_root.mkdir(parents=True, exist_ok=True)
        for existing in migrations_root.glob("*.sql"):
            existing.unlink()
        for version, body in migrations:
            (migrations_root / f"{version}.sql").write_text(body, encoding="utf-8")

    def _write_candidate_source(
        self,
        source: Path,
        migrations: list[tuple[str, str, str]],
        *,
        include_contract: bool = True,
    ) -> None:
        migrations_root = source / "src" / "data_foundation" / "migrations"
        migrations_root.mkdir(parents=True, exist_ok=True)
        for existing in migrations_root.glob("*.sql"):
            existing.unlink()
        records: list[dict[str, str]] = []
        for version, body, rollback_class in sorted(migrations):
            migration_path = migrations_root / f"{version}.sql"
            migration_path.write_text(body, encoding="utf-8")
            records.append(
                {
                    "version": version,
                    "sha256": hashlib.sha256(migration_path.read_bytes()).hexdigest(),
                    "rollbackClass": rollback_class,
                }
            )

        contract_path = source / "src" / "data_foundation" / "migration_compatibility.json"
        contract_path.unlink(missing_ok=True)
        compatibility = {
            "schemaVersion": 1,
            "policy": self.MIGRATION_POLICY,
            "preCommitWriterContract": self.PRE_COMMIT_WRITER_CONTRACT,
            "minimumReadableSchema": "unversioned",
            "maximumReadableSchema": records[-1]["version"],
            "migrationSetSha256": self._migration_set_sha256(records),
            "migrations": records,
        }
        if include_contract:
            contract_path.write_text(
                json.dumps(
                    {
                        key: compatibility[key]
                        for key in (
                            "schemaVersion",
                            "policy",
                            "preCommitWriterContract",
                            "minimumReadableSchema",
                            "maximumReadableSchema",
                            "migrations",
                        )
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        service_paths = [
            source / "advanced" / "dashboard" / "dashboard_launch_agent.py",
            source / "advanced" / "dashboard" / "rag_server_launch_agent.py",
            source / "advanced" / "pipeline" / "run_daily_pipeline.py",
            source / "advanced" / "pipeline" / "run_dashboard_foundation_refresh.py",
        ]
        for service_path in service_paths:
            service_path.parent.mkdir(parents=True, exist_ok=True)
            service_path.write_text("# fixture service entrypoint\n", encoding="utf-8")
        (source / "src" / "dashboard").mkdir(parents=True, exist_ok=True)

        payload_paths = [
            source / "pyproject.toml",
            *service_paths,
            *sorted(migrations_root.glob("*.sql")),
        ]
        if include_contract:
            payload_paths.append(contract_path)
        payload_records: list[dict[str, object]] = []
        aggregate = hashlib.sha256()
        for path in sorted(payload_paths, key=lambda item: item.relative_to(source).as_posix()):
            content = path.read_bytes()
            relative = path.relative_to(source).as_posix()
            file_hash = hashlib.sha256(content).hexdigest()
            payload_records.append(
                {"path": relative, "sha256": file_hash, "size": len(content)}
            )
            aggregate.update(relative.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(file_hash.encode("ascii"))
            aggregate.update(b"\n")
        manifest = {
            "schemaVersion": 2,
            "product": "actanara",
            "sourceLocator": {
                "kind": "login-home-relative",
                "pathComponents": ["fixture", "candidate-source"],
            },
            "deployedSourceLocator": {
                "kind": "runtime-relative",
                "pathComponents": ["app", "source"],
            },
            "releaseLocator": {
                "kind": "runtime-relative",
                "pathComponents": ["app", "releases", "fixture-tx"],
            },
            "deploymentMode": "release-symlink",
            "copiedAt": "2026-07-11T00:00:00+00:00",
            "pyprojectVersion": "1",
            "git": {
                "available": False,
                "commit": None,
                "branch": None,
                "remote": None,
                "dirty": None,
            },
            "cleanScan": {
                "status": "passed",
                "scanner": "data_foundation.release_clean.repository_clean_deployment_check",
                "scannedFiles": len(payload_records),
                "findingCount": 0,
            },
            "payload": {
                "fileCount": len(payload_records),
                "files": payload_records,
                "sha256": aggregate.hexdigest(),
            },
        }
        manifest["databaseCompatibility"] = compatibility
        (source / ".actanara-runtime-source.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _fixture(self, root: Path) -> dict[str, Path]:
        home = root / "Home"
        runtime = home / ".actanara"
        old_source = runtime / "app" / "releases" / "old"
        candidate_source = root / "candidate-source-template"
        candidate_venv = root / "candidate-venv-template"
        old_venv = runtime / ".venv"
        for path in (
            old_source,
            candidate_source,
            candidate_venv / "bin",
            old_venv / "bin",
            runtime / "config",
            runtime / "data",
            runtime / "bin",
            home / ".local" / "bin",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (runtime / "app" / "source").symlink_to("releases/old")
        (old_source / "pyproject.toml").write_text('[project]\nname="old"\nversion="0"\n', encoding="utf-8")
        (old_source / ".actanara-runtime-source.json").write_text('{"release":"old"}\n', encoding="utf-8")
        self._write_prior_migrations(
            old_source,
            [(self.BASE_MIGRATION_VERSION, self.BASE_MIGRATION_BODY)],
        )
        candidate_pyproject = candidate_source / "pyproject.toml"
        candidate_pyproject.write_text('[project]\nname="candidate"\nversion="1"\n', encoding="utf-8")
        self._write_candidate_source(
            candidate_source,
            [
                (
                    self.BASE_MIGRATION_VERSION,
                    self.BASE_MIGRATION_BODY,
                    "rollback-compatible-additive",
                )
            ],
        )
        (old_venv / "bin" / "python").write_text("old-venv\n", encoding="utf-8")
        (candidate_venv / "bin" / "python").write_text("candidate-venv\n", encoding="utf-8")
        self._write_dependency_marker(old_venv)
        self._write_dependency_marker(candidate_venv)
        (runtime / "config" / "settings.json").write_text('{"dashboard":{"port":42173}}\n', encoding="utf-8")
        (runtime / "config" / "runtime.json").write_text('{"runtime":"old"}\n', encoding="utf-8")
        database = runtime / "data" / "actanara_data.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone(), ("wal",))
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.execute(
                "CREATE TABLE update_evidence (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE)"
            )
            connection.execute("INSERT INTO update_evidence(value) VALUES ('before-update')")
            connection.execute(
                "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self.BASE_MIGRATION_VERSION, "2026-07-11T00:00:00+00:00"),
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        (runtime / "bin" / "actanara").write_text("old-cli\n", encoding="utf-8")
        return {
            "home": home,
            "runtime": runtime,
            "candidate_source": candidate_source,
            "candidate_venv": candidate_venv,
            "settings": runtime / "config" / "settings.json",
            "database": database,
            "location": root / "location.json",
            "user_cli": home / ".local" / "bin" / "actanara",
            "desktop": home / "Desktop" / "Actanara",
            "profile": home / ".zprofile",
        }

    def _make_legacy_pointers_concrete(
        self,
        fixture: dict[str, Path],
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        source = fixture["runtime"] / "app" / "source"
        prior_source = source.resolve(strict=True)
        source.unlink()
        prior_source.rename(source)
        venv = fixture["runtime"] / ".venv"
        source_stat = source.stat(follow_symlinks=False)
        venv_stat = venv.stat(follow_symlinks=False)
        return (
            (source_stat.st_dev, source_stat.st_ino),
            (venv_stat.st_dev, venv_stat.st_ino),
        )

    def _database_values(self, path: Path) -> list[str]:
        connection = sqlite3.connect(path)
        try:
            return [
                str(row[0])
                for row in connection.execute("SELECT value FROM update_evidence ORDER BY id")
            ]
        finally:
            connection.close()

    def _database_integrity(self, path: Path) -> str:
        connection = sqlite3.connect(path)
        try:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _start_health_server(
        self,
        *,
        source_commit: str | None = None,
    ) -> tuple[http.server.ThreadingHTTPServer, threading.Thread, int]:
        class HealthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                payload = {"status": "ok"}
                if source_commit is not None:
                    payload["sourceCommit"] = source_commit
                body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
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
        return server, thread, int(server.server_address[1])

    def _write_stateful_fake_launchctl(
        self,
        path: Path,
        *,
        state_dir: Path,
        calls_path: Path,
    ) -> None:
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"state = Path({str(state_dir)!r})\n"
            f"calls = Path({str(calls_path)!r})\n"
            "with calls.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "command = sys.argv[1]\n"
            "if command == 'print':\n"
            "    label = sys.argv[2].rsplit('/', 1)[-1]\n"
            "    value = state / label\n"
            "    if not value.is_file():\n"
            "        raise SystemExit(113)\n"
            "    print('state = ' + value.read_text(encoding='utf-8').strip())\n"
            "elif command == 'bootout':\n"
            "    label = sys.argv[2].rsplit('/', 1)[-1]\n"
            "    (state / label).unlink(missing_ok=True)\n"
            "elif command == 'bootstrap':\n"
            "    label = Path(sys.argv[-1]).stem\n"
            "    (state / label).write_text('waiting\\n', encoding='utf-8')\n"
            "elif command == 'kickstart':\n"
            "    label = sys.argv[-1].rsplit('/', 1)[-1]\n"
            "    (state / label).write_text('running\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_stateful_fake_systemctl(
        self,
        path: Path,
        *,
        state_path: Path,
        calls_path: Path,
    ) -> None:
        path.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"state_path = Path({str(state_path)!r})\n"
            f"calls_path = Path({str(calls_path)!r})\n"
            "args = sys.argv[1:]\n"
            "if args and args[0] == '--user':\n"
            "    args = args[1:]\n"
            "with calls_path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(args) + '\\n')\n"
            "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
            "command = args[0]\n"
            "name = args[1] if len(args) > 1 else ''\n"
            "unit = state.get('units', {}).get(name, {'enabled': 'not-found', 'active': 'not-found'})\n"
            "if command == 'is-enabled':\n"
            "    print(unit.get('enabled', 'not-found'))\n"
            "    raise SystemExit(0 if unit.get('enabled') in {'enabled', 'enabled-runtime', 'static', 'indirect'} else (4 if unit.get('enabled') == 'not-found' else 1))\n"
            "if command == 'is-active':\n"
            "    print(unit.get('active', 'not-found'))\n"
            "    raise SystemExit(0 if unit.get('active') == 'active' else (4 if unit.get('active') == 'not-found' else 3))\n"
            "if command in {'start', 'stop'}:\n"
            "    failure_key = 'failStart' if command == 'start' else 'failStop'\n"
            "    if name in state.get(failure_key, []):\n"
            "        raise SystemExit(1)\n"
            "    unit['active'] = 'active' if command == 'start' else 'inactive'\n"
            "    state.setdefault('units', {})[name] = unit\n"
            "    temporary = state_path.with_suffix('.tmp')\n"
            "    temporary.write_text(json.dumps(state, sort_keys=True) + '\\n', encoding='utf-8')\n"
            "    os.replace(temporary, state_path)\n"
            "    raise SystemExit(0)\n"
            "if command in {'enable', 'disable'}:\n"
            "    names = [value for value in args[1:] if not value.startswith('-')]\n"
            "    for selected in names:\n"
            "        selected_unit = state.get('units', {}).get(selected, {'enabled': 'disabled', 'active': 'inactive'})\n"
            "        if command == 'enable':\n"
            "            selected_unit['enabled'] = 'enabled-runtime' if '--runtime' in args else 'enabled'\n"
            "        elif '--runtime' in args:\n"
            "            if selected_unit.get('enabled') == 'enabled-runtime':\n"
            "                selected_unit['enabled'] = 'disabled'\n"
            "        elif selected_unit.get('enabled') == 'enabled':\n"
            "            selected_unit['enabled'] = 'disabled'\n"
            "        state.setdefault('units', {})[selected] = selected_unit\n"
            "    temporary = state_path.with_suffix('.tmp')\n"
            "    temporary.write_text(json.dumps(state, sort_keys=True) + '\\n', encoding='utf-8')\n"
            "    os.replace(temporary, state_path)\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_dashboard_binding_plist(
        self,
        path: Path,
        *,
        label: str,
        runtime: Path,
        source_root: Path,
        venv_root: Path,
        extra_payload: dict[str, object] | None = None,
        command: str | None = None,
    ) -> None:
        python = venv_root / "bin" / "python"
        payload: dict[str, object] = {
            "Label": label,
            "ProgramArguments": [
                "/bin/zsh",
                "-lc",
                command
                or (
                    f"cd {source_root} && exec {python} -m uvicorn app.main:app "
                    f"--app-dir {source_root / 'src' / 'dashboard'} --host 127.0.0.1 --port 42173"
                ),
            ],
            "EnvironmentVariables": {
                "ACTANARA_DASHBOARD_PROJECT_ROOT": str(source_root),
                "ACTANARA_DASHBOARD_PYTHON": str(python),
                "ACTANARA_HOME": str(runtime),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": f"{source_root}:{source_root / 'src'}:{source_root / 'src' / 'dashboard'}",
            },
        }
        if extra_payload:
            payload.update(extra_payload)
        with path.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=False)

    def _write_runtime_plist(self, path: Path, *, label: str, runtime: Path) -> None:
        self._write_dashboard_binding_plist(
            path,
            label=label,
            runtime=runtime,
            source_root=runtime / "app" / "source",
            venv_root=runtime / ".venv",
        )

    def _begin_unloaded_darwin(
        self,
        fixture: dict[str, Path],
        root: Path,
    ) -> tuple[Path, Path]:
        state_dir = root / "launchctl-state"
        state_dir.mkdir()
        calls_path = root / "launchctl-calls.log"
        fake_launchctl = root / "launchctl"
        self._write_stateful_fake_launchctl(
            fake_launchctl,
            state_dir=state_dir,
            calls_path=calls_path,
        )
        return (
            self._begin(
                fixture,
                owner_pid=os.getpid(),
                platform="Darwin",
                launchctl=str(fake_launchctl),
                uid=0,
            ),
            calls_path,
        )

    def _begin_only_result(
        self,
        fixture: dict[str, Path],
        *,
        mode: str,
        platform: str = "Linux",
        launchctl: str = "/usr/bin/true",
        systemctl: str = "",
        systemd_units: tuple[str, ...] = (),
        expected_systemd_hashes: dict[str, str] | None = None,
        xdg_config_home: Path | None = None,
        tx_id: str = "fixture-tx",
        uid: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            "begin",
            "--runtime",
            str(fixture["runtime"]),
            "--home",
            str(fixture["home"]),
            "--source-pointer",
            str(fixture["runtime"] / "app" / "source"),
            "--venv-pointer",
            str(fixture["runtime"] / ".venv"),
            "--mode",
            mode,
            "--tx-id",
            tx_id,
            "--owner-pid",
            str(os.getpid()),
            "--platform",
            platform,
            "--launchctl",
            launchctl,
            *(["--systemctl", systemctl] if systemctl else []),
            *(argument for name in systemd_units for argument in ("--systemd-unit", name)),
            *(
                argument
                for name, digest in (expected_systemd_hashes or {}).items()
                for argument in ("--expected-systemd-unit-sha256", f"{name}={digest}")
            ),
            "--uid",
            str(uid),
            check=False,
            env=(
                {
                    "XDG_CONFIG_HOME": str(
                        xdg_config_home or fixture["home"] / ".config"
                    )
                }
                if platform == "Linux"
                else None
            ),
        )

    def _begin(
        self,
        fixture: dict[str, Path],
        *,
        owner_pid: int,
        platform: str = "Linux",
        launchctl: str = "/usr/bin/true",
        systemctl: str = "",
        systemd_units: tuple[str, ...] = (),
        uid: int = 0,
        materialize_candidates: bool = True,
        mode: str = "upgrade",
        settings_only_profile_evidence: bool = False,
        xdg_config_home: Path | None = None,
    ) -> Path:
        settings_sha256 = hashlib.sha256(fixture["settings"].read_bytes()).hexdigest()
        active_venv_target = (fixture["runtime"] / ".venv").resolve()
        active_marker = active_venv_target / ".actanara-dependencies.json"
        marker_status = "trusted" if active_marker.exists() or active_marker.is_symlink() else "missing"
        marker_args: list[str] = []
        if marker_status == "trusted" and active_marker.is_file() and not active_marker.is_symlink():
            marker_args = [
                "--expected-active-marker-sha256",
                hashlib.sha256(active_marker.read_bytes()).hexdigest(),
            ]
        profile_evidence_args = ["--expected-settings-sha256", settings_sha256]
        if settings_only_profile_evidence:
            profile_evidence_args.append("--settings-only-profile-evidence")
        else:
            profile_evidence_args.extend(
                [
                    "--expected-active-venv-target",
                    str(active_venv_target),
                    "--expected-active-marker-status",
                    marker_status,
                    *marker_args,
                ]
            )
        result = self._run(
            "begin",
            "--runtime",
            str(fixture["runtime"]),
            "--home",
            str(fixture["home"]),
            "--source-pointer",
            str(fixture["runtime"] / "app" / "source"),
            "--venv-pointer",
            str(fixture["runtime"] / ".venv"),
            *profile_evidence_args,
            "--mode",
            mode,
            "--tx-id",
            "fixture-tx",
            "--owner-pid",
            str(owner_pid),
            "--platform",
            platform,
            "--launchctl",
            launchctl,
            *(["--systemctl", systemctl] if systemctl else []),
            *(argument for name in systemd_units for argument in ("--systemd-unit", name)),
            "--uid",
            str(uid),
            env=(
                {
                    "XDG_CONFIG_HOME": str(
                        xdg_config_home or fixture["home"] / ".config"
                    )
                }
                if platform == "Linux"
                else None
            ),
        )
        journal = Path(result.stdout.strip())
        if not materialize_candidates:
            return journal
        source_template = fixture["candidate_source"]
        source_temporary = Path(
            self._run(
                "reserve-artifact",
                "--state",
                str(journal),
                "--kind",
                "source-temp",
            ).stdout.strip()
        )
        shutil.copytree(source_template, source_temporary, dirs_exist_ok=True, symlinks=True)
        source_candidate = Path(
            self._run(
                "promote-source-artifact",
                "--state",
                str(journal),
            ).stdout.strip()
        )
        fixture["candidate_source"] = source_candidate
        if mode in {"upgrade", "repair"}:
            venv_template = fixture["candidate_venv"]
            venv_candidate = Path(
                self._run(
                    "reserve-artifact",
                    "--state",
                    str(journal),
                    "--kind",
                    "venv",
                ).stdout.strip()
            )
            shutil.copytree(venv_template, venv_candidate, dirs_exist_ok=True, symlinks=True)
            fixture["candidate_venv"] = venv_candidate
        return journal

    def _mark_transaction_owner_dead(self, journal: Path) -> None:
        state = json.loads(journal.read_text(encoding="utf-8"))
        state["ownerPid"] = 999_999_999
        state["ownerProcessIdentity"] = "test-dead-owner"
        journal.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

        owner_path = journal.parent / "owner.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner["ownerPid"] = state["ownerPid"]
        owner["ownerProcessIdentity"] = state["ownerProcessIdentity"]
        # owner.json and the Runtime lock are paired hard links.  Rewrite the
        # shared inode in place so recovery still validates their provenance.
        with owner_path.open("r+", encoding="utf-8") as handle:
            handle.seek(0)
            handle.write(json.dumps(owner, sort_keys=True) + "\n")
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

    def _record_and_verify_source_candidate(
        self,
        fixture: dict[str, Path],
        journal: Path,
        *,
        allow_legacy_repair: bool = False,
    ) -> None:
        self._run(
            "record-candidate",
            "--state",
            str(journal),
            "--kind",
            "source",
            "--candidate",
            str(fixture["candidate_source"]),
        )
        compatibility_args = [
            "verify-migration-compatibility",
            "--state",
            str(journal),
        ]
        if allow_legacy_repair:
            compatibility_args.append("--allow-legacy-repair")
        self._run(*compatibility_args)
        self._run(
            "record-candidate",
            "--state",
            str(journal),
            "--kind",
            "venv",
            "--candidate",
            str(fixture["candidate_venv"]),
        )

    def _prepare_stopped_candidate(
        self,
        fixture: dict[str, Path],
        journal: Path,
        *,
        allow_legacy_repair: bool = False,
    ) -> None:
        self._record_and_verify_source_candidate(
            fixture,
            journal,
            allow_legacy_repair=allow_legacy_repair,
        )
        self._run("stop", "--state", str(journal))
        self._run(
            "capture-mutable",
            "--state",
            str(journal),
            "--location",
            str(fixture["location"]),
            "--cli-shim",
            str(fixture["runtime"] / "bin" / "actanara"),
            "--user-cli-shim",
            str(fixture["user_cli"]),
            "--desktop-link",
            str(fixture["desktop"]),
            "--shell-profile",
            str(fixture["profile"]),
        )

    def _prepare_and_promote(self, fixture: dict[str, Path], journal: Path) -> None:
        self._prepare_stopped_candidate(fixture, journal)
        self._run("normalize-service-plists", "--state", str(journal))
        self._run("promote", "--state", str(journal))

    def _commit_repair_transaction(
        self,
        fixture: dict[str, Path],
    ) -> Path:
        journal = self._begin(
            fixture,
            owner_pid=os.getpid(),
            mode="repair",
            settings_only_profile_evidence=True,
        )
        self._prepare_stopped_candidate(
            fixture,
            journal,
            allow_legacy_repair=True,
        )
        self._run("normalize-service-plists", "--state", str(journal))
        self._run("promote", "--state", str(journal))
        self._run("commit-repair", "--state", str(journal))
        return journal

    def test_begin_preserves_preexisting_reserved_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            occupied = fixture["runtime"] / "app" / "venvs" / "fixture-tx"
            occupied.mkdir(parents=True)
            sentinel = occupied / "operator-owned.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Linux",
                "--launchctl",
                "/usr/bin/true",
                "--uid",
                "0",
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_linux_begin_rejects_nonmanaged_or_missing_active_systemd_unit(self):
        for definition in ("operator-owned\n", None):
            with self.subTest(definition=definition), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self._fixture(root)
                unit_root = fixture["home"] / ".config" / "systemd" / "user"
                unit_root.mkdir(parents=True)
                unit_name = "actanara-dashboard.service"
                if definition is not None:
                    (unit_root / unit_name).write_text(definition, encoding="utf-8")
                state_path = root / "systemctl-state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "units": {
                                unit_name: {"enabled": "enabled", "active": "active"}
                            }
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                fake = root / "systemctl"
                self._write_stateful_fake_systemctl(
                    fake,
                    state_path=state_path,
                    calls_path=root / "systemctl-calls.log",
                )

                result = self._begin_only_result(
                    fixture,
                    mode="upgrade",
                    systemctl=str(fake),
                    systemd_units=(unit_name,),
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertFalse(
                    (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
                )
                self.assertIn(
                    "non-Actanara" if definition is not None else "unmanaged definition",
                    result.stderr,
                )

    def test_linux_systemd_state_vector_survives_forward_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            unit_root = fixture["home"] / ".config" / "systemd" / "user"
            unit_root.mkdir(parents=True)
            units = (
                "actanara-dashboard.service",
                "actanara.daily-pipeline.timer",
            )
            for name in units:
                (unit_root / name).write_text(
                    "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                    encoding="utf-8",
                )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "units": {
                            units[0]: {"enabled": "enabled", "active": "active"},
                            units[1]: {"enabled": "disabled", "active": "inactive"},
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            calls = root / "systemctl-calls.log"
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(fake, state_path=state_path, calls_path=calls)
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                systemctl=str(fake),
                systemd_units=units,
            )

            self._prepare_and_promote(fixture, journal)
            self._run("restore-services", "--state", str(journal))
            self._run("verify", "--state", str(journal))
            self._run("commit", "--state", str(journal))

            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                current["units"],
                {
                    units[0]: {"enabled": "enabled", "active": "active"},
                    units[1]: {"enabled": "disabled", "active": "inactive"},
                },
            )
            call_lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertIn("stop actanara-dashboard.service", call_lines)
            self.assertIn("start actanara-dashboard.service", call_lines)
            self.assertNotIn("stop actanara.daily-pipeline.timer", call_lines)
            self.assertNotIn("start actanara.daily-pipeline.timer", call_lines)
            self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "committed")

    def test_linux_repair_persistently_isolates_enabled_units_and_rollback_restores_them(self):
        for original_enable_state in ("enabled", "enabled-runtime"):
            with self.subTest(enable_state=original_enable_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self._fixture(root)
                unit_root = fixture["home"] / ".config" / "systemd" / "user"
                unit_root.mkdir(parents=True)
                name = "actanara-dashboard.service"
                (unit_root / name).write_text(
                    "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                    encoding="utf-8",
                )
                state_path = root / "systemctl-state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "units": {
                                name: {
                                    "enabled": original_enable_state,
                                    "active": "active",
                                }
                            }
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                calls = root / "systemctl-calls.log"
                fake = root / "systemctl"
                self._write_stateful_fake_systemctl(
                    fake,
                    state_path=state_path,
                    calls_path=calls,
                )
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    systemctl=str(fake),
                    systemd_units=(name,),
                    mode="repair",
                    settings_only_profile_evidence=True,
                )

                self._prepare_stopped_candidate(
                    fixture,
                    journal,
                    allow_legacy_repair=True,
                )
                isolated = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    isolated["units"][name],
                    {"enabled": "disabled", "active": "inactive"},
                )
                self._run("rollback", "--state", str(journal))

                restored = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(restored["units"][name]["enabled"], original_enable_state)
                self.assertEqual(restored["units"][name]["active"], "active")
                call_lines = calls.read_text(encoding="utf-8").splitlines()
                expected_disable = (
                    f"disable --runtime {name}"
                    if original_enable_state == "enabled-runtime"
                    else f"disable {name}"
                )
                self.assertIn(expected_disable, call_lines)
                self.assertLess(
                    call_lines.index(expected_disable),
                    call_lines.index(f"stop {name}"),
                )
                expected_enable = (
                    f"enable --runtime {name}"
                    if original_enable_state == "enabled-runtime"
                    else f"enable {name}"
                )
                self.assertIn(expected_enable, call_lines)

    def test_linux_committed_repair_restores_exact_retained_systemd_vector(self):
        vectors = (
            ("enabled", "active"),
            ("enabled", "inactive"),
            ("enabled-runtime", "active"),
            ("enabled-runtime", "inactive"),
            ("disabled", "active"),
            ("disabled", "inactive"),
        )
        for enable_state, active_state in vectors:
            with (
                self.subTest(enable_state=enable_state, active_state=active_state),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                fixture = self._fixture(root)
                unit_root = fixture["home"] / ".config" / "systemd" / "user"
                unit_root.mkdir(parents=True)
                name = "actanara-dashboard.service"
                (unit_root / name).write_text(
                    "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                    encoding="utf-8",
                )
                state_path = root / "systemctl-state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "units": {
                                name: {
                                    "enabled": enable_state,
                                    "active": active_state,
                                }
                            }
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                fake = root / "systemctl"
                self._write_stateful_fake_systemctl(
                    fake,
                    state_path=state_path,
                    calls_path=root / "systemctl-calls.log",
                )
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    systemctl=str(fake),
                    systemd_units=(name,),
                    mode="repair",
                    settings_only_profile_evidence=True,
                )
                self._prepare_stopped_candidate(
                    fixture,
                    journal,
                    allow_legacy_repair=True,
                )
                self._run("normalize-service-plists", "--state", str(journal))
                self._run("promote", "--state", str(journal))
                self._run("commit-repair", "--state", str(journal))

                # Model an install handoff that temporarily left a persistent,
                # active unit; the repair restore must still converge exactly.
                current = json.loads(state_path.read_text(encoding="utf-8"))
                current["units"][name] = {
                    "enabled": "enabled",
                    "active": "active",
                }
                state_path.write_text(
                    json.dumps(current, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                self._run(
                    "restore-repair-services",
                    "--state",
                    str(journal),
                    "--unit",
                    name,
                )
                self._run(
                    "restore-repair-services",
                    "--state",
                    str(journal),
                    "--unit",
                    name,
                )

                restored = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    restored["units"][name],
                    {"enabled": enable_state, "active": active_state},
                )
                repair_state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertTrue(repair_state["repairSystemdRestoreComplete"])
                self._run("complete-repair", "--state", str(journal))

    def test_linux_committed_repair_systemd_restore_resumes_after_armed_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            unit_root = fixture["home"] / ".config" / "systemd" / "user"
            unit_root.mkdir(parents=True)
            name = "actanara-dashboard.service"
            (unit_root / name).write_text(
                "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                encoding="utf-8",
            )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps(
                    {"units": {name: {"enabled": "enabled-runtime", "active": "active"}}}
                )
                + "\n",
                encoding="utf-8",
            )
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(
                fake,
                state_path=state_path,
                calls_path=root / "systemctl-calls.log",
            )
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                systemctl=str(fake),
                systemd_units=(name,),
                mode="repair",
                settings_only_profile_evidence=True,
            )
            self._prepare_stopped_candidate(
                fixture,
                journal,
                allow_legacy_repair=True,
            )
            self._run("normalize-service-plists", "--state", str(journal))
            self._run("promote", "--state", str(journal))
            self._run("commit-repair", "--state", str(journal))

            failed = self._run(
                "restore-repair-services",
                "--state",
                str(journal),
                "--unit",
                name,
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_FAIL_PHASE": "repair-systemd-restore-armed",
                },
            )
            self.assertEqual(failed.returncode, 70, failed.stdout + failed.stderr)
            armed = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(armed["repairSystemdRestoreComplete"])

            self._run(
                "restore-repair-services",
                "--state",
                str(journal),
                "--unit",
                name,
            )
            restored = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                restored["units"][name],
                {"enabled": "enabled-runtime", "active": "active"},
            )

    def test_linux_begin_honors_xdg_config_home_for_systemd_unit_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            config_home = root / "operator-config"
            unit_root = config_home / "systemd" / "user"
            unit_root.mkdir(parents=True)
            name = "actanara-dashboard.service"
            (unit_root / name).write_text(
                "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                encoding="utf-8",
            )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps({"units": {name: {"enabled": "enabled", "active": "active"}}}) + "\n",
                encoding="utf-8",
            )
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(
                fake,
                state_path=state_path,
                calls_path=root / "systemctl-calls.log",
            )

            result = self._begin_only_result(
                fixture,
                mode="upgrade",
                systemctl=str(fake),
                systemd_units=(name,),
                xdg_config_home=config_home,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            journal = Path(result.stdout.strip())
            state = json.loads(journal.read_text(encoding="utf-8"))
            expected_root = unit_root.resolve(strict=False)
            self.assertEqual(Path(state["systemdUnitRoot"]), expected_root)
            self.assertEqual(Path(state["systemdUnits"][0]["unitPath"]), expected_root / name)
            reserved = self._run(
                "reserve-artifact",
                "--state",
                str(journal),
                "--kind",
                "source-temp",
            )
            self.assertEqual(
                Path(reserved.stdout.strip()),
                fixture["runtime"].resolve() / "app" / "releases" / ".tmp-fixture-tx",
            )

    def test_linux_begin_rejects_definition_hash_race_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            unit_root = fixture["home"] / ".config" / "systemd" / "user"
            unit_root.mkdir(parents=True)
            name = "actanara-dashboard.service"
            (unit_root / name).write_text(
                "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                encoding="utf-8",
            )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps(
                    {"units": {name: {"enabled": "enabled", "active": "active"}}}
                )
                + "\n",
                encoding="utf-8",
            )
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(
                fake,
                state_path=state_path,
                calls_path=root / "systemctl-calls.log",
            )

            result = self._begin_only_result(
                fixture,
                mode="upgrade",
                systemctl=str(fake),
                systemd_units=(name,),
                expected_systemd_hashes={name: "0" * 64},
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("does not match the update contract", result.stderr)
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_linux_systemd_restore_failure_compensates_to_prior_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            unit_root = fixture["home"] / ".config" / "systemd" / "user"
            unit_root.mkdir(parents=True)
            name = "actanara-dashboard.service"
            (unit_root / name).write_text(
                "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                encoding="utf-8",
            )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps(
                    {"units": {name: {"enabled": "enabled", "active": "active"}}}
                )
                + "\n",
                encoding="utf-8",
            )
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(
                fake,
                state_path=state_path,
                calls_path=root / "systemctl-calls.log",
            )
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                systemctl=str(fake),
                systemd_units=(name,),
            )
            self._prepare_and_promote(fixture, journal)
            failed = json.loads(state_path.read_text(encoding="utf-8"))
            failed["failStart"] = [name]
            state_path.write_text(json.dumps(failed) + "\n", encoding="utf-8")

            result = self._run("restore-services", "--state", str(journal), check=False)
            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            failed.pop("failStart")
            state_path.write_text(json.dumps(failed) + "\n", encoding="utf-8")
            self._run("rollback", "--state", str(journal))

            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(current["units"][name], {"enabled": "enabled", "active": "active"})
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "rolled-back")

    def test_linux_stale_transaction_recovery_restores_systemd_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            unit_root = fixture["home"] / ".config" / "systemd" / "user"
            unit_root.mkdir(parents=True)
            name = "actanara-dashboard.service"
            (unit_root / name).write_text(
                "# Managed by Actanara. Do not edit by hand.\n[Unit]\n",
                encoding="utf-8",
            )
            state_path = root / "systemctl-state.json"
            state_path.write_text(
                json.dumps(
                    {"units": {name: {"enabled": "enabled", "active": "active"}}}
                )
                + "\n",
                encoding="utf-8",
            )
            fake = root / "systemctl"
            self._write_stateful_fake_systemctl(
                fake,
                state_path=state_path,
                calls_path=root / "systemctl-calls.log",
            )
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                systemctl=str(fake),
                systemd_units=(name,),
            )
            self._prepare_and_promote(fixture, journal)
            self._mark_transaction_owner_dead(journal)

            self._run("recover", "--runtime", str(fixture["runtime"]))

            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(current["units"][name]["active"], "active")
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "rolled-back")

    def test_begin_rejects_stale_dependency_profile_settings_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            settings_sha256 = hashlib.sha256(fixture["settings"].read_bytes()).hexdigest()
            active_venv = (fixture["runtime"] / ".venv").resolve()
            marker = active_venv / ".actanara-dependencies.json"
            marker_sha256 = hashlib.sha256(marker.read_bytes()).hexdigest()
            fixture["settings"].write_text(
                '{"dashboard":{"port":42173},"features":{"rag":true}}\n',
                encoding="utf-8",
            )

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--expected-settings-sha256",
                settings_sha256,
                "--expected-active-venv-target",
                str(active_venv),
                "--expected-active-marker-status",
                "trusted",
                "--expected-active-marker-sha256",
                marker_sha256,
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Linux",
                "--launchctl",
                "/usr/bin/true",
                "--uid",
                "0",
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("Settings changed after dependency profile selection", result.stderr)
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / "update-transactions" / "fixture-tx").exists()
            )
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")

    def test_begin_rejects_owner_pid_that_is_not_its_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                result = self._run(
                    "begin",
                    "--runtime",
                    str(fixture["runtime"]),
                    "--home",
                    str(fixture["home"]),
                    "--source-pointer",
                    str(fixture["runtime"] / "app" / "source"),
                    "--venv-pointer",
                    str(fixture["runtime"] / ".venv"),
                    "--mode",
                    "upgrade",
                    "--tx-id",
                    "fixture-tx",
                    "--owner-pid",
                    str(unrelated.pid),
                    "--platform",
                    "Linux",
                    "--launchctl",
                    "/usr/bin/true",
                    "--uid",
                    "0",
                    check=False,
                )
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("ancestor installer process", result.stderr)
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_zsh_command_substitution_helpers_accept_installer_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            script = r'''
set -e
journal="$("$PYTHON" "$HELPER" begin \
  --runtime "$RUNTIME" --home "$TEST_HOME" \
  --source-pointer "$RUNTIME/app/source" --venv-pointer "$RUNTIME/.venv" \
  --mode upgrade --tx-id fixture-tx --owner-pid "$$" \
  --platform Linux --launchctl /usr/bin/true --uid 0)"
reserved="$("$PYTHON" "$HELPER" reserve-artifact \
  --state "$journal" --kind source-temp)"
print -r -- "$journal"
print -r -- "$reserved"
"$PYTHON" "$HELPER" rollback --state "$journal"
'''
            result = subprocess.run(
                [shutil.which("zsh") or "/bin/zsh", "-c", script],
                env={
                    **os.environ,
                    "PYTHON": sys.executable,
                    "HELPER": str(HELPER),
                    "RUNTIME": str(fixture["runtime"]),
                    "TEST_HOME": str(fixture["home"]),
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            journal_text, reserved_text = result.stdout.splitlines()
            self.assertEqual(
                Path(journal_text).resolve(),
                (
                    fixture["runtime"]
                    / "app"
                    / "update-transactions"
                    / "fixture-tx"
                    / "journal.json"
                ).resolve(),
            )
            self.assertFalse(Path(reserved_text).exists())
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_rollback_refuses_replaced_candidate_artifact_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            candidate = fixture["candidate_source"]
            shutil.rmtree(candidate)
            candidate.mkdir()
            sentinel = candidate / "foreign.txt"
            sentinel.write_text("foreign\n", encoding="utf-8")

            result = self._run("rollback", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign\n")
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rollback-failed")
            self.assertTrue(
                any(error.startswith("candidate-artifacts:") for error in state["rollbackErrors"])
            )

    def test_transaction_directory_symlink_swap_is_rejected_without_external_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            journal = self._begin(fixture, owner_pid=os.getpid())
            original = journal.parent
            moved = root / "moved-transaction"
            original.rename(moved)
            original.symlink_to(moved, target_is_directory=True)
            sentinel = moved / "external-evidence.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")

            result = self._run("rollback", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("managed directory", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

    def test_command_lock_symlink_hardlink_and_inode_replacement_are_rejected(self):
        for replacement in ("symlink", "hardlink", "regular"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self._fixture(root)
                journal = self._begin(fixture, owner_pid=os.getpid())
                command_lock = journal.parent / "command.lock"
                external = root / "operator-owned-lock-evidence"
                external.write_text("preserve\n", encoding="utf-8")
                command_lock.unlink()
                if replacement == "symlink":
                    command_lock.symlink_to(external)
                elif replacement == "hardlink":
                    os.link(external, command_lock)
                else:
                    command_lock.write_text("replacement\n", encoding="utf-8")

                result = self._run("rollback", "--state", str(journal), check=False)

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn("command lock", result.stderr)
                self.assertEqual(external.read_text(encoding="utf-8"), "preserve\n")

    def test_atomic_no_clobber_rename_preserves_an_existing_empty_directory(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("update_transaction_tested", HELPER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "owned-staging"
            target = root / "operator-created-target"
            source.mkdir()
            target.mkdir()

            with self.assertRaisesRegex(
                module.TransactionError,
                "occupied target",
            ):
                module._rename_exclusive(source, target)

            self.assertTrue(source.is_dir())
            self.assertTrue(target.is_dir())

    def test_source_artifact_transfer_sigkill_windows_roll_back_owned_paths(self):
        for phase in (
            "source-artifact-transfer-authorized",
            "source-artifact-renamed-before-journal",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                source_template = fixture["candidate_source"]
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    materialize_candidates=False,
                )
                temporary = Path(
                    self._run(
                        "reserve-artifact",
                        "--state",
                        str(journal),
                        "--kind",
                        "source-temp",
                    ).stdout.strip()
                )
                shutil.copytree(source_template, temporary, dirs_exist_ok=True)

                result = self._run(
                    "promote-source-artifact",
                    "--state",
                    str(journal),
                    check=False,
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_KILL_PHASE": phase,
                    },
                )

                self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                self._run("rollback", "--state", str(journal))
                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "rolled-back")
                self.assertFalse(temporary.exists())
                self.assertFalse(
                    (fixture["runtime"] / "app" / "releases" / "fixture-tx").exists()
                )

    def test_candidate_artifact_reservation_sigkill_windows_recover_owned_path(self):
        for phase in (
            "candidate-artifact-reservation-authorized-source-temp",
            "candidate-artifact-staging-created-source-temp",
            "candidate-artifact-marker-created-source-temp",
            "candidate-artifact-renamed-before-journal-source-temp",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    materialize_candidates=False,
                )
                result = self._run(
                    "reserve-artifact",
                    "--state",
                    str(journal),
                    "--kind",
                    "source-temp",
                    check=False,
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_KILL_PHASE": phase,
                    },
                )

                self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                reserved = Path(
                    self._run(
                        "reserve-artifact",
                        "--state",
                        str(journal),
                        "--kind",
                        "source-temp",
                    ).stdout.strip()
                )
                self.assertTrue(reserved.is_dir())
                state = json.loads(journal.read_text(encoding="utf-8"))
                artifact = next(
                    item
                    for item in state["candidateArtifacts"]
                    if item["kind"] == "source-temp"
                )
                abandoned = artifact["abandonedReservationAttemptNonces"]
                self.assertEqual(
                    len(abandoned),
                    1 if phase == "candidate-artifact-staging-created-source-temp" else 0,
                )
                self._run("rollback", "--state", str(journal))
                self.assertFalse(reserved.exists())
                self.assertEqual(
                    json.loads(journal.read_text(encoding="utf-8"))["status"],
                    "rolled-back",
                )

    def test_rollback_preserves_unmarked_transaction_staging_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                materialize_candidates=False,
            )
            result = self._run(
                "reserve-artifact",
                "--state",
                str(journal),
                "--kind",
                "source-temp",
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_KILL_PHASE": (
                        "candidate-artifact-staging-created-source-temp"
                    ),
                },
            )

            self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
            unmarked = list(journal.parent.glob(".reserve-source-temp-*"))
            self.assertEqual(len(unmarked), 1)
            self.assertFalse((unmarked[0] / ".actanara-update-owner").exists())

            self._run("rollback", "--state", str(journal))

            state = json.loads(journal.read_text(encoding="utf-8"))
            artifact = next(
                item
                for item in state["candidateArtifacts"]
                if item["kind"] == "source-temp"
            )
            self.assertEqual(state["status"], "rolled-back")
            self.assertEqual(len(artifact["abandonedReservationAttemptNonces"]), 1)
            self.assertTrue(unmarked[0].is_dir())
            self.assertFalse(
                (fixture["runtime"] / "app" / "releases" / "fixture-tx").exists()
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_candidate_artifact_cleanup_sigkill_windows_are_idempotent(self):
        for phase in (
            "candidate-artifact-cleanup-authorized-source",
            "candidate-artifact-cleanup-moved-source",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(fixture, owner_pid=os.getpid())

                result = self._run(
                    "rollback",
                    "--state",
                    str(journal),
                    check=False,
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_KILL_PHASE": phase,
                    },
                )

                self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                self._run("rollback", "--state", str(journal))
                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "rolled-back")
                self.assertFalse(fixture["candidate_source"].exists())
                self.assertFalse(fixture["candidate_venv"].exists())

    def test_migration_gate_rejects_candidate_without_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY,
                        "rollback-compatible-additive",
                    )
                ],
                include_contract=False,
            )
            journal = self._begin(fixture, owner_pid=os.getpid())

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("migration compatibility contract is missing or invalid", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertIsNone(state["databaseCompatibility"])
            self.assertEqual(state["status"], "prepared")
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_rewritten_prior_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY + "-- rewritten\n",
                        "rollback-compatible-additive",
                    )
                ],
            )
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("candidate rewrote a prior migration body", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertIsNone(state["databaseCompatibility"])
            self.assertFalse(state["serviceStopInitiated"])
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_new_breaking_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY,
                        "rollback-compatible-additive",
                    ),
                    (
                        "0002_breaking",
                        "DROP TABLE fixture_migration;\n",
                        "breaking",
                    ),
                ],
            )
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("candidate migration is not rollback-compatible additive", result.stderr)
            self.assertFalse(
                json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
            )
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_accepts_new_additive_migration_before_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY,
                        "rollback-compatible-additive",
                    ),
                    (
                        "0002_additive",
                        "CREATE TABLE fixture_additive (id INTEGER PRIMARY KEY);\n",
                        "rollback-compatible-additive",
                    ),
                ],
            )
            journal = self._begin(fixture, owner_pid=os.getpid())

            self._record_and_verify_source_candidate(fixture, journal)

            state = json.loads(journal.read_text(encoding="utf-8"))
            evidence = state["databaseCompatibility"]
            self.assertEqual(evidence["status"], "verified")
            self.assertEqual(evidence["policy"], self.MIGRATION_POLICY)
            self.assertEqual(
                evidence["preCommitWriterContract"],
                self.PRE_COMMIT_WRITER_CONTRACT,
            )
            self.assertEqual(evidence["appliedMigrations"], [self.BASE_MIGRATION_VERSION])
            self.assertEqual(
                evidence["candidateMigrations"],
                [self.BASE_MIGRATION_VERSION, "0002_additive"],
            )
            self.assertEqual(evidence["newMigrations"], ["0002_additive"])
            self._run("stop", "--state", str(journal))
            self.assertEqual(
                json.loads(journal.read_text(encoding="utf-8"))["status"],
                "stopped",
            )
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_destructive_sql_mislabeled_additive(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY,
                        "rollback-compatible-additive",
                    ),
                    (
                        "0002_false_additive",
                        "CREATE TABLE fixture_added (id INTEGER); DROP TABLE fixture_migration;\n",
                        "rollback-compatible-additive",
                    ),
                ],
            )
            journal = self._begin(fixture, owner_pid=os.getpid())

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("prior-reader-unsafe statement", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_does_not_treat_comment_marker_in_string_as_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_candidate_source(
                fixture["candidate_source"],
                [
                    (
                        self.BASE_MIGRATION_VERSION,
                        self.BASE_MIGRATION_BODY,
                        "rollback-compatible-additive",
                    ),
                    (
                        "0002_false_additive",
                        "CREATE TABLE fixture_added (value TEXT DEFAULT '--;'); "
                        "DROP TABLE fixture_migration;\n",
                        "rollback-compatible-additive",
                    ),
                ],
            )
            journal = self._begin(fixture, owner_pid=os.getpid())

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("prior-reader-unsafe statement", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_unknown_live_ledger_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with closing(sqlite3.connect(fixture["database"])) as connection:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ("9999_unknown", "2026-07-11T00:00:01+00:00"),
                )
                connection.commit()
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("live database has a migration unknown to the prior source", result.stderr)
            self.assertFalse(
                json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
            )
            self._run("rollback", "--state", str(journal))

    def test_legacy_repair_migration_flag_is_bound_to_repair_journal(self):
        cases = (
            ("upgrade", True),
            ("repair", False),
        )
        for mode, allow_legacy_repair in cases:
            with (
                self.subTest(mode=mode, allow_legacy_repair=allow_legacy_repair),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = self._fixture(Path(tmp))
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    mode=mode,
                    settings_only_profile_evidence=mode == "repair",
                )
                self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "source",
                    "--candidate",
                    str(fixture["candidate_source"]),
                )
                arguments = [
                    "verify-migration-compatibility",
                    "--state",
                    str(journal),
                ]
                if allow_legacy_repair:
                    arguments.append("--allow-legacy-repair")

                result = self._run(*arguments, check=False)

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertIsNone(state["databaseCompatibility"])
                self.assertFalse(state["serviceStopInitiated"])
                self._run("rollback", "--state", str(journal))

    def test_upgrade_migration_gate_still_rejects_missing_prior_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            source = fixture["runtime"] / "app" / "source"
            shutil.rmtree(source.resolve(strict=True))
            journal = self._begin(fixture, owner_pid=os.getpid(), mode="upgrade")
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn(
                "prior source tree is unavailable for migration compatibility",
                result.stderr,
            )
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertIsNone(state["databaseCompatibility"])
            self.assertFalse(state["serviceStopInitiated"])
            self._run("rollback", "--state", str(journal))

    def test_repair_migration_gate_accepts_only_candidate_known_live_ledger(self):
        for unknown_ledger in (False, True):
            with self.subTest(unknown_ledger=unknown_ledger), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                if unknown_ledger:
                    with closing(sqlite3.connect(fixture["database"])) as connection:
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            ("9999_unknown", "2026-07-11T00:00:01+00:00"),
                        )
                        connection.commit()
                source = fixture["runtime"] / "app" / "source"
                shutil.rmtree(source.resolve(strict=True))
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    mode="repair",
                    settings_only_profile_evidence=True,
                )
                self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "source",
                    "--candidate",
                    str(fixture["candidate_source"]),
                )

                result = self._run(
                    "verify-migration-compatibility",
                    "--state",
                    str(journal),
                    "--allow-legacy-repair",
                    check=False,
                )

                if unknown_ledger:
                    self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                    self.assertIn(
                        "live database has a migration unreadable by the candidate",
                        result.stderr,
                    )
                    self.assertIsNone(
                        json.loads(journal.read_text(encoding="utf-8"))[
                            "databaseCompatibility"
                        ]
                    )
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    evidence = json.loads(journal.read_text(encoding="utf-8"))[
                        "databaseCompatibility"
                    ]
                    self.assertTrue(evidence["legacyRepair"])
                    self.assertEqual(evidence["priorMigrations"], [])
                    self.assertEqual(
                        evidence["appliedMigrations"],
                        [self.BASE_MIGRATION_VERSION],
                    )
                self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_nonempty_database_without_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with closing(sqlite3.connect(fixture["database"])) as connection:
                connection.execute("DROP TABLE schema_migrations")
                connection.commit()
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("nonempty Runtime database has no migration ledger", result.stderr)
            self.assertFalse(
                json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
            )
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_rejects_empty_ledger_with_unknown_user_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with closing(sqlite3.connect(fixture["database"])) as connection:
                connection.execute("DELETE FROM schema_migrations")
                connection.commit()
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("nonempty Runtime database has an empty migration ledger", result.stderr)
            self.assertFalse(
                json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
            )
            self._run("rollback", "--state", str(journal))

    def test_migration_gate_accepts_database_with_only_empty_ledger_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            database = fixture["database"]
            database.unlink()
            for suffix in ("-shm", "-wal"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.commit()
            journal = self._begin(fixture, owner_pid=os.getpid())

            self._record_and_verify_source_candidate(fixture, journal)

            evidence = json.loads(journal.read_text(encoding="utf-8"))["databaseCompatibility"]
            self.assertEqual(evidence["appliedMigrations"], [])
            self._run("rollback", "--state", str(journal))

    def test_stop_rejects_candidate_without_verified_migration_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
            )

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn(
                "services cannot stop before migration compatibility is verified",
                result.stderr,
            )
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            self.assertIsNone(state["databaseCompatibility"])
            self._run("rollback", "--state", str(journal))

    def test_undeclared_candidate_migration_fails_verify_and_rollback_keeps_service_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            server, server_thread, port = self._start_health_server()
            label = "com.actanara.dashboard"
            fixture["settings"].write_text(
                json.dumps(
                    {
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": port,
                            "healthPath": "/health",
                            "serviceLabel": label,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._write_runtime_plist(
                launch_agents / f"{label}.plist",
                label=label,
                runtime=fixture["runtime"],
            )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )
            try:
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    platform="Darwin",
                    launchctl=str(fake_launchctl),
                    uid=0,
                )
                self._prepare_and_promote(fixture, journal)
                self._run("restore-services", "--state", str(journal))
                self.assertTrue((state_dir / label).is_file())
                with closing(sqlite3.connect(fixture["database"])) as connection:
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        ("0002_undeclared", "2026-07-11T00:00:02+00:00"),
                    )
                    connection.commit()

                verify = self._run("verify", "--state", str(journal), check=False)
                self.assertEqual(verify.returncode, 70, verify.stdout + verify.stderr)
                self.assertIn("candidate wrote an undeclared live migration", verify.stderr)
                calls_before = calls_path.read_text(encoding="utf-8").splitlines()

                rollback = self._run("rollback", "--state", str(journal), check=False)

                self.assertEqual(rollback.returncode, 70, rollback.stdout + rollback.stderr)
                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "rollback-failed")
                self.assertTrue(
                    any(
                        error.startswith(
                            "database-compatibility:candidate wrote an undeclared live migration"
                        )
                        for error in state["rollbackErrors"]
                    )
                )
                self.assertIn(
                    "services:not-restored-after-pointer-or-control-state-conflict",
                    state["rollbackErrors"],
                )
                calls_after = calls_path.read_text(encoding="utf-8").splitlines()[
                    len(calls_before) :
                ]
                self.assertFalse(
                    any(call.startswith(("bootstrap ", "kickstart ")) for call in calls_after)
                )
                self.assertFalse((state_dir / label).exists())
                self.assertEqual(
                    (fixture["runtime"] / "app" / "source").resolve(),
                    fixture["candidate_source"].resolve(),
                )
                self.assertIn(
                    "pointers:preserved-because-database-compatibility-is-unproven",
                    state["rollbackErrors"],
                )
                self.assertTrue(
                    (fixture["runtime"] / "app" / ".update-transaction.lock").is_file()
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

    def test_settings_only_legacy_venv_rollback_restores_pointers_and_preserves_live_sqlite_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            legacy_venv = fixture["runtime"] / ".venv"
            legacy_inode = legacy_venv.stat(follow_symlinks=False).st_ino
            legacy_python = (legacy_venv / "bin" / "python").read_bytes()
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                settings_only_profile_evidence=True,
            )
            prepared = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(
                prepared["dependencyProfileBinding"]["bindingKind"],
                "pointer-only",
            )
            self.assertEqual(
                prepared["dependencyProfileEvidence"]["activeMarkerStatus"],
                "unavailable",
            )
            self._prepare_and_promote(fixture, journal)

            source = fixture["runtime"] / "app" / "source"
            venv = fixture["runtime"] / ".venv"
            self.assertEqual(source.resolve(), fixture["candidate_source"].resolve())
            self.assertEqual(os.readlink(source), "releases/fixture-tx")
            self.assertTrue(venv.is_symlink())
            self.assertEqual(os.readlink(venv), "app/venvs/fixture-tx")
            writer = sqlite3.connect(fixture["database"])
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute(
                    "INSERT INTO update_evidence(value) VALUES ('acknowledged-external-write-during-update')"
                )
                writer.commit()
                self.assertEqual(
                    writer.execute("SELECT COUNT(*) FROM update_evidence").fetchone(),
                    (2,),
                )
            finally:
                writer.close()

            self._run("rollback", "--state", str(journal))

            self.assertEqual(os.readlink(source), "releases/old")
            self.assertTrue(venv.is_dir())
            self.assertFalse(venv.is_symlink())
            self.assertEqual(venv.stat(follow_symlinks=False).st_ino, legacy_inode)
            self.assertEqual((venv / "bin" / "python").read_bytes(), legacy_python)
            self.assertEqual(fixture["settings"].read_text(encoding="utf-8"), '{"dashboard":{"port":42173}}\n')
            self.assertEqual(
                self._database_values(fixture["database"]),
                ["before-update", "acknowledged-external-write-during-update"],
            )
            self.assertEqual(self._database_integrity(fixture["database"]), "ok")
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rolled-back")
            self.assertEqual(state["rollbackErrors"], [])
            self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())
            self._run("rollback", "--state", str(journal))
            self.assertEqual(os.readlink(source), "releases/old")
            self.assertTrue(venv.is_dir())
            self.assertFalse(venv.is_symlink())

    def test_settings_only_legacy_venv_pointer_race_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                settings_only_profile_evidence=True,
            )
            self._record_and_verify_source_candidate(fixture, journal)
            pointer = fixture["runtime"] / ".venv"
            original = root / "legacy-venv-original"
            pointer.rename(original)
            shutil.copytree(original, pointer)

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("legacy Runtime venv pointer changed", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(state["serviceStopInitiated"])
            shutil.rmtree(pointer)
            original.rename(pointer)
            self._run("rollback", "--state", str(journal))

    def test_legacy_absolute_symlinks_promote_relative_and_rollback_exactly(self):
        from advanced.dashboard import dashboard_launch_agent as dashboard_launcher
        from advanced.dashboard import rag_server_launch_agent as rag_launcher

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            runtime = fixture["runtime"]
            source = runtime / "app" / "source"
            old_source = source.resolve()
            legacy_venv = runtime / ".venv"
            old_venv = runtime / "app" / "venvs" / "legacy-absolute"
            old_venv.parent.mkdir(parents=True, exist_ok=True)
            legacy_venv.rename(old_venv)
            source.unlink()
            source.symlink_to(str(old_source))
            legacy_venv.symlink_to(str(old_venv))
            prior_source_raw = os.readlink(source)
            prior_venv_raw = os.readlink(legacy_venv)

            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)

            self.assertEqual(os.readlink(source), "releases/fixture-tx")
            self.assertEqual(os.readlink(legacy_venv), "app/venvs/fixture-tx")
            self.assertEqual(source.resolve(), fixture["candidate_source"].resolve())
            self.assertEqual(legacy_venv.resolve(), fixture["candidate_venv"].resolve())
            dashboard_launcher._require_runtime_pointers(runtime)
            rag_launcher._require_runtime_pointers(runtime)

            self._run("rollback", "--state", str(journal))

            self.assertEqual(os.readlink(source), prior_source_raw)
            self.assertEqual(os.readlink(legacy_venv), prior_venv_raw)
            self.assertEqual(source.resolve(), old_source)
            self.assertEqual(legacy_venv.resolve(), old_venv.resolve())

    def test_external_configured_database_is_bound_and_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            external_root = root / "external-database"
            external_root.mkdir()
            external_database = external_root / "nova.custom"
            fixture["database"].replace(external_database)
            fixture["database"] = external_database
            (fixture["runtime"] / "config" / "runtime.json").write_text(
                json.dumps({"databasePath": str(external_database)}) + "\n",
                encoding="utf-8",
            )
            journal = self._begin(fixture, owner_pid=os.getpid())

            self._prepare_and_promote(fixture, journal)

            state = json.loads(journal.read_text(encoding="utf-8"))
            evidence = state["databaseCompatibility"]["databaseIdentity"]
            self.assertEqual(evidence["locator"]["kind"], "external-absolute-sha256")
            records = [item for item in state["files"] if item["key"] == "database"]
            self.assertEqual(len(records), 1)
            self.assertEqual(Path(records[0]["path"]), external_database.resolve())
            self.assertTrue(Path(records[0]["backupPath"]).is_file())
            self.assertEqual(self._database_integrity(external_database), "ok")
            self._run("rollback", "--state", str(journal))
            self.assertEqual(self._database_values(external_database), ["before-update"])
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rolled-back")
            self.assertEqual(state["rollbackErrors"], [])
            self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())
            self._run("rollback", "--state", str(journal))

    def test_online_backup_captures_uncheckpointed_wal_and_hashes_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            writer = sqlite3.connect(fixture["database"])
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    "INSERT INTO update_evidence(value) VALUES ('committed-only-in-live-wal')"
                )
                writer.commit()
                wal = Path(str(fixture["database"]) + "-wal")
                self.assertTrue(wal.is_file())
                self.assertGreater(wal.stat().st_size, 0)

                journal = self._begin(fixture, owner_pid=os.getpid())
                self._record_and_verify_source_candidate(fixture, journal)
                self._run("stop", "--state", str(journal))
                self._run(
                    "capture-mutable",
                    "--state",
                    str(journal),
                    "--location",
                    str(fixture["location"]),
                    "--cli-shim",
                    str(fixture["runtime"] / "bin" / "actanara"),
                    "--user-cli-shim",
                    str(fixture["user_cli"]),
                    "--desktop-link",
                    str(fixture["desktop"]),
                    "--shell-profile",
                    str(fixture["profile"]),
                )

                state = json.loads(journal.read_text(encoding="utf-8"))
                database_records = [item for item in state["files"] if item["key"] == "database"]
                self.assertEqual(len(database_records), 1)
                record = database_records[0]
                self.assertEqual(record["kind"], "sqlite-database")
                self.assertEqual(
                    record["snapshotPolicy"],
                    "online-backup-evidence-only-preserve-live",
                )
                self.assertFalse(record["path"].endswith(("-wal", "-shm")))
                backup = Path(record["backupPath"])
                self.assertTrue(backup.is_file())
                self.assertEqual(
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                    record["sha256"],
                )
                self.assertEqual(
                    self._database_values(backup),
                    ["before-update", "committed-only-in-live-wal"],
                )
                self.assertEqual(self._database_integrity(backup), "ok")
                backup_connection = sqlite3.connect(backup)
                try:
                    self.assertEqual(
                        backup_connection.execute("PRAGMA journal_mode").fetchone(),
                        ("delete",),
                    )
                finally:
                    backup_connection.close()
                self.assertEqual(list(backup.parent.glob(backup.name + "-*")), [])
                self._run("rollback", "--state", str(journal))
                self.assertEqual(list(backup.parent.glob(backup.name + "-*")), [])
            finally:
                writer.close()

    def test_online_backup_remains_consistent_with_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            padding = sqlite3.connect(fixture["database"])
            try:
                padding.execute("CREATE TABLE backup_padding (id INTEGER PRIMARY KEY, payload BLOB)")
                padding.executemany(
                    "INSERT INTO backup_padding(payload) VALUES (zeroblob(?))",
                    ((4096,) for _ in range(8192)),
                )
                padding.commit()
                padding.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                padding.close()

            journal = self._begin(fixture, owner_pid=os.getpid())
            self._record_and_verify_source_candidate(fixture, journal)
            self._run("stop", "--state", str(journal))
            writer_committed = threading.Event()
            writer_errors: list[str] = []

            def write_after_backup_starts() -> None:
                deadline = time.monotonic() + 10.0
                backups = journal.parent / "backups"
                while time.monotonic() < deadline:
                    if list(backups.glob(".*.partial-*")):
                        try:
                            connection = sqlite3.connect(fixture["database"], timeout=5.0)
                            try:
                                connection.execute("PRAGMA wal_autocheckpoint = 0")
                                connection.execute(
                                    "INSERT INTO update_evidence(value) VALUES ('committed-during-online-backup')"
                                )
                                connection.commit()
                            finally:
                                connection.close()
                            writer_committed.set()
                        except Exception as exc:  # pragma: no cover - reported in the parent thread
                            writer_errors.append(f"{type(exc).__name__}: {exc}")
                        return
                    time.sleep(0.001)
                writer_errors.append("online backup temporary file was not observed")

            writer_thread = threading.Thread(target=write_after_backup_starts, daemon=True)
            writer_thread.start()
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    "capture-mutable",
                    "--state",
                    str(journal),
                    "--location",
                    str(fixture["location"]),
                    "--cli-shim",
                    str(fixture["runtime"] / "bin" / "actanara"),
                    "--user-cli-shim",
                    str(fixture["user_cli"]),
                    "--desktop-link",
                    str(fixture["desktop"]),
                    "--shell-profile",
                    str(fixture["profile"]),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=45,
            )
            writer_thread.join(timeout=10)

            self.assertEqual(writer_errors, [])
            self.assertTrue(writer_committed.is_set())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            record = next(item for item in state["files"] if item["key"] == "database")
            backup = Path(record["backupPath"])
            self.assertIn("committed-during-online-backup", self._database_values(backup))
            self.assertEqual(self._database_integrity(backup), "ok")
            self.assertEqual(self._database_integrity(fixture["database"]), "ok")
            self._run("rollback", "--state", str(journal))
            self.assertIn(
                "committed-during-online-backup",
                self._database_values(fixture["database"]),
            )

    def test_rollback_before_service_stop_does_not_restart_loaded_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.session-d-prestop.watchdog"
            with (launch_agents / f"{label}.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "dashboard"
                                / "dashboard_launch_agent.py"
                            ),
                            "check",
                            "--url",
                            "http://127.0.0.1:42173/health",
                            "--label",
                            "com.actanara.dashboard",
                            "--restart",
                        ],
                        "EnvironmentVariables": {"ACTANARA_HOME": str(fixture["runtime"])},
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                platform="Darwin",
                launchctl=str(fake_launchctl),
                uid=0,
            )

            self._run("rollback", "--state", str(journal))

            mutations = [
                line
                for line in calls_path.read_text(encoding="utf-8").splitlines()
                if line.startswith(("bootout ", "bootstrap ", "kickstart "))
            ]
            self.assertEqual(mutations, [])
            self.assertEqual(
                (state_dir / label).read_text(encoding="utf-8").strip(),
                "running",
            )

    def test_stop_waits_for_asynchronous_launchd_unload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            server, server_thread, port = self._start_health_server()
            fixture["settings"].write_text(
                json.dumps(
                    {
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": port,
                            "healthPath": "/health",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.dashboard"
            self._write_runtime_plist(
                launch_agents / f"{label}.plist",
                label=label,
                runtime=fixture["runtime"],
            )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            fake_launchctl.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"state = Path({str(state_dir)!r})\n"
                f"calls = Path({str(calls_path)!r})\n"
                "with calls.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "command = sys.argv[1]\n"
                "label = sys.argv[2].rsplit('/', 1)[-1]\n"
                "value = state / label\n"
                "counter = state / (label + '.unload-count')\n"
                "if command == 'bootout':\n"
                "    counter.write_text('2\\n', encoding='utf-8')\n"
                "elif command == 'print':\n"
                "    if counter.is_file():\n"
                "        remaining = int(counter.read_text(encoding='utf-8'))\n"
                "        if remaining <= 0:\n"
                "            counter.unlink()\n"
                "            value.unlink(missing_ok=True)\n"
                "            raise SystemExit(113)\n"
                "        counter.write_text(str(remaining - 1) + '\\n', encoding='utf-8')\n"
                "    if not value.is_file():\n"
                "        raise SystemExit(113)\n"
                "    print('state = ' + value.read_text(encoding='utf-8').strip())\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            try:
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    platform="Darwin",
                    launchctl=str(fake_launchctl),
                    uid=0,
                )
                self._record_and_verify_source_candidate(fixture, journal)

                self._run(
                    "stop",
                    "--state",
                    str(journal),
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_SERVICE_STATE_TIMEOUT_SECONDS": "0.8",
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "stopped")
            self.assertFalse((state_dir / label).exists())
            prints = [
                line
                for line in calls_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("print ") and line.endswith("/" + label)
            ]
            self.assertGreaterEqual(len(prints), 4)

    def test_candidate_helper_sigkill_cannot_leave_unjournaled_or_recorded_child_writes(self):
        for kill_phase in (
            "candidate-command-started-before-journal",
            "candidate-command-released",
        ):
            with self.subTest(kill_phase=kill_phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(fixture, owner_pid=os.getpid())
                child_pid_path = fixture["runtime"] / "app" / "venvs" / "candidate-child.pid"
                late_path = fixture["runtime"] / "app" / "venvs" / "candidate-late-write"
                program = (
                    "import os,time; from pathlib import Path; "
                    f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
                    "time.sleep(1.0); "
                    f"Path({str(late_path)!r}).write_text('late')"
                )
                env = {
                    **os.environ,
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_KILL_PHASE": kill_phase,
                    "ACTANARA_INSTALL_TEST_CHILD_TERM_TIMEOUT_SECONDS": "0.2",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                helper = subprocess.Popen(
                    [
                        sys.executable,
                        str(HELPER),
                        "run-candidate-command",
                        "--state",
                        str(journal),
                        "--phase",
                        "fixture-child",
                        "--",
                        sys.executable,
                        "-c",
                        program,
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                helper.wait(timeout=10)
                self.assertEqual(helper.returncode, -signal.SIGKILL)

                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(state.get("activeCommand"), "run-candidate-command:fixture-child")
                self.assertNotIn(str(late_path), journal.read_text(encoding="utf-8"))
                if kill_phase == "candidate-command-started-before-journal":
                    self.assertIsNone(state.get("candidateCommand"))
                    time.sleep(1.2)
                    self.assertFalse(child_pid_path.exists())
                else:
                    self.assertIsInstance(state.get("candidateCommand"), dict)
                    self._run(
                        "rollback",
                        "--state",
                        str(journal),
                        env={
                            "ACTANARA_INSTALL_TEST_MODE": "1",
                            "ACTANARA_INSTALL_TEST_CHILD_TERM_TIMEOUT_SECONDS": "0.2",
                        },
                    )
                    if child_pid_path.is_file():
                        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                        deadline = time.monotonic() + 1.0
                        while self._pid_alive(child_pid) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertFalse(self._pid_alive(child_pid))
                    time.sleep(1.2)
                self.assertFalse(late_path.exists())

    def test_transaction_helper_commands_are_serialized_without_state_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            helper = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "run-candidate-command",
                    "--state",
                    str(journal),
                    "--phase",
                    "serialized-fixture",
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(2.0)",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    state = json.loads(journal.read_text(encoding="utf-8"))
                    if (
                        state.get("activeCommand")
                        == "run-candidate-command:serialized-fixture"
                        and isinstance(state.get("candidateCommand"), dict)
                    ):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("first helper did not acquire the transaction command lock")

                before = journal.read_bytes()
                result = self._run("rollback", "--state", str(journal), check=False)
                recovery = self._run(
                    "recover",
                    "--runtime",
                    str(fixture["runtime"]),
                    check=False,
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn("another update transaction helper command", result.stderr)
                self.assertEqual(
                    recovery.returncode,
                    70,
                    recovery.stdout + recovery.stderr,
                )
                self.assertIn(
                    "another update transaction helper command",
                    recovery.stderr,
                )
                self.assertEqual(journal.read_bytes(), before)
                self.assertEqual(helper.wait(timeout=10), 0)
            finally:
                if helper.poll() is None:
                    helper.terminate()
                    helper.wait(timeout=5)
            self._run("rollback", "--state", str(journal))

    def test_candidate_command_success_reaps_background_process_group_before_clearing_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            child_pid_path = fixture["runtime"] / "app" / "venvs" / "background-child.pid"
            late_path = fixture["runtime"] / "app" / "venvs" / "background-late-write"
            child_program = (
                "import os,time; from pathlib import Path; "
                f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(1.0); "
                f"Path({str(late_path)!r}).write_text('late')"
            )
            parent_program = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child_program!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(0.1)"
            )

            result = self._run(
                "run-candidate-command",
                "--state",
                str(journal),
                "--phase",
                "background-fixture",
                "--",
                sys.executable,
                "-c",
                parent_program,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_CHILD_TERM_TIMEOUT_SECONDS": "0.2",
                },
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertIsNone(state.get("candidateCommand"))
            if child_pid_path.is_file():
                self.assertFalse(self._pid_alive(int(child_pid_path.read_text(encoding="utf-8"))))
            time.sleep(1.2)
            self.assertFalse(late_path.exists())

    def test_begin_rejects_loaded_default_label_without_runtime_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            production_label = "com.actanara.dashboard"
            (state_dir / production_label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Darwin",
                "--launchctl",
                str(fake_launchctl),
                "--uid",
                "0",
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("plist", result.stderr.lower())
            mutations = [
                line
                for line in calls_path.read_text(encoding="utf-8").splitlines()
                if line.startswith(("bootout ", "bootstrap ", "kickstart "))
            ]
            self.assertEqual(mutations, [])
            self.assertEqual(
                (state_dir / production_label).read_text(encoding="utf-8").strip(),
                "running",
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_begin_accepts_exact_legacy_watchdog_without_actanara_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.dashboard.watchdog"
            with (launch_agents / f"{label}.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "dashboard"
                                / "dashboard_launch_agent.py"
                            ),
                            "check",
                            "--url",
                            "http://127.0.0.1:3036/health",
                            "--label",
                            "com.actanara.dashboard",
                            "--restart",
                        ],
                        "RunAtLoad": True,
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )

            original = (launch_agents / f"{label}.plist").read_bytes()
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                platform="Darwin",
                launchctl=str(fake_launchctl),
            )

            state = json.loads(journal.read_text(encoding="utf-8"))
            watchdog = next(item for item in state["services"] if item["label"] == label)
            self.assertTrue(watchdog["plistExisted"])
            self.assertFalse(watchdog["loaded"])
            self._record_and_verify_source_candidate(fixture, journal)
            self._run("stop", "--state", str(journal))
            self._run(
                "capture-mutable",
                "--state",
                str(journal),
                "--location",
                str(fixture["location"]),
                "--cli-shim",
                str(fixture["runtime"] / "bin" / "actanara"),
                "--user-cli-shim",
                str(fixture["user_cli"]),
                "--desktop-link",
                str(fixture["desktop"]),
                "--shell-profile",
                str(fixture["profile"]),
            )
            self._run("normalize-service-plists", "--state", str(journal))
            normalized = plistlib.loads((launch_agents / f"{label}.plist").read_bytes())
            self.assertEqual(normalized["EnvironmentVariables"]["ACTANARA_HOME"], str(fixture["runtime"].resolve()))
            self.assertEqual(normalized["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"], "1")
            self._run("rollback", "--state", str(journal))
            self.assertEqual((launch_agents / f"{label}.plist").read_bytes(), original)

    def test_service_plist_rebinds_two_generation_stale_paths_and_survives_all_forward_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.binding-two-generations-old"
            plist_path = launch_agents / f"{label}.plist"
            stale_source = fixture["runtime"] / "app" / "releases" / "two-generations-old"
            stale_venv = fixture["runtime"] / "app" / "venvs" / "two-generations-old"
            self._write_dashboard_binding_plist(
                plist_path,
                label=label,
                runtime=fixture["runtime"],
                source_root=stale_source,
                venv_root=stale_venv,
            )
            plist_path.chmod(0o640)
            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_stopped_candidate(fixture, journal)

            self._run("normalize-service-plists", "--state", str(journal))

            normalized_bytes = plist_path.read_bytes()
            normalized = plistlib.loads(normalized_bytes)
            stable_source = str(fixture["runtime"].resolve() / "app" / "source")
            stable_venv = str(fixture["runtime"].resolve() / ".venv")
            serialized = json.dumps(normalized, sort_keys=True)
            self.assertIn(stable_source, serialized)
            self.assertIn(stable_venv, serialized)
            self.assertNotIn(str(stale_source), serialized)
            self.assertNotIn(str(stale_venv), serialized)
            state = json.loads(journal.read_text(encoding="utf-8"))
            service = next(item for item in state["services"] if item["label"] == label)
            self.assertTrue(service["plistNormalizationRequired"])
            self.assertTrue(service["plistNormalizationComplete"])
            self.assertTrue(service["plistBindingComplete"])
            self.assertGreaterEqual(service["plistSourceBindingCount"], 1)
            self.assertGreaterEqual(service["plistVenvBindingCount"], 1)
            self.assertEqual(
                service["normalizedPlistSha256"],
                hashlib.sha256(normalized_bytes).hexdigest(),
            )
            self.assertEqual(service["normalizedPlistMode"], 0o640)

            self._run("promote", "--state", str(journal))
            self._run("restore-services", "--state", str(journal))
            self._run("verify", "--state", str(journal))
            self._run("commit", "--state", str(journal))

            committed = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(committed["status"], "committed")
            self.assertEqual(plist_path.read_bytes(), normalized_bytes)
            self.assertEqual(plist_path.stat().st_mode & 0o777, 0o640)

    def test_already_stable_plist_records_durable_binding_without_false_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.binding-already-stable"
            plist_path = launch_agents / f"{label}.plist"
            self._write_dashboard_binding_plist(
                plist_path,
                label=label,
                runtime=fixture["runtime"],
                source_root=fixture["runtime"] / "app" / "source",
                venv_root=fixture["runtime"] / ".venv",
                extra_payload={"StandardOutPath": "/tmp/actanara-releases-archive.log"},
            )
            original = plist_path.read_bytes()
            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_stopped_candidate(fixture, journal)

            self._run("normalize-service-plists", "--state", str(journal))

            self.assertEqual(plist_path.read_bytes(), original)
            state = json.loads(journal.read_text(encoding="utf-8"))
            service = next(item for item in state["services"] if item["label"] == label)
            self.assertFalse(service["plistNormalizationRequired"])
            self.assertTrue(service["plistNormalizationStarted"])
            self.assertTrue(service["plistNormalizationComplete"])
            self.assertTrue(service["plistBindingComplete"])
            self.assertEqual(
                service["normalizedPlistSha256"],
                hashlib.sha256(original).hexdigest(),
            )
            self._run("rollback", "--state", str(journal))
            self.assertEqual(plist_path.read_bytes(), original)

    def test_service_plist_binding_rejects_prefix_confusion_embedded_generation_and_shell_injection(self):
        cases = (
            "prefix-confusion",
            "embedded-generation",
            "shell-injection",
            "extra-pythonpath",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                fixture = self._fixture(root)
                launch_agents = fixture["home"] / "Library" / "LaunchAgents"
                launch_agents.mkdir(parents=True)
                label = f"com.actanara.binding-{case}"
                plist_path = launch_agents / f"{label}.plist"
                stable_source = fixture["runtime"] / "app" / "source"
                source_root = stable_source
                extra_payload = None
                command = None
                if case == "prefix-confusion":
                    source_root = Path(str(fixture["runtime"] / "app" / "releases") + "-evil") / "old"
                elif case == "embedded-generation":
                    embedded = fixture["runtime"] / "app" / "releases" / "old" / "script.py"
                    extra_payload = {"OpaqueCommand": f"python={embedded}"}
                else:
                    python = fixture["runtime"] / ".venv" / "bin" / "python"
                    command = (
                        f"cd {stable_source} && exec {python} -m uvicorn app.main:app "
                        f"--app-dir {stable_source / 'src' / 'dashboard'} --host 127.0.0.1 "
                        "--port 42173 ; /usr/bin/true"
                    )
                self._write_dashboard_binding_plist(
                    plist_path,
                    label=label,
                    runtime=fixture["runtime"],
                    source_root=source_root,
                    venv_root=fixture["runtime"] / ".venv",
                    extra_payload=extra_payload,
                    command=command,
                )
                if case == "extra-pythonpath":
                    payload = plistlib.loads(plist_path.read_bytes())
                    payload["EnvironmentVariables"]["PYTHONPATH"] += ":/tmp/unmanaged-pythonpath"
                    with plist_path.open("wb") as handle:
                        plistlib.dump(payload, handle, sort_keys=False)
                original = plist_path.read_bytes()
                journal, _calls = self._begin_unloaded_darwin(fixture, root)
                self._prepare_stopped_candidate(fixture, journal)

                result = self._run(
                    "normalize-service-plists",
                    "--state",
                    str(journal),
                    check=False,
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertTrue(
                    any(
                        marker in result.stderr
                        for marker in (
                            "confusing",
                            "embedded concrete generation",
                            "strict uvicorn",
                            "exact product path set",
                        )
                    ),
                    result.stderr,
                )
                self._run("rollback", "--state", str(journal))
                self.assertEqual(plist_path.read_bytes(), original)

    def test_stale_service_plist_rollback_restores_exact_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.binding-rollback"
            plist_path = launch_agents / f"{label}.plist"
            self._write_dashboard_binding_plist(
                plist_path,
                label=label,
                runtime=fixture["runtime"],
                source_root=fixture["runtime"] / "app" / "releases" / "stale-source",
                venv_root=fixture["runtime"] / "app" / "venvs" / "stale-venv",
            )
            plist_path.chmod(0o640)
            original = plist_path.read_bytes()
            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_stopped_candidate(fixture, journal)
            self._run("normalize-service-plists", "--state", str(journal))
            self.assertNotEqual(plist_path.read_bytes(), original)

            self._run("rollback", "--state", str(journal))

            self.assertEqual(plist_path.read_bytes(), original)
            self.assertEqual(plist_path.stat().st_mode & 0o777, 0o640)

    def test_direct_service_plists_rebind_program_arguments_working_directory_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            runtime = fixture["runtime"]
            stale_source = runtime / "app" / "releases" / "old-direct-services"
            stale_venv = runtime / "app" / "venvs" / "old-direct-services"
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            payloads = {
                "com.actanara.binding-watchdog": {
                    "ProgramArguments": [
                        str(stale_venv / "bin" / "python"),
                        str(stale_source / "advanced" / "dashboard" / "dashboard_launch_agent.py"),
                        "check",
                        "--url",
                        "http://127.0.0.1:42173/health",
                        "--label",
                        "com.actanara.dashboard",
                        "--restart",
                    ],
                    "EnvironmentVariables": {"ACTANARA_HOME": str(runtime)},
                },
                "com.actanara.binding-rag": {
                    "ProgramArguments": [
                        str(stale_venv / "bin" / "python"),
                        str(stale_source / "advanced" / "dashboard" / "rag_server_launch_agent.py"),
                        "run",
                        "--project-root",
                        str(stale_source),
                        "--actanara-home",
                        str(runtime),
                    ],
                    "EnvironmentVariables": {
                        "ACTANARA_HOME": str(runtime),
                        "PYTHONPATH": f"{stale_source}:{stale_source / 'src'}",
                    },
                },
                "com.actanara.binding-pipeline": {
                    "ProgramArguments": [
                        str(stale_venv / "bin" / "python"),
                        str(stale_source / "advanced" / "pipeline" / "run_daily_pipeline.py"),
                    ],
                    "WorkingDirectory": str(stale_source),
                    "EnvironmentVariables": {
                        "ACTANARA_HOME": str(runtime),
                        "PYTHONPATH": f"{stale_source}:{stale_source / 'src'}:{stale_source / 'src' / 'dashboard'}",
                    },
                },
                "com.actanara.binding-dashboard-aggregation": {
                    "ProgramArguments": [
                        str(stale_venv / "bin" / "python"),
                        str(
                            stale_source
                            / "advanced"
                            / "pipeline"
                            / "run_dashboard_foundation_refresh.py"
                        ),
                    ],
                    "WorkingDirectory": str(stale_source),
                    "EnvironmentVariables": {
                        "ACTANARA_HOME": str(runtime),
                        "PYTHONPATH": f"{stale_source}:{stale_source / 'src'}:{stale_source / 'src' / 'dashboard'}",
                    },
                },
            }
            originals: dict[str, bytes] = {}
            for label, fields in payloads.items():
                path = launch_agents / f"{label}.plist"
                with path.open("wb") as handle:
                    plistlib.dump({"Label": label, **fields}, handle, sort_keys=False)
                originals[label] = path.read_bytes()
            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_stopped_candidate(fixture, journal)

            self._run("normalize-service-plists", "--state", str(journal))

            canonical_runtime = runtime.resolve()
            for label in payloads:
                path = launch_agents / f"{label}.plist"
                text = json.dumps(plistlib.loads(path.read_bytes()), sort_keys=True)
                self.assertNotIn(str(stale_source), text)
                self.assertNotIn(str(stale_venv), text)
                self.assertIn(str(canonical_runtime / "app" / "source"), text)
                self.assertIn(str(canonical_runtime / ".venv"), text)
            state = json.loads(journal.read_text(encoding="utf-8"))
            normalized_services = {
                item["label"]: item
                for item in state["services"]
                if item["label"] in payloads
            }
            self.assertEqual(set(normalized_services), set(payloads))
            self.assertTrue(all(item["plistBindingComplete"] for item in normalized_services.values()))
            self._run("rollback", "--state", str(journal))
            for label, original in originals.items():
                self.assertEqual((launch_agents / f"{label}.plist").read_bytes(), original)

    def test_real_v100_source_concrete_venv_stable_plists_normalize_to_current_builders(self):
        from advanced.dashboard import dashboard_launch_agent as dashboard_launcher
        from advanced.dashboard import rag_server_launch_agent as rag_launcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            runtime = fixture["runtime"]
            stable_source = runtime / "app" / "source"
            stable_python = runtime / ".venv" / "bin" / "python"
            concrete_source = runtime / "app" / "releases" / "old"
            concrete_venv = runtime / "app" / "venvs" / "old"
            logs = runtime / "state" / "logs"
            labels = {
                "dashboard": "com.actanara.dashboard",
                "watchdog": "com.actanara.dashboard.watchdog",
                "rag": "com.actanara.rag-server",
            }
            canonical = {
                "dashboard": dashboard_launcher.build_service_plist(
                    label=labels["dashboard"],
                    python=stable_python,
                    project_root=stable_source,
                    actanara_home=runtime,
                    host="127.0.0.1",
                    port=42173,
                    foundation=True,
                    logs_dir=logs,
                ),
                "watchdog": dashboard_launcher.build_watchdog_plist(
                    label=labels["watchdog"],
                    service_label=labels["dashboard"],
                    python=stable_python,
                    script=stable_source
                    / "advanced"
                    / "dashboard"
                    / "dashboard_launch_agent.py",
                    url="http://127.0.0.1:42173/health",
                    interval=60,
                    actanara_home=runtime,
                    logs_dir=logs,
                ),
                "rag": rag_launcher.build_service_plist(
                    label=labels["rag"],
                    python=stable_python,
                    project_root=stable_source,
                    actanara_home=runtime,
                    script=stable_source
                    / "advanced"
                    / "dashboard"
                    / "rag_server_launch_agent.py",
                    logs_dir=logs,
                ),
            }
            legacy = copy.deepcopy(canonical)
            stable_source_text = str(stable_source)
            concrete_source_text = str(concrete_source)
            dashboard_command = legacy["dashboard"]["ProgramArguments"][2]
            self.assertEqual(dashboard_command.count(stable_source_text), 2)
            legacy["dashboard"]["ProgramArguments"][2] = dashboard_command.replace(
                stable_source_text,
                concrete_source_text,
            )
            dashboard_environment = legacy["dashboard"]["EnvironmentVariables"]
            dashboard_environment["ACTANARA_DASHBOARD_PROJECT_ROOT"] = concrete_source_text
            dashboard_environment["PYTHONPATH"] = (
                f"{concrete_source}:{concrete_source / 'src'}:"
                f"{concrete_source / 'src' / 'dashboard'}"
            )
            legacy["watchdog"]["ProgramArguments"][1] = str(
                concrete_source
                / "advanced"
                / "dashboard"
                / "dashboard_launch_agent.py"
            )
            rag_arguments = legacy["rag"]["ProgramArguments"]
            rag_arguments[1] = str(
                concrete_source / "advanced" / "dashboard" / "rag_server_launch_agent.py"
            )
            project_root_index = rag_arguments.index("--project-root") + 1
            rag_arguments[project_root_index] = concrete_source_text
            legacy["rag"]["EnvironmentVariables"]["PYTHONPATH"] = (
                f"{concrete_source}:{concrete_source / 'src'}"
            )

            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            originals: dict[str, bytes] = {}
            for key, payload in legacy.items():
                serialized = json.dumps(payload, sort_keys=True)
                self.assertIn(concrete_source_text, serialized)
                self.assertIn(str(stable_python), serialized)
                self.assertNotIn(str(concrete_venv), serialized)
                path = launch_agents / f"{labels[key]}.plist"
                with path.open("wb") as handle:
                    plistlib.dump(payload, handle, sort_keys=False)
                originals[key] = path.read_bytes()

            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_stopped_candidate(fixture, journal)
            self._run("normalize-service-plists", "--state", str(journal))

            for key, expected in canonical.items():
                path = launch_agents / f"{labels[key]}.plist"
                self.assertEqual(plistlib.loads(path.read_bytes()), expected)
            state = json.loads(journal.read_text(encoding="utf-8"))
            services = {item["label"]: item for item in state["services"]}
            expected_counts = {
                labels["dashboard"]: (6, 2),
                labels["watchdog"]: (1, 1),
                labels["rag"]: (4, 1),
            }
            for label, (source_count, venv_count) in expected_counts.items():
                self.assertTrue(services[label]["plistNormalizationRequired"])
                self.assertTrue(services[label]["plistBindingComplete"])
                self.assertEqual(services[label]["plistSourceBindingCount"], source_count)
                self.assertEqual(services[label]["plistVenvBindingCount"], venv_count)

            self._run("rollback", "--state", str(journal))
            for key, original in originals.items():
                self.assertEqual(
                    (launch_agents / f"{labels[key]}.plist").read_bytes(),
                    original,
                )

    def test_forward_restore_rejects_available_git_provenance_with_short_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            manifest_path = fixture["candidate_source"] / ".actanara-runtime-source.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["git"] = {
                "available": True,
                "commit": "a" * 12,
                "branch": "main",
                "remote": "https://github.com/Neo-Isshin/actanara.git",
                "dirty": False,
            }
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.binding-short-commit"
            self._write_runtime_plist(
                launch_agents / f"{label}.plist",
                label=label,
                runtime=fixture["runtime"],
            )
            journal, _calls = self._begin_unloaded_darwin(fixture, root)
            self._prepare_and_promote(fixture, journal)

            result = self._run("restore-services", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("not a full commit id", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_forward_health_rejects_non_candidate_source_commit_but_rollback_accepts_prior_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = self._fixture(root)
            candidate_commit = "a" * 40
            prior_commit = "b" * 40
            manifest_path = fixture["candidate_source"] / ".actanara-runtime-source.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["git"] = {
                "available": True,
                "commit": candidate_commit,
                "branch": "main",
                "remote": "https://github.com/Neo-Isshin/actanara.git",
                "dirty": False,
            }
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            server, server_thread, port = self._start_health_server(source_commit=prior_commit)
            label = "com.actanara.dashboard"
            fixture["settings"].write_text(
                json.dumps(
                    {
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": port,
                            "healthPath": "/health",
                            "serviceLabel": label,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist_path = launch_agents / f"{label}.plist"
            self._write_runtime_plist(plist_path, label=label, runtime=fixture["runtime"])
            original = plist_path.read_bytes()
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )
            try:
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    platform="Darwin",
                    launchctl=str(fake_launchctl),
                    uid=0,
                )
                self._prepare_and_promote(fixture, journal)

                result = self._run(
                    "restore-services",
                    "--state",
                    str(journal),
                    check=False,
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_HEALTH_TIMEOUT_SECONDS": "0.2",
                    },
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn("non-candidate source provenance", result.stderr)
                self._run("rollback", "--state", str(journal))
                self.assertEqual(plist_path.read_bytes(), original)
                self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

    def test_begin_rejects_plist_owned_by_a_different_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.dashboard"
            self._write_runtime_plist(
                launch_agents / f"{label}.plist",
                label=label,
                runtime=root / "other-runtime",
            )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Darwin",
                "--launchctl",
                str(fake_launchctl),
                "--uid",
                "0",
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("provenance", result.stderr.lower())
            self.assertFalse(
                any(
                    line.startswith(("bootout ", "bootstrap ", "kickstart "))
                    for line in (
                        calls_path.read_text(encoding="utf-8") if calls_path.exists() else ""
                    ).splitlines()
                )
            )

    def test_begin_rejects_plist_whose_embedded_label_does_not_match_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            expected_label = "com.actanara.dashboard"
            self._write_runtime_plist(
                launch_agents / f"{expected_label}.plist",
                label="com.actanara.unrelated",
                runtime=fixture["runtime"],
            )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / expected_label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(fake_launchctl, state_dir=state_dir, calls_path=calls_path)

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Darwin",
                "--launchctl",
                str(fake_launchctl),
                "--uid",
                "0",
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("provenance", result.stderr.lower())
            self.assertFalse(
                any(
                    line.startswith(("bootout ", "bootstrap ", "kickstart "))
                    for line in (
                        calls_path.read_text(encoding="utf-8") if calls_path.exists() else ""
                    ).splitlines()
                )
            )

    def test_begin_rejects_running_scanned_service_without_recoverable_health_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            fixture["settings"].write_text("{not-json\n", encoding="utf-8")
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.example.session-d-custom-service"
            with (launch_agents / f"{label}.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "dashboard"
                                / "dashboard_launch_agent.py"
                            ),
                        ],
                        "EnvironmentVariables": {"ACTANARA_HOME": str(fixture["runtime"])},
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(fake_launchctl, state_dir=state_dir, calls_path=calls_path)

            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "fixture-tx",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Darwin",
                "--launchctl",
                str(fake_launchctl),
                "--uid",
                "0",
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_HEALTH_TIMEOUT_SECONDS": "0.2",
                },
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("no valid health endpoint", result.stderr)
            self.assertFalse(
                any(
                    line.startswith(("bootout ", "bootstrap ", "kickstart "))
                    for line in calls_path.read_text(encoding="utf-8").splitlines()
                )
            )
            self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())

    def test_repair_begin_accepts_legacy_dashboard_states_that_upgrade_rejects(self):
        cases = (
            (
                "loaded-not-running",
                "waiting",
                False,
                "loaded but not running",
            ),
            (
                "unrecoverable-health",
                "running",
                True,
                "no valid health endpoint",
            ),
        )
        for case, launch_state, invalid_settings, upgrade_error in cases:
            for mode in ("repair", "upgrade"):
                with (
                    self.subTest(case=case, mode=mode),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    fixture = self._fixture(root)
                    if invalid_settings:
                        fixture["settings"].write_text("{legacy-settings\n", encoding="utf-8")
                    launch_agents = fixture["home"] / "Library" / "LaunchAgents"
                    launch_agents.mkdir(parents=True)
                    label = "com.actanara.dashboard"
                    self._write_runtime_plist(
                        launch_agents / f"{label}.plist",
                        label=label,
                        runtime=fixture["runtime"],
                    )
                    state_dir = root / "launchctl-state"
                    state_dir.mkdir()
                    (state_dir / label).write_text(f"{launch_state}\n", encoding="utf-8")
                    calls_path = root / "launchctl-calls.log"
                    fake_launchctl = root / "launchctl"
                    self._write_stateful_fake_launchctl(
                        fake_launchctl,
                        state_dir=state_dir,
                        calls_path=calls_path,
                    )

                    result = self._begin_only_result(
                        fixture,
                        mode=mode,
                        platform="Darwin",
                        launchctl=str(fake_launchctl),
                        uid=0,
                    )

                    mutations = [
                        call
                        for call in calls_path.read_text(encoding="utf-8").splitlines()
                        if call.startswith(("bootout ", "bootstrap ", "kickstart "))
                    ]
                    self.assertEqual(mutations, [])
                    self.assertEqual(
                        (state_dir / label).read_text(encoding="utf-8").strip(),
                        launch_state,
                    )
                    if mode == "upgrade":
                        self.assertEqual(
                            result.returncode,
                            70,
                            result.stdout + result.stderr,
                        )
                        self.assertIn(upgrade_error, result.stderr)
                        self.assertFalse(
                            (
                                fixture["runtime"]
                                / "app"
                                / ".update-transaction.lock"
                            ).exists()
                        )
                        continue

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    journal = Path(result.stdout.strip())
                    state = json.loads(journal.read_text(encoding="utf-8"))
                    service = next(
                        item for item in state["services"] if item["label"] == label
                    )
                    self.assertEqual(state["status"], "prepared")
                    self.assertTrue(service["loaded"])
                    self.assertEqual(service["state"], launch_state)
                    self.assertFalse(state["serviceStopInitiated"])
                    self._run("rollback", "--state", str(journal))
                    self.assertEqual(
                        (state_dir / label).read_text(encoding="utf-8").strip(),
                        launch_state,
                    )

    def test_repair_begin_still_rejects_running_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.pipeline"
            plist_path = launch_agents / f"{label}.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "pipeline"
                                / "run_daily_pipeline.py"
                            ),
                        ],
                        "EnvironmentVariables": {
                            "ACTANARA_HOME": str(fixture["runtime"]),
                        },
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )

            result = self._begin_only_result(
                fixture,
                mode="repair",
                platform="Darwin",
                launchctl=str(fake_launchctl),
                uid=0,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("managed scheduler job is currently running", result.stderr)
            self.assertEqual(
                (state_dir / label).read_text(encoding="utf-8").strip(),
                "running",
            )
            self.assertFalse(
                any(
                    call.startswith(("bootout ", "bootstrap ", "kickstart "))
                    for call in calls_path.read_text(encoding="utf-8").splitlines()
                )
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_repair_accepts_legacy_runtime_bound_plist_without_actanara_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "actanara.daily.pipeline"
            plist_path = launch_agents / f"{label}.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "pipeline"
                                / "run_daily_pipeline.py"
                            ),
                        ],
                        "WorkingDirectory": str(fixture["runtime"] / "app" / "source"),
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )

            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                mode="repair",
                settings_only_profile_evidence=True,
                platform="Darwin",
                launchctl=str(fake_launchctl),
                uid=0,
            )
            self._prepare_stopped_candidate(
                fixture,
                journal,
                allow_legacy_repair=True,
            )
            self._run("normalize-service-plists", "--state", str(journal))
            self._run("promote", "--state", str(journal))
            self._run("commit-repair", "--state", str(journal))

            state = json.loads(journal.read_text(encoding="utf-8"))
            service = next(item for item in state["services"] if item["label"] == label)
            self.assertEqual(state["status"], "committed")
            self.assertFalse(service["loaded"])
            self.assertNotIn("ACTANARA_HOME", plistlib.loads(plist_path.read_bytes()).get("EnvironmentVariables", {}))
            self._run("complete-repair", "--state", str(journal))

    def test_verify_allows_legitimate_sqlite_writer_after_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)
            writer = sqlite3.connect(fixture["database"])
            try:
                writer.execute(
                    "INSERT INTO update_evidence(value) VALUES ('writer-before-candidate-verify')"
                )
                writer.commit()
            finally:
                writer.close()

            self._run("restore-services", "--state", str(journal))
            self._run("verify", "--state", str(journal))

            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "verified")
            self.assertEqual(
                self._database_values(fixture["database"]),
                ["before-update", "writer-before-candidate-verify"],
            )
            self.assertEqual(self._database_integrity(fixture["database"]), "ok")
            self._run("rollback", "--state", str(journal))

    def test_unchanged_existing_desktop_directory_is_shallowly_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture["desktop"].mkdir(parents=True)
            (fixture["desktop"] / "private-user-file.txt").write_text("do-not-copy\n", encoding="utf-8")
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)

            state = json.loads(journal.read_text(encoding="utf-8"))
            record = next(item for item in state["files"] if item["key"] == "desktop-link")
            self.assertEqual(record["kind"], "directory")
            self.assertIsNone(record["backupPath"])
            self.assertIsNotNone(record["directoryIdentity"])
            self.assertNotIn(
                "do-not-copy",
                "\n".join(
                    path.read_text(encoding="utf-8", errors="ignore")
                    for path in journal.parent.rglob("*")
                    if path.is_file()
                ),
            )

            self._run("restore-services", "--state", str(journal))
            self._run("verify", "--state", str(journal))
            self._run("rollback", "--state", str(journal))

    def test_verify_rejects_prior_missing_control_path_created_after_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)
            fixture["location"].write_text('{"concurrent":"created"}\n', encoding="utf-8")
            self._run("restore-services", "--state", str(journal))

            result = self._run("verify", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("protected update state changed unexpectedly: location", result.stderr)
            fixture["location"].unlink()
            self._run("rollback", "--state", str(journal))

    def test_verify_rejects_cli_mode_and_symlink_target_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            target_a = Path(tmp) / "target-a"
            target_b = Path(tmp) / "target-b"
            target_a.mkdir()
            target_b.mkdir()
            fixture["desktop"].parent.mkdir(parents=True)
            fixture["desktop"].symlink_to(target_a)
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)
            original_mode = fixture["runtime"].joinpath("bin", "actanara").stat().st_mode & 0o777
            fixture["runtime"].joinpath("bin", "actanara").chmod(0o700)
            fixture["desktop"].unlink()
            fixture["desktop"].symlink_to(target_b)
            self._run("restore-services", "--state", str(journal))

            result = self._run("verify", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("protected update state changed unexpectedly", result.stderr)
            fixture["runtime"].joinpath("bin", "actanara").chmod(original_mode)
            fixture["desktop"].unlink()
            fixture["desktop"].symlink_to(target_a)
            self._run("rollback", "--state", str(journal))

    def test_rollback_keeps_services_stopped_after_protected_state_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            server, server_thread, port = self._start_health_server()
            label = "com.actanara.dashboard"
            fixture["settings"].write_text(
                json.dumps(
                    {
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": port,
                            "healthPath": "/health",
                            "serviceLabel": label,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            self._write_runtime_plist(
                launch_agents / f"{label}.plist",
                label=label,
                runtime=fixture["runtime"],
            )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(fake_launchctl, state_dir=state_dir, calls_path=calls_path)
            try:
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    platform="Darwin",
                    launchctl=str(fake_launchctl),
                    uid=0,
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
            self._prepare_and_promote(fixture, journal)
            fixture["settings"].write_text('{"concurrent":"operator-change"}\n', encoding="utf-8")
            calls_before = calls_path.read_text(encoding="utf-8").splitlines()

            result = self._run("rollback", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rollback-failed")
            self.assertIn(
                "services:not-restored-after-pointer-or-control-state-conflict",
                state["rollbackErrors"],
            )
            calls_after = calls_path.read_text(encoding="utf-8").splitlines()[len(calls_before) :]
            self.assertFalse(any(call.startswith(("bootstrap ", "kickstart ")) for call in calls_after))
            self.assertFalse((state_dir / label).exists())

    def test_restore_services_requires_health_on_the_preserved_dashboard_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)

            class HealthHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    body = b'{"status":"ok"}\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format: str, *_args: object) -> None:
                    return

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            closed_port = int(server.server_address[1])
            fixture["settings"].write_text(
                json.dumps(
                    {
                        "dashboard": {
                            "host": "127.0.0.1",
                            "port": closed_port,
                            "healthPath": "/health",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            dashboard_plist = launch_agents / "com.actanara.dashboard.plist"
            self._write_runtime_plist(
                dashboard_plist,
                label="com.actanara.dashboard",
                runtime=fixture["runtime"],
            )
            service_state = root / "launchctl-state"
            service_state.mkdir()
            (service_state / "com.actanara.dashboard").write_text("running\n", encoding="utf-8")
            fake_launchctl = root / "launchctl"
            fake_launchctl.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"state = Path({str(service_state)!r})\n"
                "command = sys.argv[1]\n"
                "if command == 'print':\n"
                "    label = sys.argv[2].rsplit('/', 1)[-1]\n"
                "    value = state / label\n"
                "    if not value.is_file():\n"
                "        raise SystemExit(113)\n"
                "    print('state = ' + value.read_text().strip())\n"
                "elif command == 'bootout':\n"
                "    label = sys.argv[2].rsplit('/', 1)[-1]\n"
                "    (state / label).unlink(missing_ok=True)\n"
                "elif command == 'bootstrap':\n"
                "    label = Path(sys.argv[-1]).stem\n"
                "    (state / label).write_text('running\\n')\n"
                "elif command == 'kickstart':\n"
                "    label = sys.argv[-1].rsplit('/', 1)[-1]\n"
                "    (state / label).write_text('running\\n')\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            try:
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    platform="Darwin",
                    launchctl=str(fake_launchctl),
                    uid=0,
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
            self._prepare_and_promote(fixture, journal)

            result = self._run(
                "restore-services",
                "--state",
                str(journal),
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_HEALTH_TIMEOUT_SECONDS": "0.2",
                },
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("managed dashboard health check failed", result.stderr)
            self.assertIn(f"preserved port {closed_port}", result.stderr)

    def test_invalid_sqlite_fails_closed_without_durable_snapshot_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture["database"].write_bytes(b"not-a-sqlite-database")
            journal = self._begin(fixture, owner_pid=os.getpid())
            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("Runtime migration ledger is unreadable", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(state.get("mutableStateCaptured", False))
            self.assertFalse(state["serviceStopInitiated"])
            self.assertEqual(
                [item for item in state["files"] if item["key"] == "database"],
                [],
            )
            backups = journal.parent / "backups"
            self.assertEqual(list(backups.glob("*.sqlite3")), [])
            self._run("rollback", "--state", str(journal))

    def test_recover_rolls_back_nonterminal_journal_owned_by_dead_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)
            self._mark_transaction_owner_dead(journal)
            runtime_alias = root / "runtime-alias"
            runtime_alias.symlink_to(fixture["runtime"], target_is_directory=True)

            self._run("recover", "--runtime", str(runtime_alias))

            source = fixture["runtime"] / "app" / "source"
            venv = fixture["runtime"] / ".venv"
            self.assertEqual(os.readlink(source), "releases/old")
            self.assertTrue(venv.is_dir())
            self.assertFalse(venv.is_symlink())
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rolled-back")
            self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())

    def test_failure_between_source_and_venv_promotion_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
            )
            self._run("verify-migration-compatibility", "--state", str(journal))
            self._run("stop", "--state", str(journal))
            self._run(
                "capture-mutable",
                "--state",
                str(journal),
                "--location",
                str(fixture["location"]),
                "--cli-shim",
                str(fixture["runtime"] / "bin" / "actanara"),
                "--user-cli-shim",
                str(fixture["user_cli"]),
                "--desktop-link",
                str(fixture["desktop"]),
            )
            legacy_source_temp = fixture["runtime"] / "app" / ".source.next-fixture-tx"
            legacy_venv_temp = fixture["runtime"] / ".venv.next-fixture-tx"
            legacy_source_temp.write_text("operator-source\n", encoding="utf-8")
            legacy_venv_temp.write_text("operator-venv\n", encoding="utf-8")
            result = self._run(
                "promote",
                "--state",
                str(journal),
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_FAIL_PHASE": "source-pointer-promoted",
                },
            )
            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            source = fixture["runtime"] / "app" / "source"
            venv = fixture["runtime"] / ".venv"
            self.assertEqual(source.resolve(), fixture["candidate_source"].resolve())
            self.assertTrue(venv.is_dir())
            self.assertFalse(venv.is_symlink())
            self.assertEqual(
                legacy_source_temp.read_text(encoding="utf-8"),
                "operator-source\n",
            )
            self.assertEqual(
                legacy_venv_temp.read_text(encoding="utf-8"),
                "operator-venv\n",
            )

            self._run("rollback", "--state", str(journal))

            self.assertEqual(os.readlink(source), "releases/old")
            self.assertTrue(venv.is_dir())
            self.assertEqual((venv / "bin" / "python").read_text(encoding="utf-8"), "old-venv\n")

    def test_repair_commit_keeps_legacy_services_stopped_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            source_identity, venv_identity = self._make_legacy_pointers_concrete(fixture)
            launch_agents = fixture["home"] / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            label = "com.actanara.dashboard.watchdog"
            plist_path = launch_agents / f"{label}.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            str(fixture["runtime"] / ".venv" / "bin" / "python"),
                            str(
                                fixture["runtime"]
                                / "app"
                                / "source"
                                / "advanced"
                                / "dashboard"
                                / "dashboard_launch_agent.py"
                            ),
                            "check",
                            "--url",
                            "http://127.0.0.1:42173/health",
                            "--label",
                            "com.actanara.dashboard",
                            "--restart",
                        ],
                        "EnvironmentVariables": {
                            "ACTANARA_HOME": str(fixture["runtime"]),
                        },
                        "RunAtLoad": True,
                    },
                    handle,
                )
            state_dir = root / "launchctl-state"
            state_dir.mkdir()
            (state_dir / label).write_text("running\n", encoding="utf-8")
            calls_path = root / "launchctl-calls.log"
            fake_launchctl = root / "launchctl"
            self._write_stateful_fake_launchctl(
                fake_launchctl,
                state_dir=state_dir,
                calls_path=calls_path,
            )
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                platform="Darwin",
                launchctl=str(fake_launchctl),
                uid=0,
                mode="repair",
                settings_only_profile_evidence=True,
            )

            self._prepare_stopped_candidate(
                fixture,
                journal,
                allow_legacy_repair=True,
            )
            self.assertFalse((state_dir / label).exists())
            self._run("normalize-service-plists", "--state", str(journal))
            self._run("promote", "--state", str(journal))
            self._run("commit-repair", "--state", str(journal))

            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "committed")
            events = [
                json.loads(line)
                for line in (journal.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn(
                {
                    "event": "repair-committed-services-stopped",
                    "status": "committed",
                },
                [
                    {"event": event["event"], "status": event["status"]}
                    for event in events
                ],
            )
            self.assertTrue(state["serviceStopInitiated"])
            self.assertFalse((state_dir / label).exists())
            mutations = [
                call
                for call in calls_path.read_text(encoding="utf-8").splitlines()
                if call.startswith(("bootout ", "bootstrap ", "kickstart "))
            ]
            self.assertEqual(mutations, [f"bootout gui/0/{label}"])
            source_backup = Path(state["source"]["priorBackupPath"])
            venv_backup = Path(state["venv"]["priorBackupPath"])
            self.assertTrue(source_backup.is_dir())
            self.assertTrue(venv_backup.is_dir())
            self.assertEqual(
                (source_backup.stat().st_dev, source_backup.stat().st_ino),
                source_identity,
            )
            self.assertEqual(
                (venv_backup.stat().st_dev, venv_backup.stat().st_ino),
                venv_identity,
            )
            repair_backups = Path(state["repairBackupPath"])
            self.assertTrue(repair_backups.is_dir())
            self.assertTrue(any(repair_backups.iterdir()))
            self.assertTrue(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )
            self._run("complete-repair", "--state", str(journal))
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_repair_pending_marker_lifecycle_is_bound_to_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._commit_repair_transaction(fixture)
            marker = fixture["runtime"] / "app" / ".repair-configuration-pending"

            self.assertTrue(marker.is_file())
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.stat(follow_symlinks=False).st_mode & 0o777, 0o600)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(
                marker.read_text(encoding="ascii"),
                f"{state['txId']}\n",
            )
            self.assertFalse(state["repairConfigurationComplete"])

            self._run("complete-repair", "--state", str(journal))

            self.assertFalse(marker.exists())
            self.assertFalse(marker.is_symlink())
            completed = json.loads(journal.read_text(encoding="utf-8"))
            self.assertTrue(completed["repairConfigurationComplete"])

    def test_linux_pending_repair_keeps_durable_owner_after_owner_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._commit_repair_transaction(fixture)
            lock = fixture["runtime"] / "app" / ".update-transaction.lock"
            marker = fixture["runtime"] / "app" / ".repair-configuration-pending"
            self.assertTrue(lock.exists())
            self.assertTrue(marker.exists())

            concurrent = self._begin_only_result(
                fixture,
                mode="repair",
                tx_id="concurrent-repair",
            )
            self.assertEqual(concurrent.returncode, 70, concurrent.stdout + concurrent.stderr)
            self.assertIn("committed Runtime repair is pending", concurrent.stderr)
            self._mark_transaction_owner_dead(journal)
            self._run("recover", "--runtime", str(fixture["runtime"]))
            self.assertTrue(lock.exists())
            self.assertTrue(marker.exists())

            upgrade = self._begin_only_result(
                fixture,
                mode="upgrade",
                tx_id="upgrade-while-repair-pending",
            )
            self.assertEqual(upgrade.returncode, 70, upgrade.stdout + upgrade.stderr)
            self.assertIn("committed Runtime repair is pending", upgrade.stderr)
            self.assertFalse(
                (
                    fixture["runtime"]
                    / "app"
                    / "update-transactions"
                    / "upgrade-while-repair-pending"
                ).exists()
            )

            retry = self._begin_only_result(
                fixture,
                mode="repair",
                tx_id="retry-repair",
            )
            self.assertEqual(retry.returncode, 70, retry.stdout + retry.stderr)
            self.assertIn("committed Runtime repair is pending", retry.stderr)

            # Only the installer holding the inherited user mutation guard may
            # resume and complete this committed journal.  A direct helper
            # invocation after the original owner exited remains rejected.
            complete = self._run(
                "complete-repair",
                "--state",
                str(journal),
                check=False,
            )
            self.assertEqual(complete.returncode, 70)
            self.assertTrue(lock.exists())
            self.assertTrue(marker.exists())

    def test_marker_only_legacy_pending_repair_cannot_start_a_new_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._commit_repair_transaction(fixture)
            lock = fixture["runtime"] / "app" / ".update-transaction.lock"
            marker = fixture["runtime"] / "app" / ".repair-configuration-pending"
            lock.unlink()

            retry = self._begin_only_result(
                fixture,
                mode="repair",
                tx_id="replacement-repair",
            )

            self.assertEqual(retry.returncode, 70, retry.stdout + retry.stderr)
            self.assertIn("resume its existing journal", retry.stderr)
            self.assertTrue(marker.exists())
            self.assertFalse(
                (
                    fixture["runtime"]
                    / "app"
                    / "update-transactions"
                    / "replacement-repair"
                ).exists()
            )
            self._run("complete-repair", "--state", str(journal))

    def test_resumed_repair_completion_accepts_only_the_inherited_user_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._commit_repair_transaction(fixture)
            self._mark_transaction_owner_dead(journal)
            self._run("recover", "--runtime", str(fixture["runtime"]))

            sys.path.insert(0, str(ROOT / "src"))
            try:
                from data_foundation.runtime_mutation import (
                    current_runtime_mutation_guard_fd,
                    runtime_mutation_guard,
                )

                with runtime_mutation_guard(fixture["runtime"]):
                    guard_fd = current_runtime_mutation_guard_fd()
                    self.assertIsNotNone(guard_fd)
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(HELPER),
                            "complete-repair",
                            "--state",
                            str(journal),
                        ],
                        env={
                            **os.environ,
                            "ACTANARA_RUNTIME_MUTATION_GUARD_FD": str(guard_fd),
                        },
                        pass_fds=(guard_fd,),
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=30,
                    )
            finally:
                sys.path.remove(str(ROOT / "src"))

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )
            self.assertFalse(
                (fixture["runtime"] / "app" / ".repair-configuration-pending").exists()
            )

    def test_linux_complete_repair_rechecks_candidate_pointer_before_marker_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._commit_repair_transaction(fixture)
            marker = fixture["runtime"] / "app" / ".repair-configuration-pending"
            source = fixture["runtime"] / "app" / "source"
            source.unlink()
            source.symlink_to("releases/old")

            result = self._run(
                "complete-repair",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("source pointer", result.stderr)
            self.assertTrue(marker.exists())
            source.unlink()
            source.symlink_to(fixture["candidate_source"])
            self._run("complete-repair", "--state", str(journal))

    def test_linux_repair_commit_and_completion_ack_windows_are_idempotent(self):
        for phase in (
            "repair-commit-journaled-before-lock-release",
            "repair-configuration-complete-before-marker-clear",
            "repair-marker-cleared-before-lock-release",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(
                    fixture,
                    owner_pid=os.getpid(),
                    mode="repair",
                    settings_only_profile_evidence=True,
                )
                self._prepare_stopped_candidate(
                    fixture,
                    journal,
                    allow_legacy_repair=True,
                )
                self._run("normalize-service-plists", "--state", str(journal))
                self._run("promote", "--state", str(journal))
                if phase.startswith("repair-commit"):
                    failed = self._run(
                        "commit-repair",
                        "--state",
                        str(journal),
                        check=False,
                        env={
                            "ACTANARA_INSTALL_TEST_MODE": "1",
                            "ACTANARA_INSTALL_TEST_FAIL_PHASE": phase,
                        },
                    )
                    self.assertEqual(failed.returncode, 70, failed.stdout + failed.stderr)
                else:
                    self._run("commit-repair", "--state", str(journal))
                    failed = self._run(
                        "complete-repair",
                        "--state",
                        str(journal),
                        check=False,
                        env={
                            "ACTANARA_INSTALL_TEST_MODE": "1",
                            "ACTANARA_INSTALL_TEST_FAIL_PHASE": phase,
                        },
                    )
                    self.assertEqual(failed.returncode, 70, failed.stdout + failed.stderr)

                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "committed")
                self._run("complete-repair", "--state", str(journal))
                self.assertFalse(
                    (fixture["runtime"] / "app" / ".repair-configuration-pending").exists()
                )
                self.assertFalse(
                    (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
                )

    def test_complete_repair_preserves_pending_marker_for_wrong_journal_or_marker(self):
        for attack in ("wrong-journal", "wrong-marker"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._commit_repair_transaction(fixture)
                marker = fixture["runtime"] / "app" / ".repair-configuration-pending"
                self.assertTrue(marker.is_file())
                if attack == "wrong-journal":
                    wrong_dir = (
                        fixture["runtime"]
                        / "app"
                        / "update-transactions"
                        / "wrong-tx"
                    )
                    wrong_dir.mkdir()
                    wrong_journal = wrong_dir / "journal.json"
                    shutil.copy2(journal, wrong_journal)
                    state_argument = wrong_journal
                else:
                    marker.write_text("wrong-tx\n", encoding="ascii")
                    marker.chmod(0o600)
                    state_argument = journal
                preserved = marker.read_bytes()

                result = self._run(
                    "complete-repair",
                    "--state",
                    str(state_argument),
                    check=False,
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_bytes(), preserved)
                state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertFalse(state["repairConfigurationComplete"])

    def test_repair_pre_promotion_failure_rolls_back_concrete_pointer_inodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            source_identity, venv_identity = self._make_legacy_pointers_concrete(fixture)
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                mode="repair",
                settings_only_profile_evidence=True,
            )
            self._prepare_stopped_candidate(
                fixture,
                journal,
                allow_legacy_repair=True,
            )
            self._run("normalize-service-plists", "--state", str(journal))

            result = self._run(
                "promote",
                "--state",
                str(journal),
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_FAIL_PHASE": "source-promotion-armed",
                },
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            source = fixture["runtime"] / "app" / "source"
            venv = fixture["runtime"] / ".venv"
            self.assertFalse(source.is_symlink())
            self.assertFalse(venv.is_symlink())
            self.assertEqual(
                (source.stat().st_dev, source.stat().st_ino),
                source_identity,
            )
            self.assertEqual(
                (venv.stat().st_dev, venv.stat().st_ino),
                venv_identity,
            )

            self._run("rollback", "--state", str(journal))

            self.assertFalse(source.is_symlink())
            self.assertFalse(venv.is_symlink())
            self.assertEqual(
                (source.stat().st_dev, source.stat().st_ino),
                source_identity,
            )
            self.assertEqual(
                (venv.stat().st_dev, venv.stat().st_ino),
                venv_identity,
            )
            self.assertEqual(
                (source / "pyproject.toml").read_text(encoding="utf-8"),
                '[project]\nname="old"\nversion="0"\n',
            )
            self.assertEqual(
                (venv / "bin" / "python").read_text(encoding="utf-8"),
                "old-venv\n",
            )
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rolled-back")
            self.assertFalse(
                (fixture["runtime"] / "app" / ".update-transaction.lock").exists()
            )

    def test_sigkill_pointer_promotion_windows_recover_idempotently(self):
        cases = (
            ("source-promotion-armed", "source-promotion-armed"),
            ("source-pointer-replaced-before-journal", "source-promotion-armed"),
            ("source-pointer-promoted", "source-promoted"),
            ("venv-promotion-armed", "venv-promotion-armed"),
            ("venv-prior-moved-before-journal", "venv-promotion-armed"),
            ("venv-pointer-replaced-before-journal", "prior-venv-moved"),
            ("venv-pointer-promoted", "venv-promoted"),
        )
        for kill_phase, durable_phase in cases:
            with self.subTest(kill_phase=kill_phase), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(fixture, owner_pid=os.getpid())
                self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "source",
                    "--candidate",
                    str(fixture["candidate_source"]),
                )
                self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "venv",
                    "--candidate",
                    str(fixture["candidate_venv"]),
                )
                self._run("verify-migration-compatibility", "--state", str(journal))
                self._run("stop", "--state", str(journal))
                self._run(
                    "capture-mutable",
                    "--state",
                    str(journal),
                    "--location",
                    str(fixture["location"]),
                    "--cli-shim",
                    str(fixture["runtime"] / "bin" / "actanara"),
                    "--user-cli-shim",
                    str(fixture["user_cli"]),
                    "--desktop-link",
                    str(fixture["desktop"]),
                    "--shell-profile",
                    str(fixture["profile"]),
                )

                result = self._run(
                    "promote",
                    "--state",
                    str(journal),
                    check=False,
                    env={
                        "ACTANARA_INSTALL_TEST_MODE": "1",
                        "ACTANARA_INSTALL_TEST_KILL_PHASE": kill_phase,
                    },
                )

                self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                killed_state = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(killed_state["phase"], durable_phase)
                self.assertTrue((fixture["runtime"] / "app" / ".update-transaction.lock").exists())
                self._mark_transaction_owner_dead(journal)

                self._run("recover", "--runtime", str(fixture["runtime"]))

                source = fixture["runtime"] / "app" / "source"
                venv = fixture["runtime"] / ".venv"
                self.assertEqual(os.readlink(source), "releases/old")
                self.assertTrue(venv.is_dir())
                self.assertFalse(venv.is_symlink())
                self.assertEqual((venv / "bin" / "python").read_text(encoding="utf-8"), "old-venv\n")
                self.assertEqual(self._database_values(fixture["database"]), ["before-update"])
                self.assertEqual(self._database_integrity(fixture["database"]), "ok")
                recovered = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(recovered["status"], "rolled-back")
                self.assertEqual(recovered["rollbackErrors"], [])
                self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())
                self._run("recover", "--runtime", str(fixture["runtime"]))
                self.assertEqual(os.readlink(source), "releases/old")
                self.assertTrue(venv.is_dir())
                self.assertFalse(venv.is_symlink())

    def test_sigkill_after_committed_journal_keeps_candidate_and_releases_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._prepare_and_promote(fixture, journal)
            self._run("restore-services", "--state", str(journal))
            self._run("verify", "--state", str(journal))

            result = self._run(
                "commit",
                "--state",
                str(journal),
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_KILL_PHASE": "commit-journaled-before-lock-release",
                },
            )

            self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "committed")
            lock = fixture["runtime"] / "app" / ".update-transaction.lock"
            self.assertTrue(lock.exists())
            self._mark_transaction_owner_dead(journal)

            self._run("recover", "--runtime", str(fixture["runtime"]))
            self._run("recover", "--runtime", str(fixture["runtime"]))

            self.assertFalse(lock.exists())
            self.assertEqual(
                (fixture["runtime"] / "app" / "source").resolve(),
                fixture["candidate_source"].resolve(),
            )
            self.assertEqual(
                (fixture["runtime"] / ".venv").resolve(),
                fixture["candidate_venv"].resolve(),
            )
            self.assertEqual(
                json.loads(journal.read_text(encoding="utf-8"))["status"],
                "committed",
            )

    def test_sigkill_after_rolled_back_journal_recovery_only_releases_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())

            result = self._run(
                "rollback",
                "--state",
                str(journal),
                check=False,
                env={
                    "ACTANARA_INSTALL_TEST_MODE": "1",
                    "ACTANARA_INSTALL_TEST_KILL_PHASE": (
                        "rollback-journaled-before-lock-release"
                    ),
                },
            )

            self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "rolled-back")
            lock = fixture["runtime"] / "app" / ".update-transaction.lock"
            self.assertTrue(lock.exists())

            self._run("recover", "--runtime", str(fixture["runtime"]))
            self._run("recover", "--runtime", str(fixture["runtime"]))

            self.assertFalse(lock.exists())
            self.assertEqual(
                json.loads(journal.read_text(encoding="utf-8"))["status"],
                "rolled-back",
            )

    def test_venv_dependency_marker_sha_is_bound_when_candidate_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())

            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
            )

            marker = fixture["candidate_venv"] / ".actanara-dependencies.json"
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(
                state["venv"]["dependencyMarkerSha256"],
                hashlib.sha256(marker.read_bytes()).hexdigest(),
            )
            self.assertEqual(marker.stat(follow_symlinks=False).st_nlink, 1)
            self.assertEqual(marker.stat(follow_symlinks=False).st_uid, os.getuid())
            self.assertEqual(marker.stat(follow_symlinks=False).st_mode & 0o777, 0o444)
            self._run("rollback", "--state", str(journal))

    def test_venv_dependency_marker_missing_is_rejected_before_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            marker = fixture["candidate_venv"] / ".actanara-dependencies.json"
            marker.unlink()

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("dependency marker is missing", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "prepared")
            self.assertNotIn("dependencyMarkerSha256", state["venv"])
            self.assertTrue(
                (fixture["candidate_venv"] / ".actanara-update-owner").is_file()
            )
            self.assertFalse(state["serviceStopInitiated"])
            self._run("rollback", "--state", str(journal))

    def test_venv_dependency_marker_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            journal = self._begin(fixture, owner_pid=os.getpid())
            marker = fixture["candidate_venv"] / ".actanara-dependencies.json"
            marker.unlink()
            external = root / "operator-marker.json"
            external.write_text("operator-owned\n", encoding="utf-8")
            external.chmod(0o444)
            marker.symlink_to(external)

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("regular non-symlink", result.stderr)
            self.assertTrue(marker.is_symlink())
            self.assertEqual(external.read_text(encoding="utf-8"), "operator-owned\n")
            self.assertFalse(
                json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
            )
            self._run("rollback", "--state", str(journal))
            self.assertEqual(external.read_text(encoding="utf-8"), "operator-owned\n")

    def test_venv_dependency_marker_requires_exact_0444_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            marker = fixture["candidate_venv"] / ".actanara-dependencies.json"
            marker.chmod(0o644)

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("immutable 0444 permissions", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "prepared")
            self.assertFalse(state["serviceStopInitiated"])
            self._run("rollback", "--state", str(journal))

    def test_venv_dependency_marker_rejects_duplicate_keys_and_nan(self):
        payload = self._dependency_marker_payload()
        valid_text = json.dumps(payload, sort_keys=True)
        cases = {
            "duplicate-key": (
                "{\"schemaVersion\":1," + valid_text[1:]
            ).encode("utf-8"),
            "nan": json.dumps(
                {**payload, "schemaVersion": float("nan")},
                sort_keys=True,
            ).encode("utf-8"),
        }
        for name, raw in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(fixture, owner_pid=os.getpid())
                self._write_dependency_marker(fixture["candidate_venv"], raw=raw)

                result = self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "venv",
                    "--candidate",
                    str(fixture["candidate_venv"]),
                    check=False,
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn("not strict JSON", result.stderr)
                self.assertFalse(
                    json.loads(journal.read_text(encoding="utf-8"))["serviceStopInitiated"]
                )
                self._run("rollback", "--state", str(journal))

    def test_venv_dependency_marker_schema_is_revalidated_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._record_and_verify_source_candidate(fixture, journal)
            payload = self._dependency_marker_payload()
            payload.pop("distributions")
            marker = self._write_dependency_marker(
                fixture["candidate_venv"],
                payload=payload,
            )
            state = json.loads(journal.read_text(encoding="utf-8"))
            state["venv"]["dependencyMarkerSha256"] = hashlib.sha256(
                marker.read_bytes()
            ).hexdigest()
            journal.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("invalid exact schema", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            self._run("rollback", "--state", str(journal))

    def test_venv_dependency_marker_hash_is_revalidated_at_every_forward_gate(self):
        for gate in ("stop", "promote", "verify", "commit"):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                journal = self._begin(fixture, owner_pid=os.getpid())
                if gate == "stop":
                    self._record_and_verify_source_candidate(fixture, journal)
                elif gate == "promote":
                    self._prepare_stopped_candidate(fixture, journal)
                    self._run("normalize-service-plists", "--state", str(journal))
                else:
                    self._prepare_and_promote(fixture, journal)
                    self._run("restore-services", "--state", str(journal))
                    if gate == "commit":
                        self._run("verify", "--state", str(journal))

                marker = fixture["candidate_venv"] / ".actanara-dependencies.json"
                original = marker.read_bytes()
                self._write_dependency_marker(
                    fixture["candidate_venv"],
                    raw=original + b" ",
                )

                result = self._run(gate, "--state", str(journal), check=False)

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn("marker bytes changed after recording", result.stderr)
                state = json.loads(journal.read_text(encoding="utf-8"))
                if gate == "stop":
                    self.assertFalse(state["serviceStopInitiated"])
                if gate == "promote":
                    self.assertEqual(
                        os.readlink(fixture["runtime"] / "app" / "source"),
                        "releases/old",
                    )
                self.assertNotEqual(state["status"], "committed")
                self._run("rollback", "--state", str(journal))

    def test_source_only_transaction_does_not_require_candidate_venv_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                mode="source-only",
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run("verify-migration-compatibility", "--state", str(journal))

            self._run("stop", "--state", str(journal))

            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "stopped")
            binding = state["venv"]["activeReuseBinding"]
            active_marker = fixture["runtime"] / ".venv" / ".actanara-dependencies.json"
            self.assertEqual(
                binding["dependencyMarkerSha256"],
                hashlib.sha256(active_marker.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                (binding["targetDevice"], binding["targetInode"]),
                (
                    (fixture["runtime"] / ".venv").stat().st_dev,
                    (fixture["runtime"] / ".venv").stat().st_ino,
                ),
            )
            self.assertNotIn("dependencyMarkerSha256", state["venv"])
            self.assertTrue((fixture["runtime"] / ".venv").is_dir())
            self.assertFalse((fixture["runtime"] / ".venv").is_symlink())
            self._run("rollback", "--state", str(journal))

    def test_source_only_active_marker_race_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                mode="source-only",
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run("verify-migration-compatibility", "--state", str(journal))
            active_venv = fixture["runtime"] / ".venv"
            marker = active_venv / ".actanara-dependencies.json"
            original = marker.read_bytes()
            self._write_dependency_marker(active_venv, raw=original + b" ")

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("active reused venv dependency marker bytes changed", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            self._write_dependency_marker(active_venv, raw=original)
            self._run("rollback", "--state", str(journal))

    def test_source_only_active_venv_pointer_race_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            runtime = fixture["runtime"]
            active_pointer = runtime / ".venv"
            original_generation = runtime / "app" / "venvs" / "old-generation"
            original_generation.parent.mkdir(parents=True, exist_ok=True)
            active_pointer.rename(original_generation)
            active_pointer.symlink_to("app/venvs/old-generation")
            journal = self._begin(
                fixture,
                owner_pid=os.getpid(),
                mode="source-only",
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run("verify-migration-compatibility", "--state", str(journal))
            raced_generation = runtime / "app" / "venvs" / "raced-generation"
            shutil.copytree(original_generation, raced_generation)
            self.assertEqual(
                hashlib.sha256(
                    (original_generation / ".actanara-dependencies.json").read_bytes()
                ).hexdigest(),
                hashlib.sha256(
                    (raced_generation / ".actanara-dependencies.json").read_bytes()
                ).hexdigest(),
            )
            active_pointer.unlink()
            active_pointer.symlink_to("app/venvs/raced-generation")

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("pointer, target, or Python identity changed", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            active_pointer.unlink()
            active_pointer.symlink_to("app/venvs/old-generation")
            self._run("rollback", "--state", str(journal))

    def test_rebuild_profile_settings_race_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._record_and_verify_source_candidate(fixture, journal)
            original = fixture["settings"].read_bytes()
            fixture["settings"].write_bytes(original + b" ")

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("critical update control state changed concurrently: settings", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            fixture["settings"].write_bytes(original)
            self._run("rollback", "--state", str(journal))

    def test_rebuild_profile_marker_race_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._record_and_verify_source_candidate(fixture, journal)
            marker = fixture["runtime"] / ".venv" / ".actanara-dependencies.json"
            original = marker.read_bytes()
            self._write_dependency_marker(fixture["runtime"] / ".venv", raw=original + b" ")

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("dependency profile evidence changed", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            self._write_dependency_marker(fixture["runtime"] / ".venv", raw=original)
            self._run("rollback", "--state", str(journal))

    def test_legacy_missing_marker_appearance_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            active_venv = fixture["runtime"] / ".venv"
            marker = active_venv / ".actanara-dependencies.json"
            marker.unlink()
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._record_and_verify_source_candidate(fixture, journal)
            self._write_dependency_marker(active_venv)

            result = self._run("stop", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("dependency profile evidence changed", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "candidate-staged")
            self.assertFalse(state["serviceStopInitiated"])
            marker.unlink()
            self._run("rollback", "--state", str(journal))

    def test_payload_tamper_is_rejected_before_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "venv",
                "--candidate",
                str(fixture["candidate_venv"]),
            )
            self._run("verify-migration-compatibility", "--state", str(journal))
            self._run("stop", "--state", str(journal))
            self._run(
                "capture-mutable",
                "--state",
                str(journal),
                "--location",
                str(fixture["location"]),
                "--cli-shim",
                str(fixture["runtime"] / "bin" / "actanara"),
                "--user-cli-shim",
                str(fixture["user_cli"]),
                "--desktop-link",
                str(fixture["desktop"]),
            )
            (fixture["candidate_source"] / "pyproject.toml").write_text("tampered\n", encoding="utf-8")

            result = self._run("promote", "--state", str(journal), check=False)

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("changed after scan", result.stderr)
            self.assertEqual(os.readlink(fixture["runtime"] / "app" / "source"), "releases/old")
            self._run("rollback", "--state", str(journal))

    def test_private_v1_candidate_manifest_is_rejected_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            manifest_path = fixture["candidate_source"] / ".actanara-runtime-source.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schemaVersion"] = 1
            manifest["sourceRoot"] = "/Users/private-operator/Desktop/actanara"
            manifest.pop("sourceLocator", None)
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

            journal = self._begin(fixture, owner_pid=os.getpid(), materialize_candidates=False)
            temporary = Path(
                self._run(
                    "reserve-artifact",
                    "--state",
                    str(journal),
                    "--kind",
                    "source-temp",
                ).stdout.strip()
            )
            shutil.copytree(fixture["candidate_source"], temporary, dirs_exist_ok=True)
            candidate = Path(
                self._run("promote-source-artifact", "--state", str(journal)).stdout.strip()
            )

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(candidate),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("privacy schema", result.stderr)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertFalse(state.get("serviceStopInitiated"))
            self.assertNotIn("/Users/private-operator", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_v2_candidate_manifest_rejects_unknown_private_field_and_release_mismatch(self):
        private_marker = "/Users/private-operator/Desktop/actanara"
        for mutation, expected in (
            ({"debugPath": private_marker}, "exact schema"),
            (
                {
                    "releaseLocator": {
                        "kind": "runtime-relative",
                        "pathComponents": ["app", "releases", "another-tx"],
                    }
                },
                "does not match its candidate",
            ),
            ({"pyprojectVersion": private_marker}, "invalid project version"),
            (
                {
                    "git": {
                        "available": True,
                        "commit": "0" * 40,
                        "branch": "main",
                        "remote": 123,
                        "dirty": False,
                    }
                },
                "unsafe git remote",
            ),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                manifest_path = fixture["candidate_source"] / ".actanara-runtime-source.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(mutation)
                manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
                journal = self._begin(fixture, owner_pid=os.getpid())

                result = self._run(
                    "record-candidate",
                    "--state",
                    str(journal),
                    "--kind",
                    "source",
                    "--candidate",
                    str(fixture["candidate_source"]),
                    check=False,
                )

                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn(private_marker, result.stderr)
                self._run("rollback", "--state", str(journal))

    def test_missing_candidate_manifest_hash_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            journal = self._begin(fixture, owner_pid=os.getpid())
            self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
            )
            state = json.loads(journal.read_text(encoding="utf-8"))
            state["source"]["candidateSha256"] = None
            journal.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

            result = self._run(
                "verify-migration-compatibility",
                "--state",
                str(journal),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("no bound release-clean hash", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_duplicate_payload_inventory_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            manifest_path = fixture["candidate_source"] / ".actanara-runtime-source.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["payload"]["files"].append(dict(manifest["payload"]["files"][0]))
            manifest["payload"]["fileCount"] += 1
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            journal = self._begin(fixture, owner_pid=os.getpid())

            result = self._run(
                "record-candidate",
                "--state",
                str(journal),
                "--kind",
                "source",
                "--candidate",
                str(fixture["candidate_source"]),
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("duplicate path", result.stderr)
            self._run("rollback", "--state", str(journal))

    def test_unexpected_launchctl_probe_error_aborts_before_lock_is_left_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fake = Path(tmp) / "launchctl"
            fake.write_text("#!/bin/sh\nexit 70\n", encoding="utf-8")
            fake.chmod(0o755)
            result = self._run(
                "begin",
                "--runtime",
                str(fixture["runtime"]),
                "--home",
                str(fixture["home"]),
                "--source-pointer",
                str(fixture["runtime"] / "app" / "source"),
                "--venv-pointer",
                str(fixture["runtime"] / ".venv"),
                "--mode",
                "upgrade",
                "--tx-id",
                "probe-error",
                "--owner-pid",
                str(os.getpid()),
                "--platform",
                "Darwin",
                "--launchctl",
                str(fake),
                "--uid",
                "501",
                check=False,
            )
            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn("unexpected error", result.stderr)
            self.assertFalse((fixture["runtime"] / "app" / ".update-transaction.lock").exists())


if __name__ == "__main__":
    unittest.main()
