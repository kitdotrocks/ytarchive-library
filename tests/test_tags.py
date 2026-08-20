from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.library import LibraryIndex, build_file_index, suggested_tags, tag_counts
from ytarchive.metadata import MetadataStore, VideoEntry, normalize_tags
import ytarchive.metadata as metadata_module


class TagMetadataTestCase(unittest.TestCase):
    def test_normalize_tags_preserves_order_and_deduplicates_case(self) -> None:
        self.assertEqual(
            normalize_tags(["  Chill  ", "chill", "late   night", "", 4]),
            ["Chill", "late night"],
        )

    def test_tags_round_trip_and_are_sparse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            store = MetadataStore(path)
            store.upsert(
                VideoEntry(
                    id="song-1",
                    title="Song",
                    upload_date="",
                    tags=["Chill", "Instrumental"],
                )
            )

            loaded = MetadataStore(path)
            loaded.load()
            entry = loaded.get("song-1")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.tags, ["Chill", "Instrumental"])
            self.assertEqual(json.loads(path.read_text())["song-1"]["tags"], ["Chill", "Instrumental"])

            entry.tags = []
            loaded.upsert(entry)
            self.assertNotIn("tags", json.loads(path.read_text())["song-1"])

    def test_store_returns_independent_tag_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MetadataStore(Path(directory) / "metadata.json")
            store.upsert(VideoEntry(id="song-1", title="Song", upload_date="", tags=["Rock"]))
            first = store.get("song-1")
            first.tags.append("Live")
            self.assertEqual(store.get("song-1").tags, ["Rock"])

    def test_load_if_changed_skips_unchanged_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            MetadataStore(path).upsert(VideoEntry(id="song-1", title="Song", upload_date=""))
            store = MetadataStore(path)
            with mock.patch("ytarchive.metadata._load_metadata_json", wraps=metadata_module._load_metadata_json) as load:
                self.assertTrue(store.load_if_changed())
                self.assertFalse(store.load_if_changed())
                path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                self.assertTrue(store.load_if_changed())
                self.assertEqual(load.call_count, 2)


