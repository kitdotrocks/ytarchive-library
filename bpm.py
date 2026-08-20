"""Tempo analysis for local media files.

The dependency is deliberately optional: the rest of the library remains usable
without aubio, and tracks can still have their BPM entered by hand.
"""
from __future__ import annotations

import statistics
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

from .runtime import find_external_tool, windows_no_console_kwargs


BPM_ANALYSIS_VERSION = 4
MIN_CONFIDENCE = 0.70
INTERVAL_TOLERANCE = 0.12
ANALYSIS_SAMPLE_RATE = 44_100
ANALYSIS_WINDOW_SIZE = 1024
ANALYSIS_HOP_SIZE = 512


@dataclass(frozen=True)
class BpmAnalysis:
    bpm: int
    confidence: float


@dataclass
class TapTempo:
    """Estimate a tempo from recent, user-tapped beats.

    A pause starts a fresh measurement, and using the median interval makes a
    single slightly early or late tap less disruptive than an arithmetic mean.
    """

    timeout_seconds: float = 3.0
    max_taps: int = 8
    timestamps: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.timestamps.clear()

    @property
    def tap_count(self) -> int:
        return len(self.timestamps)

    def tap(self, timestamp: Optional[float] = None) -> Optional[int]:
        """Record a beat and return the current BPM estimate, if available."""
        now = time.monotonic() if timestamp is None else timestamp
        if self.timestamps and now - self.timestamps[-1] > self.timeout_seconds:
            self.reset()
        self.timestamps.append(now)
        if len(self.timestamps) > self.max_taps:
            del self.timestamps[:-self.max_taps]
        if len(self.timestamps) < 2:
            return None
        intervals = [right - left for left, right in zip(self.timestamps, self.timestamps[1:])]
        median_interval = statistics.median(intervals)
        return round(60.0 / median_interval) if median_interval > 0 else None


def aubio_available() -> bool:
    try:
        import aubio  # noqa: F401
    except Exception:
        return False
    return True


def _ffmpeg_pcm_command(ffmpeg: str, path: Path) -> list[str]:
    """Return a command that decodes the first audio stream to mono float PCM."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(ANALYSIS_SAMPLE_RATE),
        "-acodec",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]


def _fixed_chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    """Yield fixed-size blocks from a pipe, preserving a final short block."""
    pending = bytearray()
    while True:
        data = stream.read(size - len(pending))
        if data:
            pending.extend(data)
        if len(pending) == size:
            yield bytes(pending)
            pending.clear()
            continue
        if not data:
            if pending:
                yield bytes(pending)
            return


def analyze_bpm(path: Path) -> Optional[BpmAnalysis]:
    """Return a high-confidence BPM estimate, or ``None`` when uncertain."""
    if not path.exists():
        return None
    try:
        import aubio
        import numpy

        tempo = aubio.tempo(
            "default",
            ANALYSIS_WINDOW_SIZE,
            ANALYSIS_HOP_SIZE,
            ANALYSIS_SAMPLE_RATE,
        )
    except Exception:
        return None

    ffmpeg = find_external_tool("ffmpeg")
    if not ffmpeg:
        return None
    try:
        process = subprocess.Popen(
            _ffmpeg_pcm_command(ffmpeg, path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **windows_no_console_kwargs(),
        )
    except (OSError, ValueError):
        return None

    beats: list[float] = []
    detector_confidences: list[float] = []
    total_frames = 0
    try:
        if process.stdout is None:
            return None
        for raw_samples in _fixed_chunks(process.stdout, ANALYSIS_HOP_SIZE * 4):
            samples = numpy.frombuffer(raw_samples, dtype="<f4")
            read = int(samples.size)
            if read < ANALYSIS_HOP_SIZE:
                padded = numpy.zeros(ANALYSIS_HOP_SIZE, dtype=aubio.float_type)
                padded[:read] = samples
                samples = padded
            else:
                samples = samples.astype(aubio.float_type, copy=False)
            total_frames += read
            if tempo(samples)[0] != 0:
                beats.append(total_frames / ANALYSIS_SAMPLE_RATE)
                detector_confidences.append(max(0.0, min(1.0, float(tempo.get_confidence()))))
        process.stdout.close()
        if process.wait(timeout=5) != 0:
            return None
    except Exception:
        return None
    finally:
        try:
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        except OSError:
            pass
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    return _estimate_from_beats(beats, detector_confidences)


def _estimate_from_beats(beats: list[float], detector_confidences: list[float]) -> Optional[BpmAnalysis]:
    """Estimate tempo from the dominant rhythmic region of a beat sequence.

    Boundary gaps and isolated transient detections are excluded from scoring,
    which keeps silent or beatless intros/outros from lowering confidence.
    """
    indexed_intervals = [
        (index, right - left)
        for index, (left, right) in enumerate(zip(beats, beats[1:]))
        if right > left
    ]
    if len(indexed_intervals) < 3:
        return None

    # Find the interval with the largest relative-tolerance cluster. Relative
    # comparisons preserve support for unusually high BPM values without an
    # implicit absolute tempo cap.
    best_cluster: list[tuple[int, float]] = []
    for _index, candidate in indexed_intervals:
        cluster = [
            item for item in indexed_intervals
            if abs(item[1] - candidate) <= candidate * INTERVAL_TOLERANCE
        ]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
    if len(best_cluster) < 3:
        return None

    median_interval = statistics.median(value for _index, value in best_cluster)
    rhythmic_intervals = [
        (index, value) for index, value in indexed_intervals
        if abs(value - median_interval) <= median_interval * INTERVAL_TOLERANCE
    ]
    if len(rhythmic_intervals) < 3:
        return None
    bpm = 60.0 / median_interval
    median_deviation = statistics.median(abs(value - median_interval) for _index, value in rhythmic_intervals)
    regularity = max(0.0, 1.0 - median_deviation / max(median_interval * 0.12, 0.001))
    rhythmic_beat_indexes = {
        beat_index
        for interval_index, _value in rhythmic_intervals
        for beat_index in (interval_index, interval_index + 1)
    }
    core_confidences = [
        detector_confidences[index]
        for index in sorted(rhythmic_beat_indexes)
        if index < len(detector_confidences)
    ]
    detector_confidence = statistics.fmean(core_confidences) if core_confidences else 0.0
    coverage = min(1.0, len(rhythmic_intervals) / 12.0)
    consensus = len(rhythmic_intervals) / len(indexed_intervals)
    # Aubio's confidence can be conservative for dense or syncopated percussion.
    # A long, regular dominant interval is independent evidence of a stable
    # tempo, while consensus prevents a tiny accidental cluster from passing.
    confidence = (
        0.20 * detector_confidence
        + 0.30 * regularity
        + 0.35 * consensus
        + 0.15 * coverage
    )
    if confidence < MIN_CONFIDENCE:
        return None
    return BpmAnalysis(bpm=round(bpm), confidence=round(confidence, 3))


def apply_bpm_analysis(entry, *, force: bool = False) -> Optional[BpmAnalysis]:
    """Populate an entry, optionally replacing its existing BPM."""
    if (getattr(entry, "bpm", 0) > 0 and not force) or not getattr(entry, "path", None):
        return None
    result = analyze_bpm(entry.path)
    if result is None:
        return None
    entry.bpm = result.bpm
    entry.bpm_confidence = result.confidence
    entry.bpm_analysis_version = BPM_ANALYSIS_VERSION
    entry.bpm_source = "aubio"
    return result
