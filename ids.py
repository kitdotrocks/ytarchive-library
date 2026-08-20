from __future__ import annotations

import re
from pathlib import Path


YOUTUBE_ID_RE = re.compile(r"[\w-]{11}")


def prefixed_id(source: str, raw_id: str) -> str:
    return f"{source}-{raw_id}"


def split_prefixed_id(value: str) -> tuple[str, str] | None:
    source, sep, raw_id = (value or "").partition("-")
    if not sep or not source or not raw_id:
        return None
    return source, raw_id


def is_youtube_raw_id(value: str) -> bool:
    return bool(YOUTUBE_ID_RE.fullmatch((value or "").strip()))


def canonical_youtube_id(value: str) -> str:
    raw = (value or "").strip()
    parts = split_prefixed_id(raw)
    if parts and parts[0] == "yt":
        return raw
    if is_youtube_raw_id(raw):
        return prefixed_id("yt", raw)
    return raw


def canonical_local_id(value: str) -> str:
    raw = (value or "").strip()
    parts = split_prefixed_id(raw)
    if parts and parts[0] == "local":
        return raw
    return prefixed_id("local", raw)


def canonical_soundcloud_id(value: str) -> str:
    raw = (value or "").strip()
    parts = split_prefixed_id(raw)
    if parts and parts[0] == "sc":
        return raw
    return prefixed_id("sc", raw)


def canonical_bandcamp_id(value: str) -> str:
    raw = (value or "").strip()
    parts = split_prefixed_id(raw)
    if parts and parts[0] == "bc":
        return raw
    return prefixed_id("bc", raw)


def legacy_alias_ids(value: str) -> list[str]:
    canonical = (value or "").strip()
    aliases = [canonical]
    parts = split_prefixed_id(canonical)
    if parts and parts[0] == "yt":
        aliases.append(parts[1])
    elif parts and parts[0] == "sc":
        aliases.append(parts[1])
    elif parts and parts[0] == "bc":
        aliases.append(parts[1])
    elif is_youtube_raw_id(canonical):
        aliases.append(canonical_youtube_id(canonical))
    return aliases


def filename_for_id(video_id: str, suffix: str) -> str:
    return f"{video_id}{suffix}"


def stem_aliases(stem: str) -> list[str]:
    aliases = [stem]
    if is_youtube_raw_id(stem):
        aliases.append(canonical_youtube_id(stem))
    else:
        parts = split_prefixed_id(stem)
        if parts and parts[0] == "yt":
            aliases.append(parts[1])
        elif parts and parts[0] == "sc":
            aliases.append(parts[1])
        elif parts and parts[0] == "bc":
            aliases.append(parts[1])
        elif stem.isdigit():
            aliases.append(canonical_soundcloud_id(stem))
    return aliases


def classify_entry_id(video_id: str) -> str:
    parts = split_prefixed_id(video_id)
    if parts:
        return parts[0]
    if is_youtube_raw_id(video_id):
        return "yt"
    return "unknown"


def thumbnail_video_id(video_id: str) -> str | None:
    parts = split_prefixed_id(video_id)
    if parts and parts[0] == "yt":
        return parts[1]
    if is_youtube_raw_id(video_id):
        return video_id
    return None


def rename_target(path: Path, new_id: str) -> Path:
    suffix = "".join(path.suffixes)
    return path.with_name(filename_for_id(new_id, suffix))
