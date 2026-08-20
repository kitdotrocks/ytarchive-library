from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6 import QtCore, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.app import (
    MainWindow,
    ManagedServer,
    SERVER_PASSWORD_REQUIRED_STATUS,
    SettingsDialog,
    _acquire_single_instance_lock,
    _library_statistics,
    _server_connection_urls,
    _save_first_run_config,
)
from ytarchive.config import load_config
from ytarchive.library import LibraryIndex, build_file_index
from ytarchive.metadata import MetadataStore, VideoEntry
from ytarchive.updates import ReleaseInfo


class _StartupConfig:
    start_minimized_to_tray = False
    server_autostart = False
    discord_enabled = False
    cache_update_on_startup = "never"


class _StartupHarness:
    def __init__(self) -> None:
        self.config = _StartupConfig()
        self.tray = None
        self._startup_automation_pending = True
        self._startup_window_shown = False
        self._dependency_notice_shown = False
        self._mpv_preheated = True
        self._quit_on_last_window_closed_before_startup = True
        self.events: list[str] = []
        self._run_startup_automation = lambda: MainWindow._run_startup_automation(self)

    def show(self) -> None:
        self.events.append("show")

    def _show_dependency_notice(self) -> None:
        self.events.append("dependency notice")


class StartupAutomationTestCase(unittest.TestCase):
    def test_startup_shows_window_before_running_startup_automation(self) -> None:
        harness = _StartupHarness()
        with mock.patch("ytarchive.app.QtCore.QTimer.singleShot") as single_shot:
            MainWindow._run_startup_automation(harness)

        self.assertEqual(harness.events, ["show"])
        self.assertTrue(harness._startup_automation_pending)
        single_shot.assert_called_once_with(0, harness._run_startup_automation)

        MainWindow._run_startup_automation(harness)

        self.assertEqual(harness.events, ["show", "dependency notice"])
        self.assertFalse(harness._startup_automation_pending)


class SingleInstanceLockTestCase(unittest.TestCase):
    def test_lock_allows_one_owner_and_can_be_reacquired_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "ytarchive.lock"
            first = _acquire_single_instance_lock(lock_path)
            self.assertIsNotNone(first)

            second = _acquire_single_instance_lock(lock_path)
            self.assertIsNone(second)

            assert first is not None
            first.unlock()
            third = _acquire_single_instance_lock(lock_path)
            self.assertIsNotNone(third)
            if third is not None:
                third.unlock()


class LibrarySummaryTestCase(unittest.TestCase):
    def test_summary_uses_visible_track_count(self) -> None:
        entry = mock.Mock(tags=[], path=Path("track.mp3"), hidden_from_subsonic=False)
        library = mock.Mock(entries=[entry], storage_bytes=mock.Mock(return_value=0))
        harness = mock.Mock(library=library, _library_song_summary_cache="")

        MainWindow._refresh_library_song_summary(harness)

        self.assertIn("1 song visible in similar songs", harness._library_song_summary_cache)

    def test_empty_library_statistics_use_zero_values(self) -> None:
        library = mock.Mock(entries=[], storage_bytes=mock.Mock(return_value=0))

        statistics = dict(_library_statistics(mock.Mock(), mock.Mock(), library))

        self.assertEqual(statistics["Library duration"], "0:00")
        self.assertEqual(statistics["Tracks / artist"], "0")


class LibraryReloadTestCase(unittest.TestCase):
    class Harness:
        def __init__(self, metadata: MetadataStore, library: LibraryIndex) -> None:
            self.metadata = metadata
            self.library = library
            self._library_song_summary_cache = ""
            self.status_bar = mock.Mock()
            self.rendered_ids: list[str | None] = []

        def _selected_video_id(self) -> None:
            return None

        def _refresh_library_song_summary(self) -> None:
            MainWindow._refresh_library_song_summary(self)

        def _render_results(self, selected_id: str | None = None) -> None:
            self.rendered_ids.append(selected_id)

        def _library_song_summary(self) -> str:
            return self._library_song_summary_cache

        def _dependency_status(self) -> str:
            return "Playback and download tools ready"

        def statusBar(self):
            return self.status_bar

    def test_reload_syncs_add_edit_delete_without_repeated_file_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            videos = root / "videos"
            videos.mkdir()
            metadata = MetadataStore(root / "video_metadata.json")
            metadata.load()
            library = LibraryIndex(videos)
            harness = self.Harness(metadata, library)

            with mock.patch("ytarchive.library.build_file_index", wraps=build_file_index) as build:
                library.rebuild([])
                self.assertEqual(build.call_count, 1)

                song_path = videos / "song.mp3"
                song_path.write_bytes(b"audio")
                metadata.upsert(VideoEntry(id="song", title="Old title", upload_date=""))
                MainWindow._reload_library(harness, selected_id="song", force_file_scan=True)

                self.assertEqual([entry.id for entry in library.entries], ["song"])
                self.assertEqual(library.entries[0].title, "Old title")
                self.assertEqual(library.entries[0].path, song_path)
                self.assertEqual(build.call_count, 2)

                edited = metadata.get("song")
                self.assertIsNotNone(edited)
                assert edited is not None
                edited.title = "New title"
                metadata.upsert(edited)
                MainWindow._reload_library(harness, selected_id="song")

                self.assertEqual([entry.title for entry in library.entries], ["New title"])
                self.assertEqual(build.call_count, 2)

                song_path.unlink()
                self.assertTrue(metadata.delete("song"))
                MainWindow._reload_library(harness, force_file_scan=True)

                self.assertEqual(library.entries, [])
                self.assertEqual(build.call_count, 3)


