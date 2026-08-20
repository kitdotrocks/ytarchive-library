from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from PySide6 import QtCore, QtGui

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.player import MpvWidget, _MpvInputProxy


class MpvWidgetTestCase(unittest.TestCase):
    def test_mpv_key_name_maps_navigation_and_modified_text(self) -> None:
        player = MpvWidget()
        try:
            left = QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_Left,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
            ctrl_a = QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_A,
                QtCore.Qt.KeyboardModifier.ControlModifier,
                "\x01",
            )
            self.assertEqual(player._mpv_key_name(left), "LEFT")
            self.assertEqual(player._mpv_key_name(ctrl_a), "ctrl+a")
        finally:
            player.deleteLater()

    def test_input_command_is_sent_without_waiting_for_a_response(self) -> None:
        player = MpvWidget()
        process = mock.Mock()
        process.poll.return_value = None
        player._proc = process
        try:
            with mock.patch.object(player, "_send", return_value=b"") as send:
                player._send_input_command(["keypress", "SPACE"])

            payload = send.call_args.args[0]
            self.assertTrue(payload.endswith(b"\n"))
            self.assertEqual(json.loads(payload), {
                "command": ["keypress", "SPACE"],
                "request_id": 1,
            })
            self.assertEqual(send.call_args.kwargs, {"wait_response": False})
        finally:
            player._proc = None
            player.deleteLater()

    def test_windows_focus_prefers_the_input_proxy(self) -> None:
        player = MpvWidget()
        proxy = mock.Mock()
        proxy.isVisible.return_value = True
        player._input_proxy = proxy
        try:
            with mock.patch("ytarchive.player.os.name", "nt"):
                player._focus_embedded_window()
            proxy.raise_.assert_called_once_with()
            proxy.setFocus.assert_called_once_with(QtCore.Qt.FocusReason.OtherFocusReason)
        finally:
            player._input_proxy = None
            player.deleteLater()

    def test_mouse_click_sends_valid_press_and_release_events(self) -> None:
        player = MpvWidget()
        proxy = _MpvInputProxy(player)
        player._input_proxy = proxy
        player._input_size = (100, 200)
        proxy.resize(50, 100)
        press = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(5, 10),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        release = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonRelease,
            QtCore.QPointF(5, 10),
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        try:
            with mock.patch.object(player, "_send_input_commands") as send:
                proxy.mousePressEvent(press)
                proxy.mouseReleaseEvent(release)
            self.assertEqual(send.call_args_list, [
                mock.call([["mouse", 10, 20], ["keydown", "MBTN_LEFT"]]),
                mock.call([["mouse", 10, 20], ["keyup", "MBTN_LEFT"]]),
            ])
        finally:
            proxy.deleteLater()
            player.deleteLater()

    def test_windows_wid_is_passed_as_unsigned_32_bit_value(self) -> None:
        player = MpvWidget()
        try:
            with (
                mock.patch("ytarchive.player.os.name", "nt"),
                mock.patch.object(player, "winId", return_value=-1),
            ):
                self.assertEqual(player._native_window_id(), 0xFFFFFFFF)
        finally:
            player.deleteLater()

    def test_embedded_player_enables_osc_and_hides_windows_console(self) -> None:
        player = MpvWidget()
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch("ytarchive.player.shutil.which", return_value="mpv.exe"),
            mock.patch("ytarchive.player.subprocess.Popen", return_value=process) as popen,
            mock.patch.object(player, "_wait_for_ipc", return_value=True),
            mock.patch.object(player, "_observe"),
            mock.patch(
                "ytarchive.player.windows_no_console_kwargs",
                return_value={"creationflags": 0x08000000},
            ),
        ):
            self.assertTrue(player.start())

        command = popen.call_args.args[0]
        self.assertIn("--input-cursor=yes", command)
        self.assertIn("--osc=yes", command)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        player._proc = None
        player.deleteLater()


if __name__ == "__main__":
    unittest.main()
