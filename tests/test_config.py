from __future__ import annotations

import tempfile
import unittest
from unittest import mock
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import ytarchive.config as config_module
from ytarchive.config import (
    SETTING_BY_KEY,
    default_root_dir,
    effective_settings,
    load_existing_config,
    load_config,
    move_data_root,
    read_ini_settings,
    remember_root_dir,
    save_ini_settings,
)


class ConfigSettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.ini"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_include_startup_options(self) -> None:
        config = load_config(self.root)
        self.assertTrue(config.use_aria2c)
        self.assertEqual(config.browser_cookies_mode, "required")
        self.assertFalse(config.server_autostart)
        self.assertFalse(config.start_minimized_to_tray)
        self.assertTrue(config.close_to_tray)
        self.assertEqual(config.cache_update_on_startup, "never")
        self.assertTrue(config.check_for_updates_on_startup)
        self.assertFalse(config.dark_mode)
        self.assertEqual(config.discord_application_id, "")
        self.assertEqual(config.discord_presence_image_mode, "empty")
        self.assertEqual(config.discord_presence_small_image_mode, "default")
        self.assertEqual(config.discord_presence_small_image_value, "")
        self.assertTrue(config.discord_presence_default_youtube_thumbnail)
        self.assertFalse(config.discord_presence_show_default_as_small_on_override)
        self.assertEqual(config.server_password, "")
        self.assertEqual(config.server_password_hash, "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "XDG data directories are Linux-specific")
    def test_default_root_uses_xdg_data_home_when_no_legacy_checkout_exists(self) -> None:
        with (
            mock.patch.object(config_module, "_legacy_source_root_dir", return_value=None),
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}, clear=False),
        ):
            self.assertEqual(default_root_dir(), self.root / "ytarchive")

    def test_default_root_uses_local_app_data_on_windows(self) -> None:
        fake_os = mock.Mock(name="os")
        fake_os.name = "nt"
        fake_os.environ = os.environ
        with (
            mock.patch.object(config_module, "_legacy_source_root_dir", return_value=None),
            mock.patch.object(config_module, "os", fake_os),
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=False),
        ):
            self.assertEqual(default_root_dir(), (self.root / "ytarchive").resolve())

    def test_default_root_uses_a_remembered_user_selected_root(self) -> None:
        selected = self.root / "library-root"
        selected.mkdir()
        (selected / "config.ini").write_text("[Settings]\n", encoding="utf-8")
        with (
            mock.patch.object(config_module, "_legacy_source_root_dir", return_value=None),
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "platform")}, clear=False),
        ):
            remember_root_dir(selected)
            self.assertEqual(default_root_dir(), selected.resolve())

    def test_load_existing_config_requires_an_existing_ytarchive_root(self) -> None:
        library_root = self.root / "library-root"
        library_root.mkdir()
        save_ini_settings(library_root, {})

        config = load_existing_config(library_root)

        self.assertEqual(config.root_dir, library_root.resolve())
        with self.assertRaises(FileNotFoundError):
            load_existing_config(self.root / "not-a-library")

        videos = library_root / "videos"
        videos.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "library data folder, not its videos subfolder"):
            load_existing_config(videos)

        malformed = self.root / "malformed"
        malformed.mkdir()
        (malformed / "config.ini").write_text("not an ini file\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Could not read the ytarchive Library configuration"):
            load_existing_config(malformed)

    def test_move_data_root_moves_children_and_rewrites_paths(self) -> None:
        source = self.root / "old-root"
        target = self.root / "new-root"
        source.mkdir()
        (source / "videos").mkdir()
        (source / "videos" / "track.mp3").write_bytes(b"audio")
        save_ini_settings(source, {"DOWNLOAD_DIRECTORY": str(source / "videos")})

        moved = move_data_root(source, target, remember=False)

        self.assertFalse(source.exists())
        self.assertEqual(moved.root_dir, target.resolve())
        self.assertEqual(moved.download_dir, target.resolve() / "videos")
        self.assertTrue((target / "videos" / "track.mp3").is_file())
        self.assertEqual(read_ini_settings(target)["DOWNLOAD_DIRECTORY"], "videos")

    def test_subsonic_settings_are_grouped_as_an_integration(self) -> None:
        subsonic_keys = {
            "SERVER_HOST", "SERVER_PORT", "SERVER_USERNAME", "SERVER_PASSWORD",
            "SERVER_PASSWORD_HASH", "SERVER_TIMING", "SERVER_AUTOSTART",
        }
        self.assertEqual(
            {SETTING_BY_KEY[key].category for key in subsonic_keys},
            {"Integrations"},
        )

    def test_loads_discord_application_id(self) -> None:
        self.config_path.write_text(
            "[Settings]\nDISCORD_APPLICATION_ID = 1407840598706229378\n",
            encoding="utf-8",
        )
        self.assertEqual(load_config(self.root).discord_application_id, "1407840598706229378")

    def test_loads_discord_presence_image_settings(self) -> None:
        self.config_path.write_text(
            "[Settings]\nDISCORD_PRESENCE_IMAGE_MODE = discord_key\n"
            "DISCORD_PRESENCE_IMAGE_VALUE = cover-art\n"
            "DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL = true\n",
            encoding="utf-8",
        )
        config = load_config(self.root)
        self.assertEqual(config.discord_presence_image_mode, "discord_key")
        self.assertEqual(config.discord_presence_image_value, "cover-art")
        self.assertTrue(config.discord_presence_default_youtube_thumbnail)

    def test_loads_discord_small_image_on_per_song_override_setting(self) -> None:
        self.config_path.write_text(
            "[Settings]\nDISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE = true\n",
            encoding="utf-8",
        )
        self.assertTrue(load_config(self.root).discord_presence_show_default_as_small_on_override)

    def test_loads_discord_small_image_settings(self) -> None:
        self.config_path.write_text(
            "[Settings]\nDISCORD_PRESENCE_SMALL_IMAGE_MODE = discord_key\n"
            "DISCORD_PRESENCE_SMALL_IMAGE_VALUE = artist-icon\n",
            encoding="utf-8",
        )
        config = load_config(self.root)
        self.assertEqual(config.discord_presence_small_image_mode, "discord_key")
        self.assertEqual(config.discord_presence_small_image_value, "artist-icon")

    def test_loads_startup_options_and_recovers_invalid_numbers(self) -> None:
        self.config_path.write_text(
            "[Settings]\nSERVER_AUTOSTART = false\nSTART_MINIMIZED_TO_TRAY = yes\nCLOSE_TO_TRAY = no\n"
            "CACHE_UPDATE_ON_STARTUP = automatic\nCHECK_FOR_UPDATES_ON_STARTUP = no\n"
            "NUM_WORKERS = nope\nSERVER_PORT = bad\n",
            encoding="utf-8",
        )
        config = load_config(self.root)
        self.assertFalse(config.server_autostart)
        self.assertTrue(config.start_minimized_to_tray)
        self.assertFalse(config.close_to_tray)
        self.assertEqual(config.cache_update_on_startup, "automatic")
        self.assertFalse(config.check_for_updates_on_startup)
        self.assertEqual(config.workers, 4)
        self.assertEqual(config.server_port, 4533)

    def test_loads_aria2c_and_browser_cookie_settings(self) -> None:
        self.config_path.write_text(
            "[Settings]\nUSE_ARIA2C = false\nUSE_BROWSER_COOKIES = firefox:work\n",
            encoding="utf-8",
        )
        config = load_config(self.root)
        self.assertFalse(config.use_aria2c)
        self.assertEqual(config.browser_cookies, ("firefox", "work", None, None))

    def test_loads_browser_cookie_usage_mode(self) -> None:
        for mode in ("always", "required", "never"):
            self.config_path.write_text(f"[Settings]\nBROWSER_COOKIES_MODE = {mode}\n", encoding="utf-8")
            self.assertEqual(load_config(self.root).browser_cookies_mode, mode)

        self.config_path.write_text("[Settings]\nBROWSER_COOKIES_MODE = invalid\n", encoding="utf-8")
        self.assertEqual(load_config(self.root).browser_cookies_mode, "required")

    def test_loads_cookie_file_path(self) -> None:
        self.config_path.write_text("[Settings]\nCOOKIES_FILE = exported/cookies.txt\n", encoding="utf-8")
        self.assertEqual(load_config(self.root).cookies_file, (self.root / "exported/cookies.txt").resolve())

    def test_subsonic_server_can_be_explicitly_enabled(self) -> None:
        self.config_path.write_text("[Settings]\nSERVER_AUTOSTART = true\n", encoding="utf-8")
        self.assertTrue(load_config(self.root).server_autostart)

    def test_loads_dark_mode_setting(self) -> None:
        self.config_path.write_text("[Settings]\nDARK_MODE = true\n", encoding="utf-8")
        self.assertTrue(load_config(self.root).dark_mode)

    def test_loads_similarity_controls_and_disabled_tags(self) -> None:
        self.config_path.write_text(
            "[Settings]\nDISABLED_SIMILARITY_TAGS = Ambient, Live\nSIMILARITY_USE_RARITY = false\nSIMILARITY_USE_BPM = false\n"
            "SIMILARITY_BPM_MAX_DISTANCE = 12\nSIMILARITY_BPM_WEIGHT = 45\n"
            "SIMILARITY_ARTIST_WEIGHT = 80\nSIMILARITY_TAG_WEIGHT = 125\nSIMILARITY_MIN_SCORE = 20\nSIMILARITY_MAX_RESULTS = 12\n",
            encoding="utf-8",
        )
        config = load_config(self.root)
        self.assertEqual(config.disabled_similarity_tags, frozenset({"ambient", "live"}))
        self.assertFalse(config.similarity_use_rarity)
        self.assertFalse(config.similarity_use_bpm)
        self.assertEqual(config.similarity_bpm_max_distance, 12)
        self.assertEqual(config.similarity_bpm_weight, 45)
        self.assertEqual(config.similarity_artist_weight, 80)
        self.assertEqual(config.similarity_tag_weight, 125)
        self.assertEqual(config.similarity_min_score, 20)
        self.assertEqual(config.similarity_max_results, 12)

    def test_save_patches_only_settings_values_and_preserves_unknown_layout(self) -> None:
        self.config_path.write_text(
            "; personal comment\n[Settings]\nDOWNLOAD_DIRECTORY = old-videos ; archive location\nCUSTOM_SWITCH = enabled\n\n[Other]\nVALUE = kept\n",
            encoding="utf-8",
        )
        save_ini_settings(self.root, {"DOWNLOAD_DIRECTORY": "videos", "CUSTOM_SWITCH": "changed", "SERVER_AUTOSTART": "false"})
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("; personal comment", text)
        self.assertIn("DOWNLOAD_DIRECTORY = videos ; archive location", text)
        self.assertIn("CUSTOM_SWITCH = changed", text)
        self.assertIn("SERVER_AUTOSTART = false", text)
        self.assertIn("[Other]\nVALUE = kept", text)
        self.assertEqual(read_ini_settings(self.root)["CUSTOM_SWITCH"], "changed")

    def test_none_removes_override_and_effective_value_falls_back_to_default(self) -> None:
        self.config_path.write_text("[Settings]\nNUM_WORKERS = 9\n", encoding="utf-8")
        save_ini_settings(self.root, {"NUM_WORKERS": None})
        self.assertNotIn("NUM_WORKERS", self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(effective_settings(self.root)["NUM_WORKERS"], "4")