class DetailVisibilityTestCase(unittest.TestCase):
    def test_track_details_are_hidden_without_selection_or_playback(self) -> None:
        class Harness:
            current = None
            detail_fields_panel = mock.Mock()
            export_row_widget = mock.Mock()
            actions_row_widget = mock.Mock()
            _set_detail_content_visible = MainWindow._set_detail_content_visible

        harness = Harness()
        MainWindow._update_action_states(harness)

        Harness.detail_fields_panel.setVisible.assert_called_once_with(False)
        Harness.export_row_widget.setVisible.assert_called_once_with(False)
        Harness.actions_row_widget.setVisible.assert_called_once_with(False)

    def test_track_details_are_shown_for_a_selected_track(self) -> None:
        class Harness:
            current = mock.Mock(path=None)
            detail_fields_panel = mock.Mock()
            export_row_widget = mock.Mock()
            actions_row_widget = mock.Mock()
            _set_detail_content_visible = MainWindow._set_detail_content_visible

        harness = Harness()
        MainWindow._update_action_states(harness)

        Harness.detail_fields_panel.setVisible.assert_called_once_with(True)
        Harness.export_row_widget.setVisible.assert_called_once_with(True)
        Harness.actions_row_widget.setVisible.assert_called_once_with(True)


class DependencyNoticeTestCase(unittest.TestCase):
    def test_optional_dependencies_do_not_trigger_startup_popup(self) -> None:
        harness = mock.Mock(_dependency_notice_shown=False)
        with (
            mock.patch(
                "ytarchive.app._missing_dependencies",
                return_value={"required": [], "optional": ["pypresence", "aubio"]},
            ),
            mock.patch("ytarchive.app.QtWidgets.QMessageBox.information") as information,
        ):
            MainWindow._show_dependency_notice(harness)

        self.assertTrue(harness._dependency_notice_shown)
        information.assert_not_called()

    def test_required_dependencies_still_trigger_startup_popup(self) -> None:
        harness = mock.Mock(_dependency_notice_shown=False)
        with (
            mock.patch(
                "ytarchive.app._missing_dependencies",
                return_value={"required": ["mpv"], "optional": ["aubio"]},
            ),
            mock.patch("ytarchive.app.QtWidgets.QMessageBox.information") as information,
        ):
            MainWindow._show_dependency_notice(harness)

        information.assert_called_once()
        message = information.call_args.args[2]
        self.assertIn("mpv executable", message)
        self.assertNotIn("aubio", message)

    def test_dependency_status_ignores_optional_dependencies(self) -> None:
        harness = mock.Mock()
        with mock.patch(
            "ytarchive.app._missing_dependencies",
            return_value={"required": [], "optional": ["aubio"]},
        ):
            status = MainWindow._dependency_status(harness)

        self.assertEqual(status, "Playback and download tools ready")


class UpdatePromptTestCase(unittest.TestCase):
    def test_skipped_release_does_not_open_update_prompt(self) -> None:
        harness = mock.Mock()
        harness._quitting = False
        harness.config = mock.Mock(check_for_updates_on_startup=True, skipped_update_version="1.2.0")
        harness._show_update_dialog = mock.Mock()
        release = ReleaseInfo("v1.2.0", "1.2.0", "https://example.test/release")

        MainWindow._show_update_available(harness, release)

        harness._show_update_dialog.assert_not_called()

    def test_disabled_update_prompts_do_not_open_update_prompt(self) -> None:
        harness = mock.Mock()
        harness._quitting = False
        harness.config = mock.Mock(check_for_updates_on_startup=False, skipped_update_version="")
        harness._show_update_dialog = mock.Mock()
        release = ReleaseInfo("v1.2.0", "1.2.0", "https://example.test/release")

        MainWindow._show_update_available(harness, release)

        harness._show_update_dialog.assert_not_called()

    def test_skipping_update_persists_the_release_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _save_first_run_config(root, remember_root=False)
            harness = mock.Mock(config=config)
            release = ReleaseInfo("v1.2.0", "1.2.0", "https://example.test/release")

            MainWindow._skip_update_until(harness, release)

            self.assertEqual(config.skipped_update_version, "1.2.0")
            self.assertEqual(load_config(root).skipped_update_version, "1.2.0")


