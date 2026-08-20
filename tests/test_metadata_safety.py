from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.metadata import MetadataStore, VideoEntry


class MetadataSafetyTestCase(unittest.TestCase):
    def test_deferred_upserts_flush_as_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            store = MetadataStore(path)
            store.upsert(VideoEntry(id="seed", title="Seed", upload_date=""))

            with mock.patch.object(store, "_save_unlocked", wraps=store._save_unlocked) as save:
                store.upsert(VideoEntry(id="first", title="First", upload_date=""), save=False)
                store.upsert(VideoEntry(id="second", title="Second", upload_date=""), save=False)
                self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"seed"})
                store.save()

            self.assertEqual(save.call_count, 1)
            loaded = MetadataStore(path)
            loaded.load()
            self.assertEqual(loaded.ids(), {"seed", "first", "second"})

    def test_bulk_media_field_merge_uses_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            store = MetadataStore(path)
            store.bulk_upsert(
                [
                    VideoEntry(id="one", title="One", upload_date=""),
                    VideoEntry(id="two", title="Two", upload_date=""),
                ]
            )

            with mock.patch.object(store, "_save_unlocked", wraps=store._save_unlocked) as save:
                changed = store.update_media_fields_bulk(
                    {
                        "one": (120.0, 1000, 192),
                        "two": (240.0, 1200, 256),
                    }
                )

            self.assertEqual(changed, 2)
            self.assertEqual(save.call_count, 1)
            loaded = MetadataStore(path)
            loaded.load()
            self.assertEqual(loaded.get("one").duration_seconds, 120.0)
            self.assertEqual(loaded.get("two").audio_bitrate_kbps, 256)

    def test_concurrent_store_instances_preserve_both_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            MetadataStore(path).upsert(VideoEntry(id="seed", title="Seed", upload_date=""))
            first = MetadataStore(path)
            second = MetadataStore(path)
            ready = threading.Barrier(3)
            errors: list[BaseException] = []

            def write(store: MetadataStore, entry_id: str) -> None:
                try:
                    ready.wait()
                    store.upsert(VideoEntry(id=entry_id, title=entry_id, upload_date=""))
                except BaseException as exc:  # pragma: no cover - turns thread failures into test failures
                    errors.append(exc)

            threads = [
                threading.Thread(target=write, args=(first, "first")),
                threading.Thread(target=write, args=(second, "second")),
            ]
            for thread in threads:
                thread.start()
            ready.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            loaded = MetadataStore(path)
            loaded.load()
            self.assertEqual(loaded.ids(), {"seed", "first", "second"})

    def test_media_field_merge_preserves_newer_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            store = MetadataStore(path)
            store.upsert(VideoEntry(id="song", title="Song", upload_date="", tags=["ambient"]))

            stale = store.get("song")
            self.assertIsNotNone(stale)
            current = store.get("song")
            self.assertIsNotNone(current)
            assert current is not None
            current.play_count = 1
            current.lyrics = "new lyrics"
            store.upsert(current)

            assert stale is not None
            stale.duration_seconds = 120
            self.assertTrue(store.update_media_fields(stale.id, duration=120, bitrate=1000, audio_bitrate=192))

            loaded = MetadataStore(path)
            loaded.load()
            merged = loaded.get("song")
            self.assertIsNotNone(merged)
            assert merged is not None
            self.assertEqual(merged.play_count, 1)
            self.assertEqual(merged.lyrics, "new lyrics")
            self.assertEqual(merged.tags, ["ambient"])
            self.assertEqual(merged.duration_seconds, 120)
            self.assertEqual(merged.bitrate_kbps, 1000)
            self.assertEqual(merged.audio_bitrate_kbps, 192)

    def test_save_keeps_previous_snapshot_as_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            store = MetadataStore(path)
            store.upsert(VideoEntry(id="first", title="First", upload_date=""))
            store.upsert(VideoEntry(id="second", title="Second", upload_date=""))

            backup = path.with_suffix(".json.bak")
            self.assertTrue(backup.exists())
            self.assertEqual(set(json.loads(backup.read_text(encoding="utf-8"))), {"first"})
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"first", "second"})

    def test_corrupt_metadata_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            path.write_text("{broken", encoding="utf-8")

            store = MetadataStore(path)
            store.load()

            self.assertFalse(path.exists())
            quarantined = list(path.parent.glob("video_metadata.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{broken")
            self.assertEqual(store.ids(), set())

    def test_non_object_metadata_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video_metadata.json"
            path.write_text("[]", encoding="utf-8")

            MetadataStore(path).load()

            self.assertFalse(path.exists())
            self.assertEqual(len(list(path.parent.glob("video_metadata.json.corrupt-*"))), 1)
