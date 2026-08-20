from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import APP_SLUG
from .config import AppConfig
from .metadata import VideoEntry


FAILURE_TTL_SECONDS = 24 * 60 * 60
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_STORE_LOCKS: dict[Path, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


@dataclass
class ArtistProfile:
    id: str
    name: str
    image_path: str = ""
    source_url: str = ""
    source_provider: str = ""
    source_entry_id: str = ""
    fetched_at: str = ""
    failed_at: str = ""
    failure: str = ""

    @classmethod
    def from_json(cls, artist_id: str, data: dict) -> "ArtistProfile":
        return cls(
            id=artist_id,
            name=str(data.get("name", "")).strip(),
            image_path=str(data.get("image_path", "")).strip(),
            source_url=str(data.get("source_url", "")).strip(),
            source_provider=str(data.get("source_provider", "")).strip(),
            source_entry_id=str(data.get("source_entry_id", "")).strip(),
            fetched_at=str(data.get("fetched_at", "")).strip(),
            failed_at=str(data.get("failed_at", "")).strip(),
            failure=str(data.get("failure", "")).strip(),
        )

    def to_json(self) -> dict[str, str]:
        data = {"name": self.name}
        for key in ("image_path", "source_url", "source_provider", "source_entry_id", "fetched_at", "failed_at", "failure"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


class ArtistProfileStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = _store_lock(path)

    def load(self) -> dict[str, ArtistProfile]:
        with self._lock:
            return self._load_unlocked()

    def save(self, profiles: dict[str, ArtistProfile]) -> None:
        with self._lock:
            self._save_unlocked(profiles)

    def get(self, artist_id: str) -> Optional[ArtistProfile]:
        return self.load().get(artist_id)

    def upsert(self, profile: ArtistProfile) -> None:
        with self._lock:
            profiles = self._load_unlocked()
            profiles[profile.id] = profile
            self._save_unlocked(profiles)

    def clear_failures(self) -> int:
        with self._lock:
            profiles = self._load_unlocked()
            cleared = 0
            for profile in profiles.values():
                if profile.failed_at or profile.failure:
                    profile.failed_at = ""
                    profile.failure = ""
                    cleared += 1
            if cleared:
                self._save_unlocked(profiles)
            return cleared

    def _load_unlocked(self) -> dict[str, ArtistProfile]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            artist_id: ArtistProfile.from_json(artist_id, data)
            for artist_id, data in raw.items()
            if isinstance(data, dict)
        }

    def _save_unlocked(self, profiles: dict[str, ArtistProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {artist_id: profile.to_json() for artist_id, profile in sorted(profiles.items())}
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def _store_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[resolved] = lock
        return lock


def artist_id_for_name(name: str) -> str:
    value = (name or "Unknown Artist").strip() or "Unknown Artist"
    return "artist:" + hashlib.sha1(value.casefold().encode("utf-8")).hexdigest()[:16]


def artist_image_path(config: AppConfig, artist_id: str) -> Optional[Path]:
    profile = ArtistProfileStore(config.artist_profiles_path).get(artist_id)
    if not profile or not profile.image_path:
        return None
    path = Path(profile.image_path)
    if not path.is_absolute():
        path = config.root_dir / path
    return path if path.exists() and path.is_file() else None


def ensure_artist_art(config: AppConfig, artist_name: str, entries: list[VideoEntry], *, info: Optional[dict] = None) -> Optional[Path]:
    artist_name = (artist_name or "").strip()
    if not artist_name:
        return None
    artist_id = artist_id_for_name(artist_name)
    store = ArtistProfileStore(config.artist_profiles_path)
    profile = store.get(artist_id) or ArtistProfile(id=artist_id, name=artist_name)
    cached = artist_image_path(config, artist_id)
    if cached:
        return cached
    if _failure_is_fresh(profile):
        return None

    representative = _representative_entry(entries)
    image_url = _preferred_image_url_from_info(info)
    provider = str((info or {}).get("extractor_key") or (info or {}).get("extractor") or "").casefold()

    if not image_url and representative:
        image_url, provider = _lookup_provider_artist_image(representative)
    if not image_url:
        _record_failure(store, profile, representative, "No provider artist image found")
        return None

    path = _download_artist_image(config, artist_id, image_url)
    if not path:
        _record_failure(store, profile, representative, f"Could not download {image_url}")
        return None

    profile.name = artist_name
    profile.image_path = str(path)
    profile.source_url = image_url
    profile.source_provider = provider
    profile.source_entry_id = representative.id if representative else ""
    profile.fetched_at = _now()
    profile.failed_at = ""
    profile.failure = ""
    store.upsert(profile)
    return path


def maybe_prefetch_artist_art(config: AppConfig, entry: VideoEntry, *, info: Optional[dict] = None) -> None:
    try:
        ensure_artist_art(config, entry.author, [entry], info=info)
    except Exception:
        return


def _representative_entry(entries: list[VideoEntry]) -> Optional[VideoEntry]:
    if not entries:
        return None
    return sorted(entries, key=lambda entry: (_entry_mtime(entry), entry.title.casefold(), entry.id), reverse=True)[0]


def _entry_mtime(entry: VideoEntry) -> float:
    if not entry.path:
        return 0.0
    try:
        return entry.path.stat().st_mtime
    except OSError:
        return 0.0


def _failure_is_fresh(profile: ArtistProfile) -> bool:
    if not profile.failed_at:
        return False
    try:
        failed = dt.datetime.fromisoformat(profile.failed_at)
    except ValueError:
        return False
    if failed.tzinfo is None:
        failed = failed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - failed).total_seconds() < FAILURE_TTL_SECONDS


def _record_failure(store: ArtistProfileStore, profile: ArtistProfile, entry: Optional[VideoEntry], message: str) -> None:
    profile.source_entry_id = entry.id if entry else profile.source_entry_id
    profile.failed_at = _now()
    profile.failure = message[:500]
    store.upsert(profile)


def _lookup_provider_artist_image(entry: VideoEntry) -> tuple[str, str]:
    source_url = _entry_source_url(entry)
    if not source_url:
        return "", entry.source_type
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        return "", entry.source_type
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False, "playlistend": 1}) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception:
        return "", entry.source_type

    image_url = _preferred_image_url_from_info(info)
    provider = str(info.get("extractor_key") or info.get("extractor") or entry.source_type).casefold()
    if image_url:
        return image_url, provider

    channel_url = str(info.get("channel_url") or info.get("uploader_url") or "").strip()
    if channel_url:
        try:
            with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True, "playlistend": 0}) as ydl:
                channel_info = ydl.extract_info(channel_url, download=False)
        except Exception:
            channel_info = {}
        image_url = _preferred_image_url_from_info(channel_info) or _best_thumbnail_url(channel_info.get("thumbnails"))
        if image_url:
            return image_url, provider or "youtube"
    return _best_thumbnail_url(info.get("thumbnails")), provider


