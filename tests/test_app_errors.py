from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.app import (
    APP_ICON_FILENAME,
    APP_WINDOWS_ICON_FILENAME,
    AddNewDialog,
    ElidedLabel,
    _application_icon,
    _format_error_popup_message,
)
from ytarchive.config import AppConfig
from ytarchive.metadata import MetadataStore


class ErrorPopupMessageTestCase(unittest.TestCase):
    def test_includes_human_readable_source_before_error(self) -> None:
        self.assertEqual(
            _format_error_popup_message("Integrations → Discord Presence → Connection", "IPC unavailable"),
            "Source: Integrations → Discord Presence → Connection\n\nIPC unavailable",
        )

    def test_falls_back_for_blank_source_or_message(self) -> None:
        self.assertEqual(_format_error_popup_message("  ", "  "), "Source: ytarchive Library\n\nUnknown error")


class ApplicationIconTestCase(unittest.TestCase):
    def test_application_icon_asset_loads(self) -> None:
        package_dir = Path(__file__).resolve().parents[1]
        self.assertTrue((package_dir / APP_ICON_FILENAME).is_file())
        self.assertTrue((package_dir / APP_WINDOWS_ICON_FILENAME).is_file())
        self.assertFalse(_application_icon().isNull())


class AddNewDialogTestCase(unittest.TestCase):
    def test_online_log_wraps_without_horizontal_scroll_or_size_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = AppConfig(
                root_dir=root,
                download_dir=root / "videos",
                exports_dir=root / "exports",
                thumbnails_dir=root / "thumbnails",
                artist_profiles_path=root / "artist_profiles.json",
                artist_thumbnails_dir=root / "artist_thumbnails",
                metadata_path=root / "video_metadata.json",
                playlists_path=root / "playlists.txt",
                workers=1,
                browser_cookies=None,
                discord_enabled=False,
                spotify_lossless_command="",
                spotdl_command="",
                server_host="127.0.0.1",
                server_port=0,
                server_username="user",
                server_password="secret",
                server_password_hash="",
            )
            with mock.patch("ytarchive.app._missing_dependencies", return_value={"required": [], "optional": []}):
                dialog = AddNewDialog(config, MetadataStore(config.metadata_path))
            try:
                log = dialog.youtube_log
                self.assertIsInstance(dialog.youtube_status, ElidedLabel)
                self.assertEqual(log.lineWrapMode(), QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
                self.assertEqual(log.wordWrapMode(), QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                self.assertEqual(log.horizontalScrollBarPolicy(), QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self.assertEqual(
                    log.sizeAdjustPolicy(),
                    QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored,
                )
            finally:
                dialog.close()
                dialog.deleteLater()

    def test_closing_while_online_worker_runs_waits_for_worker_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = AppConfig(
                root_dir=root,
                download_dir=root / "videos",
                exports_dir=root / "exports",
                thumbnails_dir=root / "thumbnails",
                artist_profiles_path=root / "artist_profiles.json",
                artist_thumbnails_dir=root / "artist_thumbnails",
                metadata_path=root / "video_metadata.json",
                playlists_path=root / "playlists.txt",
                workers=1,
                browser_cookies=None,
                discord_enabled=False,
                spotify_lossless_command="",
                spotdl_command="",
                server_host="127.0.0.1",
                server_port=0,
                server_username="user",
                server_password="secret",
                server_password_hash="",
            )
            with mock.patch("ytarchive.app._missing_dependencies", return_value={"required": [], "optional": []}):
                dialog = AddNewDialog(config, MetadataStore(config.metadata_path))
            try:
                worker = mock.Mock()
                worker.isRunning.return_value = False
                dialog.direct_worker = worker
                event = mock.Mock()
                with mock.patch.object(dialog, "reject") as reject:
                    dialog.closeEvent(event)
                    worker.cancel.assert_called_once_with()
                    event.ignore.assert_called_once_with()
                    self.assertTrue(dialog._close_requested)

                    dialog._on_add_worker_finished()
                    reject.assert_called_once_with()
                    self.assertFalse(dialog._close_requested)
            finally:
                dialog.close()
                dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
