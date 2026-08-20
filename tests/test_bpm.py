from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.bpm import BpmAnalysis, TapTempo, _estimate_from_beats, apply_bpm_analysis
from ytarchive.metadata import MetadataStore, VideoEntry
from ytarchive.server import _song_payload


class BpmMetadataTestCase(unittest.TestCase):
    def test_tap_tempo_uses_recent_median_interval(self) -> None:
        tapper = TapTempo()
        self.assertIsNone(tapper.tap(10.0))
        self.assertEqual(tapper.tap(10.5), 120)
        self.assertEqual(tapper.tap(11.01), 119)
        self.assertEqual(tapper.tap(11.5), 120)
        self.assertEqual(tapper.tap_count, 4)

    def test_tap_tempo_restarts_after_a_pause(self) -> None:
        tapper = TapTempo(timeout_seconds=2.0)
        tapper.tap(10.0)
        self.assertIsNone(tapper.tap(13.0))
        self.assertEqual(tapper.tap_count, 1)
        self.assertEqual(tapper.tap(13.5), 120)

    def test_estimate_ignores_beatless_boundary_gaps_and_stray_beats(self) -> None:
        core = [30.0 + index * 0.5 for index in range(20)]
        beats = [1.0, *core, 115.0]
        confidences = [0.05, *([0.92] * len(core)), 0.05]
        result = _estimate_from_beats(beats, confidences)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.bpm, 120)
        self.assertGreaterEqual(result.confidence, 0.70)

    def test_estimate_rejects_an_irregular_beat_sequence(self) -> None:
        beats = [0.0, 0.2, 0.9, 2.2, 4.1, 7.0]
        self.assertIsNone(_estimate_from_beats(beats, [0.9] * len(beats)))

    def test_interval_consensus_can_overcome_conservative_detector_confidence(self) -> None:
        beats = [index * (60 / 148) for index in range(240)]
        result = _estimate_from_beats(beats, [0.13] * len(beats))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.bpm, 148)
        self.assertGreaterEqual(result.confidence, 0.70)

    def test_high_manual_bpm_round_trips_without_an_upper_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video_metadata.json"
            store = MetadataStore(path)
            store.upsert(VideoEntry(id="song", title="Song", upload_date="", bpm=100_000, bpm_source="manual", bpm_confidence=1.0))
            stored = MetadataStore(path)
            stored.load()
            entry = stored.get("song")
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.bpm, 100_000)
            self.assertEqual(entry.bpm_source, "manual")

    def test_presence_image_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video_metadata.json"
            store = MetadataStore(path)
            store.upsert(VideoEntry(
                id="song", title="Song", upload_date="",
                presence_image_mode="custom_youtube", presence_image_value="dQw4w9WgXcQ",
            ))
            stored = MetadataStore(path)
            stored.load()
            entry = stored.get("song")
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.presence_image_mode, "custom_youtube")
            self.assertEqual(entry.presence_image_value, "dQw4w9WgXcQ")

    def test_analysis_only_populates_a_confident_result(self) -> None:
        entry = VideoEntry(id="song", title="Song", upload_date="", path=Path("/tmp/song.mp3"))
        with mock.patch("ytarchive.bpm.analyze_bpm", return_value=None):
            self.assertIsNone(apply_bpm_analysis(entry))
        self.assertEqual(entry.bpm, 0)

        with mock.patch("ytarchive.bpm.analyze_bpm", return_value=BpmAnalysis(bpm=999, confidence=0.8)):
            result = apply_bpm_analysis(entry)
        self.assertEqual(result, BpmAnalysis(bpm=999, confidence=0.8))
        self.assertEqual(entry.bpm, 999)
        self.assertEqual(entry.bpm_source, "aubio")

    def test_forced_analysis_can_replace_an_existing_bpm(self) -> None:
        entry = VideoEntry(id="song", title="Song", upload_date="", bpm=120, path=Path("/tmp/song.mp3"))
        with mock.patch("ytarchive.bpm.analyze_bpm", return_value=BpmAnalysis(bpm=100_000, confidence=0.8)):
            self.assertIsNone(apply_bpm_analysis(entry))
            result = apply_bpm_analysis(entry, force=True)
        self.assertEqual(result, BpmAnalysis(bpm=100_000, confidence=0.8))
        self.assertEqual(entry.bpm, 100_000)

    def test_subsonic_song_payload_includes_bpm(self) -> None:
        entry = VideoEntry(id="song", title="Song", upload_date="", bpm=100_000)
        self.assertEqual(_song_payload(entry)["bpm"], 100_000)


if __name__ == "__main__":
    unittest.main()
