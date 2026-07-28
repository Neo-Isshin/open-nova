import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from install import dependency_contract as contract


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _package(name: str, version: str, content: bytes) -> dict[str, str]:
    filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    return {
        "name": name,
        "version": version,
        "filename": filename,
        "sha256": _sha256(content),
        "url": f"https://files.pythonhosted.org/packages/aa/bb/{filename}",
    }


WHEEL_CONTENTS = {
    "alpha-1.5-py3-none-any.whl": b"locked alpha wheel\n",
    "beta-2.0-py3-none-any.whl": b"locked beta wheel\n",
    "shared-4.0-py3-none-any.whl": b"locked shared wheel\n",
}


SECURE_TEMP_PARENT = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")


def _lock_payload() -> dict:
    packages = [
        _package("alpha", "1.5", WHEEL_CONTENTS["alpha-1.5-py3-none-any.whl"]),
        _package("beta", "2.0", WHEEL_CONTENTS["beta-2.0-py3-none-any.whl"]),
        _package("shared", "4.0", WHEEL_CONTENTS["shared-4.0-py3-none-any.whl"]),
    ]
    return {
        "schemaVersion": 1,
        "product": "actanara",
        "artifactPolicy": {
            "hashAlgorithm": "sha256",
            "hashesRequired": True,
            "sourceBuildsAllowed": False,
            "wheelsOnly": True,
        },
        "resolver": {
            "name": "pip",
            "reportSchemaVersion": "1",
            "version": "26.1.2",
        },
        "profiles": {
            "dashboard": {
                "directRequirements": ["alpha<2,>=1"],
                # Cross-environment audit evidence is deliberately a superset.
                "packages": ["alpha", "audit-only", "shared"],
            },
            "rag": {
                "directRequirements": ["beta==2"],
                "packages": ["beta", "shared"],
            },
        },
        "environments": {
            "fixture-arm": {
                "implementation": "cpython",
                "pythonMajorMinor": "3.12",
                "abi": "cpython-312-darwin",
                "platformFamily": "macos",
                "architecture": "arm64",
                "minimumMacOS": "14.0",
                "supportedProfiles": ["dashboard", "rag"],
                "profilePackages": {
                    "dashboard": ["alpha", "shared"],
                    "rag": ["beta", "shared"],
                },
                "packages": packages,
            }
        },
    }


def _probe(
    *,
    python: str = "3.12",
    abi: str = "cpython-312-darwin",
    architecture: str = "arm64",
    macos: str = "14.5",
) -> dict:
    value = {
        "implementation": "cpython",
        "pythonMajorMinor": python,
        "abi": abi,
        "platformFamily": "macos",
        "architecture": architecture,
        "macOSVersion": macos,
    }
    value["environmentId"] = contract._environment_id(value)
    return value


def _write_pyproject(
    path: Path,
    *,
    dashboard: str = "alpha>=1,<2",
    rag: str = "beta==2",
    product_version: str = "1.0.1",
) -> None:
    path.write_text(
        "\n".join(
            (
                "[project]",
                'name = "actanara"',
                f'version = "{product_version}"',
                "dependencies = []",
                "",
                "[project.optional-dependencies]",
                f"dashboard = [{json.dumps(dashboard)}]",
                f"rag = [{json.dumps(rag)}]",
                "",
            )
        ),
        encoding="utf-8",
    )


