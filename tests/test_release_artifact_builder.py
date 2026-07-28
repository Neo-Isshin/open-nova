import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

from tools.release import build_release as release_builder


LOCKED_TOOLCHAIN = {
    "python": "3.14.6",
    "pythonImplementation": "CPython",
    "build": "1.5.1",
    "packaging": "26.2",
    "pyproject-hooks": "1.2.0",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
}


def _entry(root: Path, relative: str, mode: str = "100644") -> release_builder.SourceEntry:
    path = root / relative
    if mode == "120000":
        payload = os.fsencode(os.readlink(path))
    else:
        payload = path.read_bytes()
    return release_builder.SourceEntry(
        path=relative,
        mode=mode,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repository(root: Path, *, version: str = "7.8.9") -> str:
    (root / "src" / "sample").mkdir(parents=True)
    (root / "install").mkdir()
    (root / "tools" / "release").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools==83.0.0", "wheel==0.47.0"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "actanara"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "LICENSE").write_text("fixture license\n", encoding="utf-8")
    (root / "MANIFEST.in").write_text("prune tests\n", encoding="utf-8")
    (root / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".release-ignored/\n", encoding="utf-8")
    (root / "src" / "sample" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = root / "install" / "install.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    bootstrap = root / "install" / "bootstrap.sh"
    bootstrap.write_text(
        "#!/usr/bin/env zsh\n"
        "if true; then\n"
        "resolve_official_main_commit() { return 0; }\n"
        "fi\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)
    linux_bootstrap = root / "install" / "bootstrap-linux.sh"
    linux_bootstrap.write_text("#!/bin/sh\nif true; then\nexit 0\nfi\n", encoding="utf-8")
    linux_bootstrap.chmod(0o755)
    setup = root / "install" / "setup.sh"
    setup.write_text(
        "#!/bin/sh\n"
        "if true; then\n"
        'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n'
        "select_platform_adapter() {\n"
        '  ADAPTER_PATH="install/bootstrap.sh"\n'
        '  ADAPTER_PATH="install/bootstrap-linux.sh"\n'
        "}\n"
        "download_exact_adapter() { return 0; }\n"
        "fi\n",
        encoding="utf-8",
    )
    setup.chmod(0o755)
    (root / "install" / "dependency_contract.py").write_text(
        'PRODUCT = "actanara"\n',
        encoding="utf-8",
    )
    (root / "install" / "runtime-dependencies.lock.json").write_text(
        '{"product":"actanara","schemaVersion":1}\n',
        encoding="utf-8",
    )
    (root / "tools" / "release" / "generate_runtime_lock.py").write_text(
        'raise SystemExit("release-maintainer-only")\n',
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Release Fixture")
    _git(root, "config", "user.email", "release-fixture@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def _write_fake_python_packages(
    source: release_builder.FrozenSource,
    output: Path,
    **_kwargs: object,
) -> tuple[Path, Path]:
    wheel = output / f"actanara-{source.version}-py3-none-any.whl"
    sdist = output / f"actanara-{source.version}.tar.gz"
    wheel.write_bytes(b"deterministic wheel fixture\n")
    sdist.write_bytes(b"deterministic sdist fixture\n")
    return wheel, sdist


class ReleaseArtifactBuilderTests(unittest.TestCase):
    def test_repository_release_metadata_and_commands_are_exactly_locked(self):
        root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["build-system"]["requires"],
            ["setuptools==83.0.0", "wheel==0.47.0"],
        )
        self.assertEqual(
            (root / "requirements-release.txt").read_text(encoding="utf-8").splitlines(),
            [
                "build==1.5.1",
                "packaging==26.2",
                "pyproject-hooks==1.2.0",
                "setuptools==83.0.0",
                "wheel==0.47.0",
            ],
        )
        manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("prune tests", manifest)
        self.assertIn("prune tools", manifest)
        self.assertIn("exclude requirements-release.txt", manifest)
        for readme_name in ("README.md", "README.zh-CN.md"):
            readme = (root / readme_name).read_text(encoding="utf-8")
            self.assertIn("python -B -m pip install -r requirements-release.txt", readme)
            self.assertIn("python -B -m tools.release.build_release", readme)

    def test_project_version_is_read_from_pyproject_without_release_hardcode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "actanara"\nversion = "9.4.2"\n',
                encoding="utf-8",
            )

            self.assertEqual(release_builder.read_project_version(root), "9.4.2")

    def test_full_commit_contract_accepts_sha1_and_sha256_object_formats(self):
        self.assertIsNotNone(release_builder.FULL_COMMIT_RE.fullmatch("a" * 40))
        self.assertIsNotNone(release_builder.FULL_COMMIT_RE.fullmatch("b" * 64))
        self.assertIsNone(release_builder.FULL_COMMIT_RE.fullmatch("c" * 39))
        self.assertIsNone(release_builder.FULL_COMMIT_RE.fullmatch("d" * 41))

    def test_frozen_source_requires_clean_root_repository_and_preserves_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit = _init_repository(root)

            source = release_builder.inspect_frozen_git_source(
                root,
                expected_commit=commit,
                expected_version="7.8.9",
            )

            self.assertEqual(source.commit, commit)
            self.assertEqual(
                next(entry.mode for entry in source.entries if entry.path == "install/install.sh"),
                "100755",
            )
            (root / "untracked.txt").write_text("must block\n", encoding="utf-8")
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "not clean"):
                release_builder.inspect_frozen_git_source(root)

    def test_complete_source_file_set_includes_ignored_files_and_changes_aggregate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_repository(root)
            ignored = root / ".release-ignored" / "cache.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"ignored-but-frozen\n")

            before = release_builder.inspect_source_file_set(root)
            self.assertIn(".release-ignored/cache.bin", {entry.path for entry in before})
            self.assertNotIn(".git/index", {entry.path for entry in before})
            aggregate_before = release_builder.aggregate_sha256(before)
            self.assertEqual(_git(root, "status", "--porcelain=v1"), "")

            ignored.write_bytes(b"mutated\n")
            after = release_builder.inspect_source_file_set(root)
            self.assertNotEqual(before, after)
            self.assertNotEqual(aggregate_before, release_builder.aggregate_sha256(after))

    def test_release_subprocess_environment_is_a_minimal_allowlist(self):
        injected = {
            "PATH": "/usr/bin:/bin",
            "TMPDIR": "/private/tmp",
            "LANG": "en_US.UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "HOME": "/Users/private-user",
            "PYTHONPATH": "/Users/private-user/injected",
            "CFLAGS": "-I/Users/private-user/include",
            "SETUPTOOLS_SCM_PRETEND_VERSION": "999.0",
            "PIP_INDEX_URL": "https://credential.invalid/simple",
        }
        with mock.patch.dict(os.environ, injected, clear=True):
            environment = release_builder._build_subprocess_environment(1783900800)

        self.assertEqual(environment["PATH"], injected["PATH"])
        self.assertEqual(environment["TMPDIR"], injected["TMPDIR"])
        self.assertEqual(environment["LANG"], injected["LANG"])
        self.assertEqual(environment["SSL_CERT_FILE"], injected["SSL_CERT_FILE"])
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], "1783900800")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        for forbidden in (
            "HOME",
            "PYTHONPATH",
            "CFLAGS",
            "SETUPTOOLS_SCM_PRETEND_VERSION",
            "PIP_INDEX_URL",
        ):
            self.assertNotIn(forbidden, environment)

    def test_release_toolchain_requires_all_exact_versions(self):
        good = subprocess.CompletedProcess(
            ["python", "-B", "-c", "probe"],
            0,
            json.dumps(LOCKED_TOOLCHAIN),
            "",
        )
        with mock.patch.object(release_builder.subprocess, "run", return_value=good) as run:
            self.assertEqual(release_builder.validate_release_toolchain("python"), LOCKED_TOOLCHAIN)
        self.assertIn("-B", run.call_args.args[0])
        self.assertNotIn("PYTHONPATH", run.call_args.kwargs["env"])
        probe = run.call_args.args[0][-1]
        for distribution in release_builder.EXPECTED_RELEASE_TOOLCHAIN:
            with self.subTest(distribution=distribution):
                self.assertIn(f'"{distribution}"', probe)

        wrong = dict(LOCKED_TOOLCHAIN, wheel="0.46.0")
        completed = subprocess.CompletedProcess(
            ["python", "-B", "-c", "probe"],
            0,
            json.dumps(wrong),
            "",
        )
        with mock.patch.object(release_builder.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "wheel==0.46.0"):
                release_builder.validate_release_toolchain("python")

    def test_python_build_subprocess_detects_ignored_source_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            _init_repository(source_root)
            ignored = source_root / ".release-ignored" / "cache.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"before\n")
            source = release_builder.inspect_frozen_git_source(source_root)

            observed: dict[str, object] = {}

            def mutate_source(command, **kwargs):
                observed["command"] = command
                observed["environment"] = kwargs["env"]
                ignored.write_bytes(b"after\n")
                return subprocess.CompletedProcess(command, 1, "", "synthetic failure")

            with mock.patch.object(release_builder.subprocess, "run", side_effect=mutate_source):
                with self.assertRaisesRegex(
                    release_builder.ReleaseBuildError,
                    "including ignored files",
                ):
                    release_builder.build_python_packages(
                        source,
                        root / "output",
                        source_date_epoch=1783900800,
                        python="python",
                    )

            self.assertIn("-B", observed["command"])
            self.assertIn("--no-isolation", observed["command"])
            self.assertNotIn("PYTHONPATH", observed["environment"])

    def test_runtime_payload_path_policy_has_true_and_false_positives(self):
        allowed = (
            "src/settings_parser.py",
            "src/contest_result.py",
            "src/llm_provider_test.py",
            "advanced/runtime_contract.py",
            "install/environment_reference.py",
            "install/dependency_contract.py",
            "install/runtime-dependencies.lock.json",
        )
        for relative in allowed:
            with self.subTest(allowed=relative):
                self.assertIsNone(release_builder._payload_path_violation(relative))

        blocked = (
            "src/tests/test_contract.py",
            "advanced/cache/data.json",
            "install/node_modules/tool.js",
            "src/actanara.egg-info/PKG-INFO",
            "src/state.sqlite3",
            "src/state.sqlite-wal",
            "src/service.log",
            "src/.env.example",
            "src/__pycache__/module.pyc",
            "install/build/output.bin",
        )
        for relative in blocked:
            with self.subTest(blocked=relative):
                self.assertIsNotNone(release_builder._payload_path_violation(relative))
        self.assertFalse(release_builder.is_runtime_payload_path("tools/release/build_release.py"))
        self.assertFalse(
            release_builder.is_runtime_payload_path("tools/release/generate_runtime_lock.py")
        )
        self.assertFalse(release_builder.is_runtime_payload_path("tests/test_release_artifact_builder.py"))
        self.assertFalse(release_builder.is_runtime_payload_path("requirements-release.txt"))
        self.assertIsNotNone(
            release_builder._package_member_violation("requirements-release.txt")
        )
        self.assertIsNotNone(
            release_builder._package_member_violation(
                "actanara-7.8.9/requirements-release.txt",
                sdist_wrapper=True,
            )
        )

    def test_runtime_archive_is_byte_reproducible_and_preserves_safe_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "install").mkdir()
            script = root / "install" / "install.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            os.symlink("install.sh", root / "install" / "current.sh")
            entries = (
                _entry(root, "install/current.sh", "120000"),
                _entry(root, "install/install.sh", "100755"),
            )
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"

            for output in (first, second):
                release_builder.build_runtime_archive(
                    root,
                    entries,
                    output,
                    prefix="actanara-7.8.9",
                    source_date_epoch=1783900800,
                )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first, "r:gz") as archive:
                script_info = archive.getmember("actanara-7.8.9/install/install.sh")
                link_info = archive.getmember("actanara-7.8.9/install/current.sh")
            self.assertEqual(script_info.mode, 0o755)
            self.assertEqual(script_info.mtime, 1783900800)
            self.assertTrue(link_info.issym())
            self.assertEqual(link_info.linkname, "install.sh")

    def test_relative_symlink_policy_rejects_escape_after_entering_child(self):
        self.assertTrue(
            release_builder._safe_relative_symlink(
                "install/links/current.sh",
                "nested/../../install.sh",
            )
        )
        self.assertFalse(
            release_builder._safe_relative_symlink(
                "install/links/current.sh",
                "nested/../../../../outside.sh",
            )
        )

    def test_runtime_archive_fails_if_frozen_input_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
            entry = _entry(root, "config.py")
            (root / "config.py").write_text("VALUE = 2\n", encoding="utf-8")

            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "changed after freeze"):
                release_builder.build_runtime_archive(
                    root,
                    (entry,),
                    root / "runtime.tar.gz",
                    prefix="actanara-7.8.9",
                    source_date_epoch=1783900800,
                )

    def test_python_package_boundary_allows_metadata_but_rejects_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "actanara-7.8.9-py3-none-any.whl"
            sdist = root / "actanara-7.8.9.tar.gz"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("sample/__init__.py", "VALUE = 1\n")
                archive.writestr("actanara-7.8.9.dist-info/METADATA", "Name: actanara\n")
            with sdist.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=1783900800) as zipped:
                    with tarfile.open(fileobj=zipped, mode="w") as archive:
                        for name, payload in (
                            ("actanara-7.8.9/src/sample/__init__.py", b"VALUE = 1\n"),
                            ("actanara-7.8.9/src/actanara.egg-info/PKG-INFO", b"Name: actanara\n"),
                        ):
                            info = tarfile.TarInfo(name)
                            info.size = len(payload)
                            archive.addfile(info, io.BytesIO(payload))

            release_builder.validate_python_package_contents(wheel, sdist)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("tests/test_release.py", "pass\n")
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "tests"):
                release_builder.validate_python_package_contents(wheel, sdist)

    def test_python_packages_reject_public_source_only_release_tooling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "actanara-7.8.9-py3-none-any.whl"
            sdist = root / "actanara-7.8.9.tar.gz"

            def write_wheel(*, include_tools: bool) -> None:
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("sample/__init__.py", "VALUE = 1\n")
                    if include_tools:
                        archive.writestr("tools/release/build_release.py", "raise SystemExit\n")

            def write_sdist(*, include_tools: bool) -> None:
                members = [("actanara-7.8.9/src/sample/__init__.py", b"VALUE = 1\n")]
                if include_tools:
                    members.append(
                        ("actanara-7.8.9/tools/release/build_release.py", b"raise SystemExit\n")
                    )
                with sdist.open("wb") as raw:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, mtime=1783900800
                    ) as zipped:
                        with tarfile.open(fileobj=zipped, mode="w") as archive:
                            for name, payload in members:
                                info = tarfile.TarInfo(name)
                                info.size = len(payload)
                                archive.addfile(info, io.BytesIO(payload))

            write_wheel(include_tools=True)
            write_sdist(include_tools=False)
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "public-source-only"):
                release_builder.validate_python_package_contents(wheel, sdist)

            write_wheel(include_tools=False)
            write_sdist(include_tools=True)
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "public-source-only"):
                release_builder.validate_python_package_contents(wheel, sdist)

    def test_python_package_privacy_scan_rejects_text_but_skips_binary_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "actanara-7.8.9-py3-none-any.whl"
            sdist = root / "actanara-7.8.9.tar.gz"

            def write_packages(payload: bytes) -> None:
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("sample/payload.dat", payload)
                with sdist.open("wb") as raw:
                    with gzip.GzipFile(
                        filename="",
                        mode="wb",
                        fileobj=raw,
                        mtime=1783900800,
                    ) as zipped:
                        with tarfile.open(fileobj=zipped, mode="w") as archive:
                            safe_payload = b"Name: actanara\n"
                            info = tarfile.TarInfo("actanara-7.8.9/PKG-INFO")
                            info.size = len(safe_payload)
                            archive.addfile(info, io.BytesIO(safe_payload))

            synthetic_private_path = b"cache=/" + b"Users/private-operator/private/build\n"
            synthetic_private_key = b"-----BEGIN " + b"PRIVATE KEY-----\nsynthetic\n"
            for unsafe in (
                synthetic_private_path,
                synthetic_private_key,
                b"Authorization: " + b"Bearer synthetic-secret\n",
            ):
                with self.subTest(unsafe=unsafe.splitlines()[0]):
                    write_packages(unsafe)
                    with self.assertRaisesRegex(
                        release_builder.ReleaseBuildError,
                        "privacy violation",
                    ):
                        release_builder.validate_python_package_privacy(wheel, sdist)

            write_packages(b"\xff\xfe/" + b"Users/private-operator/private/binary\x00")
            release_builder.validate_python_package_privacy(wheel, sdist)

    def test_runtime_dependency_authority_is_package_privacy_safe(self):
        repository = Path(__file__).resolve().parents[1]
        runtime_dependency_files = (
            "install/dependency_contract.py",
            "install/runtime-dependencies.lock.json",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "actanara-7.8.9-py3-none-any.whl"
            sdist = root / "actanara-7.8.9.tar.gz"
            with zipfile.ZipFile(wheel, "w") as archive:
                for relative in runtime_dependency_files:
                    archive.writestr(relative, (repository / relative).read_bytes())
            with sdist.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=1783900800) as zipped:
                    with tarfile.open(fileobj=zipped, mode="w") as archive:
                        for relative in runtime_dependency_files:
                            payload = (repository / relative).read_bytes()
                            info = tarfile.TarInfo(f"actanara-7.8.9/{relative}")
                            info.size = len(payload)
                            archive.addfile(info, io.BytesIO(payload))

            release_builder.validate_python_package_contents(wheel, sdist)
            release_builder.validate_python_package_privacy(wheel, sdist)

    def test_wheel_and_sdist_normalization_removes_input_order_and_timestamp_variance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            normalized_sets = []
            for index in (0, 1):
                input_wheel = root / f"input-{index}.whl"
                input_sdist = root / f"input-{index}.tar.gz"
                members = [
                    ("sample/__init__.py", b"VALUE = 1\n"),
                    ("actanara-7.8.9.dist-info/METADATA", b"Name: actanara\n"),
                ]
                if index:
                    members.reverse()
                with zipfile.ZipFile(input_wheel, "w") as archive:
                    for name, payload in members:
                        info = zipfile.ZipInfo(name, (2024 + index, 1, 2, 3, 4, 6))
                        archive.writestr(info, payload)
                with input_sdist.open("wb") as raw:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, mtime=1700000000 + index
                    ) as zipped:
                        with tarfile.open(fileobj=zipped, mode="w") as archive:
                            for name, payload in members:
                                info = tarfile.TarInfo("actanara-7.8.9/" + name)
                                info.size = len(payload)
                                info.mtime = 1700000000 + index
                                archive.addfile(info, io.BytesIO(payload))

                output_wheel = root / f"normalized-{index}.whl"
                output_sdist = root / f"normalized-{index}.tar.gz"
                release_builder.normalize_wheel(
                    input_wheel,
                    output_wheel,
                    source_date_epoch=1783900800,
                )
                release_builder.normalize_sdist(
                    input_sdist,
                    output_sdist,
                    source_date_epoch=1783900800,
                )
                normalized_sets.append((output_wheel.read_bytes(), output_sdist.read_bytes()))

            self.assertEqual(normalized_sets[0], normalized_sets[1])

    def test_complete_output_is_reproducible_and_contains_no_source_absolute_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            commit = _init_repository(source, version="7.8.9")
            ignored = source / ".release-ignored" / "cache.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"must-remain-byte-identical\n")
            ignored_before = ignored.read_bytes()
            index_before = (source / ".git" / "index").read_bytes()
            outputs = (base / "release-a", base / "release-b")
            with (
                mock.patch.object(
                    release_builder,
                    "validate_release_toolchain",
                    return_value=LOCKED_TOOLCHAIN,
                ),
                mock.patch.object(
                    release_builder,
                    "build_python_packages",
                    side_effect=_write_fake_python_packages,
                ),
            ):
                for output in outputs:
                    result = release_builder.build_release(
                        source,
                        output,
                        source_date_epoch=1783900800,
                        expected_commit=commit,
                        expected_version="7.8.9",
                    )
                    self.assertEqual(result["status"], "built")

            first_files = {path.name: path.read_bytes() for path in outputs[0].iterdir()}
            second_files = {path.name: path.read_bytes() for path in outputs[1].iterdir()}
            self.assertEqual(first_files, second_files)
            self.assertIn("install.sh", result["outputFiles"])
            serialized = b"".join(first_files.values())
            self.assertNotIn(os.fsencode(source), serialized)
            self.assertIn(b"actanara-7.8.9-runtime.tar.gz", first_files["SHA256SUMS"])
            self.assertIn(b"install.sh", first_files["SHA256SUMS"])
            self.assertEqual(
                first_files["install.sh"],
                (source / "install" / "setup.sh")
                .read_bytes()
                .replace(
                    release_builder.STABLE_INSTALL_REF_ANCHOR,
                    (
                        f'SOURCE_REF="${{ACTANARA_INSTALL_REF:-{commit}}}"'
                    ).encode("ascii"),
                    1,
                ),
            )
            self.assertIn(
                f'SOURCE_REF="${{ACTANARA_INSTALL_REF:-{commit}}}"'.encode("ascii"),
                first_files["install.sh"],
            )
            self.assertNotIn(
                release_builder.STABLE_INSTALL_REF_ANCHOR,
                first_files["install.sh"],
            )
            self.assertEqual(int((outputs[0] / "install.sh").stat().st_mtime), 1783900800)
            self.assertEqual((outputs[0] / "install.sh").stat().st_mode & 0o777, 0o755)
            self.assertNotIn(b"SHA256SUMS  SHA256SUMS", first_files["SHA256SUMS"])
            provenance = json.loads(first_files["release-provenance.json"])
            self.assertEqual(provenance["toolchain"], LOCKED_TOOLCHAIN)
            self.assertIn(
                "install.sh",
                {artifact["name"] for artifact in provenance["artifacts"]},
            )
            self.assertTrue(
                provenance["sourceFileSetIncludingIgnored"]["verifiedUnchangedAfterBuild"]
            )
            self.assertEqual(
                provenance["sourceFileSetIncludingIgnored"]["aggregateSha256"],
                result["sourceFileSetIncludingIgnoredAggregateSha256"],
            )
            self.assertEqual((source / ".git" / "index").read_bytes(), index_before)
            self.assertEqual(ignored.read_bytes(), ignored_before)
            self.assertEqual(_git(source, "status", "--porcelain=v1"), "")
            source_manifest = first_files["public-source-manifest.tsv"].decode("utf-8")
            runtime_manifest = first_files["runtime-payload-manifest.tsv"].decode("utf-8")
            for relative in (
                "install/dependency_contract.py",
                "install/runtime-dependencies.lock.json",
            ):
                self.assertIn(f"{relative}\t", source_manifest)
                self.assertIn(f"{relative}\t", runtime_manifest)
            generator = "tools/release/generate_runtime_lock.py"
            self.assertIn(f"{generator}\t", source_manifest)
            self.assertNotIn(f"{generator}\t", runtime_manifest)
            runtime_archive = outputs[0] / "actanara-7.8.9-runtime.tar.gz"
            with tarfile.open(runtime_archive, "r:gz") as archive:
                runtime_members = {member.name for member in archive.getmembers()}
                archived_setup = archive.extractfile(
                    "actanara-7.8.9/install/setup.sh"
                ).read()
            self.assertIn(
                release_builder.STABLE_INSTALL_REF_ANCHOR,
                archived_setup,
            )
            self.assertNotEqual(first_files["install.sh"], archived_setup)
            self.assertIn(
                "actanara-7.8.9/install/dependency_contract.py",
                runtime_members,
            )
            self.assertIn(
                "actanara-7.8.9/install/runtime-dependencies.lock.json",
                runtime_members,
            )
            self.assertNotIn(
                "actanara-7.8.9/tools/release/generate_runtime_lock.py",
                runtime_members,
            )

    def test_stable_install_asset_fails_closed_for_missing_mode_or_frozen_byte_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            _init_repository(source_root)
            source = release_builder.inspect_frozen_git_source(source_root)

            missing = release_builder.FrozenSource(
                root=source.root,
                commit=source.commit,
                version=source.version,
                entries=tuple(
                    entry
                    for entry in source.entries
                    if entry.path != release_builder.STABLE_INSTALL_SOURCE
                ),
            )
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "missing"):
                release_builder.build_stable_install_asset(
                    missing,
                    root / "missing-output",
                    source_date_epoch=1783900800,
                )

            bootstrap_entry = next(
                entry
                for entry in source.entries
                if entry.path == release_builder.STABLE_INSTALL_SOURCE
            )
            wrong_mode = release_builder.FrozenSource(
                root=source.root,
                commit=source.commit,
                version=source.version,
                entries=tuple(
                    release_builder.SourceEntry(
                        path=entry.path,
                        mode="100644" if entry is bootstrap_entry else entry.mode,
                        size=entry.size,
                        sha256=entry.sha256,
                        symlink_broken=entry.symlink_broken,
                    )
                    for entry in source.entries
                ),
            )
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "executable"):
                release_builder.build_stable_install_asset(
                    wrong_mode,
                    root / "mode-output",
                    source_date_epoch=1783900800,
                )

            (source.root / release_builder.STABLE_INSTALL_SOURCE).write_text(
                "#!/usr/bin/env zsh\nchanged\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "changed after freeze"):
                release_builder.build_stable_install_asset(
                    source,
                    root / "changed-output",
                    source_date_epoch=1783900800,
                )

    def test_stable_install_asset_rejects_invalid_or_ambiguous_cross_platform_entrypoint(self):
        valid_body = (
            "if true; then\n"
            'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n'
            "select_platform_adapter() {\n"
            '  ADAPTER_PATH="install/bootstrap.sh"\n'
            '  ADAPTER_PATH="install/bootstrap-linux.sh"\n'
            "}\n"
            "download_exact_adapter() { return 0; }\n"
            "fi\n"
        )
        cases = (
            ("#!/usr/bin/env zsh\n" + valid_body, "invalid executable format"),
            (
                "#!/bin/sh\n"
                + valid_body.replace(
                    'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n',
                    "",
                ),
                "exactly one release-ref anchor",
            ),
            (
                "#!/bin/sh\n"
                + valid_body.replace(
                    'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n',
                    'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n'
                    'SOURCE_REF="${ACTANARA_INSTALL_REF:-}"\n',
                ),
                "exactly one release-ref anchor",
            ),
            (
                "#!/bin/sh\n"
                + valid_body.replace(
                    '  ADAPTER_PATH="install/bootstrap-linux.sh"\n',
                    "",
                ),
                "cross-platform hosted-stream contract",
            ),
        )
        for payload, expected_error in cases:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root = root / "source"
                source_root.mkdir()
                _init_repository(source_root)
                setup = source_root / release_builder.STABLE_INSTALL_SOURCE
                setup.write_text(payload, encoding="utf-8")
                setup.chmod(0o755)
                _git(source_root, "add", release_builder.STABLE_INSTALL_SOURCE)
                _git(source_root, "commit", "-q", "-m", "mutate setup")
                source = release_builder.inspect_frozen_git_source(source_root)

                with self.assertRaisesRegex(
                    release_builder.ReleaseBuildError,
                    expected_error,
                ):
                    release_builder.build_stable_install_asset(
                        source,
                        root / "output",
                        source_date_epoch=1783900800,
                    )

    def test_source_date_epoch_is_explicit_and_validated(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(release_builder.ReleaseBuildError, "SOURCE_DATE_EPOCH"):
                release_builder._source_date_epoch(None)
        self.assertEqual(release_builder._source_date_epoch("1783900800"), 1783900800)
        with self.assertRaises(release_builder.ReleaseBuildError):
            release_builder._source_date_epoch("not-an-integer")


if __name__ == "__main__":
    unittest.main()