class FirstRunSetupTestCase(unittest.TestCase):
    def test_first_run_setup_creates_one_self_contained_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "application-data"

            config = _save_first_run_config(data_root, remember_root=False)

            self.assertEqual(config.root_dir, data_root.resolve())
            self.assertEqual(config.download_dir, data_root.resolve() / "videos")
            self.assertEqual(config.metadata_path, data_root.resolve() / "video_metadata.json")
            self.assertTrue((data_root / "config.ini").is_file())
            self.assertTrue(data_root.is_dir())


class LibrarySwitchTestCase(unittest.TestCase):
    def test_switch_activates_existing_root_without_moving_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old_root = base / "old-library"
            new_root = base / "new-library"
            old_config = _save_first_run_config(old_root, remember_root=False)
            _save_first_run_config(new_root, remember_root=False)
            (new_root / "video_metadata.json").write_text(
                '{"new-track": {"title": "New track", "upload_date": "20260101"}}',
                encoding="utf-8",
            )
            (new_root / "videos").mkdir()
            (new_root / "videos" / "new-track.mp3").write_bytes(b"audio")

            class Harness:
                cache_worker = None
                bpm_worker = None
                discord_rpc = None
                _remember_root_on_move = True
                _settings_dialog = None

                def __init__(self) -> None:
                    self.config = old_config
                    self.managed_server = mock.Mock(external=False)
                    self.discord_action = mock.Mock()
                    self.status_bar = mock.Mock()
                    self._ensure_config_dirs = MainWindow._ensure_config_dirs
                    self._update_integration_action_visibility = lambda: MainWindow._update_integration_action_visibility(self)
                    self._prepare_for_library_switch = mock.Mock(return_value=True)
                    self._reload_library = mock.Mock()
                    self._refresh_playback_icons = mock.Mock()

                def statusBar(self):
                    return self.status_bar

            harness = Harness()
            with (
                mock.patch("ytarchive.app.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(new_root)),
                mock.patch(
                    "ytarchive.app.QtWidgets.QMessageBox.question",
                    return_value=QtWidgets.QMessageBox.StandardButton.Yes,
                ),
                mock.patch("ytarchive.app.QtCore.QSignalBlocker"),
                mock.patch("ytarchive.app.apply_color_scheme"),
                mock.patch("ytarchive.app.remember_root_dir") as remember_root,
                mock.patch("ytarchive.cli._install_startup_log"),
                mock.patch("ytarchive.cli.close_startup_log"),
            ):
                MainWindow._switch_data_root(harness)

            selected_root = new_root.resolve()
            self.assertEqual(harness.config.root_dir, selected_root)
            self.assertEqual(harness.metadata.path, selected_root / "video_metadata.json")
            self.assertEqual(harness.library.videos_dir, selected_root / "videos")
            self.assertEqual([entry.id for entry in harness.library.entries], ["new-track"])
            harness.managed_server.stop.assert_called_once_with()
            harness._reload_library.assert_called_once_with(selected_id=None)
            remember_root.assert_called_once_with(new_root.resolve())
            self.assertTrue(old_config.root_dir.is_dir())
            self.assertTrue((old_root / "config.ini").is_file())


class IntegrationMenuTestCase(unittest.TestCase):
    def test_server_connection_urls_replace_wildcard_host_with_lan_addresses(self) -> None:
        with mock.patch("ytarchive.app._local_network_addresses", return_value=["192.168.1.20", "10.0.0.5"]):
            self.assertEqual(
                _server_connection_urls("0.0.0.0", 4533),
                ["http://192.168.1.20:4533", "http://10.0.0.5:4533"],
            )

    def test_server_connection_urls_keep_explicit_host(self) -> None:
        with mock.patch("ytarchive.app._local_network_addresses") as local_addresses:
            self.assertEqual(_server_connection_urls("music-pc", 5000), ["http://music-pc:5000"])
            local_addresses.assert_not_called()

    def test_subsonic_runtime_toggle_does_not_edit_startup_setting(self) -> None:
        managed_server = mock.Mock()
        harness = mock.Mock(managed_server=managed_server)

        MainWindow._toggle_subsonic(harness, True)
        MainWindow._toggle_subsonic(harness, False)

        managed_server.start_if_available.assert_called_once_with()
        managed_server.stop.assert_called_once_with()

    def test_missing_subsonic_password_shows_startup_popup(self) -> None:
        harness = mock.Mock()
        harness.tray_status_action = None
        harness.stop_server_action = None
        harness.subsonic_action = mock.Mock()
        harness.managed_server = mock.Mock(httpd=None, external=False)
        harness.statusBar.return_value = mock.Mock()

        with (
            mock.patch("ytarchive.app._show_error_popup") as show_error,
            mock.patch("ytarchive.app.QtCore.QSignalBlocker"),
        ):
            MainWindow._server_status_changed(harness, SERVER_PASSWORD_REQUIRED_STATUS)

        show_error.assert_called_once_with(
            harness,
            "Subsonic Server Not Started",
            "Enter a password in Settings → Integrations before starting the Subsonic server.",
            "Integrations → Subsonic Server",
        )
    def test_server_status_syncs_subsonic_menu_action(self) -> None:
        harness = mock.Mock()
        harness.tray_status_action = None
        harness.stop_server_action = None
        harness.subsonic_action = mock.Mock()
        harness.managed_server = mock.Mock(httpd=object(), external=False)
        harness.statusBar.return_value = mock.Mock()

        with mock.patch("ytarchive.app.QtCore.QSignalBlocker"):
            MainWindow._server_status_changed(harness, "Server running")

        harness.subsonic_action.setChecked.assert_called_once_with(True)
        harness.statusBar.return_value.showMessage.assert_called_once_with("Server running")

    def test_integration_menu_toggles_follow_settings_visibility(self) -> None:
        harness = mock.Mock()
        harness.config = mock.Mock(discord_enabled=False, server_autostart=True)
        harness.discord_action = mock.Mock()
        harness.subsonic_action = mock.Mock()
        harness.setup_listening_action = mock.Mock()

        MainWindow._update_integration_action_visibility(harness)

        harness.discord_action.setVisible.assert_called_once_with(False)
        harness.subsonic_action.setVisible.assert_called_once_with(True)
        harness.setup_listening_action.setVisible.assert_called_once_with(False)

        harness.discord_action.reset_mock()
        harness.subsonic_action.reset_mock()
        harness.setup_listening_action.reset_mock()
        harness.config.discord_enabled = True
        harness.config.server_autostart = False
        MainWindow._update_integration_action_visibility(harness)

        harness.discord_action.setVisible.assert_called_once_with(True)
        harness.subsonic_action.setVisible.assert_called_once_with(False)
        harness.setup_listening_action.setVisible.assert_called_once_with(True)

    def test_setup_action_opens_integrations_and_enables_sharing(self) -> None:
        enable = mock.Mock()
        password = mock.Mock()
        dialog = mock.Mock(controls={"SERVER_AUTOSTART": enable, "SERVER_PASSWORD": password})
        harness = mock.Mock(_settings_dialog=dialog)

        MainWindow._open_subsonic_setup(harness)

        harness._open_settings.assert_called_once_with("Integrations")
        enable.setChecked.assert_called_once_with(True)
        password.setFocus.assert_called_once_with()

    def test_integration_settings_shortcut_selects_integrations_page(self) -> None:
        navigation = mock.Mock()
        item = object()
        navigation.findItems.return_value = [item]
        harness = mock.Mock(navigation=navigation)

        SettingsDialog.select_category(harness, "Integrations")

        navigation.findItems.assert_called_once_with("Integrations", QtCore.Qt.MatchFlag.MatchExactly)
        navigation.setCurrentItem.assert_called_once_with(item)

    def test_library_settings_shortcut_selects_library_page(self) -> None:
        navigation = mock.Mock()
        item = object()
        navigation.findItems.return_value = [item]
        harness = mock.Mock(navigation=navigation)

        SettingsDialog.select_category(harness, "Library")

        navigation.findItems.assert_called_once_with("Library", QtCore.Qt.MatchFlag.MatchExactly)
        navigation.setCurrentItem.assert_called_once_with(item)


class ManagedServerTestCase(unittest.TestCase):
    def test_server_does_not_start_without_a_password(self) -> None:
        config = mock.Mock(server_password="", server_password_hash="")
        managed_server = ManagedServer(config)
        statuses: list[str] = []
        managed_server.statusChanged.connect(statuses.append)

        managed_server.start_if_available()

        self.assertEqual(statuses, [SERVER_PASSWORD_REQUIRED_STATUS])
        self.assertIsNone(managed_server.httpd)


if __name__ == "__main__":
    unittest.main()
