from __future__ import annotations

import argparse
import datetime as dt
from concurrent.futures import Future, ThreadPoolExecutor
import heapq
import hashlib
import json
import mimetypes
import socket
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import log
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, APP_SLUG
from .artist_art import ArtistProfileStore, artist_id_for_name, artist_image_path, ensure_artist_art
from .config import AppConfig, load_config
from .ids import thumbnail_video_id
from .library import LibraryIndex, SearchIndex, format_date
from .media import ensure_thumbnail, ffprobe_json, thumbnail_path
from .metadata import MetadataStore, VideoEntry
from .runtime import windows_no_console_kwargs


SUBSONIC_NS = "http://subsonic.org/restapi"
SERVER_VERSION = "1.16.1"
APP_VERSION = APP_SLUG
PLAY_COUNT_ESTIMATION_VERSION = 1


@dataclass
class ServerCredentials:
    username: str
    password: str = ""
    password_hash: str = ""

    def configured(self) -> bool:
        return bool(self.password or self.password_hash)


@dataclass
class LibrarySnapshot:
    entries: list[VideoEntry]
    by_id: dict[str, VideoEntry]
    artists: dict[str, list[VideoEntry]]
    albums: dict[str, list[VideoEntry]]
    similarity: "SimilarityCatalog"
    search_index: Optional[SearchIndex] = None


@dataclass
class SimilarityCatalog:
    """Precomputed values shared by similarity requests for one library view."""

    entries: list[VideoEntry]
    disabled_tags: frozenset[str]
    tag_frequencies: dict[str, int]
    tags_by_id: dict[str, frozenset[str]]
    artist_by_id: dict[str, str]


class SimilarityCatalogCache:
    """Keep one desktop similarity catalog per library/config generation."""

    def __init__(self):
        self._key: Optional[tuple[int, frozenset[str]]] = None
        self._catalog: Optional[SimilarityCatalog] = None

    def get(
        self,
        entries: list[VideoEntry],
        generation: int,
        disabled_tags: frozenset[str],
    ) -> SimilarityCatalog:
        normalized_tags = frozenset(disabled_tags)
        key = generation, normalized_tags
        if self._catalog is None or self._key != key:
            self._catalog = _similarity_catalog(entries, normalized_tags)
            self._key = key
        return self._catalog