def _write_fixture(root: Path, payload: dict | None = None, **pyproject_options):
    lock = root / "runtime-dependencies.lock.json"
    pyproject = root / "pyproject.toml"
    lock.write_text(
        json.dumps(payload or _lock_payload(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    _write_pyproject(pyproject, **pyproject_options)
    return lock, pyproject


def _selection(root: Path, profiles=("dashboard",), payload: dict | None = None):
    lock, pyproject = _write_fixture(root, payload)
    return contract.load_contract_selection(
        lock,
        pyproject,
        profiles,
        environment_probe=_probe(),
    )


def _managed_runtime(root: Path) -> tuple[Path, Path, Path]:
    runtime = root / "runtime"
    generation = runtime / "app" / "venvs" / "generation-one"
    python = generation / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (runtime / ".venv").symlink_to("app/venvs/generation-one")
    return runtime, generation, python


class RuntimeDependencyProfileTests(unittest.TestCase):
    def _write_settings(self, root: Path, payload: dict) -> tuple[Path, Path]:
        runtime, _, _ = _managed_runtime(root)
        config = runtime / "config"
        config.mkdir(parents=True)
        settings = config / "settings.json"
        settings.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        settings.chmod(0o600)
        return runtime, settings

    def test_runtime_profiles_select_rag_dependencies_without_returning_configuration(self):
        cases = (
            (
                "disabled",
                {"features": {"rag": False}, "rag": {"enabled": False}},
                ["dashboard"],
                None,
            ),
            (
                "cloud",
                {
                    "features": {"rag": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {
                            "mode": "cloud",
                            "endpoint": "https://secret-adjacent.invalid/v1",
                            "model": "custom-cloud-model",
                        },
                    },
                },
                ["dashboard", "rag-server"],
                "cloud",
            ),
            (
                "local",
                {
                    "features": {"rag": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {"mode": "local", "model": "custom-local-model"},
                    },
                },
                ["dashboard", "rag-local", "rag-server"],
                "local",
            ),
        )
        for label, settings_payload, profiles, mode in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, _ = self._write_settings(Path(temporary), settings_payload)
                parser = contract._parser()
                args = parser.parse_args(["runtime-profiles", "--runtime", str(runtime)])
                payload, returncode = contract._dispatch(args)
                self.assertEqual(returncode, 0)
                self.assertEqual(payload["profiles"], profiles)
                self.assertEqual(payload["rag"], {"enabled": mode is not None, "embeddingMode": mode})
                evidence = payload["evidence"]
                self.assertEqual(
                    evidence["settingsSha256"],
                    hashlib.sha256((runtime / "config" / "settings.json").read_bytes()).hexdigest(),
                )
                self.assertEqual(evidence["activeMarkerStatus"], "missing")
                self.assertIsNone(evidence["activeMarkerSha256"])
                self.assertEqual(
                    Path(evidence["activeVenvTarget"]),
                    (runtime / ".venv").resolve(),
                )
                encoded = json.dumps(payload, sort_keys=True)
                self.assertNotIn("endpoint", encoded)
                self.assertNotIn("custom-cloud-model", encoded)
                self.assertNotIn("custom-local-model", encoded)

    def test_runtime_profiles_reject_ambiguous_or_unsafe_settings(self):
        cases = (
            (
                "conflicting-flags",
                {"features": {"rag": True}, "rag": {"enabled": False}},
                None,
            ),
            (
                "unsupported-mode",
                {
                    "features": {"rag": True},
                    "rag": {"enabled": True, "embedding": {"mode": "unknown"}},
                },
                None,
            ),
            (
                "writable-settings",
                {"features": {"rag": False}, "rag": {"enabled": False}},
                "writable",
            ),
            (
                "symlink-settings",
                {"features": {"rag": False}, "rag": {"enabled": False}},
                "symlink",
            ),
        )
        for label, settings_payload, mutation in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                root = Path(temporary)
                runtime, settings = self._write_settings(root, settings_payload)
                if mutation == "writable":
                    settings.chmod(0o622)
                elif mutation == "symlink":
                    outside = root / "outside.json"
                    outside.write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")
                    outside.chmod(0o600)
                    settings.unlink()
                    settings.symlink_to(outside)
                with self.assertRaises(contract.ContractError) as blocked:
                    contract.runtime_dependency_profiles(runtime)
                self.assertIn(
                    blocked.exception.code,
                    {"settings-profile-untrusted", "unsafe-file", "unsafe-permissions"},
                )

    def test_force_rebuild_profiles_recover_from_legacy_venv_using_settings_only(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            runtime, settings = self._write_settings(
                root,
                {
                    "features": {"rag": True},
                    "rag": {"enabled": True, "embedding": {"mode": "cloud"}},
                },
            )
            pointer = runtime / ".venv"
            pointer.unlink()
            legacy_python = pointer / "bin" / "python"
            legacy_python.parent.mkdir(parents=True)
            legacy_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            legacy_python.chmod(0o755)

            with self.assertRaisesRegex(
                contract.ContractError,
                "active Runtime venv cannot provide trustworthy",
            ):
                contract.runtime_dependency_profiles(runtime)

            parser = contract._parser()
            args = parser.parse_args(
                [
                    "runtime-profiles",
                    "--runtime",
                    str(runtime),
                    "--allow-untrusted-active-venv",
                ]
            )
            payload, returncode = contract._dispatch(args)

            self.assertEqual(returncode, 0)
            self.assertEqual(payload["profiles"], ["dashboard", "rag-server"])
            self.assertEqual(
                payload["rag"],
                {"enabled": True, "embeddingMode": "cloud"},
            )
            self.assertEqual(
                payload["evidence"],
                {
                    "settingsSha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
                    "activeVenvTarget": str(pointer),
                    "activeMarkerStatus": "unavailable",
                    "activeMarkerSha256": None,
                },
            )

            settings.chmod(0o622)
            with self.assertRaises(contract.ContractError):
                contract.runtime_dependency_profiles(
                    runtime,
                    allow_untrusted_active_venv=True,
                )

    def test_repair_profiles_accept_pre_github_embedding_provider_semantics(self):
        cases = (
            ({"provider": "local"}, "local", ["dashboard", "rag-local", "rag-server"]),
            ({"provider": "cloud"}, "cloud", ["dashboard", "rag-server"]),
            ({"provider": "openai"}, "cloud", ["dashboard", "rag-server"]),
            (
                {"mode": "local", "provider": "legacy-provider-id"},
                "local",
                ["dashboard", "rag-local", "rag-server"],
            ),
            ({"mode": None, "provider": "voyage"}, "cloud", ["dashboard", "rag-server"]),
        )
        for embedding, expected_mode, expected_profiles in cases:
            with self.subTest(embedding=embedding), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, _ = self._write_settings(
                    Path(temporary),
                    {
                        "features": {"rag": True},
                        "rag": {
                            "enabled": True,
                            "embedding": embedding,
                        },
                    },
                )

                if embedding.get("mode") in {"local", "cloud"}:
                    strict = contract.runtime_dependency_profiles(runtime)
                    self.assertEqual(strict["rag"]["embeddingMode"], expected_mode)
                else:
                    with self.assertRaises(contract.ContractError):
                        contract.runtime_dependency_profiles(runtime)

                payload = contract.runtime_dependency_profiles(
                    runtime,
                    allow_untrusted_active_venv=True,
                    allow_legacy_settings=True,
                )

                self.assertEqual(payload["profiles"], expected_profiles)
                self.assertEqual(
                    payload["rag"],
                    {"enabled": True, "embeddingMode": expected_mode},
                )

    def test_repair_profiles_reject_ambiguous_pre_github_settings(self):
        cases = (
            (
                "unsupported-explicit-mode",
                {
                    "features": {"rag": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {"mode": "unknown", "provider": "cloud"},
                    },
                },
            ),
            (
                "non-boolean-enabled-flag",
                {
                    "features": {"rag": 1},
                    "rag": {
                        "enabled": True,
                        "embedding": {"provider": "local"},
                    },
                },
            ),
            (
                "unsupported-settings-schema",
                {
                    "schemaVersion": 2,
                    "features": {"rag": False},
                    "rag": {"enabled": False},
                },
            ),
        )
        for label, settings_payload in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, _ = self._write_settings(Path(temporary), settings_payload)

                with self.assertRaises(contract.ContractError):
                    contract.runtime_dependency_profiles(
                        runtime,
                        allow_untrusted_active_venv=True,
                        allow_legacy_settings=True,
                    )

    def test_repair_reconciles_pre_github_feature_flag_to_explicit_rag_setting(self):
        cases = (
            (
                "explicitly-disabled",
                {
                    "features": {"rag": True, "custom": True},
                    "rag": {
                        "enabled": False,
                        "embedding": {"provider": "local"},
                    },
                    "userSection": {"answer": 42},
                },
                ["dashboard"],
                {"enabled": False, "embeddingMode": None},
            ),
            (
                "explicitly-enabled",
                {
                    "features": {"rag": False, "custom": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {"provider": "cohere", "model": "user-model"},
                    },
                    "userSection": {"answer": 42},
                },
                ["dashboard", "rag-server"],
                {"enabled": True, "embeddingMode": "cloud"},
            ),
        )
        for label, settings_payload, expected_profiles, expected_rag in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, settings_path = self._write_settings(
                    Path(temporary), settings_payload
                )

                with self.assertRaises(contract.ContractError):
                    contract.runtime_dependency_profiles(runtime)

                profile = contract.runtime_dependency_profiles(
                    runtime,
                    allow_untrusted_active_venv=True,
                    allow_legacy_settings=True,
                )
                result = contract.migrate_legacy_runtime_settings(runtime)
                migrated = json.loads(settings_path.read_text(encoding="utf-8"))

                self.assertEqual(profile["profiles"], expected_profiles)
                self.assertEqual(profile["rag"], expected_rag)
                self.assertTrue(result["settingsMigrated"])
                self.assertIs(
                    migrated["features"]["rag"],
                    settings_payload["rag"]["enabled"],
                )
                self.assertIs(migrated["rag"]["enabled"], expected_rag["enabled"])
                self.assertTrue(migrated["features"]["custom"])
                self.assertEqual(migrated["userSection"], {"answer": 42})
                if expected_rag["enabled"]:
                    self.assertEqual(migrated["rag"]["embedding"]["mode"], "cloud")
                    self.assertEqual(
                        migrated["rag"]["embedding"]["providerId"], "cohere"
                    )
                    self.assertEqual(
                        migrated["rag"]["embedding"]["model"], "user-model"
                    )

    def test_repair_migrates_only_pre_github_embedding_mode(self):
        for provider, expected_mode, expected_provider_id in (
            ("local", "local", "local"),
            ("cloud", "cloud", "cloud"),
            ("cohere", "cloud", "cohere"),
        ):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, settings_path = self._write_settings(
                    Path(temporary),
                    {
                        "features": {"rag": True, "custom": True},
                        "rag": {
                            "enabled": True,
                            "embedding": {
                                "provider": provider,
                                "model": "user-model",
                            },
                            "userOption": "keep-me",
                        },
                        "userSection": {"answer": 42},
                    },
                )
                mode_before = stat.S_IMODE(settings_path.stat().st_mode)

                parser = contract._parser()
                args = parser.parse_args(
                    ["migrate-legacy-settings", "--runtime", str(runtime)]
                )
                result, returncode = contract._dispatch(args)

                self.assertEqual(returncode, 0)
                self.assertEqual(
                    result,
                    {
                        "schemaVersion": 1,
                        "status": "migrated",
                        "settingsMigrated": True,
                    },
                )
                migrated = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(migrated["rag"]["embedding"]["mode"], expected_mode)
                self.assertEqual(migrated["rag"]["embedding"]["provider"], provider)
                self.assertEqual(
                    migrated["rag"]["embedding"]["providerId"],
                    expected_provider_id,
                )
                self.assertEqual(migrated["rag"]["embedding"]["model"], "user-model")
                self.assertEqual(migrated["rag"]["userOption"], "keep-me")
                self.assertEqual(migrated["userSection"], {"answer": 42})
                self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), mode_before)

    def test_repair_leaves_current_or_disabled_settings_byte_identical(self):
        cases = (
            {
                "schemaVersion": 1,
                "features": {"rag": True},
                "rag": {
                    "enabled": True,
                    "embedding": {
                        "mode": "cloud",
                        "provider": "cloud",
                        "providerId": "cloud",
                    },
                },
            },
            {
                "schemaVersion": 1,
                "features": {"rag": False},
                "rag": {"enabled": False},
            },
        )
        for settings_payload in cases:
            with self.subTest(settings=settings_payload), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, settings_path = self._write_settings(
                    Path(temporary), settings_payload
                )
                before = settings_path.read_bytes()
                inode = settings_path.stat().st_ino

                result = contract.migrate_legacy_runtime_settings(runtime)

                self.assertEqual(result["status"], "unchanged")
                self.assertFalse(result["settingsMigrated"])
                self.assertEqual(settings_path.read_bytes(), before)
                self.assertEqual(settings_path.stat().st_ino, inode)

    def test_repair_rejects_unsupported_explicit_mode_without_writing(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            runtime, settings_path = self._write_settings(
                Path(temporary),
                {
                    "features": {"rag": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {"mode": "unknown", "provider": "cloud"},
                    },
                },
            )
            before = settings_path.read_bytes()

            with self.assertRaises(contract.ContractError):
                contract.migrate_legacy_runtime_settings(runtime)

            self.assertEqual(settings_path.read_bytes(), before)

    def test_repair_migrates_missing_component_flags_without_losing_user_settings(self):
        cases = (
            ({"userSection": {"answer": 42}}, False, None),
            (
                {
                    "features": {"rag": True},
                    "userSection": {"answer": 42},
                },
                True,
                "local",
            ),
            (
                {
                    "rag": {
                        "enabled": True,
                        "embedding": {"provider": "cohere", "model": "custom"},
                    },
                    "userSection": {"answer": 42},
                },
                True,
                "cloud",
            ),
        )
        for settings_payload, enabled, mode in cases:
            with self.subTest(settings=settings_payload), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, settings_path = self._write_settings(
                    Path(temporary), settings_payload
                )

                with self.assertRaises(contract.ContractError):
                    contract.runtime_dependency_profiles(
                        runtime,
                        allow_untrusted_active_venv=True,
                    )
                profile = contract.runtime_dependency_profiles(
                    runtime,
                    allow_untrusted_active_venv=True,
                    allow_legacy_settings=True,
                )
                result = contract.migrate_legacy_runtime_settings(runtime)
                migrated = json.loads(settings_path.read_text(encoding="utf-8"))

                self.assertEqual(profile["rag"], {"enabled": enabled, "embeddingMode": mode})
                self.assertTrue(result["settingsMigrated"])
                self.assertEqual(migrated["schemaVersion"], 1)
                self.assertIs(migrated["features"]["rag"], enabled)
                self.assertIs(migrated["rag"]["enabled"], enabled)
                if enabled:
                    self.assertEqual(migrated["rag"]["embedding"]["mode"], mode)
                    expected_provider_id = (
                        "cohere"
                        if migrated["rag"]["embedding"].get("provider") == "cohere"
                        else mode
                    )
                    self.assertEqual(
                        migrated["rag"]["embedding"]["providerId"],
                        expected_provider_id,
                    )
                self.assertEqual(migrated["userSection"], {"answer": 42})

    def test_repair_persists_inherited_service_intent_only_for_missing_fields(self):
        cases = (
            (
                {
                    "features": {"rag": False},
                    "rag": {"enabled": False},
                    "userSection": {"answer": 42},
                },
                {"scheduler": True, "dashboard": True, "dashboardServer": False, "ragServer": False},
            ),
            (
                {
                    "features": {"rag": False, "dashboard": False},
                    "rag": {"enabled": False, "server": {"enabled": True}},
                    "dashboard": {"server": {"enabled": True}},
                    "schedule": {"enabled": False},
                    "userSection": {"answer": 42},
                },
                {"scheduler": False, "dashboard": False, "dashboardServer": True, "ragServer": True},
            ),
        )
        for settings_payload, expected in cases:
            with self.subTest(settings=settings_payload), tempfile.TemporaryDirectory(
                dir=SECURE_TEMP_PARENT
            ) as temporary:
                runtime, settings_path = self._write_settings(
                    Path(temporary), settings_payload
                )

                contract.migrate_legacy_runtime_settings(
                    runtime,
                    scheduler_enabled=True,
                    dashboard_enabled=True,
                    dashboard_server_enabled=False,
                    rag_server_enabled=False,
                )
                migrated = json.loads(settings_path.read_text(encoding="utf-8"))

                self.assertIs(migrated["schedule"]["enabled"], expected["scheduler"])
                self.assertIs(migrated["features"]["dashboard"], expected["dashboard"])
                self.assertIs(
                    migrated["dashboard"]["server"]["enabled"],
                    expected["dashboardServer"],
                )
                self.assertIs(
                    migrated["rag"]["server"]["enabled"],
                    expected["ragServer"],
                )
                self.assertEqual(migrated["userSection"], {"answer": 42})

    def test_force_rebuild_profiles_ignore_untrusted_active_marker_but_not_settings(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            runtime, settings = self._write_settings(
                root,
                {"features": {"rag": False}, "rag": {"enabled": False}},
            )
            marker = (runtime / ".venv").resolve() / contract.MARKER_NAME
            marker.write_text('{"not":"a dependency marker"}\n', encoding="utf-8")
            marker.chmod(0o444)

            with self.assertRaises(contract.ContractError):
                contract.runtime_dependency_profiles(runtime)

            payload = contract.runtime_dependency_profiles(
                runtime,
                allow_untrusted_active_venv=True,
            )

            self.assertEqual(payload["profiles"], ["dashboard"])
            self.assertEqual(payload["evidence"]["activeMarkerStatus"], "unavailable")
            self.assertIsNone(payload["evidence"]["activeMarkerSha256"])
            self.assertEqual(
                payload["evidence"]["settingsSha256"],
                hashlib.sha256(settings.read_bytes()).hexdigest(),
            )

    def test_runtime_profiles_inherit_only_dev_test_from_trusted_active_marker(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            runtime, settings = self._write_settings(
                root,
                {
                    "features": {"rag": True},
                    "rag": {
                        "enabled": True,
                        "embedding": {"mode": "local", "provider": "local"},
                    },
                },
            )
            generation = (runtime / ".venv").resolve()
            contract_root = root / "contract"
            contract_root.mkdir()
            selection = _selection(contract_root)
            marker = selection.marker_payload()
            marker["profiles"] = ["dashboard", "dev-test"]
            marker["directDependencies"] = [
                marker["directDependencies"][0],
                {"profile": "dev-test", "requirements": []},
            ]
            marker["dependencyFingerprint"] = hashlib.sha256(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "algorithm": marker["fingerprintAlgorithm"],
                        "runtimeEnvironment": {
                            key: marker["lockEnvironment"][key]
                            for key in (
                                "implementation", "pythonMajorMinor", "abi",
                                "platformFamily", "architecture",
                            )
                        }
                        | {"environmentId": marker["environmentId"]},
                        "lockEnvironment": marker["lockEnvironment"],
                        "profiles": marker["profiles"],
                        "directDependencies": marker["directDependencies"],
                        "runtimeLockSha256": marker["lockSha256"],
                        "resolvedDistributions": marker["distributions"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            marker_path = generation / contract.MARKER_NAME
            marker_path.write_text(
                json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            marker_path.chmod(0o444)

            payload = contract.runtime_dependency_profiles(runtime)
            recovery_enabled_payload = contract.runtime_dependency_profiles(
                runtime,
                allow_untrusted_active_venv=True,
            )

            self.assertEqual(
                payload["profiles"],
                ["dashboard", "dev-test", "rag-local", "rag-server"],
            )
            self.assertEqual(recovery_enabled_payload, payload)
            self.assertEqual(payload["evidence"]["activeMarkerStatus"], "trusted")
            self.assertEqual(
                payload["evidence"]["activeMarkerSha256"],
                hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn(settings.read_text(encoding="utf-8"), json.dumps(payload))


class DependencyContractSelectionTests(unittest.TestCase):
    def test_requirement_normalization_exactly_matches_lock_generator_contract(self):
        self.assertEqual(
            contract.normalize_direct_dependency("Py_YAML >= 6, < 7"),
            "py-yaml<7,>=6",
        )
        self.assertEqual(contract.normalize_direct_dependency("alpha==1"), "alpha==1")
        for invalid in ("alpha[extra]>=1", "alpha @ https://example.invalid/a.whl", "alpha>=1; os_name=='x'"):
            with self.subTest(invalid=invalid), self.assertRaises(contract.ContractError):
                contract.normalize_direct_dependency(invalid)

    def test_environment_profile_packages_are_exact_authority_and_os_minor_is_not_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock, pyproject = _write_fixture(root)
            first = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe(macos="14.5")
            )
            second = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe(macos="15.7")
            )
            self.assertEqual([item["name"] for item in first.distributions], ["alpha", "shared"])
            self.assertNotIn("audit-only", [item["name"] for item in first.distributions])
            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(first.environment_id, "fixture-arm")
            self.assertNotIn("macOSVersion", first.fingerprint_payload["runtimeEnvironment"])

    def test_direct_transitive_profile_and_environment_contracts_change_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_payload = _lock_payload()
            lock, pyproject = _write_fixture(root, base_payload)
            dashboard = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )

            direct_payload = copy.deepcopy(base_payload)
            direct_payload["profiles"]["dashboard"]["directRequirements"] = ["alpha<3,>=1"]
            lock, pyproject = _write_fixture(root, direct_payload, dashboard="alpha>=1,<3")
            direct = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )
            self.assertNotEqual(dashboard.fingerprint, direct.fingerprint)

            transitive_payload = copy.deepcopy(base_payload)
            transitive_payload["environments"]["fixture-arm"]["packages"][2]["sha256"] = "f" * 64
            lock, pyproject = _write_fixture(root, transitive_payload)
            transitive = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )
            self.assertNotEqual(dashboard.fingerprint, transitive.fingerprint)

            lock, pyproject = _write_fixture(root, base_payload)
            combined = contract.load_contract_selection(
                lock, pyproject, ["rag", "dashboard"], environment_probe=_probe()
            )
            self.assertNotEqual(dashboard.fingerprint, combined.fingerprint)
            self.assertEqual(combined.profiles, ("dashboard", "rag"))

            x86_payload = copy.deepcopy(base_payload)
            x86_environment = copy.deepcopy(x86_payload["environments"]["fixture-arm"])
            x86_environment["architecture"] = "x86_64"
            x86_payload["environments"]["fixture-x86-64"] = x86_environment
            lock, pyproject = _write_fixture(root, x86_payload)
            arm = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )
            x86 = contract.load_contract_selection(
                lock,
                pyproject,
                ["dashboard"],
                environment_probe=_probe(architecture="x86_64"),
            )
            self.assertNotEqual(arm.fingerprint, x86.fingerprint)
            self.assertEqual(x86.environment_id, "fixture-x86-64")

    def test_unsupported_abi_architecture_and_old_macos_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock, pyproject = _write_fixture(Path(temporary))
            probes = (
                _probe(python="3.13", abi="cpython-313-darwin"),
                _probe(architecture="x86_64"),
                _probe(macos="13.6"),
            )
            for probe in probes:
                with self.subTest(probe=probe), self.assertRaises(contract.ContractError):
                    contract.load_contract_selection(
                        lock, pyproject, ["dashboard"], environment_probe=probe
                    )

    def test_stale_pyproject_and_empty_profile_selection_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock, pyproject = _write_fixture(root, dashboard="alpha>=1,<3")
            with self.assertRaisesRegex(contract.ContractError, "do not match pyproject"):
                contract.load_contract_selection(
                    lock, pyproject, ["dashboard"], environment_probe=_probe()
                )
            lock, pyproject = _write_fixture(root)
            with self.assertRaisesRegex(contract.ContractError, "at least one"):
                contract.load_contract_selection(
                    lock, pyproject, [], environment_probe=_probe()
                )

    def test_product_package_version_is_source_authority_not_dependency_fingerprint_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock, pyproject = _write_fixture(root, product_version="1.0.1")
            old = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )
            _write_pyproject(pyproject, product_version="1.0.2")
            new = contract.load_contract_selection(
                lock, pyproject, ["dashboard"], environment_probe=_probe()
            )
            self.assertEqual(old.fingerprint, new.fingerprint)

    def test_lock_rejects_duplicate_keys_extra_fields_and_untrusted_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock, pyproject = _write_fixture(root)
            raw = lock.read_text(encoding="utf-8").replace(
                '"product": "actanara",',
                '"product": "actanara",\n  "product": "actanara",',
                1,
            )
            lock.write_text(raw, encoding="utf-8")
            with self.assertRaises(contract.ContractError) as duplicate:
                contract.load_contract_selection(
                    lock, pyproject, ["dashboard"], environment_probe=_probe()
                )
            self.assertEqual(duplicate.exception.code, "invalid-json")

            payload = _lock_payload()
            payload["unexpected"] = True
            lock, pyproject = _write_fixture(root, payload)
            with self.assertRaises(contract.ContractError) as extra:
                contract.load_contract_selection(
                    lock, pyproject, ["dashboard"], environment_probe=_probe()
                )
            self.assertEqual(extra.exception.code, "invalid-schema")

            payload = _lock_payload()
            payload["environments"]["fixture-arm"]["packages"][0]["url"] = (
                "https://example.invalid/alpha-1.5-py3-none-any.whl"
            )
            lock, pyproject = _write_fixture(root, payload)
            with self.assertRaises(contract.ContractError) as url:
                contract.load_contract_selection(
                    lock, pyproject, ["dashboard"], environment_probe=_probe()
                )
            self.assertEqual(url.exception.code, "invalid-lock")

            payload = _lock_payload()
            payload["environments"]["fixture-arm"]["packages"][0]["url"] = (
                "https://download-r2.pytorch.org/whl/cpu/alpha-1.5-py3-none-any.whl"
            )
            lock, pyproject = _write_fixture(root, payload)
            with self.assertRaises(contract.ContractError) as non_torch:
                contract.load_contract_selection(
                    lock, pyproject, ["dashboard"], environment_probe=_probe()
                )
            self.assertEqual(non_torch.exception.code, "invalid-lock")

    def test_hashed_requirements_are_complete_pinned_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            selection = _selection(Path(temporary))
            self.assertEqual(
                contract.hashed_requirements(selection),
                "\n".join(
                    (
                        f"alpha==1.5 --hash=sha256:{_sha256(WHEEL_CONTENTS['alpha-1.5-py3-none-any.whl'])}",
                        f"shared==4.0 --hash=sha256:{_sha256(WHEEL_CONTENTS['shared-4.0-py3-none-any.whl'])}",
                        "",
                    )
                ),
            )
            download_lines = contract.exact_download_requirements(selection).splitlines()
            self.assertEqual(len(download_lines), 2)
            for distribution, line in zip(selection.distributions, download_lines, strict=True):
                artifact = distribution["artifacts"][0]
                self.assertEqual(
                    line,
                    f"{distribution['name']} @ {artifact['url']} --hash=sha256:{artifact['sha256']}",
                )


class DependencyMarkerAndRuntimeTests(unittest.TestCase):
    def test_marker_exact_schema_atomic_mode_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            venv = root / "venv"
            venv.mkdir()
            with patch.object(
                contract,
                "validate_live_distributions",
                return_value={"status": "valid", "verified": 2},
            ):
                written = contract.write_dependency_marker(venv, selection)
            marker_path = venv / contract.MARKER_NAME
            self.assertEqual(written["status"], "written")
            self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o444)
            self.assertEqual(
                set(contract.read_dependency_marker(venv)),
                contract.MARKER_FIELDS,
            )
            self.assertEqual(
                set(contract.read_dependency_marker(venv)["lockEnvironment"]),
                contract.LOCK_ENVIRONMENT_IDENTITY_FIELDS,
            )

            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            payload["schemaVersion"] = True
            marker_path.chmod(0o600)
            marker_path.write_text(json.dumps(payload), encoding="utf-8")
            marker_path.chmod(0o444)
            with self.assertRaises(contract.ContractError) as boolean_schema:
                contract.read_dependency_marker(venv)
            self.assertEqual(boolean_schema.exception.code, "invalid-marker")

            payload["schemaVersion"] = 1
            payload["dependencyFingerprint"] = "0" * 64
            marker_path.chmod(0o600)
            marker_path.write_text(json.dumps(payload), encoding="utf-8")
            marker_path.chmod(0o444)
            with self.assertRaises(contract.ContractError) as tampered:
                contract.read_dependency_marker(venv)
            self.assertEqual(tampered.exception.code, "invalid-marker")

    def test_marker_symlink_and_conflicting_immutable_marker_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            venv = root / "venv"
            venv.mkdir()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (venv / contract.MARKER_NAME).symlink_to(outside)
            with self.assertRaises(contract.ContractError) as linked:
                contract.read_dependency_marker(venv)
            self.assertEqual(linked.exception.code, "unsafe-file")

            (venv / contract.MARKER_NAME).unlink()
            (venv / contract.MARKER_NAME).write_text("{}", encoding="utf-8")
            (venv / contract.MARKER_NAME).chmod(0o444)
            with patch.object(
                contract,
                "validate_live_distributions",
                return_value={"status": "valid", "verified": 2},
            ), self.assertRaises(contract.ContractError):
                contract.write_dependency_marker(venv, selection)

    def test_live_distribution_gate_ignores_stale_product_dist_info_but_rejects_locked_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            selection = _selection(Path(temporary))
            records = {
                "distributions": [
                    {"name": "alpha", "version": "1.5"},
                    {"name": "shared", "version": "4.0"},
                    {"name": "actanara", "version": "1.0.1"},
                    {"name": "unrelated", "version": "99"},
                ]
            }
            with patch.object(contract, "_run_json_python", return_value=records):
                result = contract.validate_live_distributions("/fixture/python", selection)
            self.assertEqual(result["verified"], 2)

            records["distributions"][0]["version"] = "1.4"
            with patch.object(contract, "_run_json_python", return_value=records), self.assertRaises(
                contract.ContractError
            ) as mismatch:
                contract.validate_live_distributions("/fixture/python", selection)
            self.assertEqual(mismatch.exception.code, "live-distributions-mismatch")

    def test_managed_pointer_is_validated_before_active_python_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, generation, python = _managed_runtime(root)
            selected, reason = contract.select_plan_python(
                runtime,
                mode="auto",
                fallback_python=None,
            )
            self.assertEqual(selected, python)
            self.assertEqual(reason, "managed-active-venv-python")

            forced, forced_reason = contract.select_plan_python(
                runtime,
                mode="force-rebuild",
                fallback_python="/caller/python313",
            )
            self.assertEqual(forced, Path("/caller/python313"))
            self.assertEqual(forced_reason, "explicit-python")

            explicit, explicit_reason = contract.select_plan_python(
                runtime,
                mode="auto",
                fallback_python="/caller/python313",
            )
            self.assertEqual(explicit, Path("/caller/python313"))
            self.assertEqual(explicit_reason, "explicit-python")

            (runtime / ".venv").unlink()
            (runtime / ".venv").symlink_to(root / "outside")
            fallback, fallback_reason = contract.select_plan_python(
                runtime,
                mode="auto",
                fallback_python="/caller/python313",
            )
            self.assertEqual(fallback, Path("/caller/python313"))
            self.assertEqual(fallback_reason, "explicit-python")
            with self.assertRaises(contract.ContractError) as missing:
                contract.select_plan_python(runtime, mode="auto", fallback_python=None)
            self.assertEqual(missing.exception.code, "missing-rebuild-python")

    def test_managed_pointer_rejects_symlinked_or_writable_runtime_directory_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside_generation = outside / "venvs" / "generation-one"
            outside_python = outside_generation / "bin" / "python"
            outside_python.parent.mkdir(parents=True)
            outside_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            outside_python.chmod(0o755)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "app").symlink_to(outside)
            (runtime / ".venv").symlink_to("app/venvs/generation-one")

            with self.assertRaises(contract.ContractError) as linked:
                contract.select_plan_python(runtime, mode="auto", fallback_python=None)
            self.assertEqual(linked.exception.code, "missing-rebuild-python")

            (runtime / "app").unlink()
            generation = runtime / "app" / "venvs" / "generation-one"
            python = generation / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
            (runtime / ".venv").unlink()
            (runtime / ".venv").symlink_to("app/venvs/generation-one")
            (runtime / "app").chmod(0o777)
            try:
                with self.assertRaises(contract.ContractError) as writable:
                    contract.select_plan_python(runtime, mode="auto", fallback_python=None)
                self.assertEqual(writable.exception.code, "missing-rebuild-python")
            finally:
                (runtime / "app").chmod(0o755)

            (generation / "bin").chmod(0o777)
            try:
                with self.assertRaises(contract.ContractError) as writable_bin:
                    contract.select_plan_python(runtime, mode="auto", fallback_python=None)
                self.assertEqual(writable_bin.exception.code, "missing-rebuild-python")
            finally:
                (generation / "bin").chmod(0o755)

            python.chmod(0o777)
            try:
                with self.assertRaises(contract.ContractError) as writable_python:
                    contract.select_plan_python(runtime, mode="auto", fallback_python=None)
                self.assertEqual(writable_python.exception.code, "missing-rebuild-python")
            finally:
                python.chmod(0o755)

    def test_plan_dispatch_uses_active_python_instead_of_different_caller_abi(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            runtime, _, active_python = _managed_runtime(root)
            parser = contract._parser()
            args = parser.parse_args(
                [
                    "plan",
                    "--lock",
                    str(selection.lock_path),
                    "--pyproject",
                    str(root / "pyproject.toml"),
                    "--profile",
                    "dashboard",
                    "--runtime",
                    str(runtime),
                    "--cache-root",
                    str(root / "cache"),
                ]
            )
            with patch.object(contract, "_selection_from_args", return_value=selection) as select, patch.object(
                contract,
                "plan_update",
                return_value=({"schemaVersion": 1, "status": "ready"}, 0),
            ):
                payload, returncode = contract._dispatch(args)
            self.assertEqual(returncode, 0)
            self.assertEqual(select.call_args.kwargs["python"], active_python)
            self.assertEqual(payload["selectedPython"], str(active_python))
            self.assertEqual(payload["pythonSelectionReason"], "managed-active-venv-python")

    def test_reuse_plan_avoids_cache_and_pip_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            runtime, generation, _ = _managed_runtime(root)
            with patch.object(
                contract,
                "validate_live_distributions",
                return_value={"status": "valid", "verified": 2},
            ):
                contract.write_dependency_marker(generation, selection)
            with patch.object(
                contract,
                "validate_live_distributions",
                return_value={"status": "valid", "verified": 2},
            ), patch.object(
                contract,
                "dependency_cache_status",
                side_effect=AssertionError("cache/pip path must not be consulted for reuse"),
            ):
                plan, returncode = contract.plan_update(
                    runtime,
                    selection,
                    mode="auto",
                    offline=True,
                    cache_root=root / "missing-cache",
                )
            self.assertEqual(returncode, 0)
            self.assertEqual(plan["updateMode"], "reuse-existing-venv")
            self.assertTrue(plan["reusesRuntimeVenv"])
            self.assertFalse(plan["plannedDependenciesInstalled"])
            self.assertFalse(plan["cacheUsed"])

    def test_legacy_and_untrusted_markers_rebuild_but_explicit_source_only_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            runtime, generation, _ = _managed_runtime(root)
            miss = {
                "status": "miss",
                "usable": False,
                "path": str(root / "cache" / selection.fingerprint),
                "reason": "cache-root-missing",
            }
            with patch.object(contract, "dependency_cache_status", return_value=miss):
                plan, returncode = contract.plan_update(
                    runtime,
                    selection,
                    mode="auto",
                    offline=False,
                    cache_root=root / "cache",
                )
            self.assertEqual(returncode, 0)
            self.assertEqual(plan["reason"], "legacy-runtime-no-dependency-marker")
            self.assertEqual(plan["updateMode"], "rebuild-candidate-venv")

            with self.assertRaises(contract.ContractError) as source_only:
                contract.plan_update(
                    runtime,
                    selection,
                    mode="explicit-source-only",
                    offline=False,
                    cache_root=root / "cache",
                )
            self.assertEqual(source_only.exception.code, "source-only-incompatible")

            marker = generation / contract.MARKER_NAME
            marker.symlink_to(root / "missing-marker")
            with patch.object(contract, "dependency_cache_status", return_value=miss):
                plan, _ = contract.plan_update(
                    runtime,
                    selection,
                    mode="auto",
                    offline=False,
                    cache_root=root / "cache",
                )
            self.assertEqual(plan["reason"], "active-dependency-marker-untrusted")

    def test_offline_rebuild_cache_miss_blocks_before_service_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = _selection(root)
            runtime, _, _ = _managed_runtime(root)
            miss = {
                "status": "miss",
                "usable": False,
                "path": str(root / "cache" / selection.fingerprint),
                "reason": "wheelhouse-missing",
            }
            with patch.object(contract, "dependency_cache_status", return_value=miss):
                plan, returncode = contract.plan_update(
                    runtime,
                    selection,
                    mode="force-rebuild",
                    offline=True,
                    cache_root=root / "cache",
                )
            self.assertEqual(returncode, 3)
            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["reason"], "offline-cache-miss")
            self.assertTrue(plan["failBeforeServiceStop"])