class TagSearchTestCase(unittest.TestCase):
    def test_tag_counts_count_songs_case_insensitively(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["Chill", "chill"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["CHILL", "Workout"]),
        ]
        self.assertEqual(tag_counts(entries), {"chill": 2, "workout": 1})

    def test_suggested_tags_prefer_tags_that_appear_with_current_tags(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["Chill", "Workout"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["chill", "Focus"]),
            VideoEntry(id="three", title="Gamma", upload_date="", tags=["Focus", "Ambient"]),
            VideoEntry(id="four", title="Delta", upload_date="", tags=["Ambient"]),
        ]
        self.assertEqual(suggested_tags(entries), ["Ambient", "Chill", "Focus", "Workout"])
        self.assertEqual(suggested_tags(entries, ["CHILL"]), ["Focus", "Workout", "Ambient"])

    def test_suggested_tags_give_rare_current_tags_more_influence(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["Broad", "Popular suggestion"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["Rare", "Specific suggestion"]),
            VideoEntry(id="three", title="Gamma", upload_date="", tags=["Broad"]),
            VideoEntry(id="four", title="Delta", upload_date="", tags=["Broad"]),
            VideoEntry(id="five", title="Epsilon", upload_date="", tags=["Broad"]),
        ]
        self.assertEqual(
            suggested_tags(entries, ["Broad", "Rare"]),
            ["Specific suggestion", "Popular suggestion"],
        )

    def test_suggested_tags_penalize_tags_unrelated_to_current_tags(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["Chill", "Related"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["Chill"]),
            VideoEntry(id="three", title="Gamma", upload_date="", tags=["Unrelated"]),
            VideoEntry(id="four", title="Delta", upload_date="", tags=["Unrelated"]),
            VideoEntry(id="five", title="Epsilon", upload_date="", tags=["Unrelated"]),
            VideoEntry(id="six", title="Zeta", upload_date="", tags=["Unrelated"]),
        ]
        self.assertEqual(suggested_tags(entries, ["Chill"]), ["Related", "Unrelated"])

    def test_suggested_tags_keep_always_paired_narrow_tags_above_broad_unrelated_tags(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["G-Funk", "Gangsta Rap"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["g-funk", "Gangsta Rap"]),
            VideoEntry(id="three", title="Gamma", upload_date="", tags=["G-FUNK", "Gangsta Rap"]),
            *[
                VideoEntry(id=f"music-{number}", title="Music", upload_date="", tags=["Music"])
                for number in range(40)
            ],
        ]
        self.assertEqual(suggested_tags(entries, ["g-funk"])[0], "Gangsta Rap")

    def test_strong_narrow_tag_match_is_not_diluted_by_other_applied_tags(self) -> None:
        entries = [
            VideoEntry(id="one", title="Alpha", upload_date="", tags=["G-Funk", "Gangsta Rap", "Broad"]),
            VideoEntry(id="two", title="Beta", upload_date="", tags=["G-Funk", "Gangsta Rap"]),
            VideoEntry(id="three", title="Gamma", upload_date="", tags=["G-Funk", "Gangsta Rap"]),
            *[
                VideoEntry(id=f"broad-{number}", title="Broad", upload_date="", tags=["Broad", "Popular"])
                for number in range(40)
            ],
        ]
        self.assertEqual(suggested_tags(entries, ["G-Funk", "Broad"])[0], "Gangsta Rap")

    def test_library_search_matches_tags(self) -> None:
        library = LibraryIndex(Path("/does/not/exist"))
        tagged = VideoEntry(id="one", title="Alpha", upload_date="", tags=["Late Night"])
        other = VideoEntry(id="two", title="Beta", upload_date="", tags=["Workout"])
        library.rebuild([tagged, other])
        self.assertEqual([entry.id for entry in library.search("late night")], ["one"])

    def test_library_search_normalizes_accents_punctuation_and_typos(self) -> None:
        library = LibraryIndex(Path("/does/not/exist"))
        entry = VideoEntry(
            id="yt-dQw4w9WgXcQ",
            title="Beyoncé — Rock'n'Roll",
            author="The Beatles",
            upload_date="",
        )
        library.rebuild([entry])

        self.assertEqual(library.search("beyonce rock n roll"), [entry])
        self.assertEqual(library.search("beatls"), [entry])
        self.assertEqual(library.search("dQw4w9WgXcQ"), [entry])

    def test_library_search_requires_all_query_words(self) -> None:
        library = LibraryIndex(Path("/does/not/exist"))
        complete = VideoEntry(id="one", title="Late Night", upload_date="")
        partial = VideoEntry(id="two", title="Late", upload_date="")
        library.rebuild([complete, partial])

        self.assertEqual([entry.id for entry in library.search("late night")], ["one"])

    def test_library_search_prefers_exact_field_matches(self) -> None:
        library = LibraryIndex(Path("/does/not/exist"))
        prefix = VideoEntry(id="prefix", title="Midnight Drive Live", upload_date="")
        exact = VideoEntry(id="exact", title="Midnight Drive", upload_date="")
        library.rebuild([prefix, exact])

        self.assertEqual([entry.id for entry in library.search("midnight drive")], ["exact", "prefix"])

    def test_library_search_refreshes_cached_fields_after_metadata_edit(self) -> None:
        library = LibraryIndex(Path("/does/not/exist"))
        entry = VideoEntry(id="one", title="Old title", upload_date="")
        library.rebuild([entry])
        entry.title = "New title"

        self.assertEqual(library.search("new title"), [entry])
        self.assertEqual(library.search("old title"), [])

    def test_library_refresh_reuses_file_index_until_directory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            videos = Path(directory)
            first_path = videos / "one.mp3"
            first_path.write_bytes(b"one")
            first = VideoEntry(id="one", title="One", upload_date="")
            second = VideoEntry(id="two", title="Two", upload_date="")
            library = LibraryIndex(videos)
            with mock.patch("ytarchive.library.build_file_index", wraps=build_file_index) as build:
                library.rebuild([first])
                library.rebuild([first])
                self.assertEqual(build.call_count, 1)

                (videos / "two.mp3").write_bytes(b"two")
                library.rebuild([first, second])
                self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
