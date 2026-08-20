from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.app import MainWindow
from ytarchive.metadata import VideoEntry


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str) -> None:
        self.messages.append(message)


class _Table:
    class _SelectionModel:
        @staticmethod
        def selectedRows() -> list[object]:
            return []

    @staticmethod
    def selectionModel() -> _SelectionModel:
        return _Table._SelectionModel()


class _Label:
    def __init__(self) -> None:
        self.text = ""
        self.tooltip = ""

    def setText(self, text: str) -> None:
        self.text = text

    def setToolTip(self, text: str) -> None:
        self.tooltip = text


class QueueHarness:
    def __init__(self, queue: list[str], current_index: int | None, loop: bool) -> None:
        media_path = Path(__file__)
        self._play_queue = queue
        self._queue_current_index = current_index
        self._queue_loop_enabled = loop
        self.entries = {
            entry_id: VideoEntry(id=entry_id, title=entry_id, upload_date="", path=media_path)
            for entry_id in queue
        }
        self.played: list[str] = []
        self.render_count = 0
        self.reset_called = False
        self._status_bar = _StatusBar()
        self.table = _Table()
        self.queue_time_label = _Label()
        self._playback_entry_id: str | None = None

    def _entry_for_id(self, entry_id: str) -> VideoEntry | None:
        return self.entries.get(entry_id)

    def _render_queue(self) -> None:
        self.render_count += 1

    def _play_entry(self, entry: VideoEntry) -> None:
        self.played.append(entry.id)

    def _reset_playback_tracking(self) -> None:
        self.reset_called = True

    def _show_playing_entry_or_clear_details(self) -> None:
        pass

    def statusBar(self) -> _StatusBar:
        return self._status_bar


class QueuePlaybackTestCase(unittest.TestCase):
    def test_looping_queue_reports_time_until_next_loop(self) -> None:
        harness = QueueHarness(["first"], current_index=0, loop=True)
        harness._playback_entry_id = "first"

        MainWindow._update_queue_time(harness, position=30, duration=120)

        self.assertEqual(harness.queue_time_label.text, "Remaining listening time: 1:30 (looping)")
        self.assertIn("repeats after this pass", harness.queue_time_label.tooltip)

    def test_loop_starts_again_at_the_first_track(self) -> None:
        harness = QueueHarness(["first", "last"], current_index=1, loop=True)

        MainWindow._play_next_queued(harness)

        self.assertEqual(harness.played, ["first"])
        self.assertEqual(harness._queue_current_index, 0)
        self.assertFalse(harness.reset_called)

    def test_loop_skips_missing_tracks_without_repeating_forever(self) -> None:
        harness = QueueHarness(["missing", "available"], current_index=1, loop=True)
        harness.entries["missing"].path = Path("/path/that/does/not/exist")

        MainWindow._play_next_queued(harness)

        self.assertEqual(harness.played, ["available"])
        self.assertEqual(harness._queue_current_index, 1)

    def test_disabled_loop_finishes_at_the_end(self) -> None:
        harness = QueueHarness(["first", "last"], current_index=1, loop=False)

        MainWindow._play_next_queued(harness)

        self.assertEqual(harness.played, [])
        self.assertIsNone(harness._queue_current_index)
        self.assertTrue(harness.reset_called)
        self.assertEqual(harness._status_bar.messages[-1], "Playback queue finished")
