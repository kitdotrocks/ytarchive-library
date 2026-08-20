from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional


@dataclass
class VideoEntry:
    id: str
    title: str
    upload_date: str
    author: str = ""
    source_type: str = ""
    source_id: str = ""
    source_url: str = ""
    resolved_from: str = ""
    audio_quality: str = ""
    path: Optional[Path] = None
    last_played_at: str = ""
    playback_seconds: float = 0.0
    play_count: int = 0
    play_count_estimated_from_seconds: float = 0.0
    play_count_estimation_version: int = 0
    duration_seconds: float = 0.0
    bitrate_kbps: int = 0
    audio_bitrate_kbps: int = 0
    bpm: int = 0
    bpm_confidence: float = 0.0
    bpm_analysis_version: int = 0
    bpm_source: str = ""
    hidden_from_subsonic: bool = False
    lyrics: str = ""
    tags: list[str] = field(default_factory=list)
    presence_image_mode: str = "default"
    presence_image_value: str = ""

    @classmethod
    def from_json(cls, video_id: str, data: dict) -> "VideoEntry":
        try:
            playback_seconds = float(data.get("playback_seconds", 0) or 0)
        except (TypeError, ValueError):
            playback_seconds = 0.0
        try:
            play_count = int(data.get("play_count", 0) or 0)
        except (TypeError, ValueError):
            play_count = 0
        try:
            play_count_estimated_from_seconds = float(data.get("play_count_estimated_from_seconds", 0) or 0)
        except (TypeError, ValueError):
            play_count_estimated_from_seconds = 0.0
        try:
            play_count_estimation_version = int(data.get("play_count_estimation_version", 0) or 0)
        except (TypeError, ValueError):
            play_count_estimation_version = 0
        try:
            duration_seconds = float(data.get("duration_seconds", 0) or 0)
        except (TypeError, ValueError):
            duration_seconds = 0.0
        try:
            bitrate_kbps = int(data.get("bitrate_kbps", 0) or 0)
        except (TypeError, ValueError):
            bitrate_kbps = 0
        try:
            audio_bitrate_kbps = int(data.get("audio_bitrate_kbps", 0) or 0)
        except (TypeError, ValueError):
            audio_bitrate_kbps = 0
        try:
            bpm = int(round(float(data.get("bpm", 0) or 0)))
        except (TypeError, ValueError):
            bpm = 0
        try:
            bpm_confidence = float(data.get("bpm_confidence", 0) or 0)
        except (TypeError, ValueError):
            bpm_confidence = 0.0
        try:
            bpm_analysis_version = int(data.get("bpm_analysis_version", 0) or 0)
        except (TypeError, ValueError):
            bpm_analysis_version = 0
        hidden_from_subsonic = bool(data.get("hidden_from_subsonic", False))
        tags = normalize_tags(data.get("tags", []))
        return cls(
            id=video_id,
            title=str(data.get("title", "")).strip(),
            upload_date=str(data.get("upload_date", "")).strip(),
            author=str(data.get("author", "")).strip(),
            source_type=str(data.get("source_type", "")).strip(),
            source_id=str(data.get("source_id", "")).strip(),
            source_url=str(data.get("source_url", "")).strip(),
            resolved_from=str(data.get("resolved_from", "")).strip(),
            audio_quality=str(data.get("audio_quality", "")).strip(),
            last_played_at=str(data.get("last_played_at", "")).strip(),
            playback_seconds=max(0.0, playback_seconds),
            play_count=max(0, play_count),
            play_count_estimated_from_seconds=max(0.0, play_count_estimated_from_seconds),
            play_count_estimation_version=max(0, play_count_estimation_version),
            duration_seconds=max(0.0, duration_seconds),
            bitrate_kbps=max(0, bitrate_kbps),
            audio_bitrate_kbps=max(0, audio_bitrate_kbps),
            bpm=max(0, bpm),
            bpm_confidence=max(0.0, min(1.0, bpm_confidence)),
            bpm_analysis_version=max(0, bpm_analysis_version),
            bpm_source=str(data.get("bpm_source", "")).strip(),
            hidden_from_subsonic=hidden_from_subsonic,
            lyrics=str(data.get("lyrics", "")),
            tags=tags,
            presence_image_mode=str(data.get("presence_image_mode", "default")).strip() or "default",
            presence_image_value=str(data.get("presence_image_value", "")).strip(),
        )

    def to_json(self) -> dict:
        data = {
            "title": self.title,
            "upload_date": self.upload_date,
        }
        if self.author:
            data["author"] = self.author
        if self.source_type:
            data["source_type"] = self.source_type
        if self.source_id:
            data["source_id"] = self.source_id
        if self.source_url:
            data["source_url"] = self.source_url
        if self.resolved_from:
            data["resolved_from"] = self.resolved_from
        if self.audio_quality:
            data["audio_quality"] = self.audio_quality
        if self.last_played_at:
            data["last_played_at"] = self.last_played_at
        if self.playback_seconds > 0:
            data["playback_seconds"] = round(self.playback_seconds, 2)
        if self.play_count > 0:
            data["play_count"] = self.play_count
        if self.play_count_estimated_from_seconds > 0:
            data["play_count_estimated_from_seconds"] = round(self.play_count_estimated_from_seconds, 2)
        if self.play_count_estimation_version > 0:
            data["play_count_estimation_version"] = self.play_count_estimation_version
        if self.duration_seconds > 0:
            data["duration_seconds"] = round(self.duration_seconds, 3)
        if self.bitrate_kbps > 0:
            data["bitrate_kbps"] = self.bitrate_kbps
        if self.audio_bitrate_kbps > 0:
            data["audio_bitrate_kbps"] = self.audio_bitrate_kbps
        if self.bpm > 0:
            data["bpm"] = self.bpm
        if self.bpm_confidence > 0:
            data["bpm_confidence"] = round(self.bpm_confidence, 3)
        if self.bpm_analysis_version > 0:
            data["bpm_analysis_version"] = self.bpm_analysis_version
        if self.bpm_source:
            data["bpm_source"] = self.bpm_source
        if self.hidden_from_subsonic:
            data["hidden_from_subsonic"] = True
        if self.lyrics:
            data["lyrics"] = self.lyrics
        tags = normalize_tags(self.tags)
        if tags:
            data["tags"] = tags
        if self.presence_image_mode and self.presence_image_mode != "default":
            data["presence_image_mode"] = self.presence_image_mode
        if self.presence_image_value:
            data["presence_image_value"] = self.presence_image_value
        return data


