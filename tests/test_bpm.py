from __future__ import annotations

import io
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.bpm import (
    ANALYSIS_HOP_SIZE,
    ANALYSIS_SAMPLE_RATE,
    BpmAnalysis,
    TapTempo,
    _estimate_from_beats,
    _fixed_chunks,
    analyze_bpm,
    apply_bpm_analysis,
)
from ytarchive.metadata import MetadataStore, VideoEntry
from ytarchive.server import _song_payload


class BpmMetadataTestCase(unittest.TestCase):
    def test_fixed_chunks_reassembles_short_pipe_reads(self) -> None:
        class ShortReadStream(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                return super().read(min(size, 3) if size >= 0 else 3)

        self.assertEqual(
            list(_fixed_chunks(ShortReadStream(b"abcdefghijk"), 4)),
            [b"abcd", b"efgh", b"ijk"],
        )

    def test_analysis_decodes_media_through_ffmpeg(self) -> None:
        class FakeArray(list[float]):
            @property
            def size(self) -> int:
                return len(self)

            def astype(self, _dtype, *, copy: bool):
                self.assert_copy = copy
                return self

        class ShortReadStream(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                return super().read(min(size, 127) if size >= 0 else 127)

        class FakeDetector:
            def __init__(self) -> None:
                self.sample_counts: list[int] = []

            def __call__(self, samples: FakeArray) -> list[float]:
                self.sample_counts.append(len(samples))
                return [1.0]

            @staticmethod
            def get_confidence() -> float:
                return 0.9

        class FakeProcess:
            def __init__(self, payload: bytes) -> None:
                self.stdout = ShortReadStream(payload)

            @staticmethod
            def poll() -> int:
                return 0

            @staticmethod
            def wait(timeout=None) -> int:
                return 0

            def kill(self) -> None:
                raise AssertionError("A completed ffmpeg process should not be killed")

        detector = FakeDetector()
        fake_aubio = types.SimpleNamespace(
            float_type="float32",
            tempo=mock.Mock(return_value=detector),
        )
        fake_numpy = types.SimpleNamespace(
            frombuffer=lambda payload, dtype: FakeArray(
                struct.unpack(f"<{len(payload) // 4}f", payload)
            ),
            zeros=lambda size, dtype: FakeArray([0.0] * size),
        )
        decoded_frames = ANALYSIS_HOP_SIZE + 10
        process = FakeProcess(struct.pack(f"<{decoded_frames}f", *([0.0] * decoded_frames)))
        expected = BpmAnalysis(bpm=120, confidence=0.9)

        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "track with spaces.mp3"
            media_path.touch()
            with (
                mock.patch.dict(sys.modules, {"aubio": fake_aubio, "numpy": fake_numpy}),
                mock.patch("ytarchive.bpm.find_external_tool", return_value=r"C:\ffmpeg\ffmpeg.exe"),
                mock.patch("ytarchive.bpm.subprocess.Popen", return_value=process) as popen,
                mock.patch("ytarchive.bpm._estimate_from_beats", return_value=expected) as estimate,
            ):
                self.assertEqual(analyze_bpm(media_path), expected)

        command = popen.call_args.args[0]
        self.assertEqual(command[0], r"C:\ffmpeg\ffmpeg.exe")
        self.assertIn(str(media_path), command)
        self.assertEqual(detector.sample_counts, [ANALYSIS_HOP_SIZE, ANALYSIS_HOP_SIZE])
        beats, confidences = estimate.call_args.args
        self.assertEqual(confidences, [0.9, 0.9])
        self.assertAlmostEqual(beats[0], ANALYSIS_HOP_SIZE / ANALYSIS_SAMPLE_RATE)
        self.assertAlmostEqual(beats[1], decoded_frames / ANALYSIS_SAMPLE_RATE)

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
