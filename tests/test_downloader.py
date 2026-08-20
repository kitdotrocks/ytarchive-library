from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.config import AppConfig
from ytarchive.downloader import CacheUpdater, ResolvedVideo, SpotifyTrack, is_youtube_media_input, normalize_media_input
import ytarchive.downloader as downloader_module
from ytarchive.ids import canonical_local_id
from ytarchive.library import build_file_index
from ytarchive.metadata import MetadataStore


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self.body = body
        self.offset = 0
        self.headers = {"Content-Length": str(len(body))}
        if headers:
            self.headers.update(headers)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class DirectDownloadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = AppConfig(
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
        self.metadata = MetadataStore(self.config.metadata_path)

    def test_aria2c_disabled_uses_ytdlp_directly(self) -> None:
        self.config.use_aria2c = False
        updater = CacheUpdater(self.config, self.metadata)
        resolved = mock.Mock(id="yt-test", title="Test", info={"webpage_url": "https://example.test/video"})
        with mock.patch.object(updater, "_download_with_aria2") as aria, mock.patch.object(updater, "_download_with_ytdlp") as ytdlp:
            updater._download_resolved_candidate(resolved, "candidate", lambda _data: None, mock.Mock(is_set=lambda: False))
        aria.assert_not_called()
        ytdlp.assert_called_once()

    def test_missing_aria2c_uses_ytdlp_without_fallback_message(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        resolved = mock.Mock(id="yt-test", title="Test", info={"webpage_url": "https://example.test/video"})
        with (
            mock.patch.object(downloader_module, "aria2c_available", return_value=False),
            mock.patch.object(updater, "_download_with_aria2") as aria,
            mock.patch.object(updater, "_download_with_ytdlp") as ytdlp,
        ):
            updater._download_resolved_candidate(resolved, "candidate", lambda _data: None, mock.Mock(is_set=lambda: False))
        aria.assert_not_called()
        ytdlp.assert_called_once()

    def test_browser_cookie_usage_mode_controls_resolution(self) -> None:
        self.config.browser_cookies = ("chrome", None, None, None)
        updater = CacheUpdater(self.config, self.metadata)
        resolved = mock.Mock()

        for mode, expected_calls in (("always", [True]), ("never", [False])):
            self.config.browser_cookies_mode = mode
            with mock.patch.object(updater, "_resolve_once", return_value=resolved) as resolve_once:
                self.assertIs(updater._resolve("https://example.test/video", use_browser=False), resolved)
            self.assertEqual([call.kwargs["use_browser"] for call in resolve_once.call_args_list], expected_calls)

        self.config.browser_cookies_mode = "required"
        with mock.patch.object(updater, "_resolve_once", side_effect=[RuntimeError("needs auth"), resolved]) as resolve_once:
            self.assertIs(updater._resolve("https://example.test/video", use_browser=False), resolved)
        self.assertEqual([call.kwargs["use_browser"] for call in resolve_once.call_args_list], [False, True])

    def test_required_browser_cookies_retry_playlist_enumeration(self) -> None:
        self.config.browser_cookies = ("chrome", None, None, None)
        self.config.browser_cookies_mode = "required"
        updater = CacheUpdater(self.config, self.metadata)
        with mock.patch.object(
            updater,
            "_extract_target_once",
            side_effect=[RuntimeError("needs auth"), {"entries": [{"id": "abc123"}]}],
        ) as extract:
            urls = updater._playlist_video_urls("https://www.youtube.com/playlist?list=test")

        self.assertEqual(urls, ["https://www.youtube.com/watch?v=abc123"])
        self.assertEqual([call.kwargs["use_browser"] for call in extract.call_args_list], [False, True])

    def test_required_browser_cookies_retry_spotify_youtube_search(self) -> None:
        self.config.browser_cookies = ("chrome", None, None, None)
        self.config.browser_cookies_mode = "required"
        updater = CacheUpdater(self.config, self.metadata)
        resolved = ResolvedVideo(
            id="yt-result",
            title="Result",
            upload_date="",
            author="Artist",
            info={},
            cookie_headers={},
            used_browser_cookies=True,
        )
        track = SpotifyTrack(id="sp-track", title="Song", author="Artist", source_url="https://open.spotify.com/track/test")
        with (
            mock.patch.object(
                updater,
                "_extract_target_once",
                side_effect=[RuntimeError("needs auth"), {"entries": [{"webpage_url": "https://youtu.be/abc123"}]}],
            ) as extract,
            mock.patch.object(updater, "_resolve", return_value=resolved),
        ):
            result = updater._resolve_youtube_search(track, lambda _data: None)

        self.assertEqual(result.id, "sp-track")
        self.assertEqual([call.kwargs["use_browser"] for call in extract.call_args_list], [False, True])

    def test_spotify_youtube_search_ranks_artist_match_over_same_title(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        track = SpotifyTrack(
            id="sp-track",
            title="Song",
            author="Artist",
            source_url="https://open.spotify.com/track/1234567890123456789012",
        )
        resolved = ResolvedVideo(
            id="yt-right",
            title="Artist - Song",
            upload_date="",
            author="Artist",
            info={"title": "Artist - Song", "uploader": "Artist", "duration": 180},
            cookie_headers={},
            used_browser_cookies=False,
        )
        with (
            mock.patch.object(
                updater,
                "_extract_target_info",
                return_value={
                    "entries": [
                        {
                            "webpage_url": "https://youtu.be/wrongwrong1",
                            "title": "Other Artist - Song (Official Audio)",
                            "uploader": "Other Artist",
                            "duration": 180,
                        },
                        {
                            "webpage_url": "https://youtu.be/rightone01",
                            "title": "Artist - Song (Official Audio)",
                            "uploader": "Artist",
                            "duration": 180,
                        },
                    ]
                },
            ),
            mock.patch.object(updater, "_resolve", return_value=resolved) as resolve,
        ):
            result = updater._resolve_youtube_search(track, lambda _data: None)

        self.assertEqual(result.id, "sp-track")
        self.assertEqual(resolve.call_args.args[0], "https://youtu.be/rightone01")

    def test_bandcamp_search_does_not_choose_same_title_wrong_artist(self) -> None:
        payload = json_bytes(
            {
                "results": [
                    {
                        "type": "t",
                        "name": "Song",
                        "band_name": "Other Artist",
                        "url": "https://other.bandcamp.com/track/song",
                    },
                    {
                        "type": "t",
                        "name": "Song",
                        "band_name": "Artist",
                        "url": "https://artist.bandcamp.com/track/song",
                    },
                ]
            }
        )
        with mock.patch("ytarchive.downloader.urlopen", return_value=FakeResponse(payload)):
            result = downloader_module._bandcamp_search_track_url("Song", "Artist")
        self.assertEqual(result, "https://artist.bandcamp.com/track/song")

    def test_bandcamp_resolver_validates_the_resolved_track(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        track = SpotifyTrack(
            id="sp-track",
            title="Song",
            author="Artist",
            source_url="https://open.spotify.com/track/1234567890123456789012",
        )
        wrong = ResolvedVideo(
            id="bc-wrong",
            title="Song",
            upload_date="",
            author="Other Artist",
            info={"title": "Song", "artist": "Other Artist"},
            cookie_headers={},
            used_browser_cookies=False,
        )
        right = ResolvedVideo(
            id="bc-right",
            title="Song",
            upload_date="",
            author="Artist",
            info={"title": "Song", "artist": "Artist"},
            cookie_headers={},
            used_browser_cookies=False,
        )
        with (
            mock.patch(
                "ytarchive.downloader._bandcamp_search_candidates",
                return_value=[
                    (80.0, "https://other.bandcamp.com/track/song", "Song", "Other Artist"),
                    (100.0, "https://artist.bandcamp.com/track/song", "Song", "Artist"),
                ],
            ),
            mock.patch.object(updater, "_resolve", side_effect=[wrong, right]) as resolve,
        ):
            result = updater._resolve_bandcamp_search(track, lambda _data: None)
        self.assertEqual(result.source_url, track.source_url)
        self.assertEqual(resolve.call_count, 2)

    def test_spotify_and_monochrome_links_require_exact_track_paths(self) -> None:
        valid_spotify = "https://open.spotify.com/track/1234567890123456789012"
        self.assertEqual(normalize_media_input(valid_spotify), valid_spotify)
        for value in (
            "https://open.spotify.com/track/not-a-real-id",
            "https://open.spotify.com/album/1234567890123456789012",
            "https://open.spotify.com.evil.test/track/1234567890123456789012",
            "https://monochrome.tf/track/123/extra",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_media_input(value)

    def test_youtube_dependency_gate_is_source_aware(self) -> None:
        self.assertTrue(is_youtube_media_input("dQw4w9WgXcQ"))
        self.assertTrue(is_youtube_media_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(is_youtube_media_input("https://soundcloud.com/artist/track"))
        self.assertFalse(is_youtube_media_input("https://open.spotify.com/track/test"))
        self.assertFalse(is_youtube_media_input("https://archive.org/0/items/example/song.flac"))

    def test_cookie_file_is_temporarily_ignored(self) -> None:
        self.config.browser_cookies = ("chrome", None, None, None)
        self.config.browser_cookies_mode = "always"
        self.config.cookies_file = self.config.root_dir / "cookies.txt"
        updater = CacheUpdater(self.config, self.metadata)
        opts = updater._ydl_opts(skip_download=True, flat=False, use_browser=False)
        self.assertNotIn("cookiefile", opts)
        self.assertNotIn("cookiesfrombrowser", opts)
        self.assertIn("nocookies", opts)

    def test_ytdlp_logger_forwards_warnings_to_progress(self) -> None:
        messages = []
        updater = CacheUpdater(self.config, self.metadata)
        logger = updater._ydl_opts(skip_download=True, flat=False, use_browser=False, progress=messages.append)["logger"]

        logger.debug("web player response playability status: UNPLAYABLE")
        logger.debug("Downloading webpage")
        logger.warning("Signature solving failed")
        logger.error("No formats found")

        self.assertEqual(
            messages,
            [
                {"phase": "native", "message": "yt-dlp debug: web player response playability status: UNPLAYABLE"},
                {"phase": "native", "message": "yt-dlp warning: Signature solving failed"},
                {"phase": "native", "message": "yt-dlp error: No formats found"},
            ],
        )

    def test_ytdlp_logger_explains_youtube_rate_limit(self) -> None:
        messages = []
        updater = CacheUpdater(self.config, self.metadata)
        logger = updater._ydl_opts(skip_download=True, flat=False, use_browser=False, progress=messages.append)["logger"]

        logger.warning("[youtube] SNvDUO42Hys: Unable to download webpage: HTTP Error 429: Too Many Requests")
        logger.error("[youtube] SNvDUO42Hys: Sign in to confirm you're not a bot")

        self.assertEqual(messages[0], {
            "phase": "native",
            "message": "yt-dlp warning: [youtube] SNvDUO42Hys: Unable to download webpage: HTTP Error 429: Too Many Requests",
        })
        self.assertEqual(messages[1], {
            "phase": "native",
            "message": downloader_module.YOUTUBE_RATE_LIMIT_HINT,
        })
        self.assertEqual(len(messages), 3)

    def test_ytdlp_opts_apply_rate_limit_pacing(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        target = "https://www.youtube.com/watch?v=SNvDUO42Hys"
        extract_opts = updater._ydl_opts(skip_download=True, flat=False, use_browser=False, target=target)
        download_opts = updater._ydl_opts(skip_download=False, flat=False, use_browser=False, target=target)

        self.assertEqual(extract_opts["sleep_interval_requests"], downloader_module.YOUTUBE_SLEEP_REQUESTS)
        self.assertEqual(download_opts["sleep_interval"], downloader_module.YOUTUBE_SLEEP_INTERVAL)
        self.assertEqual(download_opts["max_sleep_interval"], downloader_module.YOUTUBE_MAX_SLEEP_INTERVAL)

    def test_ytdlp_opts_selects_node_when_deno_is_unavailable(self) -> None:
        def fake_which(executable: str):
            return "/usr/bin/node" if executable == "node" else None

        updater = CacheUpdater(self.config, self.metadata)
        with mock.patch("ytarchive.downloader.shutil.which", side_effect=fake_which):
            opts = updater._ydl_opts(skip_download=True, flat=False, use_browser=False)

        self.assertEqual(opts["js_runtimes"], {"node": {}})
        self.assertNotIn("http_headers", opts)

    def test_ytdlp_opts_finds_official_per_user_deno_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            deno = home / ".deno" / "bin" / ("deno.exe" if os.name == "nt" else "deno")
            deno.parent.mkdir(parents=True)
            deno.write_bytes(b"")
            deno.chmod(0o700)
            updater = CacheUpdater(self.config, self.metadata)
            with (
                mock.patch("ytarchive.downloader.shutil.which", return_value=None),
                mock.patch("ytarchive.downloader.Path.home", return_value=home),
            ):
                opts = updater._ydl_opts(skip_download=True, flat=False, use_browser=False)

        self.assertEqual(opts["js_runtimes"], {"deno": {"path": str(deno)}})

    def test_ytdlp_missing_components_reports_solver_and_runtime(self) -> None:
        with (
            mock.patch.object(downloader_module, "yt_dlp_ejs_available", return_value=False),
            mock.patch.object(downloader_module, "yt_dlp_js_runtime", return_value=None),
        ):
            self.assertEqual(downloader_module.yt_dlp_missing_components(), ["yt-dlp-ejs", "yt-dlp-js-runtime"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normalize_accepts_direct_media_url(self) -> None:
        url = "https://archive.org/0/items/example/song.flac"
        self.assertEqual(normalize_media_input(url), url)
        self.assertEqual(normalize_media_input("archive.org/0/items/example/song.flac"), url)

    def test_normalize_accepts_monochrome_urls(self) -> None:
        self.assertEqual(normalize_media_input("monochrome.tf/track/123"), "https://monochrome.tf/track/123")
        self.assertEqual(normalize_media_input("https://monochrome.tf/album/456"), "https://monochrome.tf/album/456")

    def test_normalize_rejects_non_video_youtube_targets_and_lookalike_hosts(self) -> None:
        invalid = (
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/channel/UC123",
            "https://www.youtube.com/watch?v=short",
            "https://youtube.com.evil.test/watch?v=SNvDUO42Hys",
            "https://www.youtube.com/shorts/short",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_media_input(value)

    def test_queue_identity_uses_canonical_youtube_id(self) -> None:
        expected = "yt-SNvDUO42Hys"
        self.assertEqual(downloader_module._canonical_submitted_id("https://www.youtube.com/watch?v=SNvDUO42Hys"), expected)
        self.assertEqual(downloader_module._canonical_submitted_id("https://youtu.be/SNvDUO42Hys"), expected)

    def test_ytdlp_result_ignores_format_fragments(self) -> None:
        directory = self.config.root_dir / "yt-dlp-result"
        directory.mkdir()
        (directory / "yt-example.f137.mp4").write_bytes(b"video fragment")
        (directory / "yt-example.f140.m4a").write_bytes(b"audio fragment")
        (directory / "yt-example.mp4.part").write_bytes(b"unfinished")
        (directory / "yt-example.mp4").write_bytes(b"merged media")

        result = CacheUpdater._completed_ytdlp_media(directory, "yt-example")

        self.assertEqual(result, directory / "yt-example.mp4")

    def test_library_ignores_ytdlp_parts_and_format_fragments(self) -> None:
        self.config.download_dir.mkdir()
        (self.config.download_dir / "yt-example.f137.mp4").write_bytes(b"video fragment")
        (self.config.download_dir / "yt-example.f140.m4a").write_bytes(b"audio fragment")
        (self.config.download_dir / "yt-example.mp4.part").write_bytes(b"unfinished")

        self.assertNotIn("yt-example", build_file_index(self.config.download_dir))

    def test_add_video_downloads_direct_media_url(self) -> None:
        body = b"flac data"
        expected_id = canonical_local_id(hashlib.sha256(body).hexdigest())
        url = "https://archive.org/0/items/example/song.flac"

        with mock.patch("ytarchive.downloader.urlopen", return_value=FakeResponse(body)):
            result = CacheUpdater(self.config, self.metadata).add_video(url, lambda _data: None, mock.Mock(is_set=lambda: False))

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.resolved_id, expected_id)
        entry = self.metadata.get(expected_id)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.title, "song")
        self.assertEqual(entry.source_type, "direct")
        self.assertEqual(entry.source_url, url)
        self.assertEqual((self.config.download_dir / f"{expected_id}.flac").read_bytes(), body)

    def test_add_video_downloads_monochrome_track(self) -> None:
        track_url = "https://monochrome.tf/track/123"
        info_body = json_bytes(
            {
                "data": {
                    "id": 123,
                    "title": "Monochrome Song",
                    "duration": 12,
                    "audioQuality": "LOSSLESS",
                    "artist": {"name": "Mono Artist"},
                    "album": {"title": "Mono Album", "releaseDate": "2024-05-06", "cover": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                }
            }
        )
        manifest_body = json_bytes(
            {
                "data": {
                    "data": {
                        "id": "123",
                        "attributes": {
                            "uri": "https://cdn.monochrome.test/song.mpd",
                            "formats": ["FLAC"],
                            "trackPresentation": "FULL",
                        },
                    }
                }
            }
        )

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if "/info/" in url:
                return FakeResponse(info_body)
            if "/trackManifests/" in url:
                return FakeResponse(manifest_body)
            if url == "https://cdn.monochrome.test/song.mpd":
                return FakeResponse(b"<MPD></MPD>", headers={"Content-Type": "application/dash+xml"})
            if url.startswith("https://resources.tidal.com/images/"):
                return FakeResponse(b"jpeg cover", headers={"Content-Type": "image/jpeg"})
            raise AssertionError(f"unexpected URL: {url}")

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"flac data")
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = ""
            return proc

        with (
            mock.patch("ytarchive.downloader.urlopen", side_effect=fake_urlopen),
            mock.patch("ytarchive.downloader.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("ytarchive.downloader.subprocess.run", side_effect=fake_run),
            mock.patch("ytarchive.downloader.CacheUpdater._audio_bitrate", return_value=1411000),
        ):
            result = CacheUpdater(self.config, self.metadata).add_video(track_url, lambda _data: None, mock.Mock(is_set=lambda: False))

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.resolved_id, "mc-123")
        entry = self.metadata.get("mc-123")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.title, "Monochrome Song")
        self.assertEqual(entry.author, "Mono Artist")
        self.assertEqual(entry.source_type, "monochrome")
        self.assertEqual(entry.source_url, track_url)
        self.assertEqual(entry.upload_date, "20240506")
        self.assertEqual(entry.duration_seconds, 12)
        self.assertEqual(entry.audio_bitrate_kbps, 1411)
        self.assertEqual((self.config.download_dir / "mc-123.flac").read_bytes(), b"flac data")
        self.assertEqual((self.config.thumbnails_dir / "mc-123.jpg").read_bytes(), b"jpeg cover")

    def test_add_video_downloads_monochrome_album(self) -> None:
        album_url = "https://monochrome.tf/album/456"
        album_body = json_bytes(
            {
                "data": {
                    "id": 456,
                    "title": "Mono Album",
                    "releaseDate": "2024-05-06",
                    "cover": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "items": [
                        {
                            "item": {
                                "id": 123,
                                "title": "First Song",
                                "duration": 12,
                                "artist": {"name": "Mono Artist"},
                                "album": {"title": "Mono Album", "releaseDate": "2024-05-06"},
                            }
                        },
                        {
                            "item": {
                                "id": 124,
                                "title": "Second Song",
                                "duration": 15,
                                "artist": {"name": "Mono Artist"},
                                "album": {"title": "Mono Album", "releaseDate": "2024-05-06"},
                            }
                        },
                    ],
                }
            }
        )

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if "/album" in url:
                return FakeResponse(album_body)
            if "/trackManifests/" in url:
                track_id = "123" if "id=123" in url else "124"
                return FakeResponse(
                    json_bytes(
                        {
                            "data": {
                                "data": {
                                    "id": track_id,
                                    "attributes": {
                                        "uri": f"https://cdn.monochrome.test/{track_id}.mpd",
                                        "formats": ["FLAC"],
                                        "trackPresentation": "FULL",
                                    },
                                }
                            }
                        }
                    )
                )
            if url.startswith("https://cdn.monochrome.test/"):
                return FakeResponse(b"<MPD></MPD>", headers={"Content-Type": "application/dash+xml"})
            if url.startswith("https://resources.tidal.com/images/"):
                return FakeResponse(b"jpeg cover", headers={"Content-Type": "image/jpeg"})
            raise AssertionError(f"unexpected URL: {url}")

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(f"flac {Path(cmd[-1]).stem}".encode("utf-8"))
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = ""
            return proc

        with (
            mock.patch("ytarchive.downloader.urlopen", side_effect=fake_urlopen),
            mock.patch("ytarchive.downloader.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("ytarchive.downloader.subprocess.run", side_effect=fake_run),
            mock.patch("ytarchive.downloader.CacheUpdater._audio_bitrate", return_value=1000000),
        ):
            result = CacheUpdater(self.config, self.metadata).add_video(album_url, lambda _data: None, mock.Mock(is_set=lambda: False))

        self.assertEqual(result.status, "downloaded")
        self.assertIn("2 downloaded", result.message)
        self.assertIsNotNone(self.metadata.get("mc-123"))
        self.assertIsNotNone(self.metadata.get("mc-124"))
        self.assertTrue((self.config.download_dir / "mc-123.flac").exists())
        self.assertTrue((self.config.download_dir / "mc-124.flac").exists())
        self.assertTrue((self.config.thumbnails_dir / "mc-123.jpg").exists())
        self.assertTrue((self.config.thumbnails_dir / "mc-124.jpg").exists())

    def test_monochrome_preview_manifest_uses_deezer_fallback(self) -> None:
        track_url = "https://monochrome.tf/track/300559332"
        info_body = json_bytes(
            {
                "data": {
                    "id": 300559332,
                    "title": "MADAME LUCY",
                    "duration": 175,
                    "artist": {"name": "Lucy Bedroque"},
                    "isrc": "QZMEP2332878",
                    "album": {"title": "MADAME LUCY", "releaseDate": "2023-09-29", "cover": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                }
            }
        )
        preview_manifest_body = json_bytes(
            {
                "data": {
                    "data": {
                        "id": "300559332",
                        "attributes": {
                            "uri": "https://cdn.monochrome.test/preview.mpd",
                            "formats": ["FLAC"],
                            "trackPresentation": "PREVIEW",
                            "previewReason": "FULL_REQUIRES_SUBSCRIPTION",
                        },
                    }
                }
            }
        )

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if "/info/" in url:
                return FakeResponse(info_body)
            if "/trackManifests/" in url:
                return FakeResponse(preview_manifest_body)
            if url.startswith("https://dzr.tabs-vs-spaces.wtf/stream/"):
                self.assertIn("QZMEP2332878", url)
                self.assertEqual(request.headers.get("Origin"), "https://monochrome.tf")
                return FakeResponse(b"full fallback audio", headers={"Content-Type": "audio/mpeg"})
            if url.startswith("https://resources.tidal.com/images/"):
                return FakeResponse(b"jpeg cover", headers={"Content-Type": "image/jpeg"})
            raise AssertionError(f"unexpected URL: {url}")

        def fake_run(cmd, **_kwargs):
            self.assertTrue(Path(cmd[cmd.index("-i") + 1]).exists())
            Path(cmd[-1]).write_bytes(b"fallback flac")
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = ""
            return proc

        with (
            mock.patch("ytarchive.downloader.urlopen", side_effect=fake_urlopen),
            mock.patch("ytarchive.downloader.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("ytarchive.downloader.subprocess.run", side_effect=fake_run),
            mock.patch("ytarchive.downloader.CacheUpdater._audio_bitrate", return_value=320000),
        ):
            result = CacheUpdater(self.config, self.metadata).add_video(track_url, lambda _data: None, mock.Mock(is_set=lambda: False))

        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.resolved_id, "mc-300559332")
        self.assertEqual((self.config.download_dir / "mc-300559332.mp3").read_bytes(), b"fallback flac")
        entry = self.metadata.get("mc-300559332")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.duration_seconds, 175)
        self.assertEqual(entry.audio_bitrate_kbps, 320)
        self.assertEqual(entry.audio_quality, "mp3")

    def test_monochrome_cover_is_embedded_as_attached_mjpeg(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"flac with cover")
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = ""
            return proc

        track_url = "https://monochrome.tf/track/123"
        info_body = json_bytes(
            {
                "data": {
                    "id": 123,
                    "title": "Monochrome Song",
                    "duration": 12,
                    "audioQuality": "LOSSLESS",
                    "artist": {"name": "Mono Artist"},
                    "album": {"title": "Mono Album", "releaseDate": "2024-05-06", "cover": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                }
            }
        )
        manifest_body = json_bytes(
            {
                "data": {
                    "data": {
                        "id": "123",
                        "attributes": {
                            "uri": "https://cdn.monochrome.test/song.mpd",
                            "formats": ["FLAC"],
                            "trackPresentation": "FULL",
                        },
                    }
                }
            }
        )

        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if "/info/" in url:
                return FakeResponse(info_body)
            if "/trackManifests/" in url:
                return FakeResponse(manifest_body)
            if url == "https://cdn.monochrome.test/song.mpd":
                return FakeResponse(b"<MPD></MPD>", headers={"Content-Type": "application/dash+xml"})
            if url.startswith("https://resources.tidal.com/images/"):
                return FakeResponse(b"jpeg cover", headers={"Content-Type": "image/jpeg"})
            raise AssertionError(f"unexpected URL: {url}")

        with (
            mock.patch("ytarchive.downloader.urlopen", side_effect=fake_urlopen),
            mock.patch("ytarchive.downloader.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("ytarchive.downloader.subprocess.run", side_effect=fake_run),
            mock.patch("ytarchive.downloader.CacheUpdater._audio_bitrate", return_value=1411000),
        ):
            CacheUpdater(self.config, self.metadata).add_video(track_url, lambda _data: None, mock.Mock(is_set=lambda: False))

        embed_call = next(cmd for cmd in calls if "attached_pic" in cmd)
        self.assertIn("mjpeg", embed_call)
        self.assertTrue(embed_call[-1].endswith(".flac"))

    def test_monochrome_format_hints_preserve_lossy_codec(self) -> None:
        self.assertEqual(
            downloader_module._monochrome_stream_format({"formats": ["MP3"]}, "https://cdn.test/track.mpd")[0],
            "mp3",
        )
        self.assertEqual(
            downloader_module._monochrome_stream_format({"codec": "AAC"}, "https://cdn.test/track.mpd")[0],
            "m4a",
        )
        self.assertEqual(
            downloader_module._monochrome_stream_format(
                {"formats": ["FLAC"], "codec": "AAC"}, "https://cdn.test/track.mpd"
            )[0],
            "m4a",
        )
        self.assertEqual(
            downloader_module._monochrome_stream_format({"formats": ["FLAC_HIRES"]}, "https://cdn.test/track.mpd")[0],
            "flac",
        )


def json_bytes(value: dict) -> bytes:
    import json

    return json.dumps(value).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
