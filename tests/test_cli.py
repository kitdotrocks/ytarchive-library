from __future__ import annotations

import unittest
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive import cli


class CliTestCase(unittest.TestCase):
    def test_root_argument_supports_separate_and_equals_forms(self) -> None:
        self.assertEqual(cli._root_argument(["--root", "/tmp/data"]), Path("/tmp/data"))
        self.assertEqual(cli._root_argument(["--root=/tmp/data"]), Path("/tmp/data"))
        self.assertIsNone(cli._root_argument(["doctor"]))

    def test_doctor_command_does_not_import_or_start_gui(self) -> None:
        with mock.patch("ytarchive.doctor.main", return_value=0) as doctor_main:
            self.assertEqual(cli.main(["doctor", "--json"]), 0)
        doctor_main.assert_called_once_with(["--json"])

    def test_server_command_dispatches_to_server_main(self) -> None:
        with mock.patch("ytarchive.server.main", return_value=0) as server_main:
            self.assertEqual(cli.main(["serve", "--root", "/tmp/data"]), 0)
        server_main.assert_called_once_with(["--root", "/tmp/data"])

    def test_shortcuts_command_dispatches_without_starting_gui(self) -> None:
        with mock.patch("ytarchive.shortcuts.main", return_value=0) as shortcuts_main:
            self.assertEqual(cli.main(["shortcuts", "--root", "/tmp/data"]), 0)
        shortcuts_main.assert_called_once_with(["--root", "/tmp/data"])

    def test_help_is_concise_and_does_not_start_gui(self) -> None:
        output = StringIO()
        with redirect_stdout(output), mock.patch("ytarchive.cli._install_startup_log") as install_log:
            self.assertEqual(cli.main(["--help"]), 0)

        help_text = output.getvalue()
        self.assertIn("Usage:", help_text)
        self.assertIn("Open the desktop library", help_text)
        self.assertIn("doctor [--json]", help_text)
        self.assertIn("shortcuts [--root PATH]", help_text)
        self.assertIn("--root PATH", help_text)
        self.assertIn("Run 'ytarchive-lib serve --help'", help_text)
        install_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
