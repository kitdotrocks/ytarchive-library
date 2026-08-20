from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.config import AppConfig
from ytarchive.artist_art import ArtistProfile, ArtistProfileStore, _entry_source_url, artist_id_for_name
from ytarchive.metadata import VideoEntry
from ytarchive.server import (
    ServerCredentials,
    SimilarityCatalogCache,
    SubsonicHandler,
    SubsonicService,
    _range_bounds,
    _similarity_catalog,
    _similarity_score,
    _tag_frequencies,
    create_server,
    migrate_play_counts,
    similar_song_entries,
)


class SimilarityScoringTestCase(unittest.TestCase):
    def test_similarity_catalog_cache_reuses_generation(self) -> None:
        entries = [
            VideoEntry(id="one", title="One", upload_date="", tags=["ambient"]),
            VideoEntry(id="two", title="Two", upload_date="", tags=["rock"]),
        ]
        cache = SimilarityCatalogCache()
        first = cache.get(entries, 1, frozenset())
        self.assertIs(cache.get(entries, 1, frozenset()), first)
        self.assertIsNot(cache.get(entries, 2, frozenset()), first)
        self.assertIsNot(cache.get(entries, 2, frozenset({"ambient"})), first)

    def test_shared_narrower_tag_scores_higher_than_broad_tag(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="", author="Seed Artist", tags=["Music", "Synthwave"])
        broad = VideoEntry(id="broad", title="Broad", upload_date="", author="Broad Artist", tags=["Music"])
        narrow = VideoEntry(id="narrow", title="Narrow", upload_date="", author="Narrow Artist", tags=["Synthwave"])
        another_broad = VideoEntry(id="another", title="Another", upload_date="", author="Another Artist", tags=["Music"])
        entries = [seed, broad, narrow, another_broad]
        frequencies = _tag_frequencies(entries)

        self.assertGreater(
            _similarity_score(seed, "Seed Artist", narrow, frequencies, len(entries)),
            _similarity_score(seed, "Seed Artist", broad, frequencies, len(entries)),
        )

    def test_rarity_scoring_can_be_disabled(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="", tags=["Music", "Synthwave"])
        broad = VideoEntry(id="broad", title="Broad", upload_date="", tags=["Music"])
        narrow = VideoEntry(id="narrow", title="Narrow", upload_date="", tags=["Synthwave"])
        entries = [seed, broad, narrow]
        config = AppConfig(
            root_dir=Path("."), download_dir=Path("videos"), exports_dir=Path("exports"),
            thumbnails_dir=Path("thumbnails"), artist_profiles_path=Path("artist_profiles.json"),
            artist_thumbnails_dir=Path("artist_thumbnails"), metadata_path=Path("video_metadata.json"),
            playlists_path=Path("playlists.txt"), workers=1, browser_cookies=None,
            discord_enabled=False, spotify_lossless_command="", spotdl_command="",
            server_host="127.0.0.1", server_port=0, server_username="user",
            server_password="secret", server_password_hash="", similarity_use_artist=False,
            similarity_use_bpm=False, similarity_use_rarity=False,
        )
        frequencies = _tag_frequencies(entries)

        self.assertEqual(
            _similarity_score(seed, "", broad, frequencies, len(entries), config),
            _similarity_score(seed, "", narrow, frequencies, len(entries), config),
        )

    def test_same_artist_outranks_a_bpm_only_match(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="", author="Seed Artist", bpm=120)
        same_artist = VideoEntry(id="artist", title="Artist", upload_date="", author="Seed Artist", bpm=170)
        tempo_match = VideoEntry(id="tempo", title="Tempo", upload_date="", author="Other Artist", bpm=120)
        entries = [seed, same_artist, tempo_match]
        frequencies = _tag_frequencies(entries)

        self.assertGreater(
            _similarity_score(seed, "Seed Artist", same_artist, frequencies, len(entries)),
            _similarity_score(seed, "Seed Artist", tempo_match, frequencies, len(entries)),
        )

    def test_artist_match_uses_the_same_weighting_as_a_tag_match(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="", author="Artist", tags=["Style"])
        same_artist = VideoEntry(id="artist", title="Artist", upload_date="", author="Artist")
        same_tag = VideoEntry(id="tag", title="Tag", upload_date="", author="Other Artist", tags=["Style"])
        entries = [seed, same_artist, same_tag]
        frequencies = _tag_frequencies(entries)

        self.assertEqual(
            _similarity_score(seed, "Artist", same_artist, frequencies, len(entries)),
            _similarity_score(seed, "Artist", same_tag, frequencies, len(entries)),
        )

    def test_unknown_artist_placeholder_is_not_a_similarity_signal(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="")
        candidate = VideoEntry(id="candidate", title="Candidate", upload_date="")
        entries = [seed, candidate]

        self.assertEqual(
            _similarity_score(seed, "Unknown Artist", candidate, _tag_frequencies(entries), len(entries)),
            0,
        )

    def test_disabled_similarity_tag_is_excluded_from_scoring(self) -> None:
        seed = VideoEntry(id="seed", title="Seed", upload_date="", tags=["Ambient"])
        candidate = VideoEntry(id="candidate", title="Candidate", upload_date="", tags=["Ambient"])
        entries = [seed, candidate]
        config = AppConfig(
            root_dir=Path("."), download_dir=Path("videos"), exports_dir=Path("exports"), thumbnails_dir=Path("thumbnails"),
            artist_profiles_path=Path("artist_profiles.json"), artist_thumbnails_dir=Path("artist_thumbnails"),
            metadata_path=Path("video_metadata.json"), playlists_path=Path("playlists.txt"), workers=1,
            browser_cookies=None, discord_enabled=False, spotify_lossless_command="", spotdl_command="", server_host="127.0.0.1",
            server_port=0, server_username="user", server_password="secret", server_password_hash="", disabled_similarity_tags=frozenset({"ambient"}),
        )
        self.assertEqual(
            _similarity_score(seed, "", candidate, _tag_frequencies(entries, config.disabled_similarity_tags), len(entries), config),
            0,
        )

    def test_optimized_selection_preserves_full_ranking_order(self) -> None:
        config = AppConfig(
            root_dir=Path("."), download_dir=Path("videos"), exports_dir=Path("exports"), thumbnails_dir=Path("thumbnails"),
            artist_profiles_path=Path("artist_profiles.json"), artist_thumbnails_dir=Path("artist_thumbnails"),
            metadata_path=Path("video_metadata.json"), playlists_path=Path("playlists.txt"), workers=1,
            browser_cookies=None, discord_enabled=False, spotify_lossless_command="", spotdl_command="", server_host="127.0.0.1",
            server_port=0, server_username="user", server_password="secret", server_password_hash="", similarity_max_results=3,
        )
        seed = VideoEntry(id="seed", title="Seed", upload_date="", author="Artist", bpm=120, tags=["Chill", "Night"])
        entries = [
            seed,
            VideoEntry(id="artist", title="Artist", upload_date="", author="Artist", bpm=150),
            VideoEntry(id="tags", title="Tags", upload_date="", author="Other", bpm=90, tags=["chill", "night"]),
            VideoEntry(id="tempo", title="Tempo", upload_date="", author="Other", bpm=60),
            VideoEntry(id="hidden", title="Hidden", upload_date="", author="Artist", hidden_from_subsonic=True),
        ]
        visible = [entry for entry in entries if not entry.hidden_from_subsonic]
        frequencies = _tag_frequencies(visible, config.disabled_similarity_tags)
        expected = sorted(
            (
                (entry, _similarity_score(seed, "Artist", entry, frequencies, len(visible), config))
                for entry in visible
                if entry.id != seed.id
            ),
            key=lambda item: (-item[1], (item[0].title or item[0].id).casefold(), item[0].id),
        )
        expected_ids = [entry.id for entry, score in expected if score >= config.similarity_min_score][:3]

        actual = similar_song_entries(entries, seed, "Artist", seed.id, config)
        cached_actual = similar_song_entries(
            entries,
            seed,
            "Artist",
            seed.id,
            config,
            catalog=_similarity_catalog(entries, config.disabled_similarity_tags),
        )

        self.assertEqual([entry.id for entry in actual], expected_ids)
        self.assertEqual([entry.id for entry in cached_actual], expected_ids)


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.videos = root / "videos"
        self.videos.mkdir()
        self.thumbnails = root / "thumbnails"
        self.thumbnails.mkdir()
        self.artist_thumbnails = root / "artist_thumbnails"
        self.artist_thumbnails.mkdir()
        self.artist_profiles = root / "artist_profiles.json"
        (self.videos / "abc123.mp3").write_bytes(b"0123456789")
        (self.videos / "embedded.mp3").write_bytes(b"embedded")
        (self.videos / "sameartist.mp3").write_bytes(b"same artist")
        (self.videos / "yt-oTqohAwUi8A.mp3").write_bytes(b"youtube")
        (self.thumbnails / "oTqohAwUi8A.jpg").write_bytes(b"jpeg")
        (root / "video_metadata.json").write_text(
            json.dumps(
                {
                    "abc123": {
                        "title": "A Test Song",
                        "author": "Example Artist",
                        "upload_date": "20240102",
                        "source_type": "youtube",
                        "source_id": "playlist-1",
                        "playback_seconds": 42.0,
                        "lyrics": "[00:01.00]First line\n[00:02.50]Second line",
                    },
                    "sameartist": {
                        "title": "Another Test Song",
                        "author": "Example Artist",
                        "upload_date": "20240105",
                    },
                    "embedded": {
                        "title": "Embedded Cover Song",
                        "author": "SoundCloud Artist",
                        "upload_date": "20240106",
                        "source_type": "soundcloud",
                    },
                    "missing": {
                        "title": "Missing Song",
                        "author": "No File",
                        "upload_date": "20240103",
                    },
                    "yt-oTqohAwUi8A": {
                        "title": "YouTube Song",
                        "author": "YT Artist",
                        "upload_date": "20240104",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.config = AppConfig(
            root_dir=root,
            download_dir=self.videos,
            exports_dir=root / "exports",
            thumbnails_dir=self.thumbnails,
            artist_profiles_path=self.artist_profiles,
            artist_thumbnails_dir=self.artist_thumbnails,
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
        self.service = SubsonicService(self.config, ServerCredentials("user", password="secret"))
        handler = type(
            "TestSubsonicHandler",
            (SubsonicHandler,),
            {"service": self.service},
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_port

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.service.close()
        self.tmp.cleanup()

    def request(self, path: str, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers=headers or {})
        return conn.getresponse()

    def auth_query(self) -> str:
        salt = "salt"
        token = hashlib.md5(("secret" + salt).encode("utf-8")).hexdigest()
        return f"u=user&t={token}&s={salt}&v=1.16.1&c=test&f=json"

    def test_ping_accepts_subsonic_token_auth(self) -> None:
        response = self.request(f"/rest/ping.view?{self.auth_query()}")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read())
        self.assertEqual(payload["subsonic-response"]["status"], "ok")

    def test_search3_maps_existing_entries_and_filters_missing_files(self) -> None:
        response = self.request(f"/rest/search3.view?{self.auth_query()}&query=Test")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["searchResult3"]["song"]
        by_id = {song["id"]: song for song in songs}
        self.assertIn("abc123", by_id)
        self.assertIn("sameartist", by_id)
        self.assertEqual(by_id["abc123"]["artist"], "Example Artist")
        self.assertEqual(by_id["abc123"]["album"], "A Test Song")

    def test_hidden_entries_are_excluded_from_subsonic(self) -> None:
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        metadata["abc123"]["hidden_from_subsonic"] = True
        (self.config.metadata_path).write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/search3.view?{self.auth_query()}&query=Test")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["searchResult3"]["song"]
        self.assertNotIn("abc123", {song["id"] for song in songs})

        response = self.request(f"/rest/getSong.view?{self.auth_query()}&id=abc123")
        self.assertEqual(response.status, 404)

        response = self.request(f"/rest/getLyrics.view?{self.auth_query()}&id=abc123")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["subsonic-response"]["lyrics"]["value"], "")

        response = self.request(f"/rest/getLyricsBySongId.view?{self.auth_query()}&id=abc123")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["subsonic-response"]["lyricsList"]["structuredLyrics"], [])

    def test_get_song_exposes_duration_and_bitrate(self) -> None:
        with mock.patch(
            "ytarchive.server.ffprobe_json",
            return_value={
                "format": {"duration": "123.4", "bit_rate": "1000000"},
                "streams": [{"codec_type": "audio", "bit_rate": "192000"}],
            },
        ):
            response = self.request(f"/rest/getSong.view?{self.auth_query()}&id=abc123")
            response_body = json.loads(response.read())
            self.service.wait_for_media_probes()
            followup = self.request(f"/rest/getSong.view?{self.auth_query()}&id=abc123")
            followup_song = json.loads(followup.read())["subsonic-response"]["song"]

        self.assertEqual(response.status, 200)
        self.assertIn(response_body["subsonic-response"]["song"].get("duration"), (None, 123))
        self.assertEqual(followup_song["duration"], 123)
        self.assertEqual(followup_song["bitRate"], 192)
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["abc123"]["duration_seconds"], 123.4)
        self.assertEqual(metadata["abc123"]["bitrate_kbps"], 1000)
        self.assertEqual(metadata["abc123"]["audio_bitrate_kbps"], 192)

    def test_get_song_does_not_probe_media_on_request_thread(self) -> None:
        with mock.patch.object(self.service, "schedule_media_probe") as schedule:
            with mock.patch("ytarchive.server.ffprobe_json") as probe:
                response = self.request(f"/rest/getSong.view?{self.auth_query()}&id=abc123")
                response.read()

        self.assertEqual(response.status, 200)
        schedule.assert_called_once()
        probe.assert_not_called()

    def test_get_song_uses_cached_duration_and_bitrate_without_probe(self) -> None:
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        metadata["abc123"]["duration_seconds"] = 321.2
        metadata["abc123"]["bitrate_kbps"] = 1000
        metadata["abc123"]["audio_bitrate_kbps"] = 256
        (self.config.metadata_path).write_text(json.dumps(metadata), encoding="utf-8")

        with mock.patch("ytarchive.server.ffprobe_json") as probe:
            response = self.request(f"/rest/getSong.view?{self.auth_query()}&id=abc123")

        self.assertEqual(response.status, 200)
        song = json.loads(response.read())["subsonic-response"]["song"]
        self.assertEqual(song["duration"], 321)
        self.assertEqual(song["bitRate"], 256)
        probe.assert_not_called()

    def test_album_duration_sums_cached_song_durations(self) -> None:
        metadata = json.loads(self.config.metadata_path.read_text(encoding="utf-8"))
        metadata["abc123"]["duration_seconds"] = 120
        self.config.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getAlbumList2.view?{self.auth_query()}&size=20")
        self.assertEqual(response.status, 200)
        albums = json.loads(response.read())["subsonic-response"]["albumList2"]["album"]
        album = next(album for album in albums if album["name"] == "A Test Song")
        self.assertEqual(album["duration"], 120)

    def test_get_lyrics_returns_manual_lrc_text_by_id(self) -> None:
        response = self.request(f"/rest/getLyrics.view?{self.auth_query()}&id=abc123")
        self.assertEqual(response.status, 200)
        lyrics = json.loads(response.read())["subsonic-response"]["lyrics"]
        self.assertEqual(lyrics["artist"], "Example Artist")
        self.assertEqual(lyrics["title"], "A Test Song")
        self.assertEqual(lyrics["value"], "[00:01.00]First line\n[00:02.50]Second line")

    def test_get_lyrics_by_song_id_returns_structured_lrc_lines(self) -> None:
        response = self.request(f"/rest/getLyricsBySongId.view?{self.auth_query()}&id=abc123")
        self.assertEqual(response.status, 200)
        lyrics_list = json.loads(response.read())["subsonic-response"]["lyricsList"]
        structured = lyrics_list["structuredLyrics"][0]
        self.assertEqual(structured["displayArtist"], "Example Artist")
        self.assertEqual(structured["displayTitle"], "A Test Song")
        self.assertEqual(structured["synced"], True)
        self.assertEqual(structured["line"], [{"start": 1000, "value": "First line"}, {"start": 2500, "value": "Second line"}])

    def test_open_subsonic_extensions_advertises_song_lyrics(self) -> None:
        response = self.request(f"/rest/getOpenSubsonicExtensions.view?{self.auth_query()}")
        self.assertEqual(response.status, 200)
        extensions = json.loads(response.read())["subsonic-response"]["openSubsonicExtensions"]["openSubsonicExtension"]
        self.assertIn({"name": "songLyrics", "versions": [1]}, extensions)

    def test_create_server_honors_explicit_ephemeral_port(self) -> None:
        self.config.server_port = 4533
        httpd = create_server(self.config, host="127.0.0.1", port=0)
        self.addCleanup(httpd.RequestHandlerClass.service.close)
        self.addCleanup(httpd.server_close)
        self.assertNotEqual(httpd.server_port, 4533)

    def test_album_list_uses_track_title_for_one_song_releases(self) -> None:
        response = self.request(f"/rest/getAlbumList2.view?{self.auth_query()}&size=20")
        self.assertEqual(response.status, 200)
        albums = json.loads(response.read())["subsonic-response"]["albumList2"]["album"]
        self.assertIn("A Test Song", {album["name"] for album in albums})
        self.assertNotIn("abc123", {album["name"] for album in albums})

    def test_recent_album_list_sorts_by_last_played_and_exposes_play_fields(self) -> None:
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        metadata["abc123"]["last_played_at"] = "2024-01-01T00:00:00+00:00"
        metadata["abc123"]["play_count"] = 1
        metadata["yt-oTqohAwUi8A"]["last_played_at"] = "2024-02-01T00:00:00+00:00"
        metadata["yt-oTqohAwUi8A"]["play_count"] = 2
        (self.config.metadata_path).write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getAlbumList2.view?{self.auth_query()}&size=20&type=recent")
        self.assertEqual(response.status, 200)
        albums = json.loads(response.read())["subsonic-response"]["albumList2"]["album"]
        self.assertEqual(albums[0]["name"], "YouTube Song")
        self.assertEqual(albums[0]["playCount"], 2)
        self.assertEqual(albums[0]["played"], "2024-02-01T00:00:00+00:00")

    def test_artist_payloads_include_cached_cover_art(self) -> None:
        artist_id = artist_id_for_name("Example Artist")
        image_path = self.artist_thumbnails / "example.jpg"
        image_path.write_bytes(b"artist")
        ArtistProfileStore(self.artist_profiles).upsert(ArtistProfile(id=artist_id, name="Example Artist", image_path=str(image_path)))

        response = self.request(f"/rest/getArtists.view?{self.auth_query()}")
        self.assertEqual(response.status, 200)
        indexes = json.loads(response.read())["subsonic-response"]["artists"]["index"]
        artists = indexes[0]["artist"]
        artist = next(item for item in artists if item["name"] == "Example Artist")
        self.assertEqual(artist["coverArt"], artist_id)

    def test_artist_profile_store_handles_concurrent_upserts(self) -> None:
        store = ArtistProfileStore(self.artist_profiles)
        errors: list[Exception] = []

        def write(index: int) -> None:
            try:
                store.upsert(ArtistProfile(id=f"artist:{index}", name=f"Artist {index}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        profiles = store.load()
        self.assertEqual(len(profiles), 20)

    def test_artist_profile_store_can_clear_cached_failures(self) -> None:
        store = ArtistProfileStore(self.artist_profiles)
        store.upsert(ArtistProfile(id="artist:failed", name="Failed", failed_at="2026-01-01T00:00:00+00:00", failure="nope"))
        self.assertEqual(store.clear_failures(), 1)
        profile = store.get("artist:failed")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.failed_at, "")
        self.assertEqual(profile.failure, "")

    def test_artist_art_reconstructs_source_url_for_legacy_youtube_entries(self) -> None:
        entry = VideoEntry(id="yt-B96Yp79E5co", title="Song", upload_date="", author="Artist")
        self.assertEqual(_entry_source_url(entry), "https://www.youtube.com/watch?v=B96Yp79E5co")

    def test_get_cover_art_serves_artist_profile_image(self) -> None:
        artist_id = artist_id_for_name("Example Artist")
        image_path = self.artist_thumbnails / "example.jpg"
        image_path.write_bytes(b"artist")
        ArtistProfileStore(self.artist_profiles).upsert(ArtistProfile(id=artist_id, name="Example Artist", image_path=str(image_path)))

        response = self.request(f"/rest/getCoverArt.view?{self.auth_query()}&id={artist_id}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"artist")

    def test_get_artist_info2_returns_artist_image_url_for_cached_art(self) -> None:
        artist_id = artist_id_for_name("Example Artist")
        image_path = self.artist_thumbnails / "example.jpg"
        image_path.write_bytes(b"artist")
        ArtistProfileStore(self.artist_profiles).upsert(ArtistProfile(id=artist_id, name="Example Artist", image_path=str(image_path)))

        response = self.request(f"/rest/getArtistInfo2.view?{self.auth_query()}&id={artist_id}")
        self.assertEqual(response.status, 200)
        info = json.loads(response.read())["subsonic-response"]["artistInfo2"]
        self.assertIn(f"/artistArt/{artist_id.replace(':', '_', 1)}", info["artistImageUrl"])

        art_path = info["artistImageUrl"].split(f"127.0.0.1:{self.port}", 1)[1]
        response = self.request(art_path)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"artist")

    def test_cover_art_resolves_youtube_prefixed_ids_to_raw_thumbnail_file(self) -> None:
        response = self.request(f"/rest/getCoverArt.view?{self.auth_query()}&id=yt-oTqohAwUi8A")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"jpeg")

    def test_cover_art_extracts_embedded_attached_picture_fallback(self) -> None:
        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"embedded-cover")
            return mock.Mock(returncode=0)

        with (
            mock.patch("ytarchive.server.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch(
                "ytarchive.server.ffprobe_json",
                return_value={"streams": [{"index": 1, "codec_type": "video", "disposition": {"attached_pic": 1}}]},
            ),
            mock.patch("ytarchive.server.subprocess.run", side_effect=fake_run),
        ):
            response = self.request(f"/rest/getCoverArt.view?{self.auth_query()}&id=embedded")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"embedded-cover")
        self.assertTrue((self.thumbnails / "embedded.jpg").exists())

    def test_get_similar_songs_prefers_same_artist_and_excludes_seed(self) -> None:
        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=abc123&count=1")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual([song["id"] for song in songs], ["sameartist"])

    def test_get_similar_songs_does_not_pad_with_unrelated_tracks(self) -> None:
        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=abc123&count=50")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual([song["id"] for song in songs], ["sameartist"])

    def test_get_similar_songs_excludes_hidden_tracks(self) -> None:
        metadata = json.loads(self.config.metadata_path.read_text(encoding="utf-8"))
        metadata["sameartist"]["hidden_from_subsonic"] = True
        self.config.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=abc123&count=50")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual(songs, [])

    def test_get_similar_songs_prefers_shared_tags_over_artist(self) -> None:
        metadata = json.loads(self.config.metadata_path.read_text(encoding="utf-8"))
        metadata["abc123"]["tags"] = ["Chill", "Late Night"]
        metadata["yt-oTqohAwUi8A"]["tags"] = ["chill", "Late Night"]
        self.config.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=abc123&count=1")

        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual([song["id"] for song in songs], ["yt-oTqohAwUi8A"])

    def test_get_similar_songs_uses_song_metadata_for_album_id_seed(self) -> None:
        metadata = json.loads(self.config.metadata_path.read_text(encoding="utf-8"))
        metadata["abc123"]["tags"] = ["Chill", "Late Night"]
        metadata["yt-oTqohAwUi8A"]["tags"] = ["chill", "Late Night"]
        self.config.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=album:abc123&count=1")

        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual([song["id"] for song in songs], ["yt-oTqohAwUi8A"])

    def test_get_similar_songs_uses_nearby_and_double_time_bpm(self) -> None:
        metadata = json.loads(self.config.metadata_path.read_text(encoding="utf-8"))
        metadata["abc123"]["bpm"] = 120
        # Keep this test focused on BPM matching; artist matches are scored
        # separately and intentionally outrank BPM-only matches.
        metadata["sameartist"]["author"] = "Other Artist"
        metadata["sameartist"]["bpm"] = 150
        metadata["yt-oTqohAwUi8A"]["bpm"] = 60
        self.config.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=abc123&count=1")

        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs"]["song"]
        self.assertEqual([song["id"] for song in songs], ["yt-oTqohAwUi8A"])

    def test_get_similar_songs2_accepts_artist_id_and_respects_count(self) -> None:
        artist_id = "artist:" + hashlib.sha1("example artist".encode("utf-8")).hexdigest()[:16]
        response = self.request(f"/rest/getSimilarSongs2.view?{self.auth_query()}&id={artist_id}&count=1")
        self.assertEqual(response.status, 200)
        songs = json.loads(response.read())["subsonic-response"]["similarSongs2"]["song"]
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["artist"], "Example Artist")

    def test_get_similar_songs_reports_missing_seed(self) -> None:
        response = self.request(f"/rest/getSimilarSongs.view?{self.auth_query()}&id=does-not-exist")
        self.assertEqual(response.status, 404)
        payload = json.loads(response.read())["subsonic-response"]
        self.assertEqual(payload["error"]["code"], 70)

    def test_stream_supports_byte_ranges(self) -> None:
        response = self.request(f"/rest/stream.view?{self.auth_query()}&id=abc123", {"Range": "bytes=2-5"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("Content-Range"), "bytes 2-5/10")
        self.assertEqual(response.read(), b"2345")

    def test_scrobble_updates_last_played_and_play_count_for_submitted_play(self) -> None:
        response = self.request(f"/rest/scrobble.view?{self.auth_query()}&id=abc123&submission=true&time=1700000000000")
        self.assertEqual(response.status, 200)
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["abc123"]["play_count"], 1)
        self.assertEqual(metadata["abc123"]["last_played_at"], "2023-11-14T22:13:20+00:00")

    def test_now_playing_scrobble_does_not_increment_play_count(self) -> None:
        response = self.request(f"/rest/scrobble.view?{self.auth_query()}&id=abc123&submission=false&time=1700000000")
        self.assertEqual(response.status, 200)
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        self.assertNotIn("play_count", metadata["abc123"])
        self.assertEqual(metadata["abc123"]["last_played_at"], "2023-11-14T22:13:20+00:00")

    def test_migrate_play_counts_uses_existing_playback_seconds(self) -> None:
        migrated, _skipped = migrate_play_counts(self.config)
        self.assertGreaterEqual(migrated, 1)
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["abc123"]["play_count"], 1)
        self.assertEqual(metadata["abc123"]["play_count_estimated_from_seconds"], 42.0)
        self.assertEqual(metadata["abc123"]["play_count_estimation_version"], 1)

    def test_migrate_play_counts_does_not_reapply_completed_estimate(self) -> None:
        migrated, _skipped = migrate_play_counts(self.config)
        self.assertGreaterEqual(migrated, 1)
        migrated, _skipped = migrate_play_counts(self.config)
        self.assertEqual(migrated, 0)

    def test_migrate_play_counts_force_overwrites_existing_counts(self) -> None:
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        metadata["abc123"]["play_count"] = 99
        (self.config.metadata_path).write_text(json.dumps(metadata), encoding="utf-8")
        migrated, _skipped = migrate_play_counts(self.config, force=True)
        self.assertGreaterEqual(migrated, 1)
        metadata = json.loads((self.config.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["abc123"]["play_count"], 1)

    def test_range_bounds_accepts_suffix_ranges(self) -> None:
        self.assertEqual(_range_bounds("bytes=-4", 10), (6, 9))


if __name__ == "__main__":
    unittest.main()