class SubsonicService:
    def __init__(self, config: AppConfig, credentials: ServerCredentials, timing: bool = False):
        self.config = config
        self.credentials = credentials
        self.timing = timing
        self.metadata = MetadataStore(config.metadata_path)
        self._snapshot_cache: Optional[LibrarySnapshot] = None
        self._snapshot_revision: Optional[tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]] = None
        self._snapshot_condition = threading.Condition()
        self._snapshot_rebuilding = False
        self._media_probe_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="subsonic-media-probe",
        )
        self._media_probe_condition = threading.Condition()
        self._media_probe_ids: set[str] = set()
        self._media_probe_futures: set[Future] = set()
        self._media_probe_ids_by_future: dict[Future, str] = {}
        self._pending_media_fields: dict[str, tuple[Optional[float], Optional[int], Optional[int]]] = {}
        self._media_flush_timer: Optional[threading.Timer] = None
        self._closed = False

    def song_payload(self, entry: VideoEntry) -> dict[str, Any]:
        """Return a song response without doing process-blocking media I/O."""
        self.schedule_media_probe(entry)
        return _song_payload(entry, probe_media=False)

    def schedule_media_probe(self, entry: VideoEntry) -> None:
        path = entry.path
        # Snapshot construction already filters missing paths. Avoid another
        # filesystem probe while handling each request; a worker can safely
        # discard a path that disappears before ffprobe runs.
        if not path:
            return
        if (
            entry.duration_seconds > 0
            and entry.bitrate_kbps > 0
            and entry.audio_bitrate_kbps > 0
        ):
            return
        with self._media_probe_condition:
            if self._closed or entry.id in self._media_probe_ids:
                return
            self._media_probe_ids.add(entry.id)
            future = self._media_probe_executor.submit(self._probe_media_fields, entry.id, path)
            self._media_probe_futures.add(future)
            self._media_probe_ids_by_future[future] = entry.id
            future.add_done_callback(self._media_probe_finished)

    @staticmethod
    def _probe_media_fields(
        video_id: str,
        path: Path,
    ) -> tuple[str, Optional[float], Optional[int], Optional[int]]:
        info = ffprobe_json(path) or {}
        duration = _duration_from_info(info)
        bitrate = _bitrate_kbps(info, path, duration)
        audio_bitrate = _audio_bitrate_kbps(info)
        return video_id, duration, bitrate, audio_bitrate

    def _media_probe_finished(self, future: Future) -> None:
        try:
            result = future.result()
        except Exception:
            result = None
        with self._media_probe_condition:
            self._media_probe_futures.discard(future)
            video_id = self._media_probe_ids_by_future.pop(future, None)
            if result and video_id:
                video_id, duration, bitrate, audio_bitrate = result
                self._media_probe_ids.discard(video_id)
                if any(value and value > 0 for value in (duration, bitrate, audio_bitrate)):
                    self._pending_media_fields[video_id] = self._merge_media_fields(
                        self._pending_media_fields.get(video_id),
                        (duration, bitrate, audio_bitrate),
                    )
                    self._update_cached_media_fields(video_id, duration, bitrate, audio_bitrate)
                    if not self._closed:
                        self._schedule_media_flush_locked()
            else:
                # A failed probe should be retryable on a later request.
                if video_id:
                    self._media_probe_ids.discard(video_id)
            self._media_probe_condition.notify_all()

    @staticmethod
    def _merge_media_fields(
        previous: Optional[tuple[Optional[float], Optional[int], Optional[int]]],
        current: tuple[Optional[float], Optional[int], Optional[int]],
    ) -> tuple[Optional[float], Optional[int], Optional[int]]:
        if previous is None:
            return current
        return tuple(new or old for old, new in zip(previous, current))  # type: ignore[return-value]

    def _schedule_media_flush_locked(self) -> None:
        if self._media_flush_timer is not None:
            return
        timer = threading.Timer(0.25, self._flush_media_fields)
        timer.daemon = True
        self._media_flush_timer = timer
        timer.start()

    def _update_cached_media_fields(
        self,
        video_id: str,
        duration: Optional[float],
        bitrate: Optional[int],
        audio_bitrate: Optional[int],
    ) -> None:
        with self._snapshot_condition:
            if not self._snapshot_cache:
                return
            entry = self._snapshot_cache.by_id.get(video_id)
            if entry is None:
                return
            _cache_song_media_fields(entry, None, duration, bitrate, audio_bitrate)

    def _flush_media_fields(self) -> None:
        with self._media_probe_condition:
            updates = self._pending_media_fields
            self._pending_media_fields = {}
            self._media_flush_timer = None
        if not updates:
            return
        try:
            self.metadata.update_media_fields_bulk(updates)
        except Exception:
            with self._media_probe_condition:
                for video_id, fields in updates.items():
                    self._pending_media_fields[video_id] = self._merge_media_fields(
                        self._pending_media_fields.get(video_id), fields
                    )
                if not self._closed:
                    self._schedule_media_flush_locked()

    def wait_for_media_probes(self, timeout: float = 5.0) -> None:
        """Drain media probes; primarily useful to deterministic callers/tests."""
        deadline = time.monotonic() + timeout
        while True:
            with self._media_probe_condition:
                futures = list(self._media_probe_futures)
            if not futures:
                break
            remaining = max(0.0, deadline - time.monotonic())
            if not remaining:
                break
            for future in futures:
                future.result(timeout=remaining)
        self._flush_media_fields()

    def close(self) -> None:
        with self._media_probe_condition:
            if self._closed:
                return
            self._closed = True
            if self._media_flush_timer is not None:
                self._media_flush_timer.cancel()
                self._media_flush_timer = None
        self._media_probe_executor.shutdown(wait=True, cancel_futures=True)
        self._flush_media_fields()

    def snapshot(self) -> LibrarySnapshot:
        started = time.perf_counter()
        while True:
            revision = self._library_revision()
            with self._snapshot_condition:
                if self._snapshot_cache is not None and self._snapshot_revision == revision:
                    if self.timing:
                        self._timing(f"snapshot cache=hit total={_elapsed_ms(started, time.perf_counter()):.1f}ms entries={len(self._snapshot_cache.entries)}")
                    return self._snapshot_cache
                if self._snapshot_rebuilding:
                    self._snapshot_condition.wait()
                    continue
                self._snapshot_rebuilding = True
                break

        try:
            while True:
                snapshot = self._build_snapshot(started)
                # Do not cache a snapshot built while its inputs were changing.
                current_revision = self._library_revision()
                if current_revision == revision:
                    break
                revision = current_revision
            with self._snapshot_condition:
                self._snapshot_cache = snapshot
                self._snapshot_revision = revision
                return snapshot
        finally:
            with self._snapshot_condition:
                self._snapshot_rebuilding = False
                self._snapshot_condition.notify_all()

    def _build_snapshot(self, started: float) -> LibrarySnapshot:
        self.metadata.load_if_changed()
        metadata_loaded = time.perf_counter()
        entries = self.metadata.entries()
        entries_copied = time.perf_counter()
        library = LibraryIndex(self.config.download_dir)
        library.rebuild(entries)
        library_rebuilt = time.perf_counter()
        entries = [entry for entry in library.entries if entry.path and entry.path.exists() and not entry.hidden_from_subsonic]
        by_id = {entry.id: entry for entry in entries}
        artists: dict[str, list[VideoEntry]] = {}
        albums: dict[str, list[VideoEntry]] = {}
        for entry in entries:
            artists.setdefault(_artist_name(entry), []).append(entry)
            albums.setdefault(_album_id(entry), []).append(entry)
        if self.timing:
            self._timing(
                "snapshot"
                f" metadata.load={_elapsed_ms(started, metadata_loaded):.1f}ms"
                f" metadata.entries={_elapsed_ms(metadata_loaded, entries_copied):.1f}ms"
                f" library.rebuild={_elapsed_ms(entries_copied, library_rebuilt):.1f}ms"
                f" catalog={_elapsed_ms(library_rebuilt, time.perf_counter()):.1f}ms"
                f" total={_elapsed_ms(started, time.perf_counter()):.1f}ms"
                f" entries={len(entries)}"
            )
        return LibrarySnapshot(
            entries=entries,
            by_id=by_id,
            artists=artists,
            albums=albums,
            similarity=_similarity_catalog(entries, self.config.disabled_similarity_tags),
            search_index=SearchIndex(entries),
        )

    def _library_revision(self) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]:
        return _path_revision(self.config.metadata_path), _path_revision(self.config.download_dir)

    def _timing(self, message: str) -> None:
        timestamp = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
        thread_name = threading.current_thread().name
        print(f"[subsonic timing] {timestamp} thread={thread_name} {message}", file=sys.stderr, flush=True)

    def authenticate(self, params: dict[str, str]) -> tuple[bool, str]:
        if not self.credentials.configured():
            return False, "Server password is not configured"
        username = params.get("u", "")
        if not secrets.compare_digest(username, self.credentials.username):
            return False, "Wrong username or password"

        password_param = params.get("p", "")
        if password_param:
            password = _decode_password_param(password_param)
            if self.credentials.password and secrets.compare_digest(password, self.credentials.password):
                return True, ""
            if self.credentials.password_hash and secrets.compare_digest(_sha256(password), self.credentials.password_hash.lower()):
                return True, ""

        token = params.get("t", "")
        salt = params.get("s", "")
        if token and salt and self.credentials.password:
            expected = hashlib.md5((self.credentials.password + salt).encode("utf-8")).hexdigest()
            if secrets.compare_digest(token.lower(), expected):
                return True, ""

        return False, "Wrong username or password"


