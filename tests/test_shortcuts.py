from __future__ import annotations

import base64
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.shortcuts import (
    _desktop_entry,
    _write_linux_shortcut,
    _write_windows_shortcut,
)


class ShortcutTestCase(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux shortcut format is only exercised on Linux")
    def test_desktop_entry_quotes_launcher_arguments_and_icon(self) -> None:
        content = _desktop_entry(
            Path("/opt/ytarchive Library/bin/ytarchive-lib"),
            ["--root", "/home/user/My Library"],
            Path("/opt/ytarchive Library/logo.svg"),
        )

        self.assertIn('Exec="/opt/ytarchive Library/bin/ytarchive-lib" "--root" "/home/user/My Library"', content)
        self.assertIn("Icon=/opt/ytarchive Library/logo.svg", content)
        self.assertIn("Terminal=false", content)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux shortcut permissions are only exercised on Linux")
    def test_linux_shortcut_is_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "applications" / "ytarchive-lib.desktop"
            _write_linux_shortcut(
                destination,
                Path("/opt/ytarchive-lib"),
                [],
                Path("/opt/logo.svg"),
            )

            self.assertTrue(destination.is_file())
            self.assertTrue(destination.stat().st_mode & stat.S_IXUSR)
            self.assertIn("Name=ytarchive Library", destination.read_text(encoding="utf-8"))

    def test_windows_shortcut_passes_target_and_arguments_to_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "Desktop" / "ytarchive Library.lnk"
            result = mock.Mock(returncode=0, stdout="", stderr="")
            with (
                mock.patch("ytarchive.shortcuts._powershell_executable", return_value="powershell.exe"),
                mock.patch("ytarchive.shortcuts.subprocess.run", return_value=result) as run,
            ):
                _write_windows_shortcut(
                    destination,
                    Path(r"C:\Python\pythonw.exe"),
                    ["-m", "ytarchive", "--root", r"C:\Music Library"],
                    Path(r"C:\Python\logo.ico"),
                )

            command = run.call_args.args[0]
            script = base64.b64decode(command[-1]).decode("utf-16le")
            environment = run.call_args.kwargs["env"]
            self.assertIn("CreateShortcut", script)
            self.assertEqual(environment["YTARCHIVE_LIB_SHORTCUT_TARGET"], r"C:\Python\pythonw.exe")
            self.assertIn('"C:\\Music Library"', environment["YTARCHIVE_LIB_SHORTCUT_ARGUMENTS"])
            self.assertEqual(environment["YTARCHIVE_LIB_SHORTCUT_ICON"], r"C:\Python\logo.ico,0")


if __name__ == "__main__":
    unittest.main()
