from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .runtime import find_external_tool, windows_no_console_kwargs


AUDIO_EXTS = {"mp3", "flac", "wav", "opus", "m4a", "ogg"}
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi"}


def have_tool(name: str) -> bool:
    return shutil.which(name) is not None or find_external_tool(name) is not None


def human_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


def export_filename_base(title: str, video_id: str) -> str:
    raw = (title or "").strip()
    if not raw:
        return video_id
    safe = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    safe = safe[:180].strip(" .")
    return safe or video_id


def ffprobe_json(path: Path) -> Optional[dict]:
    if not have_tool("ffprobe") or not path.exists():
        return None
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        **windows_no_console_kwargs(),
    )
    if proc.returncode != 0:
        return {"error": proc.stdout[-2000:]}
    return json.loads(proc.stdout)


def sha256_prefix(path: Path, length: int = 64) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[: max(8, length)]


def detect_media_tags(path: Path) -> dict[str, str]:
    info = ffprobe_json(path) or {}
    tags = {}
    for source in [info.get("format", {}).get("tags", {})] + [stream.get("tags", {}) for stream in info.get("streams", [])]:
        if isinstance(source, dict):
            for key, value in source.items():
                if value is None:
                    continue
                tags.setdefault(str(key).strip().lower(), str(value).strip())

    title = tags.get("title", "").strip()
    author = (tags.get("artist") or tags.get("album_artist") or tags.get("uploader") or "").strip()
    date = _normalize_tag_date(tags)
    return {"title": title, "author": author, "upload_date": date}


def normalize_upload_date(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"(\d{4})[-_/.:]?(\d{2})[-_/.:]?(\d{2})", raw)
    if not match:
        raise ValueError("Date must be in YYYY-MM-DD or YYYYMMDD format")
    digits = "".join(match.groups())
    try:
        return datetime.strptime(digits, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise ValueError("Date must be a real calendar date") from exc


def _normalize_tag_date(tags: dict[str, str]) -> str:
    for key in ("date", "creation_time", "year", "originaldate"):
        value = tags.get(key, "").strip()
        if not value:
            continue
        try:
            return normalize_upload_date(value)
        except ValueError:
            continue
    return ""


def convert_with_ffmpeg(src: Path, out_path: Path) -> Tuple[bool, str]:
    if not have_tool("ffmpeg"):
        return False, "ffmpeg was not found."
    out_ext = out_path.suffix.lower().lstrip(".")
    if not out_ext:
        return False, "Output needs an extension."

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if out_ext in AUDIO_EXTS:
        if out_ext == "mp3":
            cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        elif out_ext == "flac":
            cmd += ["-vn", "-c:a", "flac"]
        elif out_ext == "wav":
            cmd += ["-vn", "-c:a", "pcm_s16le"]
        elif out_ext == "opus":
            cmd += ["-vn", "-c:a", "libopus", "-b:a", "160k"]
        elif out_ext == "m4a":
            cmd += ["-vn", "-c:a", "aac", "-b:a", "192k"]
        elif out_ext == "ogg":
            cmd += ["-vn", "-c:a", "libvorbis", "-q:a", "5"]
    elif out_ext in VIDEO_EXTS:
        if src.suffix.lower().lstrip(".") == out_ext or out_ext == "mkv":
            cmd += ["-c", "copy"]
        elif out_ext == "mp4":
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        elif out_ext == "webm":
            cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "33", "-c:a", "libopus", "-b:a", "160k"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k"]
    else:
        return False, f"Unsupported output extension: .{out_ext}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        cmd + [str(out_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        **windows_no_console_kwargs(),
    )
    if proc.returncode != 0:
        return False, proc.stdout[-2000:]
    return True, f"Saved {out_path}"


def thumbnail_path(thumbnails_dir: Path, video_id: str) -> Path:
    return thumbnails_dir / f"{video_id}.jpg"


def ensure_thumbnail(thumbnails_dir: Path, video_id: str) -> Optional[Path]:
    path = thumbnail_path(thumbnails_dir, video_id)
    if path.exists():
        return path
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    for quality in ("maxresdefault", "hqdefault"):
        url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status != 200:
                    continue
                path.write_bytes(response.read())
                return path
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None
