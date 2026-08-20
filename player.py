from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import APP_SLUG
from .runtime import find_external_tool, windows_no_console_kwargs


class _MpvInputProxy(QtWidgets.QWidget):
    """Transparent Windows input surface for the native mpv child window.

    When mpv is embedded with ``--wid``, mpv creates a native child HWND
    inside this widget. Qt therefore does not receive the mouse and keyboard
    events that the child consumes. The proxy stays above that child and
    forwards those events through mpv's normal IPC input commands.
    """

    def __init__(self, player: "MpvWidget"):
        super().__init__(player)
        self._player = player
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def _make_native_surface_transparent(self) -> None:
        """Keep the Windows input HWND hit-testable without painting a box."""
        if os.name != "nt":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            get_window_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_window_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_window_long.restype = ctypes.c_ssize_t
            set_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_window_long.restype = ctypes.c_ssize_t
            set_layered_attributes = user32.SetLayeredWindowAttributes
            set_layered_attributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32]
            set_layered_attributes.restype = ctypes.c_bool

            hwnd = ctypes.c_void_p(int(self.winId()))
            exstyle = int(get_window_long(hwnd, -20))  # GWL_EXSTYLE
            set_window_long(hwnd, -20, exstyle | 0x00080000)  # WS_EX_LAYERED
            # Alpha 1 is visually transparent but remains eligible for mouse
            # hit-testing on Windows, unlike a fully zero-alpha layered HWND.
            set_layered_attributes(hwnd, 0, 1, 0x00000002)  # LWA_ALPHA
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    @staticmethod
    def _button_name(button: QtCore.Qt.MouseButton) -> Optional[str]:
        return {
            QtCore.Qt.MouseButton.LeftButton: "MBTN_LEFT",
            QtCore.Qt.MouseButton.MiddleButton: "MBTN_MID",
            QtCore.Qt.MouseButton.RightButton: "MBTN_RIGHT",
            QtCore.Qt.MouseButton.BackButton: "MBTN_BACK",
            QtCore.Qt.MouseButton.ForwardButton: "MBTN_FORWARD",
        }.get(button)

    def _mouse_position_command(self, event: QtGui.QMouseEvent) -> list[Any]:
        x, y = self._player._mouse_point(event.position())
        return ["mouse", x, y]

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self._player._send_input_command(self._mouse_position_command(event))
        event.accept()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        commands = [self._mouse_position_command(event)]
        button = self._button_name(event.button())
        if button is not None:
            commands.append(["keydown", button])
        self._player._send_input_commands(commands)
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        commands = [self._mouse_position_command(event)]
        button = self._button_name(event.button())
        if button is not None:
            commands.append(["keyup", button])
        self._player._send_input_commands(commands)
        event.accept()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        commands = [self._mouse_position_command(event)]
        button = self._button_name(event.button())
        if button is not None:
            commands.append(["keypress", f"{button}_DBL"])
        self._player._send_input_commands(commands)
        event.accept()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta()
        if abs(delta.y()) >= abs(delta.x()) and delta.y():
            key = "WHEEL_UP" if delta.y() > 0 else "WHEEL_DOWN"
        elif delta.x():
            key = "WHEEL_RIGHT" if delta.x() > 0 else "WHEEL_LEFT"
        else:
            event.ignore()
            return
        self._player._send_input_command(["keypress", key])
        event.accept()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = self._player._mpv_key_name(event)
        if key:
            self._player._send_input_command(["keypress", key])
            event.accept()
            return
        event.ignore()

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:
        key = self._player._mpv_key_name(event)
        if key:
            self._player._send_input_command(["keyup", key])
            event.accept()
            return
        event.ignore()