def _entry_source_url(entry: VideoEntry) -> str:
    if entry.source_url:
        return entry.source_url
    if entry.id.startswith("yt-") and len(entry.id) > 3:
        return f"https://www.youtube.com/watch?v={entry.id[3:]}"
    if entry.source_id.startswith("yt-") and len(entry.source_id) > 3:
        return f"https://www.youtube.com/watch?v={entry.source_id[3:]}"
    return ""


def _preferred_image_url_from_info(info: Optional[dict]) -> str:
    if not isinstance(info, dict):
        return ""
    return _recursive_image_url(info, preferred=True)


def _recursive_image_url(value: Any, *, preferred: bool) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).casefold()
            if isinstance(item, str) and item.startswith("http") and _looks_like_image_url(item):
                if not preferred or any(part in key_text for part in ("avatar", "profile", "user", "artist")):
                    return item
            found = _recursive_image_url(item, preferred=preferred)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _recursive_image_url(item, preferred=preferred)
            if found:
                return found
    return ""


def _best_thumbnail_url(thumbnails: Any) -> str:
    if not isinstance(thumbnails, list):
        return ""
    candidates = [item for item in thumbnails if isinstance(item, dict) and str(item.get("url") or "").startswith("http")]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0), reverse=True)
    return str(candidates[0].get("url") or "")


def _looks_like_image_url(value: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|webp)(\?|$)", value, re.IGNORECASE)) or "yt3.ggpht.com" in value or "yt3.googleusercontent.com" in value


def _download_artist_image(config: AppConfig, artist_id: str, url: str) -> Optional[Path]:
    config.artist_thumbnails_dir.mkdir(parents=True, exist_ok=True)
    suffix = _image_suffix(url)
    path = config.artist_thumbnails_dir / f"{_safe_artist_filename(artist_id)}{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_SLUG}/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                return None
            data = response.read(5 * 1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not data:
        return None
    path.write_bytes(data)
    return path


def _image_suffix(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTS else ".jpg"


def _safe_artist_filename(artist_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", artist_id)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
