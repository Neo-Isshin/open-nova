import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LicenseMetadataTests(unittest.TestCase):
    def test_license_is_mit_with_correct_copyright_holder(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn("Copyright (c) 2026 Neo-Isshin", text)

    def test_pep639_metadata_declares_mit(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(metadata["project"]["version"], "1.5.0")
        self.assertEqual(metadata["project"]["readme"], "README.md")
        self.assertEqual(
            metadata["project"]["authors"],
            [{"name": "Neo-Isshin", "email": "nxc8335@gmail.com"}],
        )
        self.assertEqual(metadata["project"]["license"], "MIT")
        self.assertEqual(metadata["project"]["license-files"], ["LICENSE"])
        self.assertEqual(
            metadata["project"]["urls"],
            {
                "Homepage": "https://github.com/Neo-Isshin/actanara",
                "Repository": "https://github.com/Neo-Isshin/actanara",
                "Issues": "https://github.com/Neo-Isshin/actanara/issues",
            },
        )
        self.assertEqual(
            metadata["build-system"]["requires"],
            ["setuptools==83.0.0", "wheel==0.47.0"],
        )

    def test_source_manifest_and_readmes_publish_consistent_notice(self):
        self.assertIn(
            "include LICENSE",
            (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines(),
        )
        for name in ("README.md", "README.zh-CN.md"):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("Copyright © 2026 Neo-Isshin.", content)
                self.assertIn("`MIT`", content)
                self.assertIn("](LICENSE)", content)

    def test_public_entrypoints_use_the_shared_main_setup_channel(self):
        install_command = (
            "curl -fsSL https://raw.githubusercontent.com/Neo-Isshin/actanara/"
            "main/install/setup.sh | sh"
        )
        obsolete_release_command = (
            "https://github.com/Neo-Isshin/actanara/"
            "releases/latest/download/install.sh"
        )
        for name in (
            "README.md",
            "README.zh-CN.md",
            "docs/local-operations-runbook.md",
            "docs/local-operations-runbook.zh-CN.md",
            "docs/new-user-onboarding-runbook.md",
            "docs/index.html",
        ):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(install_command, content)
                self.assertNotIn(obsolete_release_command, content)
                self.assertNotIn("raw.githubusercontent.com/Neo-Isshin/actanara/v1.0.1", content)
                self.assertNotIn("git" + "ea", content.lower())


if __name__ == "__main__":
    unittest.main()
