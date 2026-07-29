import importlib.metadata
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.version import UNKNOWN_VERSION, product_version


class ProductVersionTests(unittest.TestCase):
    def test_active_source_pyproject_is_the_version_authority(self):
        self.assertEqual(product_version(), "1.7.0")

    def test_installed_metadata_is_used_without_source_pyproject(self):
        with (
            patch("data_foundation.version._source_version", return_value=None),
            patch("data_foundation.version.importlib.metadata.version", return_value="9.8.7"),
        ):
            self.assertEqual(product_version(), "9.8.7")

    def test_missing_source_and_metadata_is_explicitly_unknown(self):
        with (
            patch("data_foundation.version._source_version", return_value=None),
            patch(
                "data_foundation.version.importlib.metadata.version",
                side_effect=importlib.metadata.PackageNotFoundError,
            ),
        ):
            self.assertEqual(product_version(), UNKNOWN_VERSION)


if __name__ == "__main__":
    unittest.main()
