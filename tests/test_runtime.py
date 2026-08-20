from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import ytarchive.runtime as runtime


class WindowsRuntimePathTestCase(unittest.TestCase):
    def test_windows_console_tools_use_no_console_flag(self) -> None:
        with (
            mock.patch.object(runtime.os, "name", "nt"),
            mock.patch.object(runtime.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            self.assertEqual(runtime.windows_no_console_kwargs(), {"creationflags": 0x08000000})

    def test_winget_links_are_added_for_desktop_launches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = Path(temporary) / "Microsoft" / "WinGet" / "Links"
            links.mkdir(parents=True)
            (links / "mpv.exe").write_bytes(b"")
            with (
                mock.patch.object(runtime.os, "name", "nt"),
                mock.patch.object(runtime, "Path", type(links)),
                mock.patch.dict(
                    runtime.os.environ,
                    {"LOCALAPPDATA": temporary, "PATH": "existing"},
                    clear=False,
                ),
            ):
                runtime._windows_runtime_directories.cache_clear()
                try:
                    runtime.prepare_external_tool_path()
                    self.assertIn(str(links), runtime.os.environ["PATH"].split(runtime.os.pathsep))
                finally:
                    runtime._windows_runtime_directories.cache_clear()

    def test_finds_per_user_program_install_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            mpv_directory = Path(temporary) / "Programs" / "mpv"
            mpv_directory.mkdir(parents=True)
            mpv = mpv_directory / "mpv.exe"
            mpv.write_bytes(b"")
            with (
                mock.patch.object(runtime.os, "name", "nt"),
                mock.patch.object(runtime, "Path", type(mpv_directory)),
                mock.patch.dict(
                    runtime.os.environ,
                    {"LOCALAPPDATA": temporary, "PATH": ""},
                    clear=False,
                ),
                mock.patch.object(runtime.shutil, "which", return_value=None),
            ):
                runtime._windows_runtime_directories.cache_clear()
                try:
                    self.assertEqual(runtime.find_external_tool("mpv"), str(mpv))
                finally:
                    runtime._windows_runtime_directories.cache_clear()

    def test_finds_winget_mpv_player_install_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary) / "Program Files"
            mpv_directory = program_files / "MPV Player"
            mpv_directory.mkdir(parents=True)
            mpv = mpv_directory / "mpv.exe"
            mpv.write_bytes(b"")
            with (
                mock.patch.object(runtime.os, "name", "nt"),
                mock.patch.object(runtime, "Path", type(mpv_directory)),
                mock.patch.dict(
                    runtime.os.environ,
                    {"LOCALAPPDATA": temporary, "ProgramFiles": str(program_files), "PATH": ""},
                    clear=False,
                ),
                mock.patch.object(runtime.shutil, "which", return_value=None),
            ):
                runtime._windows_runtime_directories.cache_clear()
                try:
                    self.assertEqual(runtime.find_external_tool("mpv"), str(mpv))
                finally:
                    runtime._windows_runtime_directories.cache_clear()


if __name__ == "__main__":
    unittest.main()