class DependencyWheelhouseTests(unittest.TestCase):
    def _populate_wheelhouse(self, root: Path, selection):
        cache = contract.ensure_secure_cache_root(root / "cache")
        wheelhouse = contract.wheelhouse_path(cache, selection)
        wheelhouse.mkdir(mode=0o700)
        for distribution in selection.distributions:
            artifact = distribution["artifacts"][0]
            path = wheelhouse / artifact["filename"]
            path.write_bytes(WHEEL_CONTENTS[artifact["filename"]])
            path.chmod(0o400)
        return cache, wheelhouse

    def test_secure_cache_manifest_detects_hash_tamper_pollution_and_symlink_roots(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            selection = _selection(root)
            cache, wheelhouse = self._populate_wheelhouse(root, selection)
            status = contract.write_wheelhouse_manifest(wheelhouse, selection)
            self.assertEqual(status["status"], "hit")
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(wheelhouse.stat().st_mode), 0o700)

            manifest = wheelhouse / contract.WHEELHOUSE_MANIFEST_NAME
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_payload["schemaVersion"] = True
            manifest.chmod(0o600)
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            manifest.chmod(0o444)
            with self.assertRaises(contract.ContractError) as boolean_schema:
                contract.verify_wheelhouse(wheelhouse, selection)
            self.assertEqual(boolean_schema.exception.code, "untrusted-cache")
            contract.write_wheelhouse_manifest(wheelhouse, selection)

            alpha = wheelhouse / "alpha-1.5-py3-none-any.whl"
            alpha.chmod(0o600)
            alpha.write_bytes(b"polluted")
            alpha.chmod(0o400)
            with self.assertRaises(contract.ContractError) as tampered:
                contract.verify_wheelhouse(wheelhouse, selection)
            self.assertEqual(tampered.exception.code, "untrusted-cache")

            alpha.chmod(0o600)
            alpha.write_bytes(WHEEL_CONTENTS[alpha.name])
            alpha.chmod(0o400)
            extra = wheelhouse / "unlocked-1-py3-none-any.whl"
            extra.write_bytes(b"unlocked")
            extra.chmod(0o400)
            with self.assertRaises(contract.ContractError):
                contract.verify_wheelhouse(wheelhouse, selection)

            linked = root / "linked-cache"
            linked.symlink_to(cache)
            with self.assertRaises(contract.ContractError) as symlink:
                contract.ensure_secure_cache_root(linked)
            self.assertEqual(symlink.exception.code, "unsafe-path")

    def test_cache_materialization_is_atomic_persistent_and_pip_is_not_repeated_on_hit(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            selection = _selection(root)
            commands: list[list[str]] = []

            def fake_pip(command, **_kwargs):
                command = list(command)
                commands.append(command)
                self.assertIn("--require-hashes", command)
                self.assertIn("--only-binary=:all:", command)
                self.assertIn("--no-deps", command)
                self.assertIn("--no-cache-dir", command)
                self.assertIn("--no-index", command)
                destination = Path(command[command.index("--dest") + 1])
                requirements = Path(command[command.index("--requirement") + 1]).read_text(
                    encoding="utf-8"
                )
                self.assertEqual(requirements, contract.exact_download_requirements(selection))
                for distribution in selection.distributions:
                    filename = distribution["artifacts"][0]["filename"]
                    (destination / filename).write_bytes(WHEEL_CONTENTS[filename])

            with patch.object(contract, "_run_pip", side_effect=fake_pip):
                first = contract.materialize_dependency_cache(
                    root / "cache", selection, python="/fixture/python"
                )
            self.assertTrue(first["materialized"])
            self.assertFalse(first["cacheUsed"])
            self.assertEqual(len(commands), 1)

            with patch.object(
                contract,
                "_run_pip",
                side_effect=AssertionError("trusted persistent cache must be reused"),
            ):
                second = contract.materialize_dependency_cache(
                    root / "cache", selection, python="/fixture/python"
                )
            self.assertFalse(second["materialized"])
            self.assertTrue(second["cacheUsed"])

    def test_offline_materialization_revalidates_cache_and_never_calls_pip(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            selection = _selection(root)
            cache, wheelhouse = self._populate_wheelhouse(root, selection)
            contract.write_wheelhouse_manifest(wheelhouse, selection)

            with patch.object(
                contract,
                "_run_pip",
                side_effect=AssertionError("offline materialization must never call pip"),
            ):
                hit = contract.materialize_dependency_cache(
                    cache,
                    selection,
                    python="/fixture/python",
                    offline=True,
                )
            self.assertEqual(hit["status"], "hit")
            self.assertTrue(hit["cacheUsed"])
            self.assertFalse(hit["materialized"])
            self.assertTrue(hit["offline"])

            shutil.rmtree(wheelhouse)
            with patch.object(
                contract,
                "_run_pip",
                side_effect=AssertionError("offline cache races must fail without pip"),
            ), self.assertRaises(contract.ContractError) as missing:
                contract.materialize_dependency_cache(
                    cache,
                    selection,
                    python="/fixture/python",
                    offline=True,
                )
            self.assertEqual(missing.exception.code, "offline-cache-miss")
            self.assertFalse(wheelhouse.exists())

    def test_locked_install_is_offline_hash_checked_and_reports_real_execution(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            selection = _selection(root)
            _, wheelhouse = self._populate_wheelhouse(root, selection)
            contract.write_wheelhouse_manifest(wheelhouse, selection)
            commands: list[list[str]] = []

            def capture(command, **_kwargs):
                commands.append(list(command))

            with patch.object(contract, "_run_pip", side_effect=capture), patch.object(
                contract,
                "validate_live_distributions",
                return_value={"status": "valid", "verified": 2},
            ):
                result = contract.install_locked_dependencies(
                    root / "cache", selection, venv_python="/candidate/bin/python"
                )
            self.assertTrue(result["dependenciesInstalled"])
            self.assertTrue(result["cacheUsed"])
            self.assertEqual(result["verifiedDistributions"], 2)
            self.assertEqual(len(commands), 1)
            command = commands[0]
            for option in ("--no-index", "--find-links", "--require-hashes", "--no-deps"):
                self.assertIn(option, command)
            self.assertIn("--no-cache-dir", command)
            with patch.dict(
                os.environ,
                {
                    "PIP_INDEX_URL": "https://attacker.invalid/simple",
                    "PIP_EXTRA_INDEX_URL": "https://attacker.invalid/extra",
                    "PYTHONPATH": "/untrusted/pythonpath",
                },
            ):
                isolated = contract._pip_environment()
            self.assertEqual(isolated["PIP_CONFIG_FILE"], os.devnull)
            self.assertEqual(isolated["PYTHONNOUSERSITE"], "1")
            self.assertEqual(isolated["PIP_NO_INPUT"], "1")
            self.assertNotIn("PIP_INDEX_URL", isolated)
            self.assertNotIn("PIP_EXTRA_INDEX_URL", isolated)
            self.assertNotIn("PYTHONPATH", isolated)

    def test_failed_pip_reports_bounded_redacted_summary_and_private_full_log(self):
        with tempfile.TemporaryDirectory(dir=SECURE_TEMP_PARENT) as temporary:
            root = Path(temporary)
            log = root / "runtime" / "state" / "logs" / "dependencies.log"
            credential_fixture = "synthetic-pip-credential-value"
            completed = subprocess.CompletedProcess(
                ["python", "-m", "pip"],
                1,
                ("collecting fixture\n" + "detail " * 5000).encode(),
                (
                    "ERROR: https://user:"
                    + credential_fixture
                    + "@packages.example.invalid/simple?token="
                    + credential_fixture
                    + "\nNo matching distribution found for fixture==9.9\n"
                ).encode(),
            )
            with (
                patch.object(contract.subprocess, "run", return_value=completed),
                patch.dict(
                    os.environ,
                    {"ACTANARA_TEST_SYNTHETIC_TOKEN": credential_fixture},
                    clear=False,
                ),
                self.assertRaises(contract.ContractError) as raised,
            ):
                contract._run_pip(
                    ["python", "-m", "pip", "install", "fixture==9.9"],
                    timeout=5,
                    code="locked-install-failed",
                    diagnostic_log=log,
                )

            summary = raised.exception.message
            self.assertLessEqual(len(summary), contract.MAX_PIP_ERROR_SUMMARY_CHARS)
            self.assertIn("No matching distribution", summary)
            self.assertNotIn(credential_fixture, summary)
            self.assertTrue(log.is_file())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            diagnostic = log.read_text(encoding="utf-8")
            self.assertIn("collecting fixture", diagnostic)
            self.assertIn("No matching distribution", diagnostic)
            self.assertNotIn(credential_fixture, diagnostic)
            self.assertIn("[redacted]", diagnostic)


if __name__ == "__main__":
    unittest.main()