class MpvWidget(QtWidgets.QFrame):
    positionChanged = QtCore.Signal(float, float)
    pauseChanged = QtCore.Signal(bool)
    fileChanged = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setStyleSheet("background: #050505;")
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self._proc: Optional[subprocess.Popen[str]] = None
        if os.name == "nt":
            self._socket_path = rf"\\.\pipe\ytarchive-mpv-{os.getpid()}"
        else:
            self._socket_path = str(Path(tempfile.gettempdir()) / f"ytarchive-mpv-{os.getpid()}.sock")
        self._reader_stop = threading.Event()
        self._request_id = 0
        self._paused = False
        self._path: Optional[Path] = None
        self._audio_only = False
        self.last_error = ""
        self._input_proxy: Optional[_MpvInputProxy] = None
        self._input_size: Optional[tuple[int, int]] = None

    def _native_window_id(self) -> int:
        window_id = int(self.winId())
        # mpv's win32 wid option takes a uint32_t.  Qt can expose the HWND as
        # a signed value when its high bit is set, which would otherwise be
        # parsed as a negative mpv window ID.
        return window_id & 0xFFFFFFFF if os.name == "nt" else window_id

    def start(self) -> bool:
        if self._proc and self._proc.poll() is None:
            if os.name == "nt" or os.path.exists(self._socket_path):
                return True
            self._terminate_process()
        mpv = shutil.which("mpv") or find_external_tool("mpv")
        if not mpv:
            self.last_error = "mpv executable could not be found"
            return False
        try:
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
        }
        popen_kwargs.update(windows_no_console_kwargs())
        self._proc = subprocess.Popen(
            [
                mpv,
                "--idle=yes",
                "--force-window=yes",
                f"--wid={self._native_window_id()}",
                f"--input-ipc-server={self._socket_path}",
                "--input-default-bindings=yes",
                "--input-vo-keyboard=yes",
                "--input-cursor=yes",
                "--osc=yes",
                "--hwdec=auto-safe",
                f"--audio-client-name={APP_SLUG}",
                "--terminal=no",
            ],
            **popen_kwargs,
        )
        if not self._wait_for_ipc():
            self.last_error = "mpv started, but its IPC socket did not become ready"
            self._terminate_process()
            return False
        self._ensure_input_proxy()
        self._observe()
        return True

    def play(self, path: Path) -> None:
        self._path = path
        if not self.start():
            return
        if self.command(["loadfile", str(path), "replace"]) is not None:
            self.set_audio_only(self._audio_only)
            self.set_paused(False)
            # mpv may create or recreate its native video child while the
            # file is loading, so raise the proxy again after that child
            # exists and keep keyboard focus on the proxy.
            self._ensure_input_proxy()
            self._refresh_input_size()
            QtCore.QTimer.singleShot(250, self._refresh_input_size)
            self._focus_embedded_window()
            self.fileChanged.emit(str(path))

    def _focus_embedded_window(self) -> None:
        """Give the native mpv child focus after a Windows embedded launch."""
        if os.name != "nt":
            self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            return
        if self._input_proxy is not None and self._input_proxy.isVisible():
            self._input_proxy.raise_()
            self._input_proxy.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetWindow.restype = ctypes.c_void_p
            user32.SetFocus.argtypes = [ctypes.c_void_p]
            parent = ctypes.c_void_p(self._native_window_id())
            child = user32.GetWindow(parent, 5)  # GW_CHILD
            if child:
                user32.SetFocus(child)
                return
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _mpv_key_name(event: QtGui.QKeyEvent) -> str:
        """Translate a Qt key event into mpv's input binding spelling."""
        special_keys = {
            int(QtCore.Qt.Key.Key_Space): "SPACE",
            int(QtCore.Qt.Key.Key_Return): "ENTER",
            int(QtCore.Qt.Key.Key_Enter): "ENTER",
            int(QtCore.Qt.Key.Key_Escape): "ESC",
            int(QtCore.Qt.Key.Key_Backspace): "BS",
            int(QtCore.Qt.Key.Key_Delete): "DEL",
            int(QtCore.Qt.Key.Key_Insert): "INS",
            int(QtCore.Qt.Key.Key_Tab): "TAB",
            int(QtCore.Qt.Key.Key_Backtab): "TAB",
            int(QtCore.Qt.Key.Key_Left): "LEFT",
            int(QtCore.Qt.Key.Key_Right): "RIGHT",
            int(QtCore.Qt.Key.Key_Up): "UP",
            int(QtCore.Qt.Key.Key_Down): "DOWN",
            int(QtCore.Qt.Key.Key_Home): "HOME",
            int(QtCore.Qt.Key.Key_End): "END",
            int(QtCore.Qt.Key.Key_PageUp): "PGUP",
            int(QtCore.Qt.Key.Key_PageDown): "PGDWN",
            int(QtCore.Qt.Key.Key_F1): "F1",
            int(QtCore.Qt.Key.Key_F2): "F2",
            int(QtCore.Qt.Key.Key_F3): "F3",
            int(QtCore.Qt.Key.Key_F4): "F4",
            int(QtCore.Qt.Key.Key_F5): "F5",
            int(QtCore.Qt.Key.Key_F6): "F6",
            int(QtCore.Qt.Key.Key_F7): "F7",
            int(QtCore.Qt.Key.Key_F8): "F8",
            int(QtCore.Qt.Key.Key_F9): "F9",
            int(QtCore.Qt.Key.Key_F10): "F10",
            int(QtCore.Qt.Key.Key_F11): "F11",
            int(QtCore.Qt.Key.Key_F12): "F12",
        }
        key_code = int(event.key())
        name = special_keys.get(key_code)
        if name is None:
            text = event.text()
            name = text if text and text.isprintable() else ""
        if not name and int(QtCore.Qt.Key.Key_A) <= key_code <= int(QtCore.Qt.Key.Key_Z):
            name = chr(key_code).lower()
        elif not name and int(QtCore.Qt.Key.Key_0) <= key_code <= int(QtCore.Qt.Key.Key_9):
            name = chr(key_code)
        if not name:
            return ""
        modifiers = event.modifiers()
        prefix = ""
        if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
            prefix += "ctrl+"
        if modifiers & QtCore.Qt.KeyboardModifier.AltModifier:
            prefix += "alt+"
        if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier and name not in {"SPACE", "TAB"}:
            prefix += "shift+"
        if modifiers & QtCore.Qt.KeyboardModifier.MetaModifier:
            prefix += "meta+"
        return prefix + name

    def _send_input_command(self, command: list[Any]) -> None:
        """Send an input event without waiting or retrying on the UI thread."""
        self._send_input_commands([command])

    def _send_input_commands(self, commands: list[list[Any]]) -> None:
        """Send ordered input events over one IPC connection."""
        if not self._proc or self._proc.poll() is not None:
            return
        payload_parts: list[bytes] = []
        for command in commands:
            self._request_id += 1
            payload_parts.append(json.dumps({"command": command, "request_id": self._request_id}).encode() + b"\n")
        payload = b"".join(payload_parts)
        for _attempt in range(3):
            if self._send(payload, wait_response=False) is not None:
                return
            time.sleep(0.01)

    def _mouse_point(self, position: QtCore.QPointF) -> tuple[int, int]:
        """Convert Qt's logical coordinates to mpv's OSD pixel coordinates."""
        logical_width = self._input_proxy.width() if self._input_proxy is not None else self.width()
        logical_height = self._input_proxy.height() if self._input_proxy is not None else self.height()
        logical_width = max(1, logical_width)
        logical_height = max(1, logical_height)
        target_width, target_height = self._input_size or (
            max(1, round(logical_width * float(self.devicePixelRatioF()))),
            max(1, round(logical_height * float(self.devicePixelRatioF()))),
        )
        x = round(position.x() * target_width / logical_width)
        y = round(position.y() * target_height / logical_height)
        return (
            max(0, min(target_width - 1, x)),
            max(0, min(target_height - 1, y)),
        )

    def _refresh_input_size(self) -> None:
        if os.name != "nt" or not self._proc or self._proc.poll() is not None:
            return
        width = self._get_property("osd-width")
        height = self._get_property("osd-height")
        try:
            width = int(width)
            height = int(height)
        except (TypeError, ValueError):
            return
        if width > 0 and height > 0:
            self._input_size = (width, height)

    def _ensure_input_proxy(self) -> None:
        if os.name != "nt":
            return
        if self._input_proxy is None:
            self._input_proxy = _MpvInputProxy(self)
        self._input_proxy.setGeometry(self.rect())
        self._input_proxy.show()
        self._input_proxy._make_native_surface_transparent()
        self._input_proxy.raise_()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._input_size = None
        if self._input_proxy is not None:
            self._input_proxy.setGeometry(self.rect())
            self._input_proxy.raise_()
            QtCore.QTimer.singleShot(0, self._refresh_input_size)

    def set_paused(self, paused: bool) -> bool:
        paused = bool(paused)
        if self.command(["set_property", "pause", paused], wait_response=True) is None:
            return False
        changed = paused != self._paused
        self._paused = paused
        if changed:
            self.pauseChanged.emit(paused)
        return True

    def toggle_pause(self) -> bool:
        paused = not self.is_paused()
        return paused if self.set_paused(paused) else self._paused

    def seek_seconds(self, seconds: float) -> bool:
        """Seek to an exact absolute timestamp instead of a prior keyframe."""
        return self.command(["seek", max(0.0, float(seconds)), "absolute+exact"], wait_response=True) is not None

    def seek_percent(self, percent: float) -> bool:
        """Compatibility helper for callers that still use percentages."""
        percent = max(0.0, min(100.0, float(percent)))
        return self.command(["seek", percent, "absolute-percent+exact"], wait_response=True) is not None

    def set_volume(self, value: int) -> None:
        self.command(["set_property", "volume", value])

    def set_audio_only(self, enabled: bool) -> None:
        self._audio_only = enabled
        value: Any = "no" if enabled else "auto"
        self.command(["set_property", "vid", value])

    @property
    def audio_only(self) -> bool:
        return self._audio_only

    def command(self, command: list[Any], *, wait_response: bool = False) -> Optional[Any]:
        self._request_id += 1
        payload = json.dumps({"command": command, "request_id": self._request_id}).encode() + b"\n"
        deadline = time.time() + 1.5
        last_error = ""
        while time.time() < deadline:
            response = self._send(payload, wait_response=wait_response)
            if response is not None:
                self.last_error = ""
                return response
            last_error = self.last_error
            time.sleep(0.05)
        self.last_error = last_error or f"mpv IPC command timed out: {command[0] if command else ''}"
        return None

    def _send(self, payload: bytes, wait_response: bool = True) -> Optional[bytes]:
        if os.name == "nt":
            return self._send_windows(payload, wait_response)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(self._socket_path)
                sock.sendall(payload)
                if not wait_response:
                    self.last_error = ""
                    return b""
                self.last_error = ""
                return sock.recv(65536)
        except OSError as exc:
            self.last_error = str(exc)
            return None

    def _send_windows(self, payload: bytes, wait_response: bool) -> Optional[bytes]:
        try:
            with open(self._socket_path, "r+b", buffering=0) as pipe:
                pipe.write(payload)
                if not wait_response:
                    self.last_error = ""
                    return b""
                deadline = time.time() + 0.5
                chunks: list[bytes] = []
                while time.time() < deadline:
                    chunk = pipe.readline()
                    if chunk:
                        chunks.append(chunk)
                        if chunk.endswith(b"\n"):
                            break
                    else:
                        time.sleep(0.01)
                if not chunks:
                    self.last_error = "timed out waiting for mpv IPC response"
                    return None
                self.last_error = ""
                return b"".join(chunks)
        except OSError as exc:
            self.last_error = str(exc)
            return None

    def _wait_for_ipc(self) -> bool:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                self.last_error = f"mpv exited with code {self._proc.returncode}"
                return False
            if os.name == "nt" or os.path.exists(self._socket_path):
                self._request_id += 1
                payload = json.dumps({"command": ["get_property", "mpv-version"], "request_id": self._request_id}).encode() + b"\n"
                if self._send(payload, wait_response=True) is not None:
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self._reader_stop.set()
        if self._input_proxy is not None:
            self._input_proxy.hide()
        if self._proc and self._proc.poll() is None and (os.name == "nt" or os.path.exists(self._socket_path)):
            self.command(["quit"])
        self._terminate_process()
        try:
            if os.name != "nt" and os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

    def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.SubprocessError:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except subprocess.SubprocessError:
                pass

    def _observe(self) -> None:
        self.command(["observe_property", 1, "time-pos"])
        self.command(["observe_property", 2, "duration"])
        self.command(["observe_property", 3, "pause"])

    def query_position(self) -> tuple[float, float]:
        pos = self._get_property("time-pos") or 0.0
        duration = self._get_property("duration") or 0.0
        return float(pos or 0), float(duration or 0)

    def is_paused(self) -> bool:
        value = self._get_property("pause")
        if value is None:
            return self._paused
        paused = bool(value)
        changed = paused != self._paused
        self._paused = paused
        if changed:
            self.pauseChanged.emit(paused)
        return paused

    def has_reached_eof(self) -> bool:
        """Whether mpv has finished the current file and returned to idle."""
        # ``eof-reached`` is not reliably retained by every mpv backend after
        # it has returned to --idle mode.  idle-active is the stable fallback
        # for the same state.
        return bool(self._get_property("eof-reached")) or bool(self._get_property("idle-active"))

    def _get_property(self, name: str) -> Any:
        self._request_id += 1
        payload = {"command": ["get_property", name], "request_id": self._request_id}
        raw = self._send(json.dumps(payload).encode() + b"\n", wait_response=True)
        if raw is None:
            return None
        try:
            response = json.loads(raw.decode().splitlines()[0])
            return response.get("data")
        except (json.JSONDecodeError, IndexError):
            return None

    @property
    def current_path(self) -> Optional[Path]:
        return self._path


class AspectRatioWidget(QtWidgets.QWidget):
    def __init__(self, child: QtWidgets.QWidget, ratio: float = 16 / 9, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.child = child
        self.ratio = ratio
        self._minimum_width = 426
        self._minimum_height = 240
        self.child.setParent(self)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAutoFillBackground(True)
        self.setStyleSheet("background: #050505;")
        self.setMinimumSize(self._minimum_width, self._minimum_height)
        policy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def resizeEvent(self, event) -> None:
        self.child.setGeometry(self.rect())
        super().resizeEvent(event)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(self._minimum_height, int(max(1, width) / self.ratio))

    def sizeHint(self):
        return QtCore.QSize(960, 540)

    def minimumSizeHint(self):
        return QtCore.QSize(self._minimum_width, self._minimum_height)
