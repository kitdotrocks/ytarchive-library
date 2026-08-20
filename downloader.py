from __future__ import annotations

import importlib.util
import html
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import hashlib
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

try:
    from yt_dlp import YoutubeDL
    YT_DLP_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - exercised when dependency is absent
    YoutubeDL = None  # type: ignore[assignment]
    YT_DLP_IMPORT_ERROR = exc

from . import APP_SLUG
from .artist_art import maybe_prefetch_artist_art
from .bpm import apply_bpm_analysis
from .config import AppConfig
from .ids import canonical_bandcamp_id, canonical_local_id, canonical_soundcloud_id, canonical_youtube_id, is_youtube_raw_id, legacy_alias_ids, split_prefixed_id
from .library import MEDIA_EXTS, build_file_index
from .media import detect_media_tags, ffprobe_json, thumbnail_path
from .metadata import MetadataStore, VideoEntry
from .runtime import windows_no_console_kwargs


ProgressCallback = Callable[[dict], None]

YOUTUBE_HOSTS = frozenset(
    {
        "youtu.be",
        "www.youtu.be",
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
SPOTIFY_HOSTS = frozenset({"spotify.com", "www.spotify.com", "open.spotify.com", "www.open.spotify.com"})
SPOTIFY_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
MATCH_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_QUERY_NOISE_TOKENS = frozenset(
    {
        "official",
        "audio",
        "video",
        "lyrics",
        "lyric",
        "visualizer",
        "visualiser",
        "topic",
        "hd",
        "hq",
        "music",
        "mv",
        "live",
        "remix",
        "version",
        "edit",
        "mix",
        "extended",
        "radio",
        "session",
        "cover",
        "instrumental",
        "by",
        "feat",
        "ft",
        "featuring",
    }
)
_MATCH_MIN_SCORE = 72.0
_YOUTUBE_SEARCH_LIMIT = 10

# yt-dlp's current ``-t sleep`` preset uses these values to reduce the
# likelihood of triggering YouTube's session/rate-limit checks.  Keep the
# values in one place so the application and its diagnostics agree about the
# pacing policy.
YOUTUBE_SLEEP_REQUESTS = 0.75
YOUTUBE_SLEEP_INTERVAL = 10
YOUTUBE_MAX_SLEEP_INTERVAL = 20
YOUTUBE_RATE_LIMIT_HINT = (
    "YouTube rate-limited this session or triggered its anti-bot check (HTTP 429). "
    "Stop retrying for now; verify the configured browser profile is logged in on "
    "the same network, then retry later with one worker. Visitor Data/PO tokens "
    "should not be pasted manually for this error."
)


class DownloadCancelled(RuntimeError):
    pass


def yt_dlp_available() -> bool:
    return YoutubeDL is not None


def aria2c_available() -> bool:
    """Return whether the optional aria2c accelerator is available on PATH."""
    return shutil.which("aria2c") is not None


def yt_dlp_ejs_available() -> bool:
    """Return whether the external YouTube challenge solver is installed."""
    if YoutubeDL is None:
        return False
    try:
        from yt_dlp.dependencies import yt_dlp_ejs
    except Exception:
        return False
    return yt_dlp_ejs is not None


def _yt_dlp_js_runtime_details() -> tuple[Optional[str], Optional[str]]:
    """Return a runtime name and any explicit executable-path override."""
    for runtime, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("quickjs", "qjs"),
        ("bun", "bun"),
    ):
        if shutil.which(executable):
            return runtime, None
        if runtime == "deno":
            # Deno's official per-user installer uses this directory. Desktop
            # launchers may not inherit a shell's updated PATH until the next
            # login, so pass the known executable path directly to yt-dlp.
            deno_name = "deno.exe" if os.name == "nt" else "deno"
            deno_path = Path.home() / ".deno" / "bin" / deno_name
            if deno_path.is_file() and (os.name == "nt" or os.access(deno_path, os.X_OK)):
                return runtime, str(deno_path)
    return None, None


def yt_dlp_js_runtime() -> Optional[str]:
    """Return the first supported JavaScript runtime the app can use."""
    runtime, _path = _yt_dlp_js_runtime_details()
    return runtime


def yt_dlp_missing_components() -> list[str]:
    """Return components required for reliable YouTube extraction."""
    if not yt_dlp_available():
        return ["yt-dlp"]
    missing = []
    if not yt_dlp_ejs_available():
        missing.append("yt-dlp-ejs")
    if yt_dlp_js_runtime() is None:
        missing.append("yt-dlp-js-runtime")
    return missing


def _is_youtube_target(value: str) -> bool:
    """Return whether a yt-dlp target is a YouTube URL or search query."""
    raw = str(value or "").strip().casefold()
    if raw.startswith("ytsearch"):
        return True
    parsed = urlparse(raw)
    return _parsed_hostname(parsed) in YOUTUBE_HOSTS


def _is_youtube_rate_limit_text(value: object) -> bool:
    """Recognize YouTube's HTTP 429 and bot-check messages."""
    text = str(value or "").casefold()
    if not any(marker in text for marker in ("youtube", "sign in to confirm", "page needs to be reloaded")):
        return False
    return any(
        marker in text
        for marker in (
            "http error 429",
            "too many requests",
            "not a bot",
            "rate-limit",
            "rate limit",
            "page needs to be reloaded",
        )
    )


def require_yt_dlp() -> None:
    if YoutubeDL is None:
        if YT_DLP_IMPORT_ERROR is None:
            raise RuntimeError("yt-dlp is not installed.")
        raise RuntimeError(f"yt-dlp is unavailable: {YT_DLP_IMPORT_ERROR}")


def test_browser_cookies(browser_cookies: tuple[str, Optional[str], Optional[str], Optional[str]]) -> int:
    """Load browser cookies and verify they can be used by yt-dlp.

    The subscriptions feed is intentionally used instead of a plain homepage:
    it requires an authenticated YouTube session, so a successful extraction
    is a meaningful end-to-end cookie check.
    """
    require_yt_dlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiesfrombrowser": browser_cookies,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
    }
    with YoutubeDL(opts) as ydl:
        ydl.extract_info("https://www.youtube.com/feed/subscriptions", download=False)
        return sum(1 for _ in ydl.cookiejar)


def test_cookie_file(cookie_file: Path) -> int:
    """Load and validate a Netscape-format cookie file against YouTube."""
    require_yt_dlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": str(cookie_file),
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
    }
    with YoutubeDL(opts) as ydl:
        ydl.extract_info("https://www.youtube.com/feed/subscriptions", download=False)
        return sum(1 for _ in ydl.cookiejar)


class QuietLogger:
    def __init__(self, progress: Optional[ProgressCallback] = None):
        self.progress = progress
        self._rate_limit_hint_sent = False

    def debug(self, msg: str) -> None:
        if self.progress and any(
            marker in msg.casefold()
            for marker in ("playability status", "javascript", "jsc", "challenge")
        ):
            self.progress({"phase": "native", "message": f"yt-dlp debug: {msg}"})

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if self.progress:
            self.progress({"phase": "native", "message": f"yt-dlp warning: {msg}"})
            if _is_youtube_rate_limit_text(msg) and not self._rate_limit_hint_sent:
                self._rate_limit_hint_sent = True
                self.progress({"phase": "native", "message": YOUTUBE_RATE_LIMIT_HINT})

    def error(self, msg: str) -> None:
        if self.progress:
            self.progress({"phase": "native", "message": f"yt-dlp error: {msg}"})
            if _is_youtube_rate_limit_text(msg) and not self._rate_limit_hint_sent:
                self._rate_limit_hint_sent = True
                self.progress({"phase": "native", "message": YOUTUBE_RATE_LIMIT_HINT})


@dataclass
class ResolvedVideo:
    id: str
    title: str
    upload_date: str
    author: str
    info: dict
    cookie_headers: dict[str, str]
    used_browser_cookies: bool
    source_type: str = ""
    source_id: str = ""
    source_url: str = ""
    resolved_from: str = ""
    audio_quality: str = ""


@dataclass
class SpotifyTrack:
    id: str
    title: str
    author: str
    source_url: str
    upload_date: str = ""
    duration_seconds: float = 0.0


@dataclass
class MonochromeTrack:
    id: str
    title: str
    author: str
    source_url: str
    album_title: str = ""
    release_date: str = ""
    duration_seconds: float = 0.0
    audio_quality: str = "flac"
    cover_id: str = ""
    isrc: str = ""


@dataclass(frozen=True)
class _MonochromeManifest:
    """A signed stream URL plus the format hint returned by Monochrome."""

    url: str
    extension: str = "flac"
    codec: str = ""
    presentation: str = ""


@dataclass(frozen=True)
class _SearchMatch:
    score: float
    title_score: float
    artist_score: float
    duration_score: float = 0.0


@dataclass
class AddVideoResult:
    status: str
    entry: Optional[VideoEntry] = None
    resolved_id: str = ""
    message: str = ""


