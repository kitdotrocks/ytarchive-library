from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional

from .ids import stem_aliases
from .metadata import VideoEntry


MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".flac", ".wav", ".opus", ".avi"}
TEMP_SUFFIX_MARKERS = (".part", ".ytdl")
_SEARCH_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SEARCH_MIN_SCORE = 80.0


@dataclass
class LibraryStats:
    metadata_entries: int
    files_found: int
    missing_files: int


def tag_counts(entries: Iterable[VideoEntry]) -> dict[str, int]:
    """Count songs per tag using case-insensitive tag identities."""
    counts: dict[str, int] = {}
    for entry in entries:
        for tag in {item.casefold() for item in entry.tags}:
            counts[tag] = counts.get(tag, 0) + 1
    return counts


def suggested_tags(entries: Iterable[VideoEntry], excluded: Iterable[str] = ()) -> list[str]:
    """Return tag suggestions for a song's current tags.

    Suggestions receive a penalty when they rarely appear with the selected
    tags. The strongest applied-tag relationship determines the penalty, so a
    perfect match with a rare current tag is not diluted by unrelated broad tags
    already on the song. Applied tags use inverse-document-frequency weight, so
    a match with a rare tag is more meaningful than one with a broad tag.
    Without selected tags, the most-used library tags rank first. Tag identities
    are case-insensitive, while the first spelling found is kept for display in
    the editor.
    """
    excluded_keys = {tag.casefold() for tag in excluded}
    counts: dict[str, int] = {}
    display_names: dict[str, str] = {}
    entry_tags: list[set[str]] = []
    for entry in entries:
        entry_keys: set[str] = set()
        for tag in entry.tags:
            key = tag.casefold()
            if key in entry_keys:
                continue
            entry_keys.add(key)
            display_names.setdefault(key, tag)
            counts[key] = counts.get(key, 0) + 1
        entry_tags.append(entry_keys)

    library_size = len(entry_tags)
    if not library_size:
        return []

    def inverse_frequency(key: str) -> float:
        # Add-one smoothing keeps universal tags from becoming completely
        # unusable while still substantially favoring narrow tags.
        return 1.0 + math.log((library_size + 1) / (counts[key] + 1))

    applied_weights = {
        key: inverse_frequency(key)
        for key in excluded_keys
        if key in counts
    }
    total_applied_weight = sum(applied_weights.values())
    strongest_applied_weight = max(applied_weights.values(), default=0.0)
    cooccurrences: dict[str, dict[str, int]] = {}
    for entry_keys in entry_tags:
        shared_tags = entry_keys & applied_weights.keys()
        if not shared_tags:
            continue
        for key in entry_keys - excluded_keys:
            candidate_counts = cooccurrences.setdefault(key, {})
            for source_tag in shared_tags:
                candidate_counts[source_tag] = candidate_counts.get(source_tag, 0) + 1

    def rank(key: str) -> tuple[float, float, int, str]:
        if not total_applied_weight:
            return (float(counts[key]), 1.0, counts[key], key)
        candidate_matches = cooccurrences.get(key, {})
        relationship = max(
            (
                weight * (candidate_matches.get(source_tag, 0) / counts[source_tag])
                / strongest_applied_weight
            )
            for source_tag, weight in applied_weights.items()
        )
        # This is a downrank, not a boost: a fully related tag keeps its normal
        # usage score, while a tag with no overlap receives a zero relevance
        # score and is sorted after all tags with positive overlap.
        penalized_score = counts[key] * relationship
        return (relationship, penalized_score, counts[key], key)

    return [
        display_names[key]
        for key in sorted(counts, key=lambda key: (-rank(key)[0], -rank(key)[1], -rank(key)[2], rank(key)[3]))
        if key not in excluded_keys
    ]


