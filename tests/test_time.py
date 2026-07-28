from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from data_foundation.time import (
    detect_system_timezone,
    detect_system_timezone_authority,
)


class SystemTimezoneDetectionTests(unittest.TestCase):
    def test_authority_accepts_standard_zoneinfo_path(self):
        with patch.object(
            Path,
            "resolve",
            return_value=Path("/usr/share/zoneinfo/Etc/UTC"),
        ):
            self.assertEqual(detect_system_timezone_authority(), "Etc/UTC")

    def test_authority_accepts_macos_zoneinfo_default_path(self):
        with (
            patch.dict(os.environ, {"TZ": "Asia/Tokyo"}, clear=False),
            patch.object(
                Path,
                "resolve",
                return_value=Path(
                    "/usr/share/zoneinfo.default/America/Los_Angeles"
                ),
            ),
        ):
            self.assertEqual(
                detect_system_timezone_authority(),
                "America/Los_Angeles",
            )

    def test_best_effort_detection_accepts_macos_zoneinfo_default_path(self):
        with (
            patch.dict(os.environ, {"TZ": ""}, clear=False),
            patch.object(
                Path,
                "resolve",
                return_value=Path(
                    "/usr/share/zoneinfo.default/America/Los_Angeles"
                ),
            ),
        ):
            self.assertEqual(detect_system_timezone(), "America/Los_Angeles")

    def test_authority_rejects_lookalike_and_invalid_zoneinfo_paths(self):
        targets = (
            "/usr/share/zoneinfo.default",
            "/usr/share/zoneinfo.default.backup/America/Los_Angeles",
            "/usr/share/zoneinfo.default/not/an/iana-zone",
        )
        for target in targets:
            with self.subTest(target=target), patch.object(
                Path,
                "resolve",
                return_value=Path(target),
            ):
                self.assertIsNone(detect_system_timezone_authority())

    def test_authority_remains_fail_closed_when_localtime_cannot_resolve(self):
        with (
            patch.dict(os.environ, {"TZ": "Etc/UTC"}, clear=False),
            patch.object(Path, "resolve", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(detect_system_timezone_authority())


if __name__ == "__main__":
    unittest.main()