def normalize_tags(value: object) -> list[str]:
    """Return clean, ordered, case-insensitively unique tag names."""
    if not isinstance(value, (list, tuple)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tag = " ".join(item.split())
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def _metadata_revision(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class MetadataStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._entries: Dict[str, VideoEntry] = {}
        self._loaded_revision: Optional[tuple[int, int]] = None
        self._pending_entries: Dict[str, VideoEntry] = {}
        self._pending_deletes: set[str] = set()

    def load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def load_if_changed(self) -> bool:
        """Reload the metadata file only when its on-disk revision changed."""
        with self._lock:
            revision = _metadata_revision(self.path)
            if revision == self._loaded_revision:
                return False
            self._load_unlocked()
            return True

    def _load_unlocked(self) -> None:
        if not self.path.exists():
            self._entries = {}
            self._loaded_revision = None
            self._apply_pending_unlocked()
            return
        try:
            raw = _load_metadata_json(self.path)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._quarantine_corrupt_file()
            self._entries = {}
            self._loaded_revision = _metadata_revision(self.path)
            self._apply_pending_unlocked()
            return
        self._entries = {
            video_id: VideoEntry.from_json(video_id, info)
            for video_id, info in raw.items()
            if isinstance(info, dict)
        }
        self._loaded_revision = _metadata_revision(self.path)
        self._apply_pending_unlocked()

    def _apply_pending_unlocked(self) -> None:
        for video_id in self._pending_deletes:
            self._entries.pop(video_id, None)
        for video_id, entry in self._pending_entries.items():
            self._entries[video_id] = entry

    def _quarantine_corrupt_file(self) -> None:
        """Move malformed metadata aside before allowing the app to continue."""
        stamp = time.time_ns()
        destination = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        self.path.replace(destination)

    def save(self) -> None:
        with self._lock:
            with _metadata_file_lock(self.path):
                # Merge deferred local updates with the latest on-disk state
                # before writing, preserving changes made by another store.
                self._load_unlocked()
                self._save_unlocked()
                self._clear_pending_unlocked()

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        backup = self.path.with_suffix(f"{self.path.suffix}.bak")
        payload = {video_id: entry.to_json() for video_id, entry in self._entries.items()}
        try:
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            if self.path.exists():
                shutil.copy2(self.path, backup)
            tmp.replace(self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        self._loaded_revision = _metadata_revision(self.path)

    def entries(self) -> list[VideoEntry]:
        with self._lock:
            return [
                VideoEntry(
                    id=e.id,
                    title=e.title,
                    upload_date=e.upload_date,
                    author=e.author,
                    source_type=e.source_type,
                    source_id=e.source_id,
                    source_url=e.source_url,
                    resolved_from=e.resolved_from,
                    audio_quality=e.audio_quality,
                    path=e.path,
                    last_played_at=e.last_played_at,
                    playback_seconds=e.playback_seconds,
                    play_count=e.play_count,
                    play_count_estimated_from_seconds=e.play_count_estimated_from_seconds,
                    play_count_estimation_version=e.play_count_estimation_version,
                    duration_seconds=e.duration_seconds,
                    bitrate_kbps=e.bitrate_kbps,
                    audio_bitrate_kbps=e.audio_bitrate_kbps,
                    bpm=e.bpm,
                    bpm_confidence=e.bpm_confidence,
                    bpm_analysis_version=e.bpm_analysis_version,
                    bpm_source=e.bpm_source,
                    hidden_from_subsonic=e.hidden_from_subsonic,
                    lyrics=e.lyrics,
                    tags=list(e.tags),
                    presence_image_mode=e.presence_image_mode,
                    presence_image_value=e.presence_image_value,
                )
                for e in self._entries.values()
            ]

    def ids(self) -> set[str]:
        with self._lock:
            return set(self._entries)

    def get(self, video_id: str) -> Optional[VideoEntry]:
        with self._lock:
            entry = self._entries.get(video_id)
            if not entry:
                return None
            return VideoEntry(
                id=entry.id,
                title=entry.title,
                upload_date=entry.upload_date,
                author=entry.author,
                source_type=entry.source_type,
                source_id=entry.source_id,
                source_url=entry.source_url,
                resolved_from=entry.resolved_from,
                audio_quality=entry.audio_quality,
                path=entry.path,
                last_played_at=entry.last_played_at,
                playback_seconds=entry.playback_seconds,
                play_count=entry.play_count,
                play_count_estimated_from_seconds=entry.play_count_estimated_from_seconds,
                play_count_estimation_version=entry.play_count_estimation_version,
                duration_seconds=entry.duration_seconds,
                bitrate_kbps=entry.bitrate_kbps,
                audio_bitrate_kbps=entry.audio_bitrate_kbps,
                bpm=entry.bpm,
                bpm_confidence=entry.bpm_confidence,
                bpm_analysis_version=entry.bpm_analysis_version,
                bpm_source=entry.bpm_source,
                hidden_from_subsonic=entry.hidden_from_subsonic,
                lyrics=entry.lyrics,
                tags=list(entry.tags),
                presence_image_mode=entry.presence_image_mode,
                presence_image_value=entry.presence_image_value,
            )

    def upsert(self, entry: VideoEntry, save: bool = True) -> None:
        with self._lock:
            if save:
                with _metadata_file_lock(self.path):
                    self._load_unlocked()
                    self._store_entry_unlocked(entry)
                    self._save_unlocked()
                    self._clear_pending_unlocked()
                return
            self._store_entry_unlocked(entry)
            self._pending_entries[entry.id] = self._entries[entry.id]
            self._pending_deletes.discard(entry.id)

    def update_media_fields(
        self,
        video_id: str,
        *,
        duration: Optional[float] = None,
        bitrate: Optional[int] = None,
        audio_bitrate: Optional[int] = None,
    ) -> bool:
        """Merge discovered media fields into the current stored entry.

        Media probing often works from a snapshot taken by another subsystem.
        Do not write that snapshot back wholesale: playback, tags, lyrics, and
        other metadata may have changed since it was copied.
        """
        return bool(
            self.update_media_fields_bulk(
                {
                    video_id: (duration, bitrate, audio_bitrate),
                }
            )
        )

    def update_media_fields_bulk(
        self,
        updates: Dict[str, tuple[Optional[float], Optional[int], Optional[int]]],
    ) -> int:
        """Merge several media probe results in one metadata transaction."""
        with self._lock:
            with _metadata_file_lock(self.path):
                had_pending = bool(self._pending_entries or self._pending_deletes)
                self._load_unlocked()
                changed_count = 0
                for video_id, (duration, bitrate, audio_bitrate) in updates.items():
                    entry = self._entries.get(video_id)
                    if entry is None:
                        continue
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
                    if changed:
                        changed_count += 1
                if changed_count or had_pending:
                    self._save_unlocked()
                    self._clear_pending_unlocked()
                return changed_count

    def _clear_pending_unlocked(self) -> None:
        self._pending_entries.clear()
        self._pending_deletes.clear()

    def _store_entry_unlocked(self, entry: VideoEntry) -> None:
        self._entries[entry.id] = VideoEntry(
            id=entry.id,
            title=entry.title,
            upload_date=entry.upload_date,
            author=entry.author,
            source_type=entry.source_type,
            source_id=entry.source_id,
            source_url=entry.source_url,
            resolved_from=entry.resolved_from,
            audio_quality=entry.audio_quality,
            path=entry.path,
            last_played_at=entry.last_played_at,
            playback_seconds=entry.playback_seconds,
            play_count=entry.play_count,
            play_count_estimated_from_seconds=entry.play_count_estimated_from_seconds,
            play_count_estimation_version=entry.play_count_estimation_version,
            duration_seconds=entry.duration_seconds,
            bitrate_kbps=entry.bitrate_kbps,
            audio_bitrate_kbps=entry.audio_bitrate_kbps,
            bpm=entry.bpm,
            bpm_confidence=entry.bpm_confidence,
            bpm_analysis_version=entry.bpm_analysis_version,
            bpm_source=entry.bpm_source,
            hidden_from_subsonic=entry.hidden_from_subsonic,
            lyrics=entry.lyrics,
            tags=list(entry.tags),
            presence_image_mode=entry.presence_image_mode,
            presence_image_value=entry.presence_image_value,
        )

    def delete(self, video_id: str, save: bool = True) -> bool:
        with self._lock:
            if save:
                with _metadata_file_lock(self.path):
                    had_pending = bool(self._pending_entries or self._pending_deletes)
                    self._load_unlocked()
                    removed = self._entries.pop(video_id, None) is not None
                    if removed or had_pending:
                        self._save_unlocked()
                        self._clear_pending_unlocked()
                    return removed
            removed = self._entries.pop(video_id, None) is not None
            if removed:
                self._pending_entries.pop(video_id, None)
                self._pending_deletes.add(video_id)
            return removed

    def bulk_upsert(self, entries: Iterable[VideoEntry]) -> None:
        with self._lock:
            with _metadata_file_lock(self.path):
                self._load_unlocked()
                for entry in entries:
                    self._store_entry_unlocked(entry)
                self._save_unlocked()
                self._clear_pending_unlocked()


@contextmanager
def _metadata_file_lock(path: Path):
    """Serialize metadata read/modify/write transactions across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_metadata_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Metadata root must be a JSON object")
    return raw