class SubsonicHandler(BaseHTTPRequestHandler):
    service: SubsonicService

    def do_GET(self) -> None:
        started = time.perf_counter()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/artistArt/"):
            try:
                self._public_artist_art(Path(parsed.path).name)
            finally:
                self._request_timing("artistArt", started)
            return
        method = Path(parsed.path).name
        if method.endswith(".view"):
            method = method[:-5]
        elif method.endswith(".json"):
            method = method[:-5]
        params = _single_params(parse_qs(parsed.query, keep_blank_values=True))

        try:
            ok, message = self.service.authenticate(params)
            if not ok:
                self._send_subsonic_error(params, 40, message, HTTPStatus.UNAUTHORIZED)
                return
            if method in {"ping", "getLicense"}:
                self._send_subsonic(params, {})
            elif method == "getOpenSubsonicExtensions":
                self._send_subsonic(params, {"openSubsonicExtensions": {"openSubsonicExtension": [{"name": "songLyrics", "versions": [1]}]}})
            elif method == "search3":
                self._send_subsonic(params, {"searchResult3": self._search3(params)})
            elif method == "getRandomSongs":
                self._send_subsonic(params, {"randomSongs": {"song": self._random_songs(params)}})
            elif method == "getSimilarSongs":
                self._send_subsonic(params, {"similarSongs": {"song": self._similar_songs(params)}})
            elif method == "getSimilarSongs2":
                self._send_subsonic(params, {"similarSongs2": {"song": self._similar_songs(params)}})
            elif method == "getSong":
                self._send_subsonic(params, {"song": self._song(params)})
            elif method == "getLyrics":
                self._send_subsonic(params, {"lyrics": self._lyrics(params)})
            elif method == "getLyricsBySongId":
                self._send_subsonic(params, {"lyricsList": self._lyrics_by_song_id(params)})
            elif method == "getArtists":
                self._send_subsonic(params, {"artists": self._artists()})
            elif method == "getMusicFolders":
                self._send_subsonic(params, {"musicFolders": {"musicFolder": [{"id": "1", "name": APP_NAME}]}})
            elif method == "getIndexes":
                self._send_subsonic(params, {"indexes": self._indexes()})
            elif method == "getMusicDirectory":
                self._send_subsonic(params, {"directory": self._music_directory(params)})
            elif method == "getGenres":
                self._send_subsonic(params, {"genres": {"genre": []}})
            elif method == "getPlaylists":
                self._send_subsonic(params, {"playlists": {"playlist": []}})
            elif method == "getStarred2":
                self._send_subsonic(params, {"starred2": {"artist": [], "album": [], "song": []}})
            elif method == "getArtist":
                self._send_subsonic(params, {"artist": self._artist(params)})
            elif method == "getArtistInfo":
                self._send_subsonic(params, {"artistInfo": self._artist_info(params)})
            elif method == "getArtistInfo2":
                self._send_subsonic(params, {"artistInfo2": self._artist_info(params)})
            elif method == "getAlbum":
                self._send_subsonic(params, {"album": self._album(params)})
            elif method == "getAlbumList2":
                self._send_subsonic(params, {"albumList2": {"album": self._album_list(params)}})
            elif method == "scrobble":
                self._scrobble(params)
                self._send_subsonic(params, {})
            elif method in {"star", "unstar"}:
                self._send_subsonic(params, {})
            elif method in {"stream", "download"}:
                self._stream(params)
            elif method == "getCoverArt":
                self._cover_art(params)
            else:
                self._send_subsonic_error(params, 0, f"Unsupported endpoint: {method}", HTTPStatus.NOT_FOUND)
        except LookupError as exc:
            self._send_subsonic_error(params, 70, str(exc), HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_subsonic_error(params, 10, str(exc), HTTPStatus.BAD_REQUEST)
        finally:
            self._request_timing(method, started)

    def _request_timing(self, method: str, started: float) -> None:
        if self.service.timing:
            self.service._timing(f"request endpoint={method} total={_elapsed_ms(started, time.perf_counter()):.1f}ms")

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _search3(self, params: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        query = params.get("query", "")
        count = _param_int(params, "songCount", 20)
        snapshot = self.service.snapshot()
        if query.strip():
            search_index = snapshot.search_index or SearchIndex(snapshot.entries)
            songs = [entry for entry in search_index.search(query, count) if entry.id in snapshot.by_id]
        else:
            songs = snapshot.entries[:count]
        artist_matches = [
            _artist_payload(self.service.config, name, entries, fetch=False)
            for name, entries in sorted(snapshot.artists.items())
            if not query or query.casefold() in name.casefold()
        ][:_param_int(params, "artistCount", 20)]
        album_matches = [
            _album_payload(album_id, entries)
            for album_id, entries in sorted(snapshot.albums.items(), key=lambda item: _album_name(item[1][0]).casefold())
            if not query or query.casefold() in _album_name(entries[0]).casefold()
        ][:_param_int(params, "albumCount", 20)]
        return {"artist": artist_matches, "album": album_matches, "song": [self.service.song_payload(entry) for entry in songs]}

    def _random_songs(self, params: dict[str, str]) -> list[dict[str, Any]]:
        count = _param_int(params, "size", 20)
        entries = self.service.snapshot().entries
        sample = random.sample(entries, min(count, len(entries))) if entries else []
        return [self.service.song_payload(entry) for entry in sample]

    def _similar_songs(self, params: dict[str, str]) -> list[dict[str, Any]]:
        count = min(_param_int(params, "count", 50), self.service.config.similarity_max_results)
        snapshot = self.service.snapshot()
        seed_artist, exclude_id, seed = _similar_seed(snapshot, params.get("id", ""))
        results = similar_song_entries(
            snapshot.entries,
            seed,
            seed_artist,
            exclude_id,
            self.service.config,
            count,
            catalog=snapshot.similarity,
        )
        return [self.service.song_payload(entry) for entry in results]

    def _song(self, params: dict[str, str]) -> dict[str, Any]:
        entry = _entry_by_id(self.service.snapshot(), params.get("id", ""))
        return self.service.song_payload(entry)

    def _lyrics(self, params: dict[str, str]) -> dict[str, Any]:
        snapshot = self.service.snapshot()
        entry = None
        song_id = params.get("id", "")
        if song_id:
            entry = snapshot.by_id.get(song_id)
        if not entry:
            title = params.get("title", "").casefold()
            artist = params.get("artist", "").casefold()
            for candidate in snapshot.entries:
                if title and (candidate.title or "").casefold() != title:
                    continue
                if artist and (candidate.author or "").casefold() != artist:
                    continue
                entry = candidate
                break
        return {
            "artist": entry.author if entry else params.get("artist", ""),
            "title": entry.title if entry else params.get("title", ""),
            "value": entry.lyrics if entry else "",
        }

    def _lyrics_by_song_id(self, params: dict[str, str]) -> dict[str, Any]:
        entry = self.service.snapshot().by_id.get(params.get("id", ""))
        if not entry or not entry.lyrics:
            return {"structuredLyrics": []}
        lines = _structured_lrc_lines(entry.lyrics)
        return {
            "structuredLyrics": [
                {
                    "displayArtist": entry.author,
                    "displayTitle": entry.title,
                    "lang": "und",
                    "synced": bool(lines and any("start" in line for line in lines)),
                    "line": lines,
                }
            ]
        }

    def _artists(self) -> dict[str, Any]:
        artists = [
            _artist_payload(self.service.config, name, entries, fetch=False)
            for name, entries in sorted(self.service.snapshot().artists.items())
        ]
        return {"ignoredArticles": "", "index": [{"name": "#", "artist": artists}]}

    def _indexes(self) -> dict[str, Any]:
        return {"ignoredArticles": "", "index": self._artists()["index"]}

    def _music_directory(self, params: dict[str, str]) -> dict[str, Any]:
        directory_id = params.get("id", "1")
        snapshot = self.service.snapshot()
        if directory_id == "1":
            children = [
                {**_artist_payload(self.service.config, name, entries, fetch=False), "title": name, "isDir": True}
                for name, entries in sorted(snapshot.artists.items())
            ]
            return {"id": "1", "name": APP_NAME, "child": children}
        for name, entries in snapshot.artists.items():
            if _artist_id(name) == directory_id:
                albums = {}
                for entry in entries:
                    albums.setdefault(_album_id(entry), []).append(entry)
                return {
                    "id": directory_id,
                    "name": name,
                    "child": [_directory_album_payload(album_id, songs) for album_id, songs in sorted(albums.items())],
                }
        entries = snapshot.albums.get(directory_id)
        if entries:
            return {"id": directory_id, "name": _album_name(entries[0]), "child": [self.service.song_payload(entry) for entry in entries]}
        raise LookupError("Directory not found")

    def _artist(self, params: dict[str, str]) -> dict[str, Any]:
        artist_id = params.get("id", "")
        snapshot = self.service.snapshot()
        for name, entries in snapshot.artists.items():
            if _artist_id(name) == artist_id:
                albums = {}
                for entry in entries:
                    albums.setdefault(_album_id(entry), []).append(entry)
                return {**_artist_payload(self.service.config, name, entries, fetch=True), "album": [_album_payload(album_id, songs) for album_id, songs in albums.items()]}
        raise LookupError("Artist not found")

    def _artist_info(self, params: dict[str, str]) -> dict[str, Any]:
        artist_id = params.get("id", "")
        snapshot = self.service.snapshot()
        for name, entries in snapshot.artists.items():
            if _artist_id(name) == artist_id:
                ensure_artist_art(self.service.config, name, entries)
                image_url = _artist_image_url(self, artist_id)
                return {"biography": "", "musicBrainzId": "", "lastFmUrl": "", **({"artistImageUrl": image_url} if image_url else {})}
        raise LookupError("Artist not found")

    def _album(self, params: dict[str, str]) -> dict[str, Any]:
        album_id = params.get("id", "")
        entries = self.service.snapshot().albums.get(album_id)
        if not entries:
            raise LookupError("Album not found")
        payload = _album_payload(album_id, entries)
        payload["song"] = [self.service.song_payload(entry) for entry in entries]
        return payload

    def _album_list(self, params: dict[str, str]) -> list[dict[str, Any]]:
        size = _param_int(params, "size", 20)
        offset = _param_int(params, "offset", 0)
        albums = _sorted_albums(self.service.snapshot().albums, params.get("type", "alphabeticalByName"))
        return [_album_payload(album_id, entries) for album_id, entries in albums[offset : offset + size]]

    def _scrobble(self, params: dict[str, str]) -> None:
        entry_id = params.get("id", "")
        if not entry_id:
            raise ValueError("Missing song id")
        snapshot = self.service.snapshot()
        _entry_by_id(snapshot, entry_id)
        self.service.metadata.load()
        entry = self.service.metadata.get(entry_id)
        if not entry:
            raise LookupError("Song not found")

        played_at = _scrobble_time(params.get("time", ""))
        entry.last_played_at = played_at
        if _is_submitted_scrobble(params):
            entry.play_count += 1
        self.service.metadata.upsert(entry)

    def _stream(self, params: dict[str, str]) -> None:
        entry = _entry_by_id(self.service.snapshot(), params.get("id", ""))
        if not entry.path:
            raise LookupError("Song file not found")
        _send_file(self, entry.path, download=False)

    def _cover_art(self, params: dict[str, str]) -> None:
        item_id = params.get("id", "")
        if item_id.startswith("artist:"):
            snapshot = self.service.snapshot()
            for name, entries in snapshot.artists.items():
                if _artist_id(name) == item_id:
                    path = artist_image_path(self.service.config, item_id) or ensure_artist_art(self.service.config, name, entries)
                    if path and path.exists():
                        _send_file(self, path, download=False)
                        return
                    raise LookupError("Artist art not found")
            raise LookupError("Artist not found")
        entry = self.service.snapshot().by_id.get(item_id)
        if not entry and item_id.startswith("album:"):
            entries = self.service.snapshot().albums.get(item_id)
            entry = entries[0] if entries else None
        if not entry:
            raise LookupError("Cover art not found")
        path = _cover_art_path(self.service.config, entry)
        if not path.exists():
            raise LookupError("Cover art not found")
        _send_file(self, path, download=False)

    def _public_artist_art(self, raw_artist_id: str) -> None:
        artist_id = raw_artist_id.replace("_", ":", 1) if raw_artist_id.startswith("artist_") else raw_artist_id
        path = artist_image_path(self.service.config, artist_id)
        if not path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        _send_file(self, path, download=False)

    def _send_subsonic(self, params: dict[str, str], payload: dict[str, Any]) -> None:
        body, content_type = _format_subsonic(params, {"status": "ok", "version": SERVER_VERSION, "type": APP_VERSION, **payload})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_subsonic_error(self, params: dict[str, str], code: int, message: str, status: HTTPStatus) -> None:
        body, content_type = _format_subsonic(
            params,
            {"status": "failed", "version": SERVER_VERSION, "type": APP_VERSION, "error": {"code": code, "message": message}},
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    config: AppConfig,
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: str = "",
    timing: Optional[bool] = None,
) -> ThreadingHTTPServer:
    credentials = ServerCredentials(
        username=config.server_username,
        password=password or config.server_password,
        password_hash=config.server_password_hash.lower(),
    )
    service = SubsonicService(config, credentials, timing=config.server_timing if timing is None else timing)
    handler = type(
        "ConfiguredSubsonicHandler",
        (SubsonicHandler,),
        {"service": service},
    )
    address = (
        config.server_host if host is None else host,
        config.server_port if port is None else port,
    )
    try:
        return ThreadingHTTPServer(address, handler)
    except Exception:
        service.close()
        raise


def run_server(
    config: AppConfig,
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: str = "",
    timing: Optional[bool] = None,
) -> None:
    credentials = ServerCredentials(
        username=config.server_username,
        password=password or config.server_password,
        password_hash=config.server_password_hash.lower(),
    )
    httpd = create_server(config, host=host, port=port, password=password, timing=timing)
    address = httpd.server_address
    print(f"Serving {APP_NAME} Subsonic API on http://{address[0]}:{address[1]}/rest")
    if not credentials.configured():
        print("No server password is configured; every request will be rejected.", file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        httpd.RequestHandlerClass.service.close()
        httpd.server_close()


def migrate_play_counts(config: AppConfig, *, force: bool = False) -> tuple[int, int]:
    metadata = MetadataStore(config.metadata_path)
    metadata.load()
    library = LibraryIndex(config.download_dir)
    library.rebuild(metadata.entries())
    migrated = 0
    skipped = 0
    for entry in library.entries:
        if entry.playback_seconds <= 0:
            skipped += 1
            continue
        if (
            not force
            and entry.play_count_estimation_version >= PLAY_COUNT_ESTIMATION_VERSION
            and entry.play_count_estimated_from_seconds == entry.playback_seconds
        ):
            skipped += 1
            continue
        if entry.play_count > 0 and not force:
            skipped += 1
            continue
        entry.play_count = _estimated_play_count(entry)
        entry.play_count_estimated_from_seconds = entry.playback_seconds
        entry.play_count_estimation_version = PLAY_COUNT_ESTIMATION_VERSION
        metadata.upsert(entry, save=False)
        migrated += 1
    if migrated:
        metadata.save()
    return migrated, skipped


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"Serve {APP_NAME} over a read-only Subsonic-compatible LAN API.")
    parser.add_argument("--host", default=None, help="Bind address. Use 0.0.0.0 for home LAN access.")
    parser.add_argument("--port", type=int, default=None, help="Bind port.")
    parser.add_argument("--password", default="", help="Server password for this run. Enables token auth without storing the password.")
    parser.add_argument("--timing", action="store_true", help="Log per-request and library-snapshot timing to stderr.")
    parser.add_argument("--root", type=Path, default=None, help="Project/config root directory.")
    parser.add_argument("--migrate-play-counts", action="store_true", help="Estimate play_count values from existing playback_seconds and exit.")
    parser.add_argument("--force", action="store_true", help="With --migrate-play-counts, overwrite existing play_count values.")
    parser.add_argument("--clear-artist-art-failures", action="store_true", help="Clear cached artist-art lookup failures and exit.")
    args = parser.parse_args(argv)
    config = load_config(args.root)
    if args.clear_artist_art_failures:
        cleared = ArtistProfileStore(config.artist_profiles_path).clear_failures()
        print(f"Cleared {cleared} artist-art failure records.")
        return 0
    if args.migrate_play_counts:
        migrated, skipped = migrate_play_counts(config, force=args.force)
        print(f"Migrated play_count for {migrated} entries; skipped {skipped}.")
        return 0
    run_server(config, host=args.host, port=args.port, password=args.password, timing=True if args.timing else None)
    return 0


def _single_params(params: dict[str, list[str]]) -> dict[str, str]:
    return {key: values[-1] if values else "" for key, values in params.items()}


def _elapsed_ms(started: float, ended: float) -> float:
    return (ended - started) * 1000


def _path_revision(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _decode_password_param(value: str) -> str:
    if value.startswith("enc:"):
        try:
            return bytes.fromhex(value[4:]).decode("utf-8")
        except ValueError:
            return ""
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _param_int(params: dict[str, str], name: str, default: int) -> int:
    try:
        return max(0, int(params.get(name, default)))
    except ValueError:
        return default


def _artist_name(entry: VideoEntry) -> str:
    return entry.author.strip() or "Unknown Artist"


def _artist_id(name: str) -> str:
    return artist_id_for_name(name)


def _artist_payload(config: AppConfig, name: str, entries: list[VideoEntry], *, fetch: bool) -> dict[str, Any]:
    artist_id = _artist_id(name)
    path = ensure_artist_art(config, name, entries) if fetch else artist_image_path(config, artist_id)
    payload: dict[str, Any] = {
        "id": artist_id,
        "name": name,
        "albumCount": len({_album_id(entry) for entry in entries}),
    }
    if path:
        payload["coverArt"] = artist_id
    return payload


def _artist_image_url(handler: BaseHTTPRequestHandler, artist_id: str) -> str:
    path = artist_image_path(handler.service.config, artist_id) if isinstance(handler, SubsonicHandler) else None
    if not path:
        return ""
    host = handler.headers.get("Host") or f"{handler.server.server_address[0]}:{handler.server.server_address[1]}"
    scheme = "http"
    public_id = artist_id.replace(":", "_", 1)
    return f"{scheme}://{host}/artistArt/{public_id}"


def _album_name(entry: VideoEntry) -> str:
    return entry.title or entry.id


def _album_id(entry: VideoEntry) -> str:
    return "album:" + entry.id


def _entry_by_id(snapshot: LibrarySnapshot, entry_id: str) -> VideoEntry:
    entry = snapshot.by_id.get(entry_id)
    if not entry:
        raise LookupError("Song not found")
    return entry


def _similar_seed(snapshot: LibrarySnapshot, item_id: str) -> tuple[str, str, Optional[VideoEntry]]:
    if not item_id:
        raise ValueError("Missing id")
    entry = snapshot.by_id.get(item_id)
    if entry:
        return _artist_name(entry), entry.id, entry
    entries = snapshot.albums.get(item_id)
    if entries:
        # Subsonic clients commonly request similar songs with a track's
        # parent album ID.  Our albums may contain a single local track, but
        # either way its metadata is the best available seed for tag and BPM
        # matching.  Without it this silently degrades to artist-only results.
        seed = entries[0]
        return _artist_name(seed), seed.id, seed
    for name in snapshot.artists:
        if _artist_id(name) == item_id:
            return name, "", None
    raise LookupError("Similar-song seed not found")


def _similarity_score(
    seed: Optional[VideoEntry],
    seed_artist: str,
    candidate: VideoEntry,
    tag_frequencies: dict[str, int],
    library_size: int,
    config: Optional[AppConfig] = None,
) -> int:
    """Score a candidate using the metadata available to OpenSubsonic.

    Shared tags and artists can be weighted by their rarity in the library.
    BPM is a secondary signal and treats common half/double-time estimates as
    equivalent.
    """
    score = 0
    use_artist = config is None or config.similarity_use_artist
    use_tags = config is None or config.similarity_use_tags
    use_rarity = config is None or config.similarity_use_rarity
    use_bpm = config is None or config.similarity_use_bpm
    disabled_tags = config.disabled_similarity_tags if config else frozenset()
    if use_artist and _is_known_artist(seed_artist) and _artist_name(candidate).casefold() == seed_artist.casefold():
        # Treat the artist as another shared tag, so it receives the same
        # inverse-frequency weighting as user-applied tags.
        score += _similarity_feature_weight(
            _artist_feature(seed_artist), tag_frequencies, library_size, use_rarity
        ) * (config.similarity_artist_weight if config else 100) // 100
    if not seed:
        return score

    if use_tags:
        seed_tags = {tag.casefold() for tag in seed.tags if tag.casefold() not in disabled_tags}
        candidate_tags = {tag.casefold() for tag in candidate.tags if tag.casefold() not in disabled_tags}
        for tag in seed_tags & candidate_tags:
            score += _similarity_feature_weight(
                tag, tag_frequencies, library_size, use_rarity
            ) * (config.similarity_tag_weight if config else 100) // 100

    bpm_distance = _bpm_distance(seed.bpm, candidate.bpm, config.similarity_use_half_double_time if config else True) if use_bpm else None
    if bpm_distance is not None:
        # Nearby tempos provide a modest boost without outranking a tag match.
        maximum = config.similarity_bpm_max_distance if config else 30
        weight = config.similarity_bpm_weight if config else 30
        score += max(0, weight - bpm_distance) if bpm_distance <= maximum else 0
    return score


def similar_song_entries(
    entries: list[VideoEntry],
    seed: Optional[VideoEntry],
    seed_artist: str,
    exclude_id: str,
    config: AppConfig,
    count: Optional[int] = None,
    *,
    catalog: Optional[SimilarityCatalog] = None,
) -> list[VideoEntry]:
    """Return the locally ranked songs related to a seed song or artist.

    This is shared by the Subsonic API and desktop UI so their similarity
    controls always produce the same results.
    """
    limit = min(count if count is not None else config.similarity_max_results, config.similarity_max_results)
    use_catalog = catalog is not None and catalog.disabled_tags == config.disabled_similarity_tags
    if use_catalog:
        visible_entries = catalog.entries
        tag_frequencies = catalog.tag_frequencies
    else:
        # The desktop UI supplies its live library directly, so avoid building
        # one-request index dictionaries there.  The server supplies a cached
        # catalog from its snapshot.
        visible_entries = [entry for entry in entries if not entry.hidden_from_subsonic]
        tag_frequencies = _tag_frequencies(visible_entries, config.disabled_similarity_tags)

    seed_tags = (
        frozenset(tag.casefold() for tag in seed.tags if tag.casefold() not in config.disabled_similarity_tags)
        if seed and config.similarity_use_tags
        else frozenset()
    )
    library_size = len(visible_entries)
    tag_weight = config.similarity_tag_weight
    use_rarity = config.similarity_use_rarity

    def feature_weight(feature: str) -> int:
        return _similarity_feature_weight(feature, tag_frequencies, library_size, use_rarity)

    tag_scores = {
        tag: feature_weight(tag) * tag_weight // 100
        for tag in seed_tags
        if tag in tag_frequencies
    }
    artist_score = 0
    seed_artist_key = seed_artist.casefold()
    artist_feature = _artist_feature(seed_artist)
    if config.similarity_use_artist and _is_known_artist(seed_artist) and artist_feature in tag_frequencies:
        artist_score = (
            feature_weight(artist_feature)
            * config.similarity_artist_weight
            // 100
        )

    scored_candidates: list[tuple[VideoEntry, int]] = []
    for entry in visible_entries:
        if entry.id == exclude_id:
            continue
        candidate_artist = catalog.artist_by_id[entry.id] if use_catalog else _artist_name(entry).casefold()
        score = artist_score if artist_score and candidate_artist == seed_artist_key else 0
        if tag_scores:
            candidate_tags = (
                catalog.tags_by_id[entry.id]
                if use_catalog
                else {tag.casefold() for tag in entry.tags if tag.casefold() not in config.disabled_similarity_tags}
            )
            score += sum(tag_scores[tag] for tag in seed_tags & candidate_tags)
        if seed and config.similarity_use_bpm:
            bpm_distance = _bpm_distance(seed.bpm, entry.bpm, config.similarity_use_half_double_time)
            if bpm_distance is not None and bpm_distance <= config.similarity_bpm_max_distance:
                score += max(0, config.similarity_bpm_weight - bpm_distance)
        if score >= config.similarity_min_score:
            scored_candidates.append((entry, score))

    # The key is the existing deterministic ordering.  Selecting only the
    # requested prefix avoids sorting the entire match set.
    sort_key = lambda item: (-item[1], (item[0].title or item[0].id).casefold(), item[0].id)
    if limit < 0:
        ranked = sorted(scored_candidates, key=sort_key)[:limit]
    else:
        ranked = heapq.nsmallest(limit, scored_candidates, key=sort_key)
    return [entry for entry, _score in ranked]


def _similarity_catalog(entries: list[VideoEntry], disabled_tags: frozenset[str]) -> SimilarityCatalog:
    """Build cacheable similarity inputs while preserving the public ranking rules."""
    visible_entries = [entry for entry in entries if not entry.hidden_from_subsonic]
    return SimilarityCatalog(
        entries=visible_entries,
        disabled_tags=disabled_tags,
        tag_frequencies=_tag_frequencies(visible_entries, disabled_tags),
        tags_by_id={
            entry.id: frozenset(tag.casefold() for tag in entry.tags if tag.casefold() not in disabled_tags)
            for entry in visible_entries
        },
        artist_by_id={entry.id: _artist_name(entry).casefold() for entry in visible_entries},
    )


def _bpm_distance(first: int, second: int, allow_half_double_time: bool = True) -> Optional[int]:
    """Return a musically useful BPM distance, or ``None`` without two BPMs."""
    if first <= 0 or second <= 0:
        return None
    distances = [abs(first - second)]
    if allow_half_double_time:
        distances.extend((abs(first * 2 - second), abs(first - second * 2)))
    return min(distances)


def _tag_frequencies(entries: list[VideoEntry], disabled_tags: frozenset[str] = frozenset()) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for entry in entries:
        for tag in {tag.casefold() for tag in entry.tags if tag.casefold() not in disabled_tags}:
            frequencies[tag] = frequencies.get(tag, 0) + 1
        artist = _artist_feature(_artist_name(entry))
        frequencies[artist] = frequencies.get(artist, 0) + 1
    return frequencies


def _artist_feature(name: str) -> str:
    return f"\0artist:{name.casefold()}"


def _is_known_artist(name: str) -> bool:
    """Whether an artist name represents metadata rather than the fallback."""
    return bool(name.strip()) and name.casefold() != "unknown artist"


def _similarity_feature_weight(
    feature: str,
    frequencies: dict[str, int],
    library_size: int,
    use_rarity: bool = True,
) -> int:
    """Weight a shared tag or artist, optionally using inverse frequency."""
    if not use_rarity:
        return 100
    # A rare, specific feature is materially stronger than one used across the
    # library. Artist identities are represented by private feature keys, so
    # they cannot collide with user-entered tag names.
    return round(100 * (1 + log((library_size + 1) / (frequencies[feature] + 1))))


def _song_payload(
    entry: VideoEntry,
    metadata: Optional[MetadataStore] = None,
    *,
    probe_media: bool = True,
) -> dict[str, Any]:
    path = entry.path
    suffix = path.suffix.lower().lstrip(".") if path else ""
    payload: dict[str, Any] = {
        "id": entry.id,
        "parent": _album_id(entry),
        "title": entry.title or entry.id,
        "album": _album_name(entry),
        "artist": _artist_name(entry),
        "isDir": False,
        "coverArt": entry.id,
        "created": _created_date(entry),
        "type": "music",
        "suffix": suffix,
        "contentType": mimetypes.guess_type(path.name if path else "")[0] or "application/octet-stream",
        "albumId": _album_id(entry),
        "artistId": _artist_id(_artist_name(entry)),
    }
    if path:
        try:
            payload["size"] = path.stat().st_size
        except OSError:
            pass
    payload.update(_song_media_fields(entry, metadata, probe_media=probe_media))
    if entry.last_played_at:
        payload["played"] = entry.last_played_at
        payload["lastPlayed"] = entry.last_played_at
    if entry.play_count > 0:
        payload["playCount"] = entry.play_count
    if entry.bpm > 0:
        payload["bpm"] = entry.bpm
    year = _year(entry)
    if year:
        payload["year"] = year
    return payload


def _album_payload(album_id: str, entries: list[VideoEntry]) -> dict[str, Any]:
    first = entries[0]
    artist = _artist_name(first)
    payload: dict[str, Any] = {
        "id": album_id,
        "name": _album_name(first),
        "artist": artist,
        "artistId": _artist_id(artist),
        "songCount": len(entries),
        # Unknown durations contribute zero; cached durations are summed so
        # Subsonic clients can display a useful album length without probing
        # every file while building a catalog response.
        "duration": max(0, round(sum(entry.duration_seconds for entry in entries if entry.duration_seconds > 0))),
        "created": _created_date(first),
        "coverArt": first.id,
    }
    play_count = _album_play_count(entries)
    last_played = _album_last_played(entries)
    if play_count > 0:
        payload["playCount"] = play_count
    if last_played:
        payload["played"] = last_played
        payload["lastPlayed"] = last_played
    year = _year(first)
    if year:
        payload["year"] = year
    return payload


def _directory_album_payload(album_id: str, entries: list[VideoEntry]) -> dict[str, Any]:
    payload = _album_payload(album_id, entries)
    payload["title"] = payload["name"]
    payload["isDir"] = True
    return payload


def _cover_art_path(config: AppConfig, entry: VideoEntry) -> Path:
    candidates: list[Path] = []
    image_suffixes = (".jpg", ".jpeg", ".png", ".webp")
    youtube_id = thumbnail_video_id(entry.id)

    for stem in [entry.id, youtube_id, entry.source_id]:
        if not stem:
            continue
        candidates.extend(config.thumbnails_dir / f"{stem}{suffix}" for suffix in image_suffixes)
        candidates.extend(config.download_dir / f"{stem}{suffix}" for suffix in image_suffixes)

    if entry.path:
        candidates.extend(entry.path.with_suffix(suffix) for suffix in image_suffixes)

    for path in candidates:
        if path.exists() and path.is_file():
            return path

    if youtube_id:
        downloaded = ensure_thumbnail(config.thumbnails_dir, youtube_id)
        if downloaded:
            return downloaded

    embedded = _extract_embedded_cover(config, entry)
    if embedded:
        return embedded

    return thumbnail_path(config.thumbnails_dir, entry.id)


def _extract_embedded_cover(config: AppConfig, entry: VideoEntry) -> Optional[Path]:
    if not entry.path or not entry.path.exists() or not shutil.which("ffmpeg"):
        return None
    stream_index = _embedded_cover_stream_index(entry.path)
    if stream_index is None:
        return None

    config.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    output = thumbnail_path(config.thumbnails_dir, entry.id)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(entry.path),
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        **windows_no_console_kwargs(),
    )
    if proc.returncode == 0 and output.exists() and output.stat().st_size > 0:
        return output
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _embedded_cover_stream_index(path: Path) -> Optional[int]:
    info = ffprobe_json(path) or {}
    for stream in info.get("streams", []):
        if not isinstance(stream, dict):
            continue
        disposition = stream.get("disposition", {})
        if stream.get("codec_type") == "video" and isinstance(disposition, dict) and int(disposition.get("attached_pic") or 0):
            try:
                return int(stream["index"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _scrobble_time(value: str) -> str:
    if value:
        try:
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).replace(microsecond=0).isoformat()
        except (OSError, OverflowError, ValueError):
            pass
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _is_submitted_scrobble(params: dict[str, str]) -> bool:
    raw = params.get("submission", "true").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def _sorted_albums(albums: dict[str, list[VideoEntry]], sort_type: str) -> list[tuple[str, list[VideoEntry]]]:
    items = list(albums.items())
    mode = (sort_type or "alphabeticalByName").strip()
    if mode in {"recent", "lastPlayed"}:
        return sorted(items, key=lambda item: (_album_last_played(item[1]), _album_name(item[1][0]).casefold()), reverse=True)
    if mode == "frequent":
        return sorted(items, key=lambda item: (_album_play_count(item[1]), _album_last_played(item[1])), reverse=True)
    if mode == "newest":
        return sorted(items, key=lambda item: (_downloaded_time(item[1][0]), _album_name(item[1][0]).casefold()), reverse=True)
    if mode == "oldest":
        return sorted(items, key=lambda item: (_downloaded_time(item[1][0]), _album_name(item[1][0]).casefold()))
    if mode == "alphabeticalByArtist":
        return sorted(items, key=lambda item: (_artist_name(item[1][0]).casefold(), _album_name(item[1][0]).casefold()))
    if mode == "random":
        random.shuffle(items)
        return items
    return sorted(items, key=lambda item: _album_name(item[1][0]).casefold())


def _album_play_count(entries: list[VideoEntry]) -> int:
    return sum(max(0, entry.play_count) for entry in entries)


def _album_last_played(entries: list[VideoEntry]) -> str:
    return max((entry.last_played_at for entry in entries if entry.last_played_at), default="")


def _downloaded_time(entry: VideoEntry) -> float:
    if not entry.path:
        return 0.0
    try:
        return entry.path.stat().st_mtime
    except OSError:
        return 0.0


def _estimated_play_count(entry: VideoEntry) -> int:
    duration = _duration_seconds(entry)
    if duration and duration > 0:
        # Count full-ish plays, but keep a single partial desktop play visible.
        return max(1, round(entry.playback_seconds / duration))
    return 1


def _duration_seconds(entry: VideoEntry) -> Optional[float]:
    if entry.duration_seconds > 0:
        return entry.duration_seconds
    return _duration_from_info(_media_info(entry))


def _song_media_fields(
    entry: VideoEntry,
    metadata: Optional[MetadataStore] = None,
    *,
    probe_media: bool = True,
) -> dict[str, int]:
    duration = entry.duration_seconds if entry.duration_seconds > 0 else None
    bitrate = entry.bitrate_kbps if entry.bitrate_kbps > 0 else None
    audio_bitrate = entry.audio_bitrate_kbps if entry.audio_bitrate_kbps > 0 else None
    if probe_media and (duration is None or bitrate is None or audio_bitrate is None):
        info = _media_info(entry)
        duration = duration or _duration_from_info(info)
        bitrate = bitrate or _bitrate_kbps(info, entry.path, duration)
        audio_bitrate = audio_bitrate or _audio_bitrate_kbps(info)
        _cache_song_media_fields(entry, metadata, duration, bitrate, audio_bitrate)
    payload: dict[str, int] = {}
    if duration and duration > 0:
        payload["duration"] = max(1, round(duration))
    subsonic_bitrate = audio_bitrate or bitrate
    if subsonic_bitrate and subsonic_bitrate > 0:
        payload["bitRate"] = subsonic_bitrate
    return payload


def _cache_song_media_fields(
    entry: VideoEntry,
    metadata: Optional[MetadataStore],
    duration: Optional[float],
    bitrate: Optional[int],
    audio_bitrate: Optional[int],
) -> None:
    changed = False
    if duration and duration > 0 and entry.duration_seconds <= 0:
        entry.duration_seconds = duration
        changed = True
    if bitrate and bitrate > 0 and entry.bitrate_kbps <= 0:
        entry.bitrate_kbps = bitrate
        changed = True
    if audio_bitrate and audio_bitrate > 0 and entry.audio_bitrate_kbps <= 0:
        entry.audio_bitrate_kbps = audio_bitrate
        changed = True
    if changed and metadata:
        metadata.update_media_fields(
            entry.id,
            duration=duration,
            bitrate=bitrate,
            audio_bitrate=audio_bitrate,
        )


def _media_info(entry: VideoEntry) -> dict:
    if not entry.path or not entry.path.exists():
        return {}
    return ffprobe_json(entry.path) or {}


def _duration_from_info(info: dict) -> Optional[float]:
    raw_duration = info.get("format", {}).get("duration")
    try:
        return float(raw_duration)
    except (TypeError, ValueError):
        pass
    for stream in info.get("streams", []):
        if not isinstance(stream, dict):
            continue
        try:
            return float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
    return None


def _bitrate_kbps(info: dict, path: Optional[Path], duration: Optional[float]) -> Optional[int]:
    try:
        bits_per_second = int(float(info.get("format", {}).get("bit_rate")))
    except (TypeError, ValueError):
        bits_per_second = 0
    if bits_per_second > 0:
        return max(1, round(bits_per_second / 1000))
    if path and duration and duration > 0:
        try:
            return max(1, round(path.stat().st_size * 8 / duration / 1000))
        except OSError:
            pass
    return None


def _audio_bitrate_kbps(info: dict) -> Optional[int]:
    for stream in info.get("streams", []):
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        try:
            bits_per_second = int(float(stream.get("bit_rate")))
        except (TypeError, ValueError):
            continue
        if bits_per_second > 0:
            return max(1, round(bits_per_second / 1000))
    return None


def _structured_lrc_lines(lyrics: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    plain: list[dict[str, str]] = []
    for raw_line in lyrics.splitlines():
        matches = list(re.finditer(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]", raw_line))
        text = re.sub(r"^(?:\[\d{1,3}:\d{2}(?:\.\d{1,3})?\])+", "", raw_line).strip()
        if not matches:
            if raw_line.strip():
                plain.append({"value": raw_line.strip()})
            continue
        for match in matches:
            parsed.append({"start": _lrc_timestamp_ms(match), "value": text})
    if parsed:
        return sorted(parsed, key=lambda item: int(item.get("start", 0)))
    return plain


def _lrc_timestamp_ms(match: re.Match[str]) -> int:
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    fraction = match.group(3) or "0"
    milliseconds = int(fraction.ljust(3, "0")[:3])
    return (minutes * 60 + seconds) * 1000 + milliseconds


def _year(entry: VideoEntry) -> Optional[int]:
    if len(entry.upload_date) >= 4 and entry.upload_date[:4].isdigit():
        return int(entry.upload_date[:4])
    return None


def _created_date(entry: VideoEntry) -> str:
    if format_date(entry.upload_date):
        return format_date(entry.upload_date) + "T00:00:00Z"
    return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _format_subsonic(params: dict[str, str], payload: dict[str, Any]) -> tuple[bytes, str]:
    if params.get("f", "").lower() == "json":
        return json.dumps({"subsonic-response": payload}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
    root = ET.Element("subsonic-response", {"xmlns": SUBSONIC_NS})
    _fill_xml(root, payload)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), "text/xml; charset=utf-8"


def _fill_xml(element: ET.Element, value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                child = ET.SubElement(element, key)
                _fill_xml(child, item)
            elif isinstance(item, list):
                for member in item:
                    child = ET.SubElement(element, key)
                    _fill_xml(child, member)
            elif isinstance(item, bool):
                element.set(key, "true" if item else "false")
            else:
                element.set(key, str(item))
    elif isinstance(value, list):
        for item in value:
            child = ET.SubElement(element, "item")
            _fill_xml(child, item)


def _send_file(handler: BaseHTTPRequestHandler, path: Path, download: bool) -> None:
    started = time.perf_counter()
    timing_service = getattr(handler, "service", None)
    timing = isinstance(timing_service, SubsonicService) and timing_service.timing
    if not path.exists() or not path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    total = path.stat().st_size
    start, end = _range_bounds(handler.headers.get("Range"), total)
    status = HTTPStatus.PARTIAL_CONTENT if start or end != total - 1 else HTTPStatus.OK
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    if download:
        handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    if status == HTTPStatus.PARTIAL_CONTENT:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total}")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.end_headers()
    headers_sent = time.perf_counter()
    sent = 0
    first_byte_at: Optional[float] = None
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
                sent += len(chunk)
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        return
    finally:
        if timing:
            finished = time.perf_counter()
            first_byte = _elapsed_ms(started, first_byte_at) if first_byte_at else 0.0
            timing_service._timing(
                "file"
                f" headers={_elapsed_ms(started, headers_sent):.1f}ms"
                f" first-byte={first_byte:.1f}ms"
                f" transfer={_elapsed_ms(headers_sent, finished):.1f}ms"
                f" total={_elapsed_ms(started, finished):.1f}ms"
                f" bytes={sent}"
            )


def _range_bounds(header: Optional[str], total: int) -> tuple[int, int]:
    if not header or not header.startswith("bytes="):
        return 0, max(0, total - 1)
    raw = header.removeprefix("bytes=").split(",", 1)[0].strip()
    if "-" not in raw:
        return 0, max(0, total - 1)
    start_raw, end_raw = raw.split("-", 1)
    try:
        if start_raw:
            start = int(start_raw)
            end = int(end_raw) if end_raw else total - 1
        else:
            length = int(end_raw)
            start = max(0, total - length)
            end = total - 1
    except ValueError:
        return 0, max(0, total - 1)
    start = max(0, min(start, total - 1))
    end = max(start, min(end, total - 1))
    return start, end