class Aria2Rpc:
    def __init__(self, work_dir: Path, max_connections: int = 8):
        self.work_dir = work_dir
        self.max_connections = max(1, max_connections)
        self.port = _free_port()
        self.secret = str(uuid.uuid4())
        self.proc: Optional[subprocess.Popen[str]] = None

    def start(self) -> None:
        if not shutil.which("aria2c"):
            raise RuntimeError("aria2c was not found")
        self.proc = subprocess.Popen(
            [
                "aria2c",
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={self.port}",
                f"--rpc-secret={self.secret}",
                "--no-conf=true",
                "--console-log-level=warn",
                "--summary-interval=0",
                "--download-result=hide",
                "--file-allocation=none",
                "--continue=true",
                "--allow-overwrite=true",
                "--auto-file-renaming=false",
                f"-x{self.max_connections}",
                f"-j{self.max_connections}",
                f"-s{self.max_connections}",
                "--min-split-size=1M",
                f"--dir={self.work_dir}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **windows_no_console_kwargs(),
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                self.tell_active()
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("aria2c RPC did not become ready")

    def stop(self) -> None:
        try:
            self.call("aria2.shutdown", [])
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def add_uri(self, url: str, out_name: str, headers: dict[str, str]) -> str:
        options: dict[str, object] = {"out": out_name, "continue": "true", "allow-overwrite": "true"}
        if headers:
            options["header"] = [f"{key}: {value}" for key, value in headers.items()]
        return self.call("aria2.addUri", [[url], options])

    def tell_status(self, gid: str) -> dict:
        return self.call(
            "aria2.tellStatus",
            [gid, ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorMessage", "files"]],
        )

    def tell_active(self) -> list[dict]:
        return self.call("aria2.tellActive", [[]])

    def call(self, method: str, params: list) -> object:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": method,
                "params": [f"token:{self.secret}", *params],
            }
        ).encode()
        request = Request(
            f"http://127.0.0.1:{self.port}/jsonrpc",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            decoded = json.loads(response.read().decode())
        if "error" in decoded:
            raise RuntimeError(decoded["error"])
        return decoded["result"]


class CacheUpdater:
    def __init__(self, config: AppConfig, metadata: MetadataStore):
        self.config = config
        self.metadata = metadata
        self._youtube_extract_lock = threading.Lock()
        self._youtube_extract_next_at = 0.0

    def _store_entry(self, entry: VideoEntry, save: bool = True) -> None:
        # Download work already happens away from the UI.  A failed or
        # low-confidence analysis simply leaves the BPM empty.
        apply_bpm_analysis(entry)
        self.metadata.upsert(entry, save=save)

    def update(self, progress: ProgressCallback, stop_event) -> None:
        require_yt_dlp()
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        playlists = self._read_playlists()
        known = {canonical_youtube_id(video_id) if is_youtube_raw_id(video_id) else video_id for video_id in self.metadata.ids()}
        seen = set(known)
        submitted = 0
        confirmed = 0
        completed = 0
        failures = 0
        skipped = 0
        queue_done = 0
        active_rows: dict[tuple[str, str], dict] = {}
        active_lock = threading.Lock()
        counter_lock = threading.Lock()
        confirmed_ids: set[str] = set()
        queue_done_keys: set[str] = set()
        futures: set[Future] = set()
        future_urls: dict[Future, str] = {}
        # YouTube's anti-bot/rate-limit state is session-sensitive.  Running
        # several full yt-dlp extractions at once makes a 429 much more
        # likely; workers still control fragment/aria2 concurrency below.
        parallel_downloads = 1

        def mark_queue_done(key: str) -> int:
            nonlocal queue_done
            if key not in queue_done_keys:
                queue_done_keys.add(key)
                queue_done += 1
            return queue_done

        def emit_progress(data: dict) -> None:
            nonlocal confirmed
            video_id = data.get("video_id")
            if data.get("phase") == "confirmed" and video_id:
                with counter_lock:
                    if video_id not in confirmed_ids:
                        confirmed_ids.add(video_id)
                        confirmed += 1
                    current_queue_done = mark_queue_done(video_id)
                    download_total = confirmed
                    download_done = completed + failures
                payload = dict(data)
                payload.update(
                    {
                        "queue_total": submitted,
                        "queue_done": current_queue_done,
                        "download_total": download_total,
                        "download_done": download_done,
                        "failures": failures,
                    }
                )
                progress(payload)
                return
            if "active" not in data or not video_id:
                progress(data)
                return
            with active_lock:
                for key in [key for key in active_rows if key[0] == video_id]:
                    del active_rows[key]
                for row in data.get("active") or []:
                    row = dict(row)
                    row["video_id"] = video_id
                    active_rows[(video_id, row.get("file", ""))] = row
                payload = dict(data)
                payload["active"] = list(active_rows.values())
            progress(payload)

        def clear_active(video_url: str) -> None:
            video_id = _canonical_submitted_id(video_url)
            if not video_id:
                return
            with active_lock:
                for key in [key for key in active_rows if key[0] == video_id]:
                    del active_rows[key]
                active = list(active_rows.values())
            progress({"phase": "transfer", "message": "", "active": active})

        def submit_download(executor: ThreadPoolExecutor, video_url: str) -> None:
            nonlocal submitted
            submitted += 1
            future = executor.submit(self._download_one, video_url, emit_progress, stop_event)
            futures.add(future)
            future_urls[future] = video_url
            progress(
                {
                    "phase": "download",
                    "message": f"Queued {submitted} candidates; {len(futures)} active/pending",
                    "queue_done": queue_done,
                    "queue_total": submitted,
                    "download_done": completed + failures,
                    "download_total": confirmed,
                    "failures": failures,
                }
            )

        def drain_completed(block: bool) -> None:
            nonlocal completed, failures, skipped, futures
            while futures:
                done, remaining = wait(futures, timeout=None if block else 0, return_when=FIRST_COMPLETED)
                if not done:
                    return
                futures = remaining
                for future in done:
                    video_url = future_urls.pop(future, "")
                    video_id = _canonical_submitted_id(video_url)
                    clear_active(video_url)
                    try:
                        entry = future.result()
                    except DownloadCancelled:
                        raise
                    except (Exception, SystemExit) as exc:
                        if video_id in confirmed_ids:
                            failures += 1
                            progress({"phase": "error", "message": f"{video_url}: {exc}"})
                        else:
                            skipped += 1
                            with counter_lock:
                                current_queue_done = mark_queue_done(video_id or video_url)
                            progress(
                                {
                                    "phase": "skip",
                                    "message": f"Skipped unavailable: {video_url}",
                                    "queue_done": current_queue_done,
                                    "queue_total": submitted,
                                }
                            )
                    else:
                        completed += 1
                        self._store_entry(entry, save=True)
                    progress(
                        {
                            "phase": "download",
                            "message": f"Finished {completed + failures}/{confirmed}; {skipped} unavailable skipped; {len(futures)} active/pending",
                            "queue_done": queue_done,
                            "queue_total": submitted,
                            "download_done": completed + failures,
                            "download_total": confirmed,
                            "failures": failures,
                        }
                    )
                if not block:
                    continue

        progress(
            {
                "phase": "scan",
                "message": f"Scanning playlists; up to {parallel_downloads} videos download in parallel",
                "playlist_total": len(playlists),
                "queue_total": 0,
                "queue_done": 0,
                "download_total": 0,
                "download_done": 0,
            }
        )
        with ThreadPoolExecutor(max_workers=parallel_downloads, thread_name_prefix="download") as executor:
            for playlist_index, playlist_url in enumerate(playlists, start=1):
                self._check_cancelled(stop_event)
                try:
                    urls = self._playlist_video_urls(playlist_url, progress=progress)
                except Exception as exc:
                    progress({"phase": "error", "message": f"Playlist failed: {exc}"})
                    continue
                for url in urls:
                    self._check_cancelled(stop_event)
                    canonical_id = _canonical_submitted_id(url)
                    if canonical_id and canonical_id not in seen:
                        seen.add(canonical_id)
                        submit_download(executor, url)
                        drain_completed(block=False)
                progress(
                    {
                        "phase": "scan",
                        "message": f"Scanned playlist {playlist_index}/{len(playlists)}; queued {submitted}",
                        "playlist_done": playlist_index,
                        "playlist_total": len(playlists),
                        "queued_total": submitted,
                        "queue_done": queue_done,
                        "queue_total": submitted,
                        "download_done": completed + failures,
                        "download_total": confirmed,
                    }
                )
                drain_completed(block=False)
            drain_completed(block=True)

        progress({"phase": "done", "message": f"Finished: {completed} downloaded, {failures} failed, {skipped} unavailable skipped"})

    def add_video(self, raw_input: str, progress: ProgressCallback, stop_event) -> AddVideoResult:
        video_url = normalize_media_input(raw_input)
        if _is_monochrome_url(video_url):
            return self._add_monochrome(video_url, progress, stop_event)
        if _is_spotify_url(video_url):
            return self._add_spotify_track(video_url, progress, stop_event)
        if _is_direct_media_url(urlparse(video_url)):
            existing = self._get_existing_source_url(video_url)
            if existing:
                return AddVideoResult(
                    status="exists",
                    entry=existing,
                    resolved_id=existing.id,
                    message=f"Already in library: {existing.title or existing.id}",
                )
            entry = self._download_direct_file(video_url, progress, stop_event)
            existing = self._get_existing_entry(entry.id)
            if existing:
                return AddVideoResult(
                    status="exists",
                    entry=existing,
                    resolved_id=existing.id,
                    message=f"Already in library: {existing.title or existing.id}",
                )
            self._store_entry(entry, save=True)
            return AddVideoResult(status="downloaded", entry=entry, resolved_id=entry.id, message=f"Added {entry.title}")
        known_id = _video_id_from_url(video_url)
        if known_id:
            existing = self._get_existing_entry(canonical_youtube_id(known_id))
            if existing:
                return AddVideoResult(
                    status="exists",
                    entry=existing,
                    resolved_id=existing.id,
                    message=f"Already in library: {existing.title or existing.id}",
                )

        entry = self._download_one(video_url, progress, stop_event)
        existing = self._get_existing_entry(entry.id)
        if existing:
            return AddVideoResult(
                status="exists",
                entry=existing,
                resolved_id=existing.id,
                message=f"Already in library: {existing.title or existing.id}",
            )
        self._store_entry(entry, save=True)
        return AddVideoResult(status="downloaded", entry=entry, resolved_id=entry.id, message=f"Added {entry.title}")

    def _add_monochrome(self, url: str, progress: ProgressCallback, stop_event) -> AddVideoResult:
        kind, item_id = _monochrome_item_from_url(url)
        if kind == "track":
            track = self._resolve_monochrome_track(item_id, source_url=url)
            existing = self._get_existing_entry(track.id)
            if existing:
                return AddVideoResult(
                    status="exists",
                    entry=existing,
                    resolved_id=existing.id,
                    message=f"Already in library: {existing.title or existing.id}",
                )
            entry = self._download_monochrome_track(track, progress, stop_event)
            self._store_entry(entry, save=True)
            return AddVideoResult(status="downloaded", entry=entry, resolved_id=entry.id, message=f"Added {entry.title}")
        if kind != "album":
            raise ValueError("Monochrome URL must point to a track or album")

        album, tracks = self._resolve_monochrome_album(item_id, source_url=url)
        if not tracks:
            raise RuntimeError("Monochrome album has no downloadable tracks")
        added = 0
        existing_count = 0
        first_entry: Optional[VideoEntry] = None
        for index, track in enumerate(tracks, start=1):
            self._check_cancelled(stop_event)
            existing = self._get_existing_entry(track.id)
            if existing:
                existing_count += 1
                if first_entry is None:
                    first_entry = existing
                progress({"phase": "skip", "video_id": track.id, "message": f"Already in library: {track.title}"})
                continue
            progress({"phase": "scan", "video_id": track.id, "message": f"Downloading album track {index}/{len(tracks)}: {track.title}"})
            entry = self._download_monochrome_track(track, progress, stop_event)
            self._store_entry(entry, save=True)
            added += 1
            if first_entry is None:
                first_entry = entry
        status = "downloaded" if added else "exists"
        title = str(album.get("title") or item_id)
        return AddVideoResult(
            status=status,
            entry=first_entry,
            resolved_id=first_entry.id if first_entry else "",
            message=f"Added Monochrome album {title}: {added} downloaded, {existing_count} already present",
        )

    def _resolve_monochrome_track(self, track_id: str, source_url: str) -> MonochromeTrack:
        payload = self._monochrome_get("/info/", {"id": track_id})
        data = payload.get("data", payload)
        items = data if isinstance(data, list) else [data]
        raw = next((item.get("item") if isinstance(item, dict) and isinstance(item.get("item"), dict) else item for item in items if isinstance(item, dict)), None)
        if not raw:
            raise RuntimeError("Could not resolve Monochrome track metadata")
        return _monochrome_track_from_payload(raw, source_url)

    def _resolve_monochrome_album(self, album_id: str, source_url: str) -> tuple[dict, list[MonochromeTrack]]:
        payload = self._monochrome_get("/album", {"id": album_id, "limit": "999", "offset": "0"})
        album = payload.get("data", payload)
        if not isinstance(album, dict):
            raise RuntimeError("Could not resolve Monochrome album metadata")
        source_album_url = source_url
        tracks: list[MonochromeTrack] = []
        for item in album.get("items") or []:
            raw = item.get("item") if isinstance(item, dict) else item
            if not isinstance(raw, dict):
                continue
            track_url = f"https://monochrome.tf/track/{raw.get('id')}"
            track = _monochrome_track_from_payload(raw, track_url)
            track.album_title = track.album_title or str(album.get("title") or "")
            track.release_date = str(album.get("releaseDate") or "").strip() or track.release_date
            track.cover_id = track.cover_id or str(album.get("cover") or "").strip()
            if not track.source_url:
                track.source_url = source_album_url
            tracks.append(track)
        return album, tracks

    def _download_monochrome_track(self, track: MonochromeTrack, progress: ProgressCallback, stop_event) -> VideoEntry:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required to download Monochrome tracks")
        progress({"phase": "confirmed", "video_id": track.id, "message": f"Resolved Monochrome track: {track.title}"})
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        out_path: Optional[Path] = None
        quality_hint = ""
        with tempfile.TemporaryDirectory(prefix="ytarchive-monochrome-", dir=str(self.config.root_dir)) as tmpdir:
            tmp_root = Path(tmpdir)
            tmp_out = tmp_root / f"{track.id}.flac"
            progress(
                {
                    "phase": "transfer",
                    "video_id": track.id,
                    "message": f"Downloading Monochrome audio: {track.title}",
                    "active": [
                        {
                            "title": track.title,
                            "file": tmp_out.name,
                            "status": "downloading",
                            "completed": 0,
                            "total": 0,
                            "speed": 0,
                        }
                    ],
                }
            )
            try:
                manifest = self._monochrome_signed_manifest(track.id)
                quality_hint = manifest.codec
                tmp_out = tmp_root / f"{track.id}.{manifest.extension}"
                manifest_path = tmp_root / f"{track.id}.mpd"
                manifest_request = Request(manifest.url, headers=_monochrome_browser_headers())
                with urlopen(manifest_request, timeout=30) as response:
                    manifest_path.write_bytes(response.read())
                self._check_cancelled(stop_event)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "file,http,https,tcp,tls,crypto",
                    "-i",
                    str(manifest_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    # Keep the codec returned by the service.  Encoding every
                    # stream as FLAC makes an AAC/MP3 preview look lossless
                    # without restoring any quality.
                    "copy",
                    str(tmp_out),
                ]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    **windows_no_console_kwargs(),
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stdout[-2000:] or "ffmpeg failed while downloading Monochrome track")
            except Exception as manifest_error:
                if not track.isrc:
                    raise
                progress({"phase": "native", "video_id": track.id, "message": f"Full-quality Monochrome stream unavailable; trying Deezer fallback: {manifest_error}"})
                tmp_out = self._download_monochrome_deezer_fallback(track, tmp_root, progress, stop_event)
            if not tmp_out.exists() or tmp_out.stat().st_size <= 0:
                raise RuntimeError("Monochrome download produced an empty file")
            out_path = self.config.download_dir / f"{track.id}{tmp_out.suffix.lower()}"
            if out_path.exists():
                out_path.unlink()
            tmp_out.replace(out_path)
            for sibling in self.config.download_dir.glob(f"{track.id}.*"):
                if sibling == out_path or sibling.suffix.lower() not in MEDIA_EXTS:
                    continue
                try:
                    sibling.unlink()
                except OSError:
                    pass

        if out_path is None:
            raise RuntimeError("Monochrome download did not produce a media path")
        cover_path = self._download_monochrome_cover(track)
        if cover_path:
            self._embed_cover_art(out_path, cover_path)
        bitrate = self._audio_bitrate(out_path)
        quality = _audio_quality_from_hint(quality_hint) or _audio_quality_from_extension(out_path.suffix)
        return VideoEntry(
            id=track.id,
            title=track.title,
            upload_date=_date_to_yyyymmdd(track.release_date),
            author=track.author,
            source_type="monochrome",
            source_id=track.id,
            source_url=track.source_url,
            resolved_from="monochrome",
            audio_quality=quality,
            path=out_path,
            duration_seconds=track.duration_seconds,
            bitrate_kbps=round(bitrate / 1000) if bitrate else 0,
            audio_bitrate_kbps=round(bitrate / 1000) if bitrate else 0,
        )

    def _monochrome_signed_manifest(self, track_id: str) -> _MonochromeManifest:
        remote_id = track_id[3:] if track_id.startswith("mc-") else track_id
        last_error = "no streaming instance returned a usable manifest"
        for payload in self._monochrome_streaming_manifest_payloads(remote_id):
            data = payload.get("data", payload)
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            attributes = data.get("attributes", {}) if isinstance(data, dict) else {}
            presentation = str(attributes.get("trackPresentation") or "").upper()
            preview_reason = str(attributes.get("previewReason") or "").strip()
            if presentation == "PREVIEW" or preview_reason:
                last_error = f"Monochrome returned a preview manifest ({preview_reason or presentation})"
                continue
            uri = str(attributes.get("uri") or "").strip()
            if uri:
                extension, codec = _monochrome_stream_format(attributes, uri)
                return _MonochromeManifest(uri, extension=extension, codec=codec, presentation=presentation)
            last_error = "Monochrome did not return a signed track manifest"
        raise RuntimeError(f"{last_error}; try the link again later or use a different Monochrome streaming instance")

    def _monochrome_signed_manifest_url(self, track_id: str) -> str:
        """Return only the URL for callers that do not need format metadata."""
        return self._monochrome_signed_manifest(track_id).url

    def _monochrome_streaming_manifest_payloads(self, remote_id: str):
        from urllib.parse import urlencode

        bases = [
            "https://hifi.geeked.wtf",
            "https://maus.qqdl.site",
            "https://vogel.qqdl.site",
            "https://katze.qqdl.site",
            "https://hund.qqdl.site",
            "https://wolf.qqdl.site",
        ]
        params = [
            ("id", remote_id),
            ("quality", "LOSSLESS"),
            ("adaptive", "false"),
            ("formats", "FLAC"),
            ("formats", "FLAC_HIRES"),
        ]
        query = urlencode(params)
        for base in bases:
            url = f"{base}/trackManifests/?{query}"
            request = Request(url, headers={"User-Agent": APP_SLUG})
            try:
                with urlopen(request, timeout=8) as response:
                    yield json.loads(response.read().decode("utf-8"))
            except Exception:
                continue

    def _download_monochrome_cover(self, track: MonochromeTrack) -> Optional[Path]:
        if not track.cover_id:
            return None
        cover_id = track.cover_id.replace("-", "/")
        url = f"https://resources.tidal.com/images/{cover_id}/1280x1280.jpg"
        self.config.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        path = thumbnail_path(self.config.thumbnails_dir, track.id)
        if path.exists():
            return path
        request = Request(url, headers={"User-Agent": APP_SLUG})
        try:
            with urlopen(request, timeout=20) as response:
                data = response.read()
        except Exception:
            return None
        if data:
            path.write_bytes(data)
            return path
        return None

    def _embed_cover_art(self, media_path: Path, cover_path: Path) -> None:
        if not media_path.exists() or not cover_path.exists():
            return
        # Keep a real container suffix so ffmpeg does not try to infer an
        # output format from ``.flac.cover``/``.mp3.cover``.  The old name
        # silently caused embedding to fail on real ffmpeg installations.
        output = media_path.with_name(f".{media_path.stem}.cover{media_path.suffix}")
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(media_path),
                "-i",
                str(cover_path),
                "-map",
                "0:a:0",
                "-map",
                "1:v:0",
                "-c:a",
                "copy",
                "-c:v",
                "mjpeg",
                "-frames:v",
                "1",
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v:0",
                "mimetype=image/jpeg",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            **windows_no_console_kwargs(),
        )
        if proc.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            return
        output.replace(media_path)

    def _download_monochrome_deezer_fallback(
        self,
        track: MonochromeTrack,
        output_dir: Path,
        progress: ProgressCallback,
        stop_event,
    ) -> Path:
        from urllib.parse import urlencode

        url = "https://dzr.tabs-vs-spaces.wtf/stream/?" + urlencode({"isrc": track.isrc, "format": "FLAC"})
        headers = _monochrome_browser_headers()
        request = Request(url, headers=headers)
        source_path = output_dir / f"{track.id}.source"
        completed = 0
        with urlopen(request, timeout=30) as response:
            total = int(response.headers.get("Content-Length") or 0)
            content_type = str(response.headers.get("Content-Type") or "")
            with source_path.open("wb") as handle:
                while True:
                    self._check_cancelled(stop_event)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    completed += len(chunk)
                    progress(
                        {
                            "phase": "transfer",
                            "video_id": track.id,
                            "message": f"Downloading Monochrome fallback: {track.title}",
                            "active": [
                                {
                                    "title": track.title,
                                    "file": source_path.name,
                                    "status": "downloading",
                                    "completed": completed,
                                    "total": total,
                                    "speed": 0,
                                }
                            ],
                        }
                    )
        if completed <= 0:
            raise RuntimeError("Deezer fallback returned an empty stream")
        extension = _media_extension_from_content_type(content_type)
        if not extension:
            extension = _media_extension_from_bytes(source_path)
        if not extension:
            raise RuntimeError("Deezer fallback returned an unrecognized audio format")
        output = output_dir / f"{track.id}.{extension}"
        source_path.replace(output)
        return output

    def _monochrome_get(self, path: str, params: dict[str, object], streaming: bool = False) -> dict:
        from urllib.parse import urlencode

        bases = ["https://eu-central.monochrome.tf", "https://us-west.monochrome.tf", "https://api.monochrome.tf"]
        query_items: list[tuple[str, str]] = []
        for key, value in params.items():
            if isinstance(value, list):
                query_items.extend((key, str(item)) for item in value)
            else:
                query_items.append((key, str(value)))
        query = urlencode(query_items)
        endpoint = path if path.startswith("/") else f"/{path}"
        last_error: Optional[Exception] = None
        for base in bases:
            url = f"{base}{endpoint}?{query}"
            request = Request(url, headers={"User-Agent": APP_SLUG})
            try:
                with urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                last_error = exc
                if streaming:
                    continue
        raise RuntimeError(f"Monochrome API request failed: {last_error}")

    def _add_spotify_track(self, track_url: str, progress: ProgressCallback, stop_event) -> AddVideoResult:
        track = self._resolve_spotify_track(track_url)
        existing = self._get_existing_entry(track.id)
        if existing:
            return AddVideoResult(
                status="exists",
                entry=existing,
                resolved_id=existing.id,
                message=f"Already in library: {existing.title or existing.id}",
            )

        progress({"phase": "confirmed", "video_id": track.id, "message": f"Resolved Spotify track: {track.title}"})
        entry = self._download_spotify_track(track, progress, stop_event)
        self._store_entry(entry, save=True)
        return AddVideoResult(status="downloaded", entry=entry, resolved_id=entry.id, message=f"Added {entry.title}")

    def _resolve_spotify_track(self, track_url: str) -> SpotifyTrack:
        track_id = _spotify_track_id_from_url(track_url)
        if not track_id:
            raise ValueError("Spotify URL must point to a track")
        title, author = _spotify_oembed_metadata(track_url)
        if not title:
            raise RuntimeError("Could not resolve Spotify track metadata")
        return SpotifyTrack(id=f"sp-{track_id}", title=title, author=author, source_url=track_url)

    def _download_spotify_track(self, track: SpotifyTrack, progress: ProgressCallback, stop_event) -> VideoEntry:
        lossless_path = self._try_lossless_backend(track, progress, stop_event)
        if lossless_path:
            return VideoEntry(
                id=track.id,
                title=track.title,
                upload_date=track.upload_date,
                author=track.author,
                source_type="spotify",
                source_id=track.id,
                source_url=track.source_url,
                resolved_from="lossless-command",
                audio_quality=lossless_path.suffix.lower().lstrip("."),
                path=lossless_path,
            )

        bandcamp_choice = self._try_bandcamp_candidate(track, progress, stop_event)
        spotdl_choice = self._try_spotdl_download(track, progress, stop_event, candidate_suffix="spotdl")
        chosen = self._choose_best_candidate(bandcamp_choice, spotdl_choice)
        if chosen:
            final_path = self._promote_candidate(track.id, chosen["path"], discard=[choice["path"] for choice in (bandcamp_choice, spotdl_choice) if choice and choice["path"] != chosen["path"]])
            return VideoEntry(
                id=track.id,
                title=track.title,
                upload_date=track.upload_date,
                author=track.author,
                source_type="spotify",
                source_id=track.id,
                source_url=track.source_url,
                resolved_from=str(chosen["resolved_from"]),
                audio_quality=self._path_quality(final_path),
                path=final_path,
            )

        resolved = self._resolve_youtube_search(track, progress)
        if self.config.use_aria2c and aria2c_available():
            try:
                self._download_with_aria2(resolved, progress, stop_event)
            except Exception as aria_error:
                progress({"phase": "native", "message": f"aria2 fallback for {track.title}: {aria_error}"})
                self._download_with_ytdlp(str(resolved.info.get("webpage_url") or ""), resolved, progress, stop_event)
        else:
            self._download_with_ytdlp(str(resolved.info.get("webpage_url") or ""), resolved, progress, stop_event)
        return VideoEntry(
            id=track.id,
            title=track.title,
            upload_date=track.upload_date,
            author=track.author,
            source_type="spotify",
            source_id=track.id,
            source_url=track.source_url,
            resolved_from=resolved.resolved_from or "youtube-search",
            audio_quality=resolved.audio_quality or self._path_quality(self._final_entry_path(track.id)),
            path=self._final_entry_path(track.id),
        )

    def _try_bandcamp_candidate(self, track: SpotifyTrack, progress: ProgressCallback, stop_event) -> Optional[dict]:
        resolved = None
        try:
            resolved = self._resolve_bandcamp_search(track, progress)
            candidate_id = f"{track.id}__bandcamp"
            candidate_path = self._download_resolved_candidate(resolved, candidate_id, progress, stop_event)
            return {
                "path": candidate_path,
                "resolved_from": "bandcamp-search",
                "bitrate": self._audio_bitrate(candidate_path),
                "lossless": _is_lossless_ext(candidate_path.suffix),
            }
        except Exception as exc:
            if resolved is not None:
                progress({"phase": "native", "video_id": track.id, "message": f"Bandcamp fallback failed: {exc}"})
            else:
                progress({"phase": "native", "video_id": track.id, "message": f"Bandcamp search fallback unavailable: {exc}"})
            return None

    def _get_existing_entry(self, video_id: str) -> Optional[VideoEntry]:
        for candidate in legacy_alias_ids(video_id):
            existing = self.metadata.get(candidate)
            if existing:
                return existing
        return None

    def _get_existing_source_url(self, source_url: str) -> Optional[VideoEntry]:
        self.metadata.load()
        for entry in self.metadata.entries():
            if entry.source_url == source_url:
                return entry
        return None

    def _read_playlists(self) -> list[str]:
        if not self.config.playlists_path.exists():
            return []
        return [line.strip() for line in self.config.playlists_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _playlist_video_urls(self, playlist_url: str, progress: Optional[ProgressCallback] = None) -> list[str]:
        require_yt_dlp()
        info = self._extract_target_info(playlist_url, flat=True, progress=progress)
        urls: list[str] = []
        for entry in info.get("entries") or []:
            raw = entry.get("url") or entry.get("webpage_url") or entry.get("id")
            if not raw:
                continue
            if raw.startswith("http"):
                urls.append(raw)
            else:
                urls.append(f"https://www.youtube.com/watch?v={raw}")
        return urls

    def _download_one(self, video_url: str, progress: ProgressCallback, stop_event) -> VideoEntry:
        resolved = self._resolve(video_url, use_browser=False, progress=progress)
        progress({"phase": "confirmed", "video_id": resolved.id, "message": f"Confirmed available: {resolved.title}"})
        if self.config.use_aria2c and aria2c_available():
            try:
                self._download_with_aria2(resolved, progress, stop_event)
            except Exception as aria_error:
                progress({"phase": "native", "message": f"aria2 fallback for {resolved.title}: {aria_error}"})
                if self.config.browser_cookies and not resolved.used_browser_cookies:
                    try:
                        resolved = self._resolve(video_url, use_browser=True, progress=progress)
                    except Exception:
                        pass
                self._download_with_ytdlp(video_url, resolved, progress, stop_event)
        else:
            self._download_with_ytdlp(video_url, resolved, progress, stop_event)

        entry = VideoEntry(
            id=resolved.id,
            title=resolved.title or "Unknown Title",
            upload_date=resolved.upload_date or "",
            author=resolved.author or "",
            source_type=resolved.source_type or _source_type_from_id(resolved.id),
            source_id=resolved.id,
            source_url=resolved.source_url or video_url,
            resolved_from=resolved.resolved_from or _source_type_from_id(resolved.id),
            audio_quality=resolved.audio_quality or self._path_quality(self._final_entry_path(resolved.id)),
            path=self._final_entry_path(resolved.id),
        )
        maybe_prefetch_artist_art(self.config, entry, info=resolved.info)
        return entry

    def _download_direct_file(self, media_url: str, progress: ProgressCallback, stop_event) -> VideoEntry:
        parsed = urlparse(media_url)
        suffix = Path(unquote(parsed.path)).suffix.lower()
        if suffix not in MEDIA_EXTS:
            raise ValueError("Direct file URL must end with a supported media extension")
        filename = Path(unquote(parsed.path)).name or f"download{suffix}"
        title = Path(filename).stem or "Direct Download"
        progress({"phase": "confirmed", "message": f"Downloading direct file: {filename}"})

        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ytarchive-direct-", dir=str(self.config.root_dir)) as tmpdir:
            tmp_path = Path(tmpdir) / filename
            request = Request(media_url, headers={"User-Agent": APP_SLUG})
            digest = hashlib.sha256()
            completed = 0
            with urlopen(request, timeout=30) as response:
                total = int(response.headers.get("Content-Length") or 0)
                with tmp_path.open("wb") as handle:
                    while True:
                        self._check_cancelled(stop_event)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        digest.update(chunk)
                        completed += len(chunk)
                        progress(
                            {
                                "phase": "transfer",
                                "message": filename,
                                "active": [
                                    {
                                        "title": title,
                                        "file": filename,
                                        "status": "downloading",
                                        "completed": completed,
                                        "total": total,
                                        "speed": 0,
                                    }
                                ],
                            }
                        )

            if completed <= 0:
                raise RuntimeError("Direct file download produced an empty file")
            video_id = canonical_local_id(digest.hexdigest())
            out_path = self.config.download_dir / f"{video_id}{suffix}"
            if out_path.exists():
                tmp_path.unlink(missing_ok=True)
            else:
                tmp_path.replace(out_path)
            return VideoEntry(
                id=video_id,
                title=title,
                upload_date="",
                author="",
                source_type="direct",
                source_id=video_id,
                source_url=media_url,
                resolved_from="direct-file",
                audio_quality=suffix.lstrip("."),
                path=out_path,
            )

    def _resolve(
        self,
        video_url: str,
        use_browser: bool,
        progress: Optional[ProgressCallback] = None,
    ) -> ResolvedVideo:
        use_browser = use_browser or bool(self.config.browser_cookies and self.config.browser_cookies_mode == "always")
        try:
            return self._resolve_once(video_url, use_browser=use_browser, progress=progress)
        except Exception as exc:
            if (
                not use_browser
                and self.config.browser_cookies
                and self.config.browser_cookies_mode == "required"
            ):
                if progress and _is_youtube_rate_limit_text(exc):
                    progress(
                        {
                            "phase": "native",
                            "message": "Retrying YouTube extraction once with the configured browser cookies.",
                        }
                    )
                return self._resolve_once(video_url, use_browser=True, progress=progress)
            raise

    def _should_use_browser_cookies(self) -> bool:
        return bool(self.config.browser_cookies and self.config.browser_cookies_mode == "always")

    def _extract_target_info(
        self,
        target: str,
        *,
        flat: bool,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        """Extract metadata, retrying once with browser cookies when required."""
        use_browser = self._should_use_browser_cookies()
        try:
            return self._extract_target_once(target, flat=flat, use_browser=use_browser, progress=progress)
        except Exception:
            if not use_browser and self.config.browser_cookies and self.config.browser_cookies_mode == "required":
                if progress:
                    progress(
                        {
                            "phase": "native",
                            "message": "Retrying extraction once with the configured browser cookies.",
                        }
                    )
                return self._extract_target_once(target, flat=flat, use_browser=True, progress=progress)
            raise

    def _extract_target_once(
        self,
        target: str,
        *,
        flat: bool,
        use_browser: bool,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        with YoutubeDL(
            self._ydl_opts(
                skip_download=True,
                flat=flat,
                use_browser=use_browser,
                progress=progress,
                target=target,
            )
        ) as ydl:
            return self._extract_info(ydl, target)

    def _resolve_once(
        self,
        video_url: str,
        use_browser: bool,
        progress: Optional[ProgressCallback] = None,
    ) -> ResolvedVideo:
        require_yt_dlp()
        opts = self._ydl_opts(
            skip_download=True,
            flat=False,
            use_browser=use_browser,
            progress=progress,
            target=video_url,
        )
        cookie_headers: dict[str, str] = {}
        with YoutubeDL(opts) as ydl:
            info = self._extract_info(ydl, video_url)
            for fmt in info.get("requested_formats") or [info]:
                url = fmt.get("url")
                if url:
                    cookie = ydl.cookiejar.get_cookie_header(url)
                    if cookie:
                        cookie_headers[url] = cookie
        return ResolvedVideo(
            id=_resolved_entry_id(info, video_url),
            title=info.get("title") or "Unknown Title",
            upload_date=info.get("upload_date") or "",
            author=info.get("uploader") or info.get("channel") or "",
            info=info,
            cookie_headers=cookie_headers,
            used_browser_cookies=use_browser,
            source_type=_source_type_from_info(info, video_url),
            source_id=_resolved_entry_id(info, video_url),
            source_url=str(info.get("webpage_url") or video_url),
            resolved_from=_source_type_from_info(info, video_url),
            audio_quality=(info.get("ext") or ""),
        )

    def _resolve_youtube_search(self, track: SpotifyTrack, progress: ProgressCallback) -> ResolvedVideo:
        require_yt_dlp()
        if track.author:
            queries = [
                f"ytsearch{_YOUTUBE_SEARCH_LIMIT}:{track.author} - {track.title}",
                f"ytsearch{_YOUTUBE_SEARCH_LIMIT}:{track.title} {track.author} official audio",
            ]
        else:
            queries = [f"ytsearch{_YOUTUBE_SEARCH_LIMIT}:{track.title} official audio"]
        progress({"phase": "scan", "video_id": track.id, "message": f"Searching YouTube for {track.title} by {track.author}"})
        candidates: dict[str, tuple[dict, _SearchMatch, bool]] = {}
        fallback_entry: Optional[dict] = None
        selected_metadata_known = False
        for query in queries:
            info = self._extract_target_info(query, flat=False, progress=progress)
            query_had_metadata = False
            for item in info.get("entries") or []:
                if not isinstance(item, dict):
                    continue
                webpage_url = _youtube_result_url(item)
                if not webpage_url:
                    continue
                fallback_entry = fallback_entry or item
                title, author, duration = _search_result_metadata(item)
                metadata_known = bool(title or author)
                if not metadata_known:
                    continue
                query_had_metadata = True
                match = _song_match(track.title, track.author, title, author, track.duration_seconds, duration)
                current = candidates.get(webpage_url)
                if current is None or match.score > current[1].score:
                    candidates[webpage_url] = (item, match, True)
            best = max((value[1] for value in candidates.values()), default=None, key=lambda value: value.score)
            if best and best.score >= 90:
                break
            if fallback_entry is not None and not candidates:
                # Flat/mock extractors may expose only one URL. There is no
                # metadata to improve by trying the second query, so resolve
                # the first usable result immediately.
                break
            if fallback_entry is not None and not query_had_metadata:
                break

        if candidates:
            ranked_candidates = sorted(candidates.values(), key=lambda value: value[1].score, reverse=True)
            entry, match, _metadata_known = ranked_candidates[0]
            selected_metadata_known = True
            if match.score < _MATCH_MIN_SCORE:
                raise RuntimeError(
                    f"YouTube search found no confident match for '{track.title}' by '{track.author}'"
                )
        elif fallback_entry is not None:
            # Flat/mock extractors occasionally provide only a URL.  Resolve
            # that URL below; real yt-dlp search results include metadata and
            # therefore take the strict branch above.
            entry = fallback_entry
        else:
            raise RuntimeError("YouTube fallback search returned no usable result")

        resolved: Optional[ResolvedVideo] = None
        if selected_metadata_known:
            last_mismatch = ""
            for candidate_entry, candidate_match, _ in ranked_candidates:
                if candidate_match.score < _MATCH_MIN_SCORE:
                    break
                webpage_url = _youtube_result_url(candidate_entry)
                if not webpage_url:
                    continue
                try:
                    candidate_resolved = self._resolve(webpage_url, use_browser=False, progress=progress)
                except Exception as exc:
                    last_mismatch = str(exc)
                    continue
                resolved_info_title, resolved_info_author, resolved_duration = _search_result_metadata(candidate_resolved.info)
                resolved_match = _song_match(
                    track.title,
                    track.author,
                    resolved_info_title or candidate_resolved.title,
                    resolved_info_author or candidate_resolved.author,
                    track.duration_seconds,
                    resolved_duration,
                )
                if resolved_match.score < _MATCH_MIN_SCORE:
                    last_mismatch = f"YouTube result '{candidate_resolved.title}' did not match"
                    continue
                resolved = candidate_resolved
                entry = candidate_entry
                break
            if resolved is None:
                raise RuntimeError(last_mismatch or "YouTube search results could not be resolved")
        else:
            webpage_url = _youtube_result_url(entry)
            if not webpage_url:
                raise RuntimeError("YouTube fallback result had no URL")
            resolved = self._resolve(webpage_url, use_browser=False, progress=progress)
        resolved.id = track.id
        resolved.title = track.title
        resolved.author = track.author
        resolved.upload_date = track.upload_date
        resolved.source_type = "spotify"
        resolved.source_id = track.id
        resolved.source_url = track.source_url
        resolved.resolved_from = "youtube-search"
        resolved.audio_quality = (entry.get("ext") or resolved.audio_quality or "mp4")
        return resolved

    def _resolve_bandcamp_search(self, track: SpotifyTrack, progress: ProgressCallback) -> ResolvedVideo:
        progress({"phase": "scan", "video_id": track.id, "message": f"Searching Bandcamp for {track.title}"})
        candidates = _bandcamp_search_candidates(track.title, track.author)
        if not candidates:
            raise RuntimeError("Bandcamp search returned no usable result")
        mismatch_count = 0
        for _search_score, webpage_url, _candidate_title, _candidate_author in candidates:
            try:
                resolved = self._resolve(webpage_url, use_browser=False, progress=progress)
            except Exception:
                continue
            title, author, duration = _search_result_metadata(resolved.info)
            title = title or resolved.title
            author = author or resolved.author
            if title or author:
                match = _song_match(track.title, track.author, title, author, track.duration_seconds, duration)
                if match.score < _MATCH_MIN_SCORE:
                    mismatch_count += 1
                    continue
            elif _search_score < _MATCH_MIN_SCORE:
                mismatch_count += 1
                continue
            resolved.id = track.id
            resolved.title = track.title
            resolved.author = track.author
            resolved.upload_date = track.upload_date
            resolved.source_type = "spotify"
            resolved.source_id = track.id
            resolved.source_url = track.source_url
            resolved.resolved_from = "bandcamp-search"
            resolved.audio_quality = resolved.audio_quality or (resolved.info.get("ext") or "")
            return resolved
        detail = "; all candidates disagreed with the Spotify title/artist" if mismatch_count else ""
        raise RuntimeError(f"Bandcamp search returned no confident match{detail}")

    def _try_spotdl_download(self, track: SpotifyTrack, progress: ProgressCallback, stop_event, candidate_suffix: str = "spotdl") -> Optional[dict]:
        command = self._spotdl_command(track)
        if not command:
            progress({"phase": "native", "video_id": track.id, "message": "spotDL not configured or not installed; falling back to direct YouTube search"})
            return None
        self._check_cancelled(stop_event)
        progress({"phase": "scan", "video_id": track.id, "message": f"Trying spotDL for {track.title}"})
        with tempfile.TemporaryDirectory(prefix=f"ytarchive-spotdl-{track.id}-", dir=str(self.config.root_dir)) as tmpdir:
            tmp_path = Path(tmpdir)
            proc = subprocess.run(
                command,
                cwd=str(tmp_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                **windows_no_console_kwargs(),
            )
            if proc.returncode != 0:
                progress({"phase": "native", "video_id": track.id, "message": f"spotDL failed: {proc.stdout[-400:]}"})
                return None
            media_file = _largest_media_file(tmp_path)
            if not media_file:
                progress({"phase": "native", "video_id": track.id, "message": "spotDL completed but produced no media file"})
                return None
            if not self._validate_spotify_media_file(track, media_file, "spotDL"):
                progress(
                    {
                        "phase": "native",
                        "video_id": track.id,
                        "message": "spotDL produced audio whose title/artist could not be verified; discarding it",
                    }
                )
                return None
            target = self.config.download_dir / f"{track.id}__{candidate_suffix}{media_file.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(media_file), str(target))
        return {
            "path": target,
            "resolved_from": "spotdl",
            "bitrate": self._audio_bitrate(target),
            "lossless": _is_lossless_ext(target.suffix),
        }

    def _spotdl_command(self, track: SpotifyTrack) -> Optional[list[str]]:
        template = (self.config.spotdl_command or "").strip()
        if template:
            return shlex.split(
                template.format(
                    id=track.id,
                    title=track.title,
                    author=track.author,
                    url=track.source_url,
                    output_dir=str(self.config.download_dir),
                )
            )
        exe = shutil.which("spotdl")
        if not exe:
            if importlib.util.find_spec("spotdl") is None:
                return None
            return [sys.executable, "-m", "spotdl", track.source_url]
        return [exe, track.source_url]

    def _try_lossless_backend(self, track: SpotifyTrack, progress: ProgressCallback, stop_event) -> Optional[Path]:
        template = (self.config.spotify_lossless_command or "").strip()
        if not template:
            return None
        self._check_cancelled(stop_event)
        output = self.config.download_dir / f"{track.id}.flac"
        query = f"{track.title} {track.author}".strip()
        command = template.format(
            id=track.id,
            title=track.title,
            author=track.author,
            query=query,
            url=track.source_url,
            output=str(output),
        )
        progress({"phase": "scan", "video_id": track.id, "message": "Trying lossless backend..."})
        proc = subprocess.run(
            shlex.split(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            **windows_no_console_kwargs(),
        )
        if proc.returncode != 0:
            progress({"phase": "native", "video_id": track.id, "message": f"Lossless backend failed: {proc.stdout[-400:]}"})
            return None
        if output.exists():
            if not self._validate_spotify_media_file(track, output, "lossless backend"):
                progress(
                    {
                        "phase": "native",
                        "video_id": track.id,
                        "message": "Lossless backend output could not be matched to the Spotify track; discarding it",
                    }
                )
                output.unlink(missing_ok=True)
                return None
            return output
        candidates = sorted(self.config.download_dir.glob(f"{track.id}.*"))
        for candidate in candidates:
            if self._validate_spotify_media_file(track, candidate, "lossless backend"):
                return candidate
        return None

    def _validate_spotify_media_file(self, track: SpotifyTrack, path: Path, source: str) -> bool:
        """Require tags (or a strong filename match) before accepting a fallback."""
        if not path.exists() or path.stat().st_size <= 0:
            return False
        # If ffprobe is unavailable there is no reliable metadata signal.  Do
        # not accept an unverified external download just because it is
        # non-empty; the doctor command reports the missing tool and the
        # caller can fall back to a source whose metadata was resolved first.
        if not shutil.which("ffprobe"):
            return False
        try:
            tags = detect_media_tags(path)
        except Exception:
            return False
        title = tags.get("title", "")
        author = tags.get("author", "")
        if not title and not author:
            title = path.stem.replace("_", " ").replace("-", " ")
        match = _song_match(track.title, track.author, title, author)
        return match.score >= _MATCH_MIN_SCORE

    def _download_with_aria2(self, resolved: ResolvedVideo, progress: ProgressCallback, stop_event) -> None:
        formats = resolved.info.get("requested_formats") or [resolved.info]
        # aria2 only understands a single, stable file URL.  Handing it an
        # HLS/DASH manifest makes it save the playlist's short-lived
        # googlevideo fragments (often named seg.ts) instead of a video.
        # Let yt-dlp handle those protocols, including their refresh logic and
        # ffmpeg merge step.
        fragment_formats = [
            fmt
            for fmt in formats
            if str(fmt.get("protocol") or "").casefold() not in {"http", "https"}
        ]
        if fragment_formats:
            protocols = ", ".join(sorted({str(fmt.get("protocol") or "unknown") for fmt in fragment_formats}))
            raise RuntimeError(f"yt-dlp must handle fragmented stream protocol(s): {protocols}")
        urls = [fmt.get("url") for fmt in formats if fmt.get("url")]
        if not urls:
            raise RuntimeError("yt-dlp did not expose direct media URLs")

        headers_base = dict(resolved.info.get("http_headers") or {})
        with tempfile.TemporaryDirectory(prefix=f"ytarchive-{resolved.id}-", dir=str(self.config.root_dir)) as tmpdir:
            tmp_path = Path(tmpdir)
            aria = Aria2Rpc(tmp_path, max_connections=max(2, min(self.config.workers, 8)))
            aria.start()
            gids: list[tuple[str, Path]] = []
            try:
                for idx, fmt in enumerate(formats):
                    url = fmt.get("url")
                    if not url:
                        continue
                    ext = fmt.get("ext") or ("m4a" if fmt.get("vcodec") == "none" else "mp4")
                    part_name = f"{resolved.id}.{idx}.{ext}"
                    headers = dict(headers_base)
                    headers.update(fmt.get("http_headers") or {})
                    if resolved.cookie_headers.get(url):
                        headers["Cookie"] = resolved.cookie_headers[url]
                    gid = aria.add_uri(url, part_name, headers)
                    gids.append((gid, tmp_path / part_name))

                while gids:
                    self._check_cancelled(stop_event)
                    active = []
                    done: list[tuple[str, Path]] = []
                    for gid, path in gids:
                        status = aria.tell_status(gid)
                        total = int(status.get("totalLength") or 0)
                        completed = int(status.get("completedLength") or 0)
                        speed = int(status.get("downloadSpeed") or 0)
                        state = status.get("status", "")
                        active.append(
                            {
                                "video_id": resolved.id,
                                "title": resolved.title,
                                "file": path.name,
                                "status": state,
                                "completed": completed,
                                "total": total,
                                "speed": speed,
                            }
                        )
                        if state == "complete":
                            done.append((gid, path))
                        elif state == "error":
                            raise RuntimeError(status.get("errorMessage") or "aria2 transfer failed")
                    progress({"phase": "transfer", "video_id": resolved.id, "message": resolved.title, "active": active})
                    gids = [(gid, path) for gid, path in gids if (gid, path) not in done]
                    if gids:
                        time.sleep(0.25)
            finally:
                aria.stop()

            output = self.config.download_dir / f"{resolved.id}.mp4"
            downloaded_files = sorted([p for p in tmp_path.iterdir() if p.is_file() and p.name.startswith(resolved.id)])
            if len(downloaded_files) == 1:
                self._remux_or_move(downloaded_files[0], output)
            else:
                self._merge(downloaded_files, output)

    def _download_with_ytdlp(self, video_url: str, resolved: ResolvedVideo, progress: ProgressCallback, stop_event) -> None:
        require_yt_dlp()
        def hook(data: dict) -> None:
            self._check_cancelled(stop_event)
            if data.get("status") == "downloading":
                progress(
                    {
                        "phase": "transfer",
                        "video_id": resolved.id,
                        "message": resolved.title,
                        "active": [
                            {
                                "video_id": resolved.id,
                                "title": resolved.title,
                                "file": Path(data.get("filename", "")).name,
                                "status": "downloading",
                                "completed": data.get("downloaded_bytes") or 0,
                                "total": data.get("total_bytes") or data.get("total_bytes_estimate") or 0,
                                "speed": data.get("speed") or 0,
                            }
                        ],
                    }
                )

        # Keep yt-dlp's fragments, sidecars, and temporary files outside the
        # library.  A failed merge must not leave a fragment that is later
        # mistaken for the downloaded video.
        with tempfile.TemporaryDirectory(prefix=f"ytarchive-ytdlp-{resolved.id}-", dir=str(self.config.root_dir)) as tmpdir:
            tmp_path = Path(tmpdir)
            opts = self._ydl_opts(
                skip_download=False,
                flat=False,
                use_browser=resolved.used_browser_cookies,
                progress=progress,
                target=video_url,
            )
            opts["progress_hooks"] = [hook]
            opts["outtmpl"] = str(tmp_path / f"{resolved.id}.%(ext)s")
            with YoutubeDL(opts) as ydl:
                ydl.download([video_url])

            completed = self._completed_ytdlp_media(tmp_path, resolved.id)
            if completed is None:
                raise RuntimeError("yt-dlp did not produce a completed, merged media file")
            self.config.download_dir.mkdir(parents=True, exist_ok=True)
            destination = self.config.download_dir / f"{resolved.id}{completed.suffix.lower()}"
            completed.replace(destination)

    def _final_entry_path(self, video_id: str) -> Path:
        indexed = build_file_index(self.config.download_dir).get(video_id)
        if indexed:
            return indexed
        for path in sorted(self.config.download_dir.glob(f"{video_id}.*")):
            if path.is_file() and path.suffix.lower() in MEDIA_EXTS and not re.search(r"\.f\d+\.", path.name):
                return path
        return self.config.download_dir / f"{video_id}.mp4"

    @staticmethod
    def _completed_ytdlp_media(directory: Path, video_id: str) -> Optional[Path]:
        """Return the merged yt-dlp result, never a format fragment or part."""
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() in MEDIA_EXTS
            and path.name.startswith(f"{video_id}.")
            and not re.search(r"\.f\d+\.", path.name)
            and not path.name.endswith((".part", ".ytdl"))
            and path.stat().st_size > 0
        ]
        if not candidates:
            return None
        # yt-dlp is configured to remux to MP4, but retain a valid media file
        # if a site only permits another supported container.
        return sorted(candidates, key=lambda path: (path.suffix.lower() != ".mp4", -path.stat().st_size, path.name))[0]

    @staticmethod
    def _path_quality(path: Path) -> str:
        return path.suffix.lower().lstrip(".") if path.suffix else ""

    def _download_resolved_candidate(self, resolved: ResolvedVideo, candidate_id: str, progress: ProgressCallback, stop_event) -> Path:
        original_id = resolved.id
        resolved.id = candidate_id
        try:
            if self.config.use_aria2c and aria2c_available():
                try:
                    self._download_with_aria2(resolved, progress, stop_event)
                except Exception as aria_error:
                    progress({"phase": "native", "video_id": original_id, "message": f"aria2 fallback for {resolved.title}: {aria_error}"})
                    self._download_with_ytdlp(str(resolved.info.get("webpage_url") or ""), resolved, progress, stop_event)
            else:
                self._download_with_ytdlp(str(resolved.info.get("webpage_url") or ""), resolved, progress, stop_event)
            return self._final_entry_path(candidate_id)
        finally:
            resolved.id = original_id

    def _choose_best_candidate(self, bandcamp_choice: Optional[dict], spotdl_choice: Optional[dict]) -> Optional[dict]:
        choices = [choice for choice in (bandcamp_choice, spotdl_choice) if choice]
        if not choices:
            return None
        choices.sort(key=lambda choice: (1 if choice["lossless"] else 0, int(choice["bitrate"] or 0)), reverse=True)
        return choices[0]

    def _promote_candidate(self, final_id: str, chosen: Path, discard: list[Path]) -> Path:
        final_path = self.config.download_dir / f"{final_id}{chosen.suffix.lower()}"
        if chosen != final_path:
            if final_path.exists():
                final_path.unlink()
            chosen.replace(final_path)
        for loser in discard:
            try:
                if loser.exists():
                    loser.unlink()
            except OSError:
                pass
        for sibling in self.config.download_dir.glob(f"{final_id}__*"):
            try:
                sibling.unlink()
            except OSError:
                pass
        return final_path

    def _audio_bitrate(self, path: Path) -> int:
        info = ffprobe_json(path) or {}
        try:
            if info.get("format", {}).get("bit_rate"):
                return int(info["format"]["bit_rate"])
        except (TypeError, ValueError):
            pass
        for stream in info.get("streams", []):
            if stream.get("codec_type") != "audio":
                continue
            try:
                return int(stream.get("bit_rate") or 0)
            except (TypeError, ValueError):
                continue
        return 0

    def _ydl_opts(
        self,
        skip_download: bool,
        flat: bool,
        use_browser: bool,
        progress: Optional[ProgressCallback] = None,
        target: Optional[str] = None,
    ) -> dict:
        youtube_target = _is_youtube_target(target or "")
        opts = {
            "logger": QuietLogger(progress),
            "quiet": True,
            "no_warnings": True,
            "skip_download": skip_download,
            "extract_flat": flat,
            "format": "bestvideo[height<=360]+bestaudio/best",
            "merge_output_format": "mp4",
            "remux_video": "mp4",
            "concurrent_fragment_downloads": max(1, min(self.config.workers, 16)),
            "source_address": "0.0.0.0",
        }
        if youtube_target:
            opts["sleep_interval_requests"] = YOUTUBE_SLEEP_REQUESTS
        runtime, runtime_path = _yt_dlp_js_runtime_details()
        if runtime_path:
            opts["js_runtimes"] = {runtime: {"path": runtime_path}}
        elif runtime and runtime != "deno":
            # yt-dlp enables Deno by default, but other supported runtimes
            # must be explicitly selected through the Python API.
            opts["js_runtimes"] = {runtime: {}}
        if use_browser and self.config.browser_cookies:
            opts["cookiesfrombrowser"] = self.config.browser_cookies
        # Cookie-file loading is temporarily disabled while its 403 failure
        # path is isolated. Browser-cookie loading remains supported.
        if not use_browser:
            opts["nocookies"] = True
        if not skip_download:
            opts.update({
                "writethumbnail": True,
                "embedthumbnail": True,
                "addmetadata": True,
                "postprocessors": [
                    {"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True},
                    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
                ],
            })
            if youtube_target:
                opts.update(
                    {
                        "sleep_interval": YOUTUBE_SLEEP_INTERVAL,
                        "max_sleep_interval": YOUTUBE_MAX_SLEEP_INTERVAL,
                    }
                )
        return opts

    def _extract_info(self, ydl, target: str) -> dict:
        """Extract a target while pacing concurrent YouTube API requests."""
        if not _is_youtube_target(target):
            return ydl.extract_info(target, download=False)

        with self._youtube_extract_lock:
            delay = self._youtube_extract_next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                return ydl.extract_info(target, download=False)
            finally:
                self._youtube_extract_next_at = time.monotonic() + YOUTUBE_SLEEP_REQUESTS

    def _merge(self, parts: list[Path], output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y"]
        for part in parts:
            cmd += ["-i", str(part)]
        cmd += ["-c", "copy", "-movflags", "+faststart", str(output)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            **windows_no_console_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout[-2000:])

    def _remux_or_move(self, src: Path, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".mp4":
            src.replace(output)
            return
        cmd = ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(output)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            **windows_no_console_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout[-2000:])

    @staticmethod
    def _check_cancelled(stop_event) -> None:
        if stop_event.is_set():
            raise DownloadCancelled()


def _video_id_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        raw = url.strip()
        parts = split_prefixed_id(raw)
        if parts and parts[0] == "yt" and is_youtube_raw_id(parts[1]):
            return parts[1]
        return raw if is_youtube_raw_id(raw) else None
    host = _parsed_hostname(parsed)
    if host not in YOUTUBE_HOSTS:
        return None
    if host in {"youtu.be", "www.youtu.be"}:
        parts = [part for part in parsed.path.split("/") if part]
        return parts[0] if len(parts) == 1 and is_youtube_raw_id(parts[0]) else None
    if host in YOUTUBE_HOSTS:
        query_id = parse_qs(parsed.query).get("v", [])
        if query_id and is_youtube_raw_id(query_id[0]):
            return query_id[0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] in {"shorts", "embed", "live", "v"} and is_youtube_raw_id(parts[1]):
            return parts[1]
    return None


def _canonical_submitted_id(url: str) -> str:
    """Return the identity used by queue bookkeeping for a submitted URL."""
    video_id = _video_id_from_url(url)
    if video_id:
        return canonical_youtube_id(video_id)
    return url.strip()


def normalize_media_input(raw_input: str) -> str:
    value = (raw_input or "").strip()
    if not value:
        raise ValueError("Enter a YouTube, SoundCloud, Spotify, Bandcamp, Monochrome, or direct media file link, or a YouTube video ID")
    if is_youtube_raw_id(value):
        return f"https://www.youtube.com/watch?v={value}"
    if "://" not in value:
        direct_url = f"https://{value}"
        direct_parsed = urlparse(direct_url)
        if _is_supported_media_url(direct_parsed) or _is_direct_media_url(direct_parsed):
            return direct_url
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if _is_supported_media_url(parsed) or _is_direct_media_url(parsed):
            return value
    raise ValueError("Input must be a YouTube, SoundCloud, Spotify, Bandcamp, Monochrome, or direct media file link, or an 11-character YouTube ID")


def is_youtube_media_input(raw_input: str) -> bool:
    """Return whether an Add New input explicitly needs YouTube extraction."""
    try:
        normalized = normalize_media_input(raw_input)
    except ValueError:
        return False
    parsed = urlparse(normalized)
    return not _is_direct_media_url(parsed) and _is_youtube_target(normalized)


def _resolved_entry_id(info: dict, video_url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    raw_id = str(info.get("id") or "").strip()
    if "soundcloud" in extractor:
        if raw_id:
            return canonical_soundcloud_id(raw_id)
        return canonical_soundcloud_id(_hash_text(info.get("webpage_url") or video_url))
    if "bandcamp" in extractor:
        if raw_id:
            return canonical_bandcamp_id(raw_id)
        return canonical_bandcamp_id(_hash_text(info.get("webpage_url") or video_url))
    return canonical_youtube_id(raw_id or _video_id_from_url(video_url) or "")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _is_supported_media_url(parsed) -> bool:
    host = _parsed_hostname(parsed)
    if host in YOUTUBE_HOSTS:
        return _video_id_from_url(parsed.geturl()) is not None
    if _is_monochrome_url(parsed.geturl()):
        return True
    if host in SPOTIFY_HOSTS:
        return _spotify_track_id_from_url(parsed.geturl()) is not None
    return host in SOUNDCLOUD_HOSTS or host == "bandcamp.com" or host == "www.bandcamp.com" or host.endswith(".bandcamp.com")


def _parsed_hostname(parsed) -> str:
    try:
        return (parsed.hostname or "").casefold()
    except ValueError:
        return ""


def _is_direct_media_url(parsed) -> bool:
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return Path(unquote(parsed.path)).suffix.lower() in MEDIA_EXTS


def _is_spotify_url(url: str) -> bool:
    return _spotify_track_id_from_url(url) is not None


def _is_monochrome_url(url: str) -> bool:
    try:
        _monochrome_item_from_url(url)
        return True
    except ValueError:
        return False


def _monochrome_item_from_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = _parsed_hostname(parsed)
    if host not in {"monochrome.tf", "www.monochrome.tf"}:
        raise ValueError("Not a Monochrome URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].casefold() not in {"track", "album"} or not parts[1].isdigit():
        raise ValueError("Monochrome URL must point to a track or album")
    return parts[0].casefold(), parts[1]


def _monochrome_track_from_payload(raw: dict, source_url: str) -> MonochromeTrack:
    track_id = str(raw.get("id") or raw.get("trackId") or "").strip()
    if not track_id:
        raise RuntimeError("Monochrome track metadata did not include an ID")
    artists = raw.get("artists") if isinstance(raw.get("artists"), list) else []
    artist = raw.get("artist") if isinstance(raw.get("artist"), dict) else {}
    author = ", ".join(str(item.get("name") or "").strip() for item in artists if isinstance(item, dict) and item.get("name"))
    if not author:
        author = str(artist.get("name") or "").strip()
    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    duration = raw.get("duration") or 0
    try:
        duration_seconds = float(duration or 0)
    except (TypeError, ValueError):
        duration_seconds = 0.0
    return MonochromeTrack(
        id=f"mc-{track_id}",
        title=str(raw.get("title") or track_id).strip(),
        author=author,
        source_url=source_url or f"https://monochrome.tf/track/{track_id}",
        album_title=str(album.get("title") or "").strip(),
        release_date=str(album.get("releaseDate") or raw.get("streamStartDate") or "").strip(),
        duration_seconds=duration_seconds,
        audio_quality=str(raw.get("audioQuality") or "flac").lower(),
        cover_id=str(album.get("cover") or raw.get("cover") or "").strip(),
        isrc=str(raw.get("isrc") or "").strip(),
    )


def _monochrome_browser_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Origin": "https://monochrome.tf",
        "Referer": "https://monochrome.tf/",
    }


def _audio_quality_from_extension(extension: str) -> str:
    """Expose the actual stored codec/container as the quality label."""
    normalized = str(extension or "").lower().lstrip(".")
    return {
        "m4a": "aac",
        "mp4": "aac",
        "oga": "vorbis",
        "ogg": "vorbis",
    }.get(normalized, normalized)


def _audio_quality_from_hint(hint: str) -> str:
    value = str(hint or "").casefold()
    if "flac" in value:
        return "flac"
    if "alac" in value:
        return "alac"
    if "opus" in value:
        return "opus"
    if "vorbis" in value:
        return "vorbis"
    if "mp3" in value or "mpeg" in value:
        return "mp3"
    if "aac" in value:
        return "aac"
    if "wav" in value or "pcm" in value:
        return "wav"
    return ""


def _media_extension_from_content_type(content_type: str) -> str:
    value = str(content_type or "").casefold()
    if "flac" in value:
        return "flac"
    if "mpeg" in value or "mp3" in value:
        return "mp3"
    if "opus" in value:
        return "opus"
    if "ogg" in value or "vorbis" in value:
        return "ogg"
    if "wav" in value or "wave" in value or "pcm" in value:
        return "wav"
    if "mp4" in value or "m4a" in value or "aac" in value:
        return "m4a"
    return ""


def _media_extension_from_bytes(path: Path) -> str:
    try:
        header = path.read_bytes()[:64]
    except OSError:
        return ""
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"ID3") or _looks_like_mp3_frame(header):
        return "mp3"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "m4a"
    return ""


def _looks_like_mp3_frame(header: bytes) -> bool:
    if len(header) < 2:
        return False
    return header[0] == 0xFF and (header[1] & 0xE0) == 0xE0


def _monochrome_stream_format(attributes: dict, manifest_url: str) -> tuple[str, str]:
    """Translate service format hints into a safe output suffix."""
    explicit_values: list[str] = []
    for key in ("codec", "codecs", "audioCodec", "mimeType", "contentType", "container"):
        value = attributes.get(key)
        if isinstance(value, (list, tuple)):
            explicit_values.extend(str(item).casefold() for item in value if item)
        elif value:
            explicit_values.append(str(value).casefold())
    explicit_hint = " ".join(explicit_values)
    explicit_extension = _audio_extension_from_hint(explicit_hint)
    if explicit_extension:
        return explicit_extension, explicit_hint

    values: list[str] = []
    formats = attributes.get("formats") or attributes.get("format") or []
    if isinstance(formats, str):
        formats = [formats]
    if isinstance(formats, (list, tuple)):
        for value in formats:
            if isinstance(value, dict):
                value = value.get("format") or value.get("name") or value.get("codec") or ""
            if value:
                values.append(str(value).casefold())
    hint = " ".join(values)
    extension = _audio_extension_from_hint(hint)
    if extension:
        return extension, hint
    # The request asks for FLAC first, so an instance that omits the hint is
    # still expected to return FLAC.  ``copy`` below will fail loudly if it
    # violates that contract instead of silently transcoding it.
    return "flac", hint


def _audio_extension_from_hint(hint: str) -> str:
    value = str(hint or "").casefold()
    if "flac" in value:
        return "flac"
    if "alac" in value:
        return "m4a"
    if "opus" in value:
        return "opus"
    if "vorbis" in value or "ogg" in value:
        return "ogg"
    if "mp3" in value or "mpeg" in value:
        return "mp3"
    if "aac" in value or "mp4" in value or "m4a" in value or "mp4a" in value:
        return "m4a"
    if "wav" in value or "pcm" in value:
        return "wav"
    return ""


def _date_to_yyyymmdd(value: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value or "")
    if match:
        return "".join(match.groups())
    return ""


def _spotify_track_id_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if _parsed_hostname(parsed) not in SPOTIFY_HOSTS:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "track":
            track_id = parts[index + 1]
            return track_id if SPOTIFY_TRACK_ID_RE.fullmatch(track_id) else None
    return None


def _spotify_oembed_metadata(track_url: str) -> tuple[str, str]:
    request = Request(
        f"https://open.spotify.com/oembed?url={quote(track_url, safe='')}",
        headers={"User-Agent": APP_SLUG},
    )
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    title = str(payload.get("title") or "").strip()
    author = str(payload.get("author_name") or "").strip()
    return title, author


def _search_result_metadata(item: object) -> tuple[str, str, float]:
    """Extract comparable title/artist/duration fields from result data."""
    if not isinstance(item, dict):
        return "", "", 0.0
    title_value: object = item.get("title") or item.get("name") or item.get("track") or ""
    if isinstance(title_value, dict):
        title_value = title_value.get("name") or title_value.get("title") or ""
    title = str(title_value or "").strip()

    author_value: object = ""
    for key in (
        "artist", "artist_name", "artistName", "uploader", "channel", "creator",
        "band_name", "album_artist", "albumArtist",
    ):
        value = item.get(key)
        if value:
            author_value = value
            break
    if isinstance(author_value, dict):
        author_value = author_value.get("name") or author_value.get("title") or ""
    elif isinstance(author_value, list):
        author_value = ", ".join(
            str(value.get("name") or value.get("title") or "").strip()
            if isinstance(value, dict)
            else str(value).strip()
            for value in author_value
            if value
        )
    author = str(author_value or "").strip()

    duration_value = item.get("duration") or item.get("duration_seconds") or item.get("durationSeconds") or 0
    try:
        duration = max(0.0, float(duration_value or 0))
    except (TypeError, ValueError):
        duration = 0.0
    return title, author, duration


def _youtube_result_url(item: dict) -> Optional[str]:
    value = str(item.get("webpage_url") or item.get("original_url") or item.get("url") or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value if _parsed_hostname(urlparse(value)) in YOUTUBE_HOSTS else None
    raw_id = value.split("&", 1)[0].split("?", 1)[0]
    if is_youtube_raw_id(raw_id):
        return f"https://www.youtube.com/watch?v={raw_id}"
    return None


def _normalise_match_tokens(value: str, *, remove_noise: bool = False) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    tokens = tuple(MATCH_TOKEN_RE.findall(folded))
    if remove_noise:
        filtered = tuple(token for token in tokens if token not in _QUERY_NOISE_TOKENS)
        if filtered:
            return filtered
    return tokens


def _normalise_match_text(value: str, *, remove_noise: bool = False) -> str:
    return " ".join(_normalise_match_tokens(value, remove_noise=remove_noise))


def _field_match_score(expected: str, actual: str, *, actual_is_title: bool = False) -> float:
    expected_text = _normalise_match_text(expected)
    actual_text = _normalise_match_text(actual)
    if not expected_text or not actual_text:
        return 0.0
    if expected_text == actual_text:
        return 1.0
    actual_core = _normalise_match_text(actual, remove_noise=actual_is_title)
    if expected_text == actual_core:
        return 1.0
    if actual_text in expected_text:
        return 0.86
    expected_tokens = set(_normalise_match_tokens(expected))
    actual_tokens = set(_normalise_match_tokens(actual, remove_noise=actual_is_title))
    if not expected_tokens or not actual_tokens:
        return 0.0
    overlap = len(expected_tokens & actual_tokens)
    precision = overlap / len(actual_tokens)
    recall = overlap / len(expected_tokens)
    f1 = (2 * precision * recall) / (precision + recall) if precision + recall else 0.0
    ratio = SequenceMatcher(None, expected_text, actual_core or actual_text).ratio()
    return max(f1, ratio)


def _song_match(
    expected_title: str,
    expected_author: str,
    actual_title: str,
    actual_author: str,
    expected_duration: float = 0.0,
    actual_duration: float = 0.0,
) -> _SearchMatch:
    title_value = _remove_artist_tokens_from_title(
        actual_title,
        expected_author,
        protected_tokens=set(_normalise_match_tokens(expected_title)),
    )
    title_score = _field_match_score(expected_title, title_value, actual_is_title=True)
    if expected_author:
        direct_artist_score = _artist_match_score(expected_author, actual_author)
        # YouTube often puts the artist in the title while its uploader is a
        # label, topic channel, or distributor.
        title_artist_score = _field_match_score(expected_author, actual_title, actual_is_title=True)
        artist_score = max(direct_artist_score, title_artist_score)
        score = title_score * 63.0 + artist_score * 37.0
        expected_title_tokens = _normalise_match_tokens(expected_title)
        title_floor = 0.78 if len(expected_title_tokens) <= 1 else 0.62
        if title_score < title_floor or artist_score < 0.62:
            score *= 0.55
    else:
        artist_score = 1.0
        score = title_score * 100.0

    duration_score = 0.0
    if expected_duration > 0 and actual_duration > 0:
        relative_delta = abs(expected_duration - actual_duration) / max(expected_duration, 1.0)
        if relative_delta <= 0.03:
            duration_score = 1.0
        elif relative_delta <= 0.08:
            duration_score = 0.75
        elif relative_delta <= 0.18:
            duration_score = 0.35
        else:
            score -= min(15.0, relative_delta * 20.0)
        score += duration_score * 5.0
    return _SearchMatch(max(0.0, min(100.0, score)), title_score, artist_score, duration_score)


def _remove_artist_tokens_from_title(
    title: str,
    artist: str,
    *,
    protected_tokens: set[str] = frozenset(),
) -> str:
    artist_tokens = set(_normalise_match_tokens(artist))
    if not artist_tokens:
        return title
    title_tokens = _normalise_match_tokens(title)
    if artist_tokens & protected_tokens:
        return title
    if title_tokens[: len(artist_tokens)] == tuple(_normalise_match_tokens(artist)):
        return " ".join(title_tokens[len(artist_tokens) :]) or title
    if title_tokens[-len(artist_tokens) :] == tuple(_normalise_match_tokens(artist)):
        return " ".join(title_tokens[: -len(artist_tokens)]) or title
    return title


def _artist_match_score(expected: str, actual: str) -> float:
    """Score artist identity without treating ``Other Artist`` as ``Artist``."""
    expected_tokens = set(_normalise_match_tokens(expected, remove_noise=True))
    actual_tokens = set(_normalise_match_tokens(actual, remove_noise=True))
    if not expected_tokens or not actual_tokens:
        return 0.0
    if expected_tokens == actual_tokens:
        return 1.0
    if expected_tokens.issubset(actual_tokens):
        # Extra non-noise words usually identify a different artist.  A title
        # can still rescue legitimate featured-artist/uploader variants.
        return max(0.0, 0.52 - 0.08 * len(actual_tokens - expected_tokens))
    if actual_tokens.issubset(expected_tokens):
        return 0.78
    return _field_match_score(expected, actual)


def _bandcamp_search_candidates(title: str, author: str) -> list[tuple[float, str, str, str]]:
    """Return ranked Bandcamp tracks; never silently choose result one."""
    query_text = f"{title} {author}".strip()
    api_url = f"https://bandcamp.com/api/fuzzysearch/1/app_autocomplete?q={quote(query_text, safe='')}"
    request = Request(
        api_url,
        headers={"User-Agent": f"Mozilla/5.0 (X11; Linux x86_64) {APP_SLUG}"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        candidates: list[tuple[float, str, str, str]] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "t":
                continue
            url = _normalize_bandcamp_url(str(item.get("url") or ""))
            if not url:
                continue
            score = _bandcamp_result_score(title, author, item)
            candidates.append(
                (
                    score,
                    url,
                    str(item.get("name") or "").strip(),
                    str(item.get("band_name") or "").strip(),
                )
            )
        if candidates:
            candidates.sort(key=lambda value: (-value[0], value[1]))
            return candidates
    except Exception:
        pass

    query = quote(query_text, safe="")
    request = Request(
        f"https://bandcamp.com/search?q={query}",
        headers={"User-Agent": APP_SLUG},
    )
    try:
        with urlopen(request, timeout=10) as response:
            page = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    blocks = re.findall(r'(<li\b[^>]*class="[^"]*searchresult[^"]*"[\s\S]*?</li>)', page, flags=re.IGNORECASE)
    candidates: list[tuple[float, str, str, str]] = []
    for block in blocks:
        if "/track/" not in block:
            continue
        url_match = re.search(r'href="([^"]+/track/[^"]+)"', block, flags=re.IGNORECASE)
        if url_match:
            candidate_url = _normalize_bandcamp_url(url_match.group(1))
            if not candidate_url:
                continue
            candidate_title = _html_fragment_text(
                re.search(r'class="[^"]*(?:result-title|track-title)[^"]*"[^>]*>(.*?)</', block, flags=re.IGNORECASE)
            )
            candidate_author = _html_fragment_text(
                re.search(r'class="[^"]*(?:result-artist|artist)[^"]*"[^>]*>(.*?)</', block, flags=re.IGNORECASE)
            )
            score = _song_match(title, author, candidate_title, candidate_author).score if candidate_title else 0.0
            candidates.append((score, candidate_url, candidate_title, candidate_author))
    if candidates:
        return candidates

    fallback = re.search(r'href="([^"]+/track/[^"]+)"', page, flags=re.IGNORECASE)
    if fallback:
        url = _normalize_bandcamp_url(fallback.group(1))
        return [(0.0, url, "", "")] if url else []
    return []


def _bandcamp_search_track_url(title: str, author: str) -> Optional[str]:
    candidates = _bandcamp_search_candidates(title, author)
    if not candidates:
        return None
    score, url, _candidate_title, _candidate_author = candidates[0]
    return url if score >= _MATCH_MIN_SCORE else None


def _normalize_bandcamp_url(value: str) -> str:
    value = _html_unescape(value.strip())
    if not value:
        return ""
    matches = list(re.finditer(r"https?://", value))
    if len(matches) > 1:
        value = value[matches[-1].start() :]
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("/"):
        value = f"https://bandcamp.com{value}"
    parsed = urlparse(value)
    host = _parsed_hostname(parsed)
    parts = [part for part in parsed.path.split("/") if part]
    if not host or not (host == "bandcamp.com" or host.endswith(".bandcamp.com")):
        return ""
    return value if len(parts) >= 2 and parts[0].casefold() == "track" else ""


def _bandcamp_result_score(title: str, author: str, item: dict) -> float:
    result_title = str(item.get("name") or item.get("title") or "")
    result_author = str(item.get("band_name") or item.get("artist") or "")
    return _song_match(title, author, result_title, result_author).score


def _norm_match_text(value: str) -> str:
    return _normalise_match_text(value)


def _html_unescape(value: str) -> str:
    return html.unescape(value)


def _html_fragment_text(match: Optional[re.Match[str]]) -> str:
    if not match:
        return ""
    fragment = re.sub(r"<[^>]+>", " ", _html_unescape(match.group(1)))
    return " ".join(fragment.split())


def _source_type_from_id(value: str) -> str:
    if value.startswith("yt-") or is_youtube_raw_id(value):
        return "youtube"
    if value.startswith("sc-"):
        return "soundcloud"
    if value.startswith("bc-"):
        return "bandcamp"
    if value.startswith("sp-"):
        return "spotify"
    if value.startswith("local-"):
        return "local"
    return ""


def _source_type_from_info(info: dict, video_url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    if "soundcloud" in extractor:
        return "soundcloud"
    if "bandcamp" in extractor:
        return "bandcamp"
    if "youtube" in extractor:
        return "youtube"
    return _source_type_from_id(_resolved_entry_id(info, video_url))


def _largest_media_file(root: Path) -> Optional[Path]:
    candidates = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTS]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: (path.stat().st_size, path.name), reverse=True)[0]


def _is_lossless_ext(ext: str) -> bool:
    return (ext or "").lower().lstrip(".") in {"flac", "wav", "aiff", "alac"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