def _search_tokens(value: str) -> tuple[str, ...]:
    """Return accent-insensitive, punctuation-independent search tokens."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    return tuple(_SEARCH_TOKEN_RE.findall(folded))


def norm_text(value: str) -> str:
    """Normalize text for sorting and searching.

    Keeping normalized words separated makes a query such as ``rock n roll``
    match ``Rock'n'Roll`` while preventing punctuation from affecting ranking.
    """
    return " ".join(_search_tokens(value))


def format_date(value: str) -> str:
    if re.fullmatch(r"\d{8}", value or ""):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value or ""


def build_file_index(videos_dir: Path) -> dict[str, Path]:
    by_id: dict[str, list[Path]] = {}
    if not videos_dir.exists():
        return {}
    for path in videos_dir.iterdir():
        if not path.is_file():
            continue
        # Download parts and separate audio/video format files are not playable
        # library entries.  In particular, don't index a fragment left behind
        # by an interrupted yt-dlp merge.
        if path.suffix.lower() not in MEDIA_EXTS:
            continue
        if path.name.endswith(TEMP_SUFFIX_MARKERS) or re.search(r"\.f\d+\.", path.name):
            continue
        video_id = path.name.split(".")[0]
        for alias in stem_aliases(video_id):
            by_id.setdefault(alias, []).append(path)

    return {video_id: _choose_best_path(paths, video_id) for video_id, paths in by_id.items()}


def _choose_best_path(paths: list[Path], video_id: str) -> Path:
    def score(path: Path) -> tuple[int, int]:
        name = path.name
        if name == f"{video_id}.mp4":
            return 0, len(name)
        if name.endswith(TEMP_SUFFIX_MARKERS):
            return 3, len(name)
        if re.search(r"\.f\d+\.", name):
            return 2, len(name)
        return 1, len(name)

    return sorted(paths, key=score)[0]


def _directory_revision(path: Path) -> Optional[tuple[int, int, tuple[str, ...]]]:
    """Return a revision for the directory entries used by the index.

    Directory timestamps are not reliable enough on every supported
    filesystem to detect rapid changes, especially on Windows. Keep the
    metadata as a quick signal and include entry names so additions,
    removals, and renames still invalidate the cached file index.
    """
    try:
        stat = path.stat()
        names = tuple(sorted(entry.name for entry in path.iterdir()))
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, names


@dataclass(frozen=True)
class _SearchRecord:
    entry: VideoEntry
    title: str
    author: str
    video_id: str
    tags: tuple[str, ...]
    title_tokens: tuple[str, ...]
    author_tokens: tuple[str, ...]
    id_tokens: tuple[str, ...]
    tag_tokens: tuple[tuple[str, ...], ...]

    @classmethod
    def from_entry(cls, entry: VideoEntry) -> "_SearchRecord":
        title = norm_text(entry.title)
        author = norm_text(entry.author)
        video_id = norm_text(entry.id)
        normalized_tags: list[str] = []
        for tag in entry.tags:
            normalized = norm_text(tag)
            if normalized:
                normalized_tags.append(normalized)
        tags = tuple(normalized_tags)
        return cls(
            entry=entry,
            title=title,
            author=author,
            video_id=video_id,
            tags=tags,
            title_tokens=tuple(title.split()),
            author_tokens=tuple(author.split()),
            id_tokens=tuple(video_id.split()),
            tag_tokens=tuple(tuple(tag.split()) for tag in tags),
        )


class SearchIndex:
    """Precompute normalized fields and token lookups for library searches."""

    def __init__(self, entries: Iterable[VideoEntry]):
        self.entries = tuple(entries)
        self._records = tuple(_SearchRecord.from_entry(entry) for entry in self.entries)
        self._token_records: dict[str, set[int]] = {}
        self._prefix_records: dict[str, set[int]] = {}
        for index, record in enumerate(self._records):
            tokens = set(record.title_tokens) | set(record.author_tokens) | set(record.id_tokens)
            tokens.update(token for tag in record.tag_tokens for token in tag)
            for token in tokens:
                self._token_records.setdefault(token, set()).add(index)
                # Prefix lookups cover the common partial-word case without
                # requiring a scan of every record for every keystroke.
                for length in range(1, len(token) + 1):
                    self._prefix_records.setdefault(token[:length], set()).add(index)

    def search(self, query: str, limit: int = 100) -> list[VideoEntry]:
        if limit <= 0:
            return []
        query_tokens = _search_tokens(query)
        if not query_tokens:
            return list(self.entries[:limit])

        candidates = self._candidate_records(query_tokens)
        scored = [
            (record, self._score_record(query_tokens, record))
            for record in candidates
        ]
        scored = [(record, score) for record, score in scored if score >= _SEARCH_MIN_SCORE]
        scored.sort(key=lambda item: (-item[1], item[0].title, item[0].author, item[0].entry.id))
        return [record.entry for record, _score in scored[:limit]]

    def _candidate_records(self, query_tokens: tuple[str, ...]) -> list[_SearchRecord]:
        """Use exact/prefix postings when possible, retaining fuzzy recall."""
        postings: list[set[int]] = []
        for token in query_tokens:
            matches = self._token_records.get(token)
            if matches is None:
                matches = self._prefix_records.get(token)
            if matches is None:
                # A token absent from the postings may be a typo or a
                # substring, both of which need the complete catalog.
                return list(self._records)
            postings.append(matches)
        candidate_ids: set[int] = set()
        for matches in postings:
            candidate_ids.update(matches)
        return [self._records[index] for index in candidate_ids]

    @classmethod
    def score(cls, query: str, entry: VideoEntry) -> float:
        query_tokens = _search_tokens(query)
        if not query_tokens:
            return 0.0
        return cls._score_record(query_tokens, _SearchRecord.from_entry(entry))

    @staticmethod
    def _score_record(query_tokens: tuple[str, ...], record: _SearchRecord) -> float:
        query_text = " ".join(query_tokens)
        scalar_fields = (
            (record.title, record.title_tokens, 1.00),
            (record.author, record.author_tokens, 0.90),
            (record.video_id, record.id_tokens, 0.78),
        )

        # Every query word must be findable somewhere. This prevents a match
        # on just one word of a multi-word query from outranking a complete
        # result, while still allowing words to span title/artist/tag fields.
        token_score = 0.0
        for query_token in query_tokens:
            best_match = 0.0
            for _field_value, field_tokens, field_weight in scalar_fields:
                for candidate_token in field_tokens:
                    best_match = max(
                        best_match,
                        _token_match_strength(query_token, candidate_token) * field_weight,
                    )
            for tag_tokens in record.tag_tokens:
                for candidate_token in tag_tokens:
                    best_match = max(best_match, _token_match_strength(query_token, candidate_token) * 0.92)
            if best_match <= 0:
                return 0.0
            token_score += best_match * 120.0

        score = token_score
        # Whole-field and phrase matches carry the strongest relevance signal.
        raw_id = " ".join(record.id_tokens[1:]) if len(record.id_tokens) > 1 else ""
        if query_text == record.video_id or query_text == raw_id:
            score += 1250.0
        elif record.video_id.startswith(query_text):
            score += 850.0
        elif query_text in record.video_id:
            score += 500.0

        if query_text == record.title:
            score += 1100.0
        elif query_text in record.title:
            score += 650.0 + max(0.0, 50.0 - record.title.find(query_text) * 0.25)

        if query_text == record.author:
            score += 900.0
        elif query_text in record.author:
            score += 460.0

        for tag in record.tags:
            if query_text == tag:
                score += 980.0
            elif query_text in tag:
                score += 560.0

        # A typo in a longer word should remain discoverable, but short fuzzy
        # matches are intentionally excluded because they produce noise.
        if len(query_text) >= 4:
            for field_value, field_tokens, field_weight in scalar_fields[:2]:
                ratio = SequenceMatcher(None, query_text, field_value).ratio()
                if ratio >= 0.72:
                    score += ratio * 100.0 * field_weight
            for tag in record.tags:
                ratio = SequenceMatcher(None, query_text, tag).ratio()
                if ratio >= 0.72:
                    score += ratio * 100.0 * 0.92
        return score


def _token_match_strength(query_token: str, candidate_token: str) -> float:
    if query_token == candidate_token:
        return 1.0
    if candidate_token.startswith(query_token):
        return 0.82
    if len(query_token) >= 3 and query_token in candidate_token:
        return 0.65
    if len(query_token) >= 3 and len(candidate_token) >= 3:
        ratio = SequenceMatcher(None, query_token, candidate_token).ratio()
        if ratio >= 0.72:
            return 0.45 + ratio * 0.55
    return 0.0


class LibraryIndex:
    def __init__(self, videos_dir: Path):
        self.videos_dir = videos_dir
        self.file_index: dict[str, Path] = {}
        self.entries: list[VideoEntry] = []
        self._file_index_revision: Optional[tuple[int, int, tuple[str, ...]]] = None
        self._file_index_path: Optional[Path] = None
        self._file_index_ready = False
        self._search_index: Optional[SearchIndex] = None
        self._entries_by_id: dict[str, VideoEntry] = {}
        self._built = False
        self._generation = 0
        self._storage_bytes: Optional[int] = None

    @property
    def generation(self) -> int:
        """Monotonic generation for metadata/file-derived library caches."""

        return self._generation

    @property
    def ready(self) -> bool:
        return self._built

    def refresh_file_index(self, *, force: bool = False) -> bool:
        """Refresh media paths only when the media directory changed.

        A library refresh can still rebuild metadata-derived rows, but an
        unchanged media directory does not need another full ``iterdir`` scan.
        Return whether a filesystem scan was performed.
        """
        revision = _directory_revision(self.videos_dir)
        if (
            not force
            and self._file_index_ready
            and self._file_index_path == self.videos_dir
            and self._file_index_revision == revision
        ):
            return False
        self.file_index = build_file_index(self.videos_dir)
        self._file_index_revision = revision
        self._file_index_path = self.videos_dir
        self._file_index_ready = True
        self._storage_bytes = None
        return True

    def rebuild(self, entries: Iterable[VideoEntry], *, force_file_scan: bool = False) -> LibraryStats:
        self.refresh_file_index(force=force_file_scan)
        rebuilt: list[VideoEntry] = []
        for entry in entries:
            entry.path = self.file_index.get(entry.id)
            rebuilt.append(entry)
        rebuilt.sort(key=lambda e: norm_text(e.title))
        self.entries = rebuilt
        self._entries_by_id = {}
        for entry in rebuilt:
            self._entries_by_id.setdefault(entry.id, entry)
        self._search_index = None
        self._generation += 1
        self._built = True
        return self.stats()

    def relink(self, *, force_file_scan: bool = False) -> LibraryStats:
        """Apply a changed media directory to the existing metadata rows."""
        self.refresh_file_index(force=force_file_scan)
        for entry in self.entries:
            entry.path = self.file_index.get(entry.id)
        self._generation += 1
        return self.stats()

    def stats(self) -> LibraryStats:
        found = sum(1 for entry in self.entries if entry.path)
        return LibraryStats(len(self.entries), found, len(self.entries) - found)

    def invalidate_search(self) -> None:
        """Drop the cached search catalog after in-memory metadata edits."""
        self._search_index = None
        self._generation += 1

    def search(self, query: str, limit: int = 100) -> list[VideoEntry]:
        if self._search_index is None:
            self._search_index = SearchIndex(self.entries)
        return self._search_index.search(query, limit)

    def storage_bytes(self) -> int:
        """Return cached media storage usage, refreshing after file-index changes."""

        if self._storage_bytes is not None:
            return self._storage_bytes

        total = 0
        seen_paths: set[Path] = set()
        for path in self.file_index.values():
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                total += path.stat().st_size
            except OSError:
                continue
        self._storage_bytes = total
        return total

    @staticmethod
    def search_entries(entries: Iterable[VideoEntry], query: str, limit: int = 100) -> list[VideoEntry]:
        return SearchIndex(entries).search(query, limit)

    @staticmethod
    def _score(query: str, entry: VideoEntry) -> float:
        return SearchIndex.score(query, entry)

    def by_id(self, video_id: str) -> Optional[VideoEntry]:
        return self._entries_by_id.get(video_id)
