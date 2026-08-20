from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive import doctor


class DependencyReportTestCase(unittest.TestCase):
    def test_missing_aria2c_is_not_reported(self) -> None:
        def executable(name: str) -> str | None:
            return None if name == "aria2c" else f"/usr/bin/{name}"

        with (
            mock.patch.object(doctor, "_python_package_available", return_value=True),
            mock.patch.object(doctor, "yt_dlp_available", return_value=True),
            mock.patch.object(doctor, "yt_dlp_ejs_available", return_value=True),
            mock.patch.object(doctor, "yt_dlp_js_runtime", return_value="deno"),
            mock.patch.object(doctor, "aubio_available", return_value=True),
            mock.patch.object(doctor.shutil, "which", side_effect=executable),
        ):
            missing = doctor.missing_dependencies()

        self.assertNotIn("aria2c", missing["required"])
        self.assertNotIn("aria2c", missing["optional"])


if __name__ == "__main__":
    unittest.main()
