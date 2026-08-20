from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

from . import APP_COMMAND, APP_NAME, APP_SLUG, PROJECT_REPOSITORY, __version__
from .config import (
    AppConfig,
    SETTING_SPECS,
    STORAGE_SETTING_KEYS,
    effective_settings,
    load_existing_config,
    load_config,
    move_data_root,
    read_ini_settings,
    remember_root_dir,
    resolve_root,
    save_ini_settings,
)
from .bpm import TapTempo, apply_bpm_analysis, aubio_available
from .doctor import (
    DEPENDENCY_HELP,
    DEPENDENCY_LABELS,
    dependency_report,
    format_dependency_report,
    missing_dependencies,
)
from .downloader import (
    CacheUpdater,
    DownloadCancelled,
    test_browser_cookies,
    test_cookie_file,
    is_youtube_media_input,
)
from .ids import canonical_local_id, is_youtube_raw_id, thumbnail_video_id
from .library import LibraryIndex, MEDIA_EXTS, format_date, suggested_tags, tag_counts
from .media import (
    convert_with_ffmpeg,
    detect_media_tags,
    export_filename_base,
    ffprobe_json,
    human_size,
    normalize_upload_date,
    sha256_prefix,
    thumbnail_path,
)
from .metadata import MetadataStore, VideoEntry, normalize_tags
from .player import AspectRatioWidget, MpvWidget
from .runtime import windows_no_console_kwargs
from .server import SimilarityCatalogCache, create_server, similar_song_entries
from .updates import ReleaseInfo, find_update


PRESENCE_IMAGE_MODES = {
    "default": "Default",
    "default_large": "Default Large Image",
    "url": "Image URL",
    "youtube": "YouTube Thumbnail",
    "custom_youtube": "Custom YouTube Thumbnail",
    "discord_key": "Discord Image Key",
    "empty": "Empty",
}

APP_ICON_FILENAME = "logo.svg"
APP_WINDOWS_ICON_FILENAME = "logo.ico"
DISCORD_APPLICATION_SETUP_URL = "https://docs.discord.com/developers/quick-start/getting-started"
SERVER_PASSWORD_REQUIRED_STATUS = "Server not started: a Subsonic password is required"


def _single_instance_lock_path() -> Path:
    """Return the per-user lock path shared by all GUI data roots."""
    location = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    lock_directory = Path(location) if location else Path(tempfile.gettempdir()) / APP_SLUG
    lock_directory.mkdir(parents=True, exist_ok=True)
    return lock_directory / f"{APP_SLUG}.lock"


def _acquire_single_instance_lock(lock_path: Optional[Path] = None) -> Optional[QtCore.QLockFile]:
    """Acquire the GUI lock, returning ``None`` when another instance owns it."""
    path = Path(lock_path) if lock_path is not None else _single_instance_lock_path()
    lock = QtCore.QLockFile(str(path))
    if lock.tryLock(0):
        return lock
    if lock.error() != QtCore.QLockFile.LockError.LockFailedError:
        raise OSError(f"Could not access the application lock file: {path}")
    return None


def _application_icon() -> QtGui.QIcon:
    """Load the bundled application logo in scalable and native formats."""
    package_dir = Path(__file__).resolve().parent
    icon = QtGui.QIcon()
    for filename in (APP_ICON_FILENAME, APP_WINDOWS_ICON_FILENAME):
        icon_path = package_dir / filename
        if icon_path.is_file():
            icon.addFile(str(icon_path))
    return icon


def _set_windows_app_identity() -> None:
    """Give Windows taskbar grouping a stable identity independent of Python."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"kitdotrocks.{APP_SLUG}")
    except (AttributeError, OSError):
        pass


def _format_error_popup_message(source: str, message: object) -> str:
    """Make popup errors identify the app feature that produced them."""
    source_text = str(source).strip() or APP_NAME
    message_text = str(message).strip() or "Unknown error"
    return f"Source: {source_text}\n\n{message_text}"


def _show_error_popup(parent: QtWidgets.QWidget, title: str, message: object, source: str) -> None:
    """Show a consistent, user-readable error with a useful source label."""
    QtWidgets.QMessageBox.warning(parent, title, _format_error_popup_message(source, message))


def _youtube_presence_image(video_id: str, cache: dict[str, Optional[str]]) -> Optional[str]:
    """Return the first available i.ytimg thumbnail, or None for Discord's default."""
    if video_id in cache:
        return cache[video_id]
    for quality in ("maxresdefault", "hqdefault"):
        url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
        try:
            # A ranged GET is more widely supported than HEAD by image CDNs.
            request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= response.status < 300:
                    cache[video_id] = url
                    return url
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            continue
    cache[video_id] = None
    return None


_system_palette: Optional[QtGui.QPalette] = None
QUEUE_ROW_TOKEN_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1


def _blend_palette_colors(foreground: str, background: str, foreground_weight: float) -> QtGui.QColor:
    front = QtGui.QColor(foreground)
    back = QtGui.QColor(background)
    weight = max(0.0, min(1.0, foreground_weight))
    return QtGui.QColor(
        round(front.red() * weight + back.red() * (1.0 - weight)),
        round(front.green() * weight + back.green() * (1.0 - weight)),
        round(front.blue() * weight + back.blue() * (1.0 - weight)),
    )


def _ensure_consistent_qt_style(app: QtWidgets.QApplication) -> None:
    """Use the same cross-platform Qt style as the Linux build on Windows."""
    global _system_palette
    if _system_palette is None:
        # Capture the native palette before switching styles so the system
        # theme can still be detected when the user selects "system".
        _system_palette = QtGui.QPalette(app.palette())
    # The Linux build uses Fusion by default. Select it explicitly on Windows
    # so both platforms use the same widget geometry and painting code.
    if os.name != "nt":
        return
    if app.style().objectName().casefold() != "fusion":
        app.setStyle("Fusion")


def apply_color_scheme(theme: str | bool) -> None:
    """Apply the selected application-wide colour scheme."""
    global _system_palette
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    _ensure_consistent_qt_style(app)
    if isinstance(theme, bool):
        theme = "dark" if theme else "system"
    theme = str(theme).strip().lower()
    themes = {
        "light": ("#ffffff", "#202124", "#ffffff", "#f1f3f4", "#4f7cac", "#ffffff", "#1769aa"),
        "dark": ("#18191b", "#e8eaed", "#242529", "#1e1f22", "#3d6e9f", "#ffffff", "#8ab4f8"),
        "ocean": ("#10232e", "#e5f6ff", "#183743", "#14303a", "#2386a8", "#ffffff", "#7dd3fc"),
        "forest": ("#17241b", "#e5f3e7", "#203226", "#1b2b20", "#3f8f5b", "#ffffff", "#8bd5a0"),
        "sunset": ("#2b1d22", "#fff0e5", "#3a2727", "#312124", "#b76556", "#ffffff", "#ffb199"),
        "purple": ("#211a2b", "#f1e8ff", "#2d2340", "#261d35", "#815ac7", "#ffffff", "#c4a1ff"),
        "oled": ("#000000", "#f2f2f2", "#000000", "#080808", "#4d4d4d", "#ffffff", "#bdbdbd"),
    }
    if theme == "system":
        system_window = _system_palette.color(QtGui.QPalette.ColorRole.Window)
        theme = "dark" if system_window.lightness() < 128 else "light"
    if theme not in themes:
        system_window = _system_palette.color(QtGui.QPalette.ColorRole.Window)
        theme = "dark" if system_window.lightness() < 128 else "light"
    palette = QtGui.QPalette(_system_palette)
    window, text, base, alternate, highlight, highlighted_text, link = themes[theme]
    secondary_text = _blend_palette_colors(text, window, 0.62)
    placeholder_text = _blend_palette_colors(text, base, 0.48)
    disabled_border_color = _blend_palette_colors(text, window, 0.14)
    colors = {
        QtGui.QPalette.ColorRole.Window: window,
        QtGui.QPalette.ColorRole.WindowText: text,
        QtGui.QPalette.ColorRole.Base: base,
        QtGui.QPalette.ColorRole.AlternateBase: alternate,
        QtGui.QPalette.ColorRole.ToolTipBase: base,
        QtGui.QPalette.ColorRole.ToolTipText: text,
        QtGui.QPalette.ColorRole.Text: text,
        QtGui.QPalette.ColorRole.PlaceholderText: placeholder_text,
        QtGui.QPalette.ColorRole.Button: alternate,
        QtGui.QPalette.ColorRole.ButtonText: text,
        QtGui.QPalette.ColorRole.Mid: secondary_text,
        QtGui.QPalette.ColorRole.BrightText: highlighted_text,
        QtGui.QPalette.ColorRole.Highlight: highlight,
        QtGui.QPalette.ColorRole.HighlightedText: highlighted_text,
        QtGui.QPalette.ColorRole.Link: link,
        QtGui.QPalette.ColorRole.Light: _blend_palette_colors("#ffffff", alternate, 0.35).name(),
        QtGui.QPalette.ColorRole.Midlight: _blend_palette_colors("#ffffff", alternate, 0.15).name(),
        QtGui.QPalette.ColorRole.Dark: _blend_palette_colors(alternate, "#000000", 0.45).name(),
        QtGui.QPalette.ColorRole.Shadow: _blend_palette_colors(alternate, "#000000", 0.70).name(),
    }
    for group in (QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorGroup.Inactive):
        for role, color in colors.items():
            palette.setColor(group, role, QtGui.QColor(color))
    disabled = QtGui.QPalette.ColorGroup.Disabled
    disabled_window_text = _blend_palette_colors(text, window, 0.45)
    disabled_text = _blend_palette_colors(text, base, 0.45)
    disabled_button_text = _blend_palette_colors(text, alternate, 0.45)
    disabled_placeholder = _blend_palette_colors(text, base, 0.28)
    disabled_surface = alternate
    disabled_button = alternate
    disabled_colors = {
        QtGui.QPalette.ColorRole.Window: window,
        QtGui.QPalette.ColorRole.WindowText: disabled_window_text,
        QtGui.QPalette.ColorRole.Base: disabled_surface,
        QtGui.QPalette.ColorRole.AlternateBase: disabled_surface,
        QtGui.QPalette.ColorRole.ToolTipBase: base,
        QtGui.QPalette.ColorRole.ToolTipText: disabled_text,
        QtGui.QPalette.ColorRole.Text: disabled_text,
        QtGui.QPalette.ColorRole.PlaceholderText: disabled_placeholder,
        QtGui.QPalette.ColorRole.Button: disabled_button,
        QtGui.QPalette.ColorRole.ButtonText: disabled_button_text,
        # Fusion uses Mid and the bevel roles for disabled borders and tabs;
        # leaving any of these inherited from the host palette reintroduces
        # bright Windows light-mode outlines in an otherwise dark theme.
        QtGui.QPalette.ColorRole.Mid: disabled_border_color,
        QtGui.QPalette.ColorRole.BrightText: disabled_button_text,
        QtGui.QPalette.ColorRole.Highlight: _blend_palette_colors(highlight, disabled_surface, 0.45),
        QtGui.QPalette.ColorRole.HighlightedText: disabled_button_text,
        QtGui.QPalette.ColorRole.Link: _blend_palette_colors(link, window, 0.45),
        QtGui.QPalette.ColorRole.Light: _blend_palette_colors(disabled_surface, window, 0.65),
        QtGui.QPalette.ColorRole.Midlight: _blend_palette_colors(disabled_surface, window, 0.35),
        QtGui.QPalette.ColorRole.Dark: _blend_palette_colors(disabled_surface, "#000000", 0.45),
        QtGui.QPalette.ColorRole.Shadow: _blend_palette_colors(disabled_surface, "#000000", 0.70),
    }
    for role, color in disabled_colors.items():
        palette.setColor(disabled, role, QtGui.QColor(color))
    app.setPalette(palette)
    # Keep this palette-only. A global stylesheet changes QToolButton auto-
    # raise behavior and tab metrics, which made the Windows build diverge
    # from Linux even when both used Fusion.
    app.setStyleSheet("")


class ManagedServer(QtCore.QObject):
    statusChanged = QtCore.Signal(str)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.httpd = None
        self.thread: Optional[threading.Thread] = None
        self.status = "Stopped"
        self.external = False

    def start_if_available(self) -> None:
        if self.httpd or self.external:
            return
        if not self.config.server_password and not self.config.server_password_hash:
            self._set_status(SERVER_PASSWORD_REQUIRED_STATUS)
            return
        try:
            self.httpd = create_server(self.config)
        except OSError as exc:
            if _address_in_use(exc):
                self.external = True
                self._set_status(f"External server already running on {self.config.server_host}:{self.config.server_port}")
                return
            self._set_status(f"Server failed: {exc}")
            return

        self.thread = threading.Thread(target=self.httpd.serve_forever, name=f"{APP_SLUG}-server", daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self._set_status(f"Server running on http://{host}:{port}")

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None
        if self.external:
            self.external = False
            self._set_status("External server left running")
        else:
            self._set_status("Server stopped")

    def _set_status(self, status: str) -> None:
        self.status = status
        self.statusChanged.emit(status)


def _address_in_use(exc: OSError) -> bool:
    return getattr(exc, "errno", None) in {98, 48, 10048} or "address already in use" in str(exc).casefold()


def _missing_dependencies() -> dict[str, list[str]]:
    return missing_dependencies()


def _format_dependency_message(title: str, items: list[str], detail: str) -> str:
    lines = [detail, ""]
    for item in items:
        label = DEPENDENCY_LABELS.get(item, item)
        help_text = DEPENDENCY_HELP.get(item, "Install it and make sure the app can access it.")
        lines.append(f"- {label}: {help_text}")
    return title + "\n\n" + "\n".join(lines)


def _normalise_setup_path(value: str) -> Path:
    """Resolve a setup path without requiring it to exist yet."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Choose a folder before continuing.")
    return Path(raw).expanduser().resolve()


def _save_first_run_config(data_root: Path, *, remember_root: bool = True) -> AppConfig:
    """Create the selected data root and persist the initial configuration."""
    data_root = _normalise_setup_path(str(data_root))
    if data_root.exists() and not data_root.is_dir():
        raise OSError(f"The selected path is not a folder: {data_root}")
    data_root.mkdir(parents=True, exist_ok=True)
    # An empty [Settings] section intentionally leaves every storage location
    # relative to the selected root, using the defaults in config.py.
    save_ini_settings(data_root, {})
    if remember_root:
        remember_root_dir(data_root)
    return load_config(data_root)


class FirstRunDirectoriesPage(QtWidgets.QWizardPage):
    """Collect the single application-data root on first launch."""

    def __init__(self, initial_root: Path, *, root_locked: bool = False):
        super().__init__()
        initial_root = Path(initial_root).expanduser().resolve()
        self.setTitle("Choose your data folder")
        self.setSubTitle(f"{APP_NAME} keeps downloaded media, library information, artwork, playlists, settings, exports, and logs together in this folder.")

        self._data_root_edit = QtWidgets.QLineEdit(str(initial_root))
        self._data_root_edit.setPlaceholderText("Library data folder")
        self._root_locked = root_locked
        self._data_root_edit.setReadOnly(root_locked)

        intro = QtWidgets.QLabel(
            "Choose a folder with enough free space for your collection. You can move it later from Settings."
        )
        intro.setWordWrap(True)

        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow("Library data folder:", self._path_row(self._data_root_edit, f"Choose {APP_NAME} library data folder", enabled=not root_locked))

        self._error_label = QtWidgets.QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #b3261e;")
        self._error_label.hide()

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addSpacing(8)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addStretch(1)

    def _path_row(self, edit: QtWidgets.QLineEdit, title: str, *, enabled: bool = True) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.setEnabled(enabled)
        browse.clicked.connect(lambda: self._browse_for(edit, title))
        layout.addWidget(browse)
        return row

    def _browse_for(self, edit: QtWidgets.QLineEdit, title: str) -> None:
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, title, edit.text().strip() or str(Path.home()))
        if not selected:
            return
        edit.setText(selected)

    def selected_root(self) -> Path:
        return _normalise_setup_path(self._data_root_edit.text())

    def validatePage(self) -> bool:
        try:
            data_root = self.selected_root()
            if data_root.exists() and not data_root.is_dir():
                raise ValueError(f"The selected path is not a folder: {data_root}")
        except (OSError, ValueError) as exc:
            self._error_label.setText(str(exc))
            self._error_label.show()
            return False
        self._error_label.clear()
        self._error_label.hide()
        return True


class FirstRunDependenciesPage(QtWidgets.QWizardPage):
    """Run the same dependency check exposed by the installed doctor command."""

    def __init__(self):
        super().__init__()
        self.setTitle("Check required tools")
        self.setSubTitle(f"{APP_NAME} now checks the tools it uses for playback and online downloads.")

        self._status_label = QtWidgets.QLabel()
        self._status_label.setWordWrap(True)
        self._report = QtWidgets.QPlainTextEdit()
        self._report.setReadOnly(True)
        self._report.setMinimumHeight(280)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report, 1)

    def initializePage(self) -> None:
        report = dependency_report()
        self._report.setPlainText(format_dependency_report(report))
        missing = report["missing"]
        if missing["required"]:
            self._status_label.setText(
                "Some required tools are missing. Rerun the setup helper or install the items listed below. You can still open the app and review its settings."
            )
            self._status_label.setStyleSheet("color: #b3261e;")
        else:
            self._status_label.setText("Everything needed for playback and downloads is ready. Any missing optional features are listed below.")
            self._status_label.setStyleSheet("")


class FirstRunWizard(QtWidgets.QWizard):
    """The modal setup flow shown when an application root has no config yet."""

    def __init__(self, initial_root: Path, *, remember_root: bool = True, root_locked: bool = False):
        super().__init__()
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        icon = _application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(700, 540)
        self._config: Optional[AppConfig] = None
        self._remember_root = remember_root
        self._directories_page = FirstRunDirectoriesPage(initial_root, root_locked=root_locked)
        self._dependencies_page = FirstRunDependenciesPage()
        self.addPage(self._directories_page)
        self.addPage(self._dependencies_page)
        self.setButtonText(QtWidgets.QWizard.WizardButton.NextButton, "Check required tools")
        self.setButtonText(QtWidgets.QWizard.WizardButton.FinishButton, f"Start {APP_NAME}")

    @property
    def config(self) -> Optional[AppConfig]:
        return self._config

    def accept(self) -> None:
        try:
            self._config = _save_first_run_config(
                self._directories_page.selected_root(),
                remember_root=self._remember_root,
            )
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, "Setup could not be saved", str(exc))
            return
        super().accept()


def _run_first_run_setup(
    initial_root: Path,
    *,
    remember_root: bool = True,
    root_locked: bool = False,
) -> Optional[AppConfig]:
    wizard = FirstRunWizard(initial_root, remember_root=remember_root, root_locked=root_locked)
    if wizard.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return wizard.config


class ExportWorker(QtCore.QThread):
    finished = QtCore.Signal(bool, str)

    def __init__(self, src: Path, out: Path):
        super().__init__()
        self.src = src
        self.out = out

    def run(self) -> None:
        self.finished.emit(*convert_with_ffmpeg(self.src, self.out))


class BpmAnalysisWorker(QtCore.QThread):
    progress = QtCore.Signal(int, int, str, int)
    completed = QtCore.Signal(int, int)

    def __init__(self, entries: list[VideoEntry], metadata: MetadataStore, *, force: bool = False):
        super().__init__()
        self.entries = [entry for entry in entries if entry.path and (force or entry.bpm <= 0)]
        self.metadata = metadata
        self.force = force
        self.stop_event = threading.Event()

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        saved = 0
        for index, entry in enumerate(self.entries, start=1):
            if self.stop_event.is_set():
                break
            if apply_bpm_analysis(entry, force=self.force):
                self.metadata.upsert(entry)
                saved += 1
            self.progress.emit(index, len(self.entries), entry.title or entry.id, saved)
        self.completed.emit(saved, len(self.entries))


class VideoTableModel(QtCore.QAbstractTableModel):
    headers = ("Title", "Author", "Date", "ID", "File")

    def __init__(self):
        super().__init__()
        self.rows: list[VideoEntry] = []

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        entry = self.rows[index.row()]
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return entry.id
        if entry.hidden_from_subsonic and role == QtCore.Qt.ItemDataRole.ForegroundRole:
            return QtGui.QBrush(
                QtWidgets.QApplication.palette().color(
                    QtGui.QPalette.ColorGroup.Disabled,
                    QtGui.QPalette.ColorRole.Text,
                )
            )
        if entry.hidden_from_subsonic and role == QtCore.Qt.ItemDataRole.BackgroundRole:
            return QtGui.QBrush(QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.AlternateBase))
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            entry.title,
            entry.author,
            format_date(entry.upload_date),
            entry.id,
            "yes" if entry.path else "missing",
        )
        return values[index.column()]

    def headerData(self, section: int, orientation: QtCore.Qt.Orientation, role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orientation == QtCore.Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def set_rows(self, rows: list[VideoEntry]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def entry_at(self, row: int) -> Optional[VideoEntry]:
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None

    def refresh_entry(self, video_id: str) -> None:
        for row, entry in enumerate(self.rows):
            if entry.id != video_id:
                continue
            top_left = self.index(row, 0)
            bottom_right = self.index(row, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right, [])
            return


class ElidedLabel(QtWidgets.QLabel):
    """A single-line label that keeps the complete value in its tooltip."""

    def __init__(self, text: str = "", parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._full_text = text
        self.setWordWrap(False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setText(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elision()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full_text, QtCore.Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())
        ))


class TrackListWidget(QtWidgets.QListWidget):
    """List widget which reports stable row tokens after an internal drag/drop."""

    orderChanged = QtCore.Signal(list)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        super().dropEvent(event)
        self.orderChanged.emit([
            self.item(row).data(QUEUE_ROW_TOKEN_ROLE)
            for row in range(self.count())
        ])


class TrackCard(QtWidgets.QWidget):
    """Compact, clickable song presentation used by Queue and Similar."""

    selected = QtCore.Signal(str)
    activated = QtCore.Signal(str)

    def __init__(
        self,
        entry: VideoEntry,
        thumbnails_dir: Path,
        *,
        actions: list[tuple[str, str, object]] | None = None,
        now_playing: bool = False,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.entry_id = entry.id
        self._press_pos: Optional[QtCore.QPoint] = None
        self._list_item: Optional[QtWidgets.QListWidgetItem] = None
        self.setMinimumHeight(92)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(8)
        self.thumbnail = QtWidgets.QLabel("No\nthumbnail")
        self.thumbnail.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setFixedSize(70, 70)
        self.thumbnail.setStyleSheet("border: 1px solid palette(mid); color: palette(mid); font-size: 10px;")
        # Prefer the downloaded thumbnail cache for every source.  The raw
        # YouTube ID is only a compatibility fallback for legacy cache names.
        image_suffixes = (".jpg", ".jpeg", ".png", ".webp")
        youtube_id = thumbnail_video_id(entry.id)
        stems = (entry.id, youtube_id, entry.source_id)
        cached_path = next(
            (
                thumbnails_dir / f"{stem}{suffix}"
                for stem in stems if stem
                for suffix in image_suffixes
                if (thumbnails_dir / f"{stem}{suffix}").is_file()
            ),
            None,
        )
        if cached_path:
            pixmap = QtGui.QPixmap(cached_path)
            if not pixmap.isNull():
                # Use a centered crop rather than letterboxing widescreen art.
                scaled = pixmap.scaled(
                    self.thumbnail.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
                x = max(0, (scaled.width() - self.thumbnail.width()) // 2)
                y = max(0, (scaled.height() - self.thumbnail.height()) // 2)
                self.thumbnail.setPixmap(scaled.copy(x, y, self.thumbnail.width(), self.thumbnail.height()))
        layout.addWidget(self.thumbnail)

        text_layout = QtWidgets.QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)
        self.title = ElidedLabel(parent=self)
        title_font = self.title.font()
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.set_full_text(entry.title or entry.id)
        self.byline = ElidedLabel(parent=self)
        self.byline.set_full_text(entry.author or "Unknown artist")
        self.date_label = ElidedLabel(parent=self)
        self.date_label.setStyleSheet("color: palette(mid);")
        self.date_label.set_full_text(format_date(entry.upload_date) if entry.upload_date else "Unknown upload date")
        self.id_label = ElidedLabel(parent=self)
        self.id_label.setStyleSheet("color: palette(mid);")
        self.id_label.set_full_text(entry.id)
        title_row = QtWidgets.QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(self.title, 1)
        if now_playing:
            now_playing_label = QtWidgets.QLabel("▶ NOW PLAYING")
            now_playing_label.setObjectName("nowPlayingBadge")
            now_playing_label.setStyleSheet(
                "background: palette(highlight); color: palette(highlighted-text); border: 1px solid palette(highlight); "
                "border-radius: 3px; padding: 1px 4px; font-size: 9px; font-weight: 700;"
            )
            title_row.addWidget(now_playing_label)
            self.setObjectName("nowPlayingCard")
            self.setStyleSheet(
                "#nowPlayingCard { background: palette(alternate-base); border: 2px solid palette(highlight); border-radius: 5px; }"
            )
        text_layout.addLayout(title_row)
        text_layout.addWidget(self.byline)
        text_layout.addWidget(self.date_label)
        text_layout.addWidget(self.id_label)
        layout.addLayout(text_layout, 1)

        for text, tooltip, callback in actions or []:
            button = QtWidgets.QToolButton(self)
            button.setText(text)
            button.setToolTip(tooltip)
            button.setAutoRaise(True)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            layout.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            list_widget = self._track_list_widget()
            if list_widget and self._list_item:
                list_widget.setCurrentItem(self._list_item)
            self.selected.emit(self.entry_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.activated.emit(self.entry_id)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        list_widget = self._track_list_widget()
        if (
            list_widget and self._list_item and self._press_pos is not None
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QtWidgets.QApplication.startDragDistance()
        ):
            list_widget.setCurrentItem(self._list_item)
            list_widget.startDrag(QtCore.Qt.DropAction.MoveAction)
            self._press_pos = None
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _track_list_widget(self) -> Optional[TrackListWidget]:
        parent = self.parentWidget()
        while parent and not isinstance(parent, TrackListWidget):
            parent = parent.parentWidget()
        return parent if isinstance(parent, TrackListWidget) else None

    def set_list_item(self, item: QtWidgets.QListWidgetItem) -> None:
        self._list_item = item


class TagCompleter(QtWidgets.QCompleter):
    """Complete only the tag currently being typed in a comma-separated list."""

    def splitPath(self, path: str) -> list[str]:
        return [path.rsplit(",", 1)[-1].strip()]

    def pathFromIndex(self, index: QtCore.QModelIndex) -> str:
        completion = super().pathFromIndex(index)
        editor = self.widget()
        if not isinstance(editor, QtWidgets.QLineEdit) or "," not in editor.text():
            return completion
        preceding, _separator, _fragment = editor.text().rpartition(",")
        return f"{preceding}, {completion}"


class TagEditor(QtWidgets.QWidget):
    """Compact per-song tag editor with a horizontally ordered pill row."""

    tagsChanged = QtCore.Signal(list)
    tagsCopied = QtCore.Signal(str)

    _TAG_SCROLLBAR_STYLE = """
        QScrollBar:horizontal {
            background: transparent;
            border: none;
            height: 7px;
            margin: 1px 6px 0 6px;
        }
        QScrollBar::handle:horizontal {
            background: palette(mid);
            border: none;
            border-radius: 3px;
            min-width: 28px;
        }
        QScrollBar::handle:horizontal:hover {
            background: palette(highlight);
        }
        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {
            width: 0px;
            background: transparent;
            border: none;
        }
        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {
            background: transparent;
        }
    """

    _TAG_SCROLL_MIN_HEIGHT = 38
    _SUGGESTION_SCROLL_MIN_HEIGHT = 40

    @classmethod
    def _style_tag_scroll_area(cls, scroll: QtWidgets.QScrollArea) -> None:
        scroll.horizontalScrollBar().setStyleSheet(cls._TAG_SCROLLBAR_STYLE)

    @classmethod
    def _fit_tag_scroll_area(
        cls, scroll: QtWidgets.QScrollArea, container: QtWidgets.QWidget, minimum_height: int
    ) -> None:
        """Keep the pill row clear of the horizontal scrollbar at every scale."""
        content_height = container.sizeHint().height()
        scrollbar_height = scroll.horizontalScrollBar().sizeHint().height()
        scroll.setFixedHeight(max(minimum_height, content_height + scrollbar_height + 3))

    def _update_minimum_height(self) -> None:
        """Prevent the parent details grid from compressing the tag editor."""
        self.setMinimumHeight(self.layout().sizeHint().height())
        self.updateGeometry()

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._tags: list[str] = []
        self._counts: dict[str, int] = {}
        self._suggestions: list[str] = []
        self._disabled_tags: frozenset[str] = frozenset()
        self._editing = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.pill_container = QtWidgets.QWidget()
        self.pill_layout = QtWidgets.QHBoxLayout(self.pill_container)
        self.pill_layout.setContentsMargins(0, 0, 0, 0)
        self.pill_layout.setSpacing(5)
        self.pill_layout.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.pill_container)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(self._TAG_SCROLL_MIN_HEIGHT)
        self._style_tag_scroll_area(scroll)
        self._pill_scroll = scroll
        layout.addWidget(scroll)

        self.suggestion_section = QtWidgets.QWidget()
        suggestion_row = QtWidgets.QHBoxLayout(self.suggestion_section)
        suggestion_row.setContentsMargins(0, 0, 0, 4)
        suggestion_row.setSpacing(5)
        suggestion_label = QtWidgets.QLabel("Suggested tags")
        suggestion_label.setStyleSheet("color: palette(link); font-weight: 600;")
        suggestion_row.addWidget(suggestion_label)
        self.suggestion_container = QtWidgets.QWidget()
        self.suggestion_layout = QtWidgets.QHBoxLayout(self.suggestion_container)
        self.suggestion_layout.setContentsMargins(0, 0, 0, 0)
        self.suggestion_layout.setSpacing(5)
        self.suggestion_layout.addStretch(1)
        suggestion_scroll = QtWidgets.QScrollArea()
        suggestion_scroll.setWidgetResizable(True)
        suggestion_scroll.setWidget(self.suggestion_container)
        suggestion_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        suggestion_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        suggestion_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        suggestion_scroll.setFixedHeight(self._SUGGESTION_SCROLL_MIN_HEIGHT)
        self._style_tag_scroll_area(suggestion_scroll)
        self._suggestion_scroll = suggestion_scroll
        suggestion_row.addWidget(suggestion_scroll, stretch=1)
        layout.addWidget(self.suggestion_section)

        self.add_section = QtWidgets.QWidget()
        add_row = QtWidgets.QHBoxLayout(self.add_section)
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(5)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("Add a tag")
        self.input.setToolTip("Start typing for tag suggestions based on this song's current tags, or enter a new tag.")
        self._suggestion_model = QtCore.QStringListModel(self)
        self._completer = TagCompleter(self._suggestion_model, self)
        self._completer.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
        self.input.setCompleter(self._completer)
        self.input.returnPressed.connect(self._add_tags)
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.setFixedWidth(56)
        self.add_button.clicked.connect(self._add_tags)
        self.copy_button = QtWidgets.QPushButton("Copy Tags")
        self.copy_button.setToolTip("Copy this song's tags as a comma-separated list")
        self.copy_button.clicked.connect(self._copy_tags)
        add_row.addWidget(self.input, stretch=1)
        add_row.addWidget(self.add_button)
        add_row.addWidget(self.copy_button)
        layout.addWidget(self.add_section)
        self.setEnabled(False)

    def set_tags(
        self, tags: list[str], counts: dict[str, int], suggestions: Optional[list[str]] = None,
        disabled_tags: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self._tags = normalize_tags(tags)
        self._counts = dict(counts)
        self._suggestions = suggestions or []
        self._disabled_tags = frozenset(tag.casefold() for tag in disabled_tags)
        self._suggestion_model.setStringList(self._suggestions)
        self._refresh_tag_pills()
        self._set_suggestion_pills()
        self.copy_button.setEnabled(bool(self._tags))
        self._update_minimum_height()

    def set_editing(self, editing: bool) -> None:
        self._editing = editing
        self._refresh_tag_pills()
        self._set_suggestion_pills()
        self.add_section.setVisible(editing)
        self._update_minimum_height()

    def set_disabled_tags(self, disabled_tags: frozenset[str] | set[str]) -> None:
        self._disabled_tags = frozenset(tag.casefold() for tag in disabled_tags)
        self._refresh_tag_pills()

    def _refresh_tag_pills(self) -> None:
        while self.pill_layout.count() > 1:
            item = self.pill_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for tag in self._tags:
            count = self._counts.get(tag.casefold(), 0)
            disabled = tag.casefold() in self._disabled_tags
            pill = QtWidgets.QToolButton()
            # QAbstractButton interprets '&' as a keyboard-mnemonic marker.
            # Escape it for display while retaining the original tag in the
            # click callback below.
            suffix = "   ×" if self._editing else ""
            pill.setText(f"{tag.replace('&', '&&')}   {count}{suffix}")
            tooltip = f"{count} song{'s' if count != 1 else ''} in the library."
            if disabled:
                tooltip += " Disabled for Similar Songs."
            if self._editing:
                tooltip += " Click to remove this tag."
                pill.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                pill.clicked.connect(lambda _checked=False, value=tag: self._remove_tag(value))
            pill.setAutoRaise(False)
            if disabled:
                pill.setStyleSheet(
                    "QToolButton { background: palette(mid); color: palette(base); border: 0; "
                    "border-radius: 12px; padding: 4px 9px; } "
                    "QToolButton:hover { background: palette(dark); }"
                )
            else:
                pill.setStyleSheet(
                    "QToolButton { background: palette(highlight); color: palette(highlighted-text); border: 0; "
                    "border-radius: 12px; padding: 4px 9px; } "
                    "QToolButton:hover { background: palette(dark); }"
                )
            self.pill_layout.insertWidget(self.pill_layout.count() - 1, pill)
        self._fit_tag_scroll_area(self._pill_scroll, self.pill_container, self._TAG_SCROLL_MIN_HEIGHT)

    def _set_suggestion_pills(self) -> None:
        while self.suggestion_layout.count() > 1:
            item = self.suggestion_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for tag in self._suggestions:
            pill = QtWidgets.QToolButton()
            pill.setText(f"+ {tag.replace('&', '&&')}")
            pill.setToolTip("Suggested from tags often used with this song's current tags. Click to add.")
            pill.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            pill.setStyleSheet(
                "QToolButton { background: palette(alternate-base); color: palette(link); border: 1px solid palette(link); "
                "border-radius: 12px; padding: 3px 8px; } "
                "QToolButton:hover { background: palette(highlight); color: palette(highlighted-text); }"
            )
            pill.clicked.connect(lambda _checked=False, value=tag: self._add_suggestion(value))
            self.suggestion_layout.insertWidget(self.suggestion_layout.count() - 1, pill)
        self._fit_tag_scroll_area(
            self._suggestion_scroll,
            self.suggestion_container,
            self._SUGGESTION_SCROLL_MIN_HEIGHT,
        )
        self.suggestion_section.setVisible(self._editing and bool(self._suggestions))

    def _add_tags(self) -> None:
        additions = [part for part in self.input.text().split(",")]
        updated = normalize_tags([*self._tags, *additions])
        self.input.clear()
        if updated != self._tags:
            self.tagsChanged.emit(updated)

    def _remove_tag(self, tag: str) -> None:
        key = tag.casefold()
        self.tagsChanged.emit([item for item in self._tags if item.casefold() != key])

    def _add_suggestion(self, tag: str) -> None:
        self.tagsChanged.emit(normalize_tags([*self._tags, tag]))

    def _copy_tags(self) -> None:
        text = ", ".join(self._tags)
        if not text:
            return
        QtWidgets.QApplication.clipboard().setText(text)
        self.tagsCopied.emit(text)


class CacheUpdateWorker(QtCore.QThread):
    progress = QtCore.Signal(dict)
    finished = QtCore.Signal()

    def __init__(self, config: AppConfig, metadata: MetadataStore):
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.stop_event = threading.Event()

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        try:
            updater.update(self.progress.emit, self.stop_event)
        except DownloadCancelled:
            self.progress.emit({"phase": "cancelled", "message": "Cancelled"})
        except (Exception, SystemExit) as exc:
            self.progress.emit({"phase": "error", "message": str(exc)})
        finally:
            self.finished.emit()


class UpdateCheckWorker(QtCore.QThread):
    """Look up a newer public release without holding up the GUI thread."""

    update_available = QtCore.Signal(object)

    def __init__(self, current_version: str, repository: str = PROJECT_REPOSITORY):
        super().__init__()
        self.current_version = current_version
        self.repository = repository

    def run(self) -> None:
        try:
            release = find_update(self.current_version, self.repository)
        except Exception:
            # Update checks are deliberately best effort.  A missing network,
            # a rate limit, or an unpublished first release must never make
            # the application look broken at startup.
            return
        if release is not None:
            self.update_available.emit(release)


class CacheDialog(QtWidgets.QDialog):
    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataStore,
        parent: Optional[QtWidgets.QWidget] = None,
        worker: Optional[CacheUpdateWorker] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Update Download Cache")
        self.resize(760, 460)
        self.worker = worker or CacheUpdateWorker(config, metadata)
        self.owns_worker = worker is None
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self._last_phase = ""
        self._had_error_or_cancelled = False

        self.status = QtWidgets.QLabel("Preparing...")
        self.playlist_bar = QtWidgets.QProgressBar()
        self.queue_bar = QtWidgets.QProgressBar()
        self.download_bar = QtWidgets.QProgressBar()
        self.active = QtWidgets.QTableWidget(0, 5)
        self.active.setHorizontalHeaderLabels(["Item", "File", "Status", "Progress", "Speed"])
        self.active.horizontalHeader().setStretchLastSection(True)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.shutdown_checkbox = QtWidgets.QCheckBox("Shutdown after completed")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.worker.cancel)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.shutdown_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.cancel_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(QtWidgets.QLabel("Playlists"))
        layout.addWidget(self.playlist_bar)
        layout.addWidget(QtWidgets.QLabel("Queued videos checked"))
        layout.addWidget(self.queue_bar)
        layout.addWidget(QtWidgets.QLabel("Downloads"))
        layout.addWidget(self.download_bar)
        layout.addWidget(self.active)
        layout.addWidget(self.log)
        layout.addLayout(controls)
        if self.owns_worker:
            self.worker.start()

    def _on_progress(self, data: dict) -> None:
        self._last_phase = data.get("phase", self._last_phase)
        if self._last_phase in {"error", "cancelled"}:
            self._had_error_or_cancelled = True
        self.status.setText(data.get("message", ""))
        if data.get("playlist_total"):
            self.playlist_bar.setMaximum(data["playlist_total"])
            self.playlist_bar.setValue(data.get("playlist_done", 0))
        if data.get("queue_total") is not None:
            self.queue_bar.setMaximum(data.get("queue_total") or 1)
            self.queue_bar.setValue(data.get("queue_done", 0))
        if data.get("download_total") is not None:
            self.download_bar.setMaximum(data.get("download_total") or 1)
            self.download_bar.setValue(data.get("download_done", 0))
        if data.get("phase") in {"error", "native", "skip", "done", "cancelled"}:
            self.log.appendPlainText(data.get("message", ""))
        if "active" in data:
            self._render_active(data["active"])

    def _render_active(self, rows: list[dict]) -> None:
        self.active.setRowCount(len(rows))
        for row, item in enumerate(rows):
            total = int(item.get("total") or 0)
            done = int(item.get("completed") or 0)
            pct = f"{int(done / total * 100)}%" if total else "..."
            values = [
                item.get("title", ""),
                item.get("file", ""),
                item.get("status", ""),
                pct,
                human_size(int(item.get("speed") or 0)) + "/s" if item.get("speed") else "",
            ]
            for col, value in enumerate(values):
                self.active.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))

    def _on_finished(self) -> None:
        self.cancel_btn.setText("Close")
        try:
            self.cancel_btn.clicked.disconnect()
        except TypeError:
            pass
        self.cancel_btn.clicked.connect(self.accept)
        if self._last_phase == "done" and not self._had_error_or_cancelled and self.shutdown_checkbox.isChecked():
            self._shutdown_after_completed()

    def _shutdown_after_completed(self) -> None:
        candidates: list[list[str]]
        if os.name == "nt":
            candidates = [["shutdown", "/s", "/t", "0"]]
        elif sys.platform == "darwin":
            candidates = [["osascript", "-e", 'tell application "System Events" to shut down']]
        else:
            candidates = [["systemctl", "poweroff"], ["shutdown", "-h", "now"]]

        last_error = "No supported shutdown command found"
        for cmd in candidates:
            exe = shutil.which(cmd[0])
            if not exe:
                continue
            try:
                subprocess.Popen(
                    [exe, *cmd[1:]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **windows_no_console_kwargs(),
                )
                self.log.appendPlainText("Shutdown requested.")
                return
            except Exception as exc:
                last_error = str(exc)
        self.log.appendPlainText(f"Shutdown failed: {last_error}")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.owns_worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(event)


class _CookieTestWorker(QtCore.QObject):
    finished = QtCore.Signal(bool, str, object, object)

    def __init__(self, source, button, enabled):
        super().__init__()
        self.source = source
        self.button = button
        self.enabled = enabled

    @QtCore.Slot()
    def run(self) -> None:
        try:
            count = test_cookie_file(self.source) if isinstance(self.source, Path) else test_browser_cookies(self.source)
            self.finished.emit(True, f"Cookies loaded and authenticated successfully ({count} cookies loaded).", self.button, self.enabled)
        except (Exception, SystemExit) as exc:
            self.finished.emit(False, f"Cookie test failed: {exc}", self.button, self.enabled)


def _library_statistics(
    config: AppConfig,
    metadata: MetadataStore,
    library: Optional[LibraryIndex] = None,
) -> tuple[tuple[str, str], ...]:
    """Return display-ready statistics for the Library settings page."""
    if library is None:
        library = LibraryIndex(config.download_dir)
        library.rebuild(metadata.entries())
    entries = library.entries
    metadata_entries = len(entries)
    missing_files = sum(1 for entry in entries if not entry.path)

    artists = {entry.author.strip().casefold() for entry in entries if entry.author.strip()}
    tags = {tag.strip().casefold() for entry in entries for tag in entry.tags if tag.strip()}
    tagged_tracks = sum(1 for entry in entries if entry.tags)
    library_duration = sum(max(0.0, entry.duration_seconds) for entry in entries)
    listened_seconds = sum(max(0.0, entry.playback_seconds) for entry in entries)
    total_plays = sum(max(0, entry.play_count) for entry in entries)
    storage_bytes = library.storage_bytes()
    visible_tracks = sum(
        1 for entry in entries if entry.path and not entry.hidden_from_subsonic
    )
    tracks_per_artist = (
        metadata_entries / len(artists)
        if artists
        else 0.0
    )

    statistics = [
        ("Tracks", f"{metadata_entries:,}"),
        ("Missing files", f"{missing_files:,}"),
        ("Artists", f"{len(artists):,}"),
        ("Tracks / artist", f"{tracks_per_artist:,.1f}" if artists else "0"),
        ("Tagged tracks", f"{tagged_tracks:,}"),
        ("Tags", f"{len(tags):,}"),
        (
            "Library duration",
            _format_playback_duration(library_duration) if entries else "0:00",
        ),
        ("Time listened", _format_playback_duration(listened_seconds)),
        ("Plays listened", f"{total_plays:,}"),
        ("Storage used", human_size(storage_bytes)),
        ("Visible in similar songs", f"{visible_tracks:,}"),
    ]
    return tuple(statistics)


class _LibraryStatisticsSignals(QtCore.QObject):
    finished = QtCore.Signal(object)
    failed = QtCore.Signal()


class _LibraryStatisticsTask(QtCore.QRunnable):
    """Calculate Settings' library statistics without blocking the GUI thread."""

    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataStore,
        library: Optional[LibraryIndex],
        signals: _LibraryStatisticsSignals,
    ) -> None:
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.library = library
        self.signals = signals

    def run(self) -> None:
        try:
            self.signals.finished.emit(_library_statistics(self.config, self.metadata, self.library))
        except Exception:
            self.signals.failed.emit()


class SettingsDialog(QtWidgets.QDialog):
    """A staged editor for config.ini.  Nothing is written until Apply."""

    changed = QtCore.Signal()
    _LIBRARY_STATISTIC_NAMES = (
        "Tracks",
        "Missing files",
        "Artists",
        "Tracks / artist",
        "Tagged tracks",
        "Tags",
        "Library duration",
        "Time listened",
        "Plays listened",
        "Storage used",
        "Visible in similar songs",
    )

    def __init__(
        self, config: AppConfig, metadata: MetadataStore, parent: QtWidgets.QWidget,
        apply_callback, tags_changed_callback, select_video_callback, move_data_callback=None,
        switch_library_callback=None,
        library: Optional[LibraryIndex] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.metadata = metadata
        self.apply_callback = apply_callback
        self.tags_changed_callback = tags_changed_callback
        self.select_video_callback = select_video_callback
        self.move_data_callback = move_data_callback
        self.switch_library_callback = switch_library_callback
        self.library = library
        self.setWindowTitle("Settings")
        self.resize(900, 620)
        self.raw_initial = read_ini_settings(config.root_dir)
        self.values = effective_settings(config.root_dir)
        self.original_values = dict(self.values)
        self.reset_keys: set[str] = set()
        self.controls: dict[str, QtWidgets.QWidget] = {}
        self.setting_labels: dict[str, QtWidgets.QLabel] = {}
        self.advanced_rows: dict[str, int] = {}
        self.pending_tag_updates: dict[str, list[str]] = {}
        self._tags_page_loaded = False
        self._tags_page_index = -1
        self._library_statistic_labels: dict[str, QtWidgets.QLabel] = {}

        self.navigation = QtWidgets.QListWidget()
        self.navigation.setFixedWidth(170)
        self.pages = QtWidgets.QStackedWidget()
        for category in ("Library", "Downloads", "Integrations", "Startup", "Appearance"):
            self.navigation.addItem(category)
            self.pages.addWidget(self._build_category_page(category))
        self.navigation.addItem("Tags")
        self._tags_page_index = self.navigation.count() - 1
        self.pages.addWidget(self._settings_page_placeholder("Tag management loads when selected."))
        self.navigation.addItem("Algorithm")
        self.pages.addWidget(self._build_category_page("Algorithm"))
        self.navigation.addItem("Advanced")
        self.pages.addWidget(self._build_advanced_page())
        self.navigation.currentRowChanged.connect(self._settings_page_changed)
        self.navigation.setCurrentRow(0)
        self._update_algorithm_field_enabled()

        self.reset_tab_btn = QtWidgets.QPushButton("Reset This Tab")
        self.reset_tab_btn.clicked.connect(self._reset_current_tab)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.clicked.connect(self._apply)
        apply_close_btn = QtWidgets.QPushButton("Apply and Close")
        apply_close_btn.clicked.connect(self._apply_and_close)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.reset_tab_btn)
        controls.addStretch(1)
        controls.addWidget(close_btn)
        controls.addWidget(self.apply_btn)
        controls.addWidget(apply_close_btn)
        root = QtWidgets.QVBoxLayout(self)
        content = QtWidgets.QHBoxLayout()
        content.addWidget(self.navigation)
        content.addWidget(self.pages, 1)
        root.addLayout(content, 1)
        root.addLayout(controls)
        # Let the dialog paint before doing the library-wide file-stat pass.
        # The tag page is even heavier, so it is built only when selected.
        self._library_statistics_timer = QtCore.QTimer(self)
        self._library_statistics_timer.setSingleShot(True)
        self._library_statistics_timer.timeout.connect(self._start_library_statistics)
        self._library_statistics_timer.start(0)

    @staticmethod
    def _settings_page_placeholder(message: str) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(message)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: palette(mid);")
        layout.addWidget(label)
        return page

    def _settings_page_changed(self, index: int) -> None:
        if index == self._tags_page_index and not self._tags_page_loaded:
            placeholder = self.pages.widget(index)
            page = self._build_tags_page()
            self.pages.removeWidget(placeholder)
            placeholder.deleteLater()
            self.pages.insertWidget(index, page)
            self._tags_page_loaded = True
        self.pages.setCurrentIndex(index)

    def select_category(self, category: str) -> None:
        """Show a settings category when opened from a menu shortcut."""
        matches = self.navigation.findItems(
            str(category),
            QtCore.Qt.MatchFlag.MatchExactly,
        )
        if matches:
            self.navigation.setCurrentItem(matches[0])

    def _build_category_page(self, category: str) -> QtWidgets.QWidget:
        if category == "Library":
            return self._build_library_page()
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        if category == "Algorithm":
            form.addRow(QtWidgets.QLabel("Artists and tags can use rarity-based scoring. BPM adds a smaller score only within the configured distance."))
        for spec in (item for item in SETTING_SPECS if item.category == category):
            if category == "Appearance" and spec.key == "DARK_MODE":
                continue
            if category == "Integrations" and spec.key in {
                "DISCORD_APPLICATION_ID", "DISCORD_PRESENCE_IMAGE_MODE", "DISCORD_PRESENCE_IMAGE_VALUE",
                "DISCORD_PRESENCE_SMALL_IMAGE_MODE", "DISCORD_PRESENCE_SMALL_IMAGE_VALUE",
                "DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL", "DISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE",
                "SERVER_HOST", "SERVER_PORT", "SERVER_USERNAME", "SERVER_PASSWORD",
                "SERVER_PASSWORD_HASH", "SERVER_TIMING",
            }:
                continue
            field = self._create_browser_cookie_settings() if category == "Downloads" and spec.key == "USE_BROWSER_COOKIES" else self._field_for(spec.key, spec.default, spec.sensitive)
            discord_settings: Optional[QtWidgets.QWidget] = None
            subsonic_settings: Optional[QtWidgets.QWidget] = None
            integration_toggle: Optional[QtWidgets.QToolButton] = None
            if category == "Integrations" and spec.key == "DISCORD_RPC":
                integration_toggle, discord_settings = self._create_discord_presence_settings()
            elif category == "Integrations" and spec.key == "SERVER_AUTOSTART":
                integration_toggle, subsonic_settings = self._create_subsonic_settings()
            reset = QtWidgets.QToolButton()
            reset.setText("↺")
            reset.setToolTip(f"Reset {spec.label} to its default")
            reset.clicked.connect(lambda _checked=False, key=spec.key: self._reset_key(key))
            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(field, 1)
            if integration_toggle:
                row.addWidget(integration_toggle)
            row.addWidget(reset)
            wrapper = QtWidgets.QWidget()
            wrapper.setLayout(row)
            label = QtWidgets.QLabel()
            label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.setting_labels[spec.key] = label
            self._refresh_setting_label(spec.key)
            form.addRow(label, wrapper)
            if discord_settings:
                form.addRow(discord_settings)
            if subsonic_settings:
                form.addRow(subsonic_settings)
        if category == "Integrations":
            self._update_presence_default_image_value_enabled()
            self._update_presence_image_value_enabled("DISCORD_PRESENCE_SMALL_IMAGE_MODE")
        form.addItem(QtWidgets.QSpacerItem(1, 1, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding))
        return page

    def _build_library_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        highlight_row = QtWidgets.QHBoxLayout()
        for index, label in enumerate(("Tracks", "Library duration", "Time listened")):
            highlight = QtWidgets.QVBoxLayout()
            if index == 0:
                alignment = QtCore.Qt.AlignmentFlag.AlignLeft
            elif index == 1:
                alignment = QtCore.Qt.AlignmentFlag.AlignHCenter
            else:
                alignment = QtCore.Qt.AlignmentFlag.AlignRight
            name_label = QtWidgets.QLabel(label)
            name_label.setAlignment(alignment)
            value_label = QtWidgets.QLabel("Loading…")
            value_font = value_label.font()
            value_font.setBold(True)
            value_font.setPointSize(18)
            value_label.setFont(value_font)
            value_label.setAlignment(alignment)
            value_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            highlight.addWidget(name_label)
            highlight.addWidget(value_label)
            highlight_row.addLayout(highlight, 1)
            self._library_statistic_labels[label] = value_label
        layout.addLayout(highlight_row)

        intro = QtWidgets.QLabel(
            f"Choose where {APP_NAME} keeps your library."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        root_form = QtWidgets.QFormLayout()
        root_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        root_row = QtWidgets.QWidget()
        root_row_layout = QtWidgets.QHBoxLayout(root_row)
        root_row_layout.setContentsMargins(0, 0, 0, 0)
        root_edit = QtWidgets.QLineEdit(str(self.config.root_dir))
        root_edit.setReadOnly(True)
        root_edit.setToolTip("Use Open existing… to switch libraries, or Move data… to relocate this archive.")
        self.storage_root_edit = root_edit
        open_button = QtWidgets.QPushButton("Open existing…")
        open_button.setToolTip(f"Switch to another existing {APP_NAME} library without moving it.")
        open_button.setEnabled(self.switch_library_callback is not None)
        if self.switch_library_callback is not None:
            open_button.clicked.connect(self._request_switch_library)
        move_button = QtWidgets.QPushButton("Move data…")
        move_button.setToolTip(f"Move the complete {APP_NAME} library data folder.")
        move_button.setEnabled(self.move_data_callback is not None)
        if self.move_data_callback is not None:
            move_button.clicked.connect(self._request_move_data)
        root_row_layout.addWidget(root_edit, 1)
        root_row_layout.addWidget(open_button)
        root_row_layout.addWidget(move_button)
        root_form.addRow("Library data folder:", root_row)
        layout.addLayout(root_form)

        stats_box = QtWidgets.QGroupBox("Library statistics")
        stats_box_layout = QtWidgets.QVBoxLayout(stats_box)
        detail_layout = QtWidgets.QGridLayout()
        detail_layout.setColumnStretch(1, 1)
        detail_layout.setColumnStretch(3, 1)
        highlight_names = {"Tracks", "Library duration", "Time listened"}
        details = [label for label in self._LIBRARY_STATISTIC_NAMES if label not in highlight_names]
        for index, label in enumerate(details):
            row, pair = divmod(index, 2)
            column = pair * 2
            name_label = QtWidgets.QLabel(f"{label}:")
            value_label = QtWidgets.QLabel("Loading…")
            value_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            detail_layout.addWidget(name_label, row, column)
            detail_layout.addWidget(value_label, row, column + 1)
            self._library_statistic_labels[label] = value_label
        stats_box_layout.addLayout(detail_layout)
        layout.addWidget(stats_box)
        layout.addStretch(1)
        return page

    def _start_library_statistics(self) -> None:
        if not self._library_statistic_labels:
            return
        library = self.library
        if library is not None:
            # Avoid reading mutable live lists from the worker while the main
            # window may be refreshing the library.
            library_snapshot = LibraryIndex(library.videos_dir)
            library_snapshot.entries = list(library.entries)
            library_snapshot.file_index = dict(library.file_index)
        else:
            library_snapshot = None
        signals = _LibraryStatisticsSignals()
        signals.finished.connect(
            self._library_statistics_loaded,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            self._library_statistics_failed,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        self._library_statistics_signals = signals
        QtCore.QThreadPool.globalInstance().start(
            _LibraryStatisticsTask(self.config, self.metadata, library_snapshot, signals)
        )

    @QtCore.Slot(object)
    def _library_statistics_loaded(self, statistics: object) -> None:
        values = dict(statistics) if isinstance(statistics, (dict, tuple, list)) else {}
        for name, label in self._library_statistic_labels.items():
            label.setText(values.get(name, "Unavailable"))

    @QtCore.Slot()
    def _library_statistics_failed(self) -> None:
        for label in self._library_statistic_labels.values():
            label.setText("Unavailable")

    def _request_move_data(self) -> None:
        """Apply staged settings before asking the main window to move storage."""
        if self._apply() and self.move_data_callback is not None:
            self.move_data_callback()

    def _request_switch_library(self) -> None:
        """Apply staged settings before asking the main window to switch roots."""
        if self._apply() and self.switch_library_callback is not None:
            self.switch_library_callback()

    @staticmethod
    def _browser_choices() -> tuple[tuple[str, str], ...]:
        return (
            ("chrome", "Chrome / Chromium"), ("firefox", "Firefox"),
            ("edge", "Microsoft Edge"), ("brave", "Brave"),
            ("opera", "Opera"), ("vivaldi", "Vivaldi"), ("safari", "Safari"),
        )

    def _browser_profiles(self, browser: str) -> list[str]:
        profiles = ["Default"]
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roots = {
            "chrome": [home / ".config/google-chrome", local_appdata / "Google/Chrome/User Data"],
            "chromium": [home / ".config/chromium", local_appdata / "Chromium/User Data"],
            "edge": [home / ".config/microsoft-edge", local_appdata / "Microsoft/Edge/User Data"],
            "brave": [home / ".config/BraveSoftware/Brave-Browser", local_appdata / "BraveSoftware/Brave-Browser/User Data"],
            "opera": [home / ".config/opera", appdata / "Opera Software/Opera Stable"],
            "vivaldi": [home / ".config/vivaldi", local_appdata / "Vivaldi/User Data"],
            "firefox": [home / ".mozilla/firefox", appdata / "Mozilla/Firefox/Profiles"],
            "safari": [home / "Library/Safari"],
        }
        for root in roots.get(browser, []):
            if root.is_dir():
                for child in sorted(root.iterdir()):
                    if child.is_dir() and (
                        browser == "firefox" or child.name == "Default" or child.name.startswith("Profile ")
                    ):
                        profiles.append(child.name)
        return list(dict.fromkeys(profiles))

    def _create_browser_cookie_settings(self) -> QtWidgets.QWidget:
        raw = self.values.get("USE_BROWSER_COOKIES", "")
        parsed = raw.split(":", 1) if raw else ["", ""]
        browser = parsed[0].strip().lower()
        profile = parsed[1].strip() if len(parsed) > 1 else "Default"
        holder = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        enabled = QtWidgets.QCheckBox("Use browser cookies")
        enabled.setChecked(bool(browser))
        browser_box = QtWidgets.QComboBox()
        for value, label in self._browser_choices():
            browser_box.addItem(label, value)
        browser_box.setCurrentIndex(max(0, browser_box.findData(browser or "chrome")))
        profile_box = QtWidgets.QComboBox()
        profile_box.setEditable(True)
        profile_box.setToolTip("Browser profile name, such as Default or Profile 1")

        def refresh_profiles(selected: str = profile) -> None:
            current = selected or profile_box.currentText() or "Default"
            profile_box.blockSignals(True)
            profile_box.clear()
            profile_box.addItems(self._browser_profiles(str(browser_box.currentData())))
            profile_box.setEditText(current)
            profile_box.blockSignals(False)

        def update_value() -> None:
            value = ""
            if enabled.isChecked():
                selected_profile = profile_box.currentText().strip() or "Default"
                value = str(browser_box.currentData()) + (":" + selected_profile if selected_profile.lower() != "default" else "")
            self._set_value("USE_BROWSER_COOKIES", value)

        browser_box.currentIndexChanged.connect(lambda _index: (refresh_profiles(), update_value()))
        profile_box.currentTextChanged.connect(lambda _text: update_value())
        enabled.toggled.connect(lambda _checked: (update_value(), browser_box.setEnabled(enabled.isChecked()), profile_box.setEnabled(enabled.isChecked()), test_btn.setEnabled(enabled.isChecked())))
        refresh_profiles(profile or "Default")
        browser_box.setEnabled(enabled.isChecked())
        profile_box.setEnabled(enabled.isChecked())
        test_btn = QtWidgets.QPushButton("Test cookies")
        test_btn.setEnabled(enabled.isChecked())
        test_btn.clicked.connect(lambda: self._test_browser_cookies(browser_box, profile_box, enabled, test_btn))
        layout.addWidget(enabled)
        layout.addWidget(browser_box, 1)
        layout.addWidget(profile_box, 1)
        layout.addWidget(test_btn)
        self.controls["USE_BROWSER_COOKIES"] = holder
        self.browser_cookie_enabled = enabled
        self.browser_cookie_box = browser_box
        self.browser_profile_box = profile_box
        return holder

    def _test_browser_cookies(self, browser_box, profile_box, enabled, button) -> None:
        if not enabled.isChecked():
            return
        browser = str(browser_box.currentData())
        profile = profile_box.currentText().strip() or "Default"
        cookies = (browser, None if profile.lower() == "default" else profile, None, None)
        self._start_cookie_test(cookies, button, enabled)

    def _test_cookie_file(self, field, button) -> None:
        path = field.text().strip()
        if not path:
            _show_error_popup(
                self,
                "Cookie file",
                "Choose a cookies.txt file first.",
                "Settings → Browser cookies → Cookie file",
            )
            return
        cookie_path = Path(path).expanduser()
        if not cookie_path.is_absolute():
            cookie_path = self.config.root_dir / cookie_path
        self._start_cookie_test(cookie_path, button, source_label="cookie file")

    def _start_cookie_test(self, source, button, enabled=None, source_label: str = "browser profile") -> None:
        button.setEnabled(False)
        self._cookie_test_done = False
        self._cookie_test_thread = QtCore.QThread(self)
        worker = _CookieTestWorker(source, button, enabled)
        worker.moveToThread(self._cookie_test_thread)
        self._cookie_test_thread.started.connect(worker.run)
        worker.finished.connect(self._cookie_test_finished, QtCore.Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._cookie_test_thread.quit)
        worker.finished.connect(worker.deleteLater)
        self._cookie_test_thread.finished.connect(self._cookie_test_thread.deleteLater)
        self._cookie_test_timer = QtCore.QTimer(self)
        self._cookie_test_timer.setSingleShot(True)
        self._cookie_test_timer.timeout.connect(lambda: self._cookie_test_timed_out(button, enabled, source_label))
        self._cookie_test_timer.start(15000)
        self._cookie_test_thread.start()

    @QtCore.Slot(bool, str, object, object)
    def _cookie_test_finished(self, ok: bool, message: str, button, enabled=None) -> None:
        if getattr(self, "_cookie_test_done", False):
            return
        self._cookie_test_done = True
        timer = getattr(self, "_cookie_test_timer", None)
        if timer:
            timer.stop()
        button.setEnabled(enabled.isChecked() if enabled else True)
        if ok:
            QtWidgets.QMessageBox.information(self, "Browser cookies", message)
        else:
            _show_error_popup(self, "Browser cookies", message, "Settings → Browser cookies")

    def _cookie_test_timed_out(self, button, enabled=None, source_label: str = "browser profile") -> None:
        if getattr(self, "_cookie_test_done", False):
            return
        self._cookie_test_done = True
        thread = getattr(self, "_cookie_test_thread", None)
        if thread and thread.isRunning():
            thread.terminate()
            thread.wait(1000)
        button.setEnabled(enabled.isChecked() if enabled else True)
        _show_error_popup(
            self,
            "Browser cookies",
            f"The cookie test timed out while reading the {source_label}. "
            + ("Make sure the browser is closed and try again." if source_label == "browser profile" else "Check that the cookie file is valid and readable, then try again."),
            f"Settings → Browser cookies → {source_label.title()}",
        )

    def _create_discord_presence_settings(self) -> tuple[QtWidgets.QToolButton, QtWidgets.QWidget]:
        toggle = QtWidgets.QToolButton()
        toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        toggle.setToolTip("Show Discord Presence settings")
        toggle.setFixedSize(24, 24)
        toggle.setCheckable(True)
        toggle.setChecked(self.values.get("DISCORD_RPC", "false").lower() in {"1", "true", "yes", "on"})
        toggle.toggled.connect(self._set_discord_presence_settings_open)
        self.discord_presence_settings_toggle = toggle

        container = QtWidgets.QFrame()
        container.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        container.setObjectName("discordPresenceSettingsPanel")
        container.setStyleSheet(
            "QFrame#discordPresenceSettingsPanel {"
            " background-color: rgba(0, 0, 0, 28);"
            " border: 1px solid palette(mid);"
            " border-radius: 4px;"
            "}"
        )
        details = QtWidgets.QFormLayout(container)
        details.setContentsMargins(14, 8, 14, 8)
        details.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        setup_help = QtWidgets.QLabel(
            f'Need a Discord application? Follow the '
            f'<a href="{DISCORD_APPLICATION_SETUP_URL}">Discord application setup instructions</a>, '
            "then copy the Application ID from General Information."
        )
        setup_help.setTextFormat(QtCore.Qt.TextFormat.RichText)
        setup_help.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        setup_help.setOpenExternalLinks(True)
        setup_help.setWordWrap(True)
        details.addRow(setup_help)
        for key in (
            "DISCORD_APPLICATION_ID",
            "DISCORD_PRESENCE_IMAGE_MODE",
            "DISCORD_PRESENCE_SMALL_IMAGE_MODE",
            "DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL",
            "DISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE",
        ):
            spec = next(item for item in SETTING_SPECS if item.key == key)
            field = self._field_for(spec.key, spec.default, spec.sensitive)
            reset = QtWidgets.QToolButton()
            reset.setText("↺")
            reset.setToolTip(f"Reset {spec.label} to its default")
            reset.clicked.connect(lambda _checked=False, name=key: self._reset_key(name))
            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(field, 1)
            row.addWidget(reset)
            wrapper = QtWidgets.QWidget()
            wrapper.setLayout(row)
            label = QtWidgets.QLabel()
            label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.setting_labels[spec.key] = label
            self._refresh_setting_label(spec.key)
            details.addRow(label, wrapper)
        self.discord_presence_settings_container = container
        self._set_discord_presence_settings_open(toggle.isChecked())
        return toggle, container

    def _set_discord_presence_settings_open(self, open_: bool) -> None:
        container = getattr(self, "discord_presence_settings_container", None)
        toggle = getattr(self, "discord_presence_settings_toggle", None)
        if container:
            container.setVisible(open_)
            container.setEnabled(self.values.get("DISCORD_RPC", "false").lower() in {"1", "true", "yes", "on"})
        if toggle:
            toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if open_ else QtCore.Qt.ArrowType.RightArrow)
            toggle.setToolTip("Hide Discord Presence settings" if open_ else "Show Discord Presence settings")

    def _create_subsonic_settings(self) -> tuple[QtWidgets.QToolButton, QtWidgets.QWidget]:
        toggle = QtWidgets.QToolButton()
        toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        toggle.setToolTip("Show Subsonic server settings")
        toggle.setFixedSize(24, 24)
        toggle.setCheckable(True)
        toggle.setChecked(self.values.get("SERVER_AUTOSTART", "false").lower() in {"1", "true", "yes", "on"})
        toggle.toggled.connect(self._set_subsonic_settings_open)
        self.subsonic_settings_toggle = toggle

        container = QtWidgets.QFrame()
        container.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        container.setObjectName("subsonicSettingsPanel")
        container.setStyleSheet(
            "QFrame#subsonicSettingsPanel {"
            " background-color: rgba(0, 0, 0, 28);"
            " border: 1px solid palette(mid);"
            " border-radius: 4px;"
            "}"
        )
        details = QtWidgets.QFormLayout(container)
        details.setContentsMargins(14, 8, 14, 8)
        details.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for key in (
            "SERVER_HOST", "SERVER_PORT", "SERVER_USERNAME", "SERVER_PASSWORD",
            "SERVER_PASSWORD_HASH", "SERVER_TIMING",
        ):
            spec = next(item for item in SETTING_SPECS if item.key == key)
            field = self._field_for(spec.key, spec.default, spec.sensitive)
            reset = QtWidgets.QToolButton()
            reset.setText("↺")
            reset.setToolTip(f"Reset {spec.label} to its default")
            reset.clicked.connect(lambda _checked=False, name=key: self._reset_key(name))
            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(field, 1)
            row.addWidget(reset)
            wrapper = QtWidgets.QWidget()
            wrapper.setLayout(row)
            label = QtWidgets.QLabel()
            label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.setting_labels[spec.key] = label
            self._refresh_setting_label(spec.key)
            details.addRow(label, wrapper)
        self.subsonic_settings_container = container
        self._set_subsonic_settings_open(toggle.isChecked())
        return toggle, container

    def _set_subsonic_settings_open(self, open_: bool) -> None:
        container = getattr(self, "subsonic_settings_container", None)
        toggle = getattr(self, "subsonic_settings_toggle", None)
        if container:
            container.setVisible(open_)
            container.setEnabled(self.values.get("SERVER_AUTOSTART", "false").lower() in {"1", "true", "yes", "on"})
        if toggle:
            toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if open_ else QtCore.Qt.ArrowType.RightArrow)
            toggle.setToolTip("Hide Subsonic server settings" if open_ else "Show Subsonic server settings")

    def _field_for(self, key: str, default: str, sensitive: bool) -> QtWidgets.QWidget:
        value = self.values.get(key, default)
        if key in {
            "DISCORD_RPC", "SERVER_TIMING", "SERVER_AUTOSTART", "START_MINIMIZED_TO_TRAY", "CLOSE_TO_TRAY",
            "CHECK_FOR_UPDATES_ON_STARTUP", "USE_ARIA2C",
            "DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL",
            "DISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE",
            "DARK_MODE",
            "SHOW_PLAYBACK_BAR",
            "SIMILARITY_USE_ARTIST", "SIMILARITY_USE_TAGS", "SIMILARITY_USE_RARITY", "SIMILARITY_USE_BPM", "SIMILARITY_USE_HALF_DOUBLE_TIME",
        }:
            field = QtWidgets.QCheckBox()
            field.setChecked(value.lower() in {"1", "true", "yes", "on"})
            field.toggled.connect(lambda checked, name=key: self._set_value(name, "true" if checked else "false"))
        elif key == "COLOR_THEME":
            field = QtWidgets.QComboBox()
            current_theme = value
            for theme_value, label in (
                ("system", "System"), ("light", "Light"), ("dark", "Dark"),
                ("ocean", "Ocean"), ("forest", "Forest"), ("sunset", "Sunset"),
                ("purple", "Purple"),
            ):
                field.addItem(label, theme_value)
            field.setCurrentIndex(max(0, field.findData(current_theme)))
            field.currentIndexChanged.connect(lambda _index, name=key, box=field: self._set_value(name, str(box.currentData())))
        elif key == "DISCORD_PRESENCE_IMAGE_MODE":
            field = QtWidgets.QComboBox()
            field.addItem("Empty (Discord default)", "empty")
            field.addItem("Image URL", "url")
            field.addItem("Discord Image Key", "discord_key")
            field.setCurrentIndex(max(0, field.findData(value)))
            field.currentIndexChanged.connect(lambda _index, name=key, box=field: self._set_value(name, str(box.currentData())))
            value_field = QtWidgets.QLineEdit(self.values.get("DISCORD_PRESENCE_IMAGE_VALUE", ""))
            value_field.setPlaceholderText("URL or Discord image key")
            value_field.textChanged.connect(lambda text, name="DISCORD_PRESENCE_IMAGE_VALUE": self._set_value(name, text))
            holder = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            layout.addWidget(field)
            layout.addWidget(value_field, 1)
            self.controls["DISCORD_PRESENCE_IMAGE_VALUE"] = value_field
            self.controls[key] = field
            return holder
        elif key == "DISCORD_PRESENCE_SMALL_IMAGE_MODE":
            field = QtWidgets.QComboBox()
            field.addItem("Default", "default")
            field.addItem("Empty (hide small image)", "empty")
            field.addItem("Image URL", "url")
            field.addItem("Discord Image Key", "discord_key")
            field.setCurrentIndex(max(0, field.findData(value)))
            field.currentIndexChanged.connect(lambda _index, name=key, box=field: self._set_value(name, str(box.currentData())))
            value_field = QtWidgets.QLineEdit(self.values.get("DISCORD_PRESENCE_SMALL_IMAGE_VALUE", ""))
            value_field.setPlaceholderText("URL or Discord image key")
            value_field.textChanged.connect(
                lambda text, name="DISCORD_PRESENCE_SMALL_IMAGE_VALUE": self._set_value(name, text)
            )
            holder = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            layout.addWidget(field)
            layout.addWidget(value_field, 1)
            self.controls["DISCORD_PRESENCE_SMALL_IMAGE_VALUE"] = value_field
            self.controls[key] = field
            return holder
        elif key == "CACHE_UPDATE_ON_STARTUP":
            field = QtWidgets.QComboBox()
            field.addItem("Ask before updating cache", "ask")
            field.addItem("Don't update cache", "never")
            field.addItem("Update cache on startup automatically", "automatic")
            field.setCurrentIndex(max(0, field.findData(value)))
            field.currentIndexChanged.connect(lambda _index, name=key, box=field: self._set_value(name, str(box.currentData())))
        elif key == "BROWSER_COOKIES_MODE":
            field = QtWidgets.QComboBox()
            field.addItem("Always", "always")
            field.addItem("Only when required", "required")
            field.addItem("Never", "never")
            selected = value if value in {"always", "required", "never"} else "required"
            field.setCurrentIndex(max(0, field.findData(selected)))
            field.currentIndexChanged.connect(lambda _index, name=key, box=field: self._set_value(name, str(box.currentData())))
        elif key in {
            "NUM_WORKERS", "SERVER_PORT", "SIMILARITY_ARTIST_WEIGHT", "SIMILARITY_TAG_WEIGHT",
            "SIMILARITY_BPM_MAX_DISTANCE", "SIMILARITY_BPM_WEIGHT", "SIMILARITY_MIN_SCORE", "SIMILARITY_MAX_RESULTS",
        }:
            field = QtWidgets.QSpinBox()
            if key == "SERVER_PORT":
                field.setRange(1, 65535)
            elif key == "NUM_WORKERS":
                field.setRange(1, 64)
            elif key == "SIMILARITY_MIN_SCORE":
                field.setRange(0, 10000)
            elif key == "SIMILARITY_MAX_RESULTS":
                field.setRange(1, 500)
            else:
                field.setRange(0, 500)
            field.setValue(int(value) if value.isdigit() and int(value) >= field.minimum() else int(default))
            field.valueChanged.connect(lambda number, name=key: self._set_value(name, str(number)))
        else:
            field = QtWidgets.QLineEdit(value)
            if key == "COOKIES_FILE":
                field.setPlaceholderText("Netscape cookies.txt file (optional)")
                field.setEnabled(False)
                field.setToolTip("Temporarily disabled while cookie-file 403 handling is being isolated.")
            if sensitive:
                field.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            field.textChanged.connect(lambda text, name=key: self._set_value(name, text))
            if sensitive:
                reveal = QtWidgets.QToolButton()
                reveal.setText("Show")
                reveal.setCheckable(True)
                reveal.setToolTip("Reveal this value")

                def set_revealed(visible: bool, edit=field, button=reveal) -> None:
                    edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if visible else QtWidgets.QLineEdit.EchoMode.Password)
                    button.setText("Hide" if visible else "Show")
                    button.setToolTip("Hide this value" if visible else "Reveal this value")

                reveal.toggled.connect(set_revealed)
                holder = QtWidgets.QWidget()
                layout = QtWidgets.QHBoxLayout(holder)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(field, 1)
                layout.addWidget(reveal)
                self.controls[key] = field
                return holder
            if key.endswith(("DIRECTORY", "_PATH")) or key == "COOKIES_FILE":
                browse = QtWidgets.QPushButton("Browse")
                if key == "COOKIES_FILE":
                    browse.setEnabled(False)
                    browse.setToolTip("Cookie-file support is temporarily disabled")
                browse.clicked.connect(
                    lambda _checked=False, edit=field, is_directory=key.endswith("DIRECTORY"), open_file=key == "COOKIES_FILE": self._browse_path(edit, is_directory, open_file)
                )
                holder = QtWidgets.QWidget()
                layout = QtWidgets.QHBoxLayout(holder)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(field, 1)
                layout.addWidget(browse)
                if key == "COOKIES_FILE":
                    test = QtWidgets.QPushButton("Test cookies")
                    test.setEnabled(False)
                    test.setToolTip("Cookie-file support is temporarily disabled")
                    test.clicked.connect(lambda _checked=False, edit=field, button=test: self._test_cookie_file(edit, button))
                    layout.addWidget(test)
                self.controls[key] = field
                return holder
        self.controls[key] = field
        return field

    def _browse_path(self, edit: QtWidgets.QLineEdit, is_directory: bool, open_file: bool = False) -> None:
        if is_directory:
            selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose directory", edit.text() or str(self.config.root_dir))
        else:
            dialog = QtWidgets.QFileDialog.getOpenFileName if open_file else QtWidgets.QFileDialog.getSaveFileName
            selected, _filter = dialog(self, "Choose file", edit.text() or str(self.config.root_dir))
        if selected:
            edit.setText(selected)

    def _build_tags_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(QtWidgets.QLabel("Tags are stored on videos. Disabled tags stay attached but are ignored by Similar Songs."))
        splitter = QtWidgets.QSplitter()
        self.tag_table = QtWidgets.QTableWidget(0, 3)
        self.tag_table.setHorizontalHeaderLabels(["Tag", "Videos", "Similar Songs"])
        self.tag_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tag_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tag_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tag_table.horizontalHeader().setStretchLastSection(True)
        self.tag_table.itemSelectionChanged.connect(self._tag_selection_changed)
        splitter.addWidget(self.tag_table)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.addWidget(QtWidgets.QLabel("Videos using selected tag"))
        self.tag_videos = QtWidgets.QListWidget()
        self.tag_videos.itemSelectionChanged.connect(self._select_tag_video_in_library)
        right_layout.addWidget(self.tag_videos, 1)
        self.tag_disable_btn = QtWidgets.QPushButton("Disable for Similar Songs")
        self.tag_disable_btn.clicked.connect(self._toggle_selected_tag)
        self.tag_rename_btn = QtWidgets.QPushButton("Rename Tag")
        self.tag_rename_btn.clicked.connect(self._rename_selected_tag)
        self.tag_delete_btn = QtWidgets.QPushButton("Remove Tag from All Videos")
        self.tag_delete_btn.clicked.connect(self._delete_selected_tag)
        self.tag_remove_video_btn = QtWidgets.QPushButton("Remove Tag from Selected Video")
        self.tag_remove_video_btn.clicked.connect(self._remove_tag_from_selected_video)
        right_layout.addWidget(self.tag_disable_btn)
        right_layout.addWidget(self.tag_rename_btn)
        right_layout.addWidget(self.tag_delete_btn)
        right_layout.addWidget(self.tag_remove_video_btn)
        splitter.addWidget(right)
        splitter.setSizes([420, 420])
        layout.addWidget(splitter, 1)
        self._refresh_tag_management()
        return page

    def _disabled_tags(self) -> set[str]:
        return {part.strip().casefold() for part in self.values.get("DISABLED_SIMILARITY_TAGS", "").split(",") if part.strip()}

    def _tag_entries(self) -> list[VideoEntry]:
        entries = self.metadata.entries()
        for entry in entries:
            if entry.id in self.pending_tag_updates:
                entry.tags = list(self.pending_tag_updates[entry.id])
        return entries

    def _refresh_tag_management(self, selected: Optional[str] = None) -> None:
        if not hasattr(self, "tag_table"):
            return
        selected = selected or self._selected_tag_name()
        counts: dict[str, tuple[str, int]] = {}
        for entry in self._tag_entries():
            for tag in entry.tags:
                key = tag.casefold()
                display, count = counts.get(key, (tag, 0))
                counts[key] = (display, count + 1)
        disabled = self._disabled_tags()
        blocker = QtCore.QSignalBlocker(self.tag_table)
        self.tag_table.setRowCount(len(counts))
        for row, (key, (tag, count)) in enumerate(sorted(counts.items(), key=lambda item: item[1][0].casefold())):
            self.tag_table.setItem(row, 0, QtWidgets.QTableWidgetItem(tag))
            self.tag_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(count)))
            self.tag_table.setItem(row, 2, QtWidgets.QTableWidgetItem("Disabled" if key in disabled else "Enabled"))
            if key in disabled:
                for column in range(self.tag_table.columnCount()):
                    self.tag_table.item(row, column).setForeground(
                        QtGui.QBrush(
                            QtWidgets.QApplication.palette().color(
                                QtGui.QPalette.ColorGroup.Disabled,
                                QtGui.QPalette.ColorRole.Text,
                            )
                        )
                    )
            if key == (selected or "").casefold():
                self.tag_table.selectRow(row)
        del blocker
        self._tag_selection_changed()

    def _selected_tag_name(self) -> Optional[str]:
        selected = self.tag_table.selectedItems() if hasattr(self, "tag_table") else []
        return selected[0].text() if selected else None

    def _tag_selection_changed(self) -> None:
        tag = self._selected_tag_name()
        if not tag:
            self.tag_videos.clear()
            self.tag_disable_btn.setEnabled(False)
            self.tag_rename_btn.setEnabled(False)
            self.tag_delete_btn.setEnabled(False)
            self.tag_remove_video_btn.setEnabled(False)
            return
        key = tag.casefold()
        self.tag_videos.clear()
        for entry in sorted(self._tag_entries(), key=lambda item: ((item.title or item.id).casefold(), item.id)):
            if any(item.casefold() == key for item in entry.tags):
                item = QtWidgets.QListWidgetItem(f"{entry.title or entry.id} — {entry.author or 'Unknown Artist'}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, entry.id)
                self.tag_videos.addItem(item)
        disabled = key in self._disabled_tags()
        self.tag_disable_btn.setText("Enable for Similar Songs" if disabled else "Disable for Similar Songs")
        self.tag_disable_btn.setEnabled(True)
        self.tag_rename_btn.setEnabled(True)
        self.tag_delete_btn.setEnabled(True)
        self.tag_remove_video_btn.setEnabled(bool(self.tag_videos.currentItem()))

    def _select_tag_video_in_library(self) -> None:
        item = self.tag_videos.currentItem()
        if item:
            self.select_video_callback(str(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        self.tag_remove_video_btn.setEnabled(bool(item))

    def _toggle_selected_tag(self) -> None:
        tag = self._selected_tag_name()
        if not tag:
            return
        disabled = self._disabled_tags()
        key = tag.casefold()
        if key in disabled:
            disabled.remove(key)
        else:
            disabled.add(key)
        self._set_value("DISABLED_SIMILARITY_TAGS", ", ".join(sorted(disabled)))
        self._refresh_tag_management(tag)

    def _rename_selected_tag(self) -> None:
        tag = self._selected_tag_name()
        if not tag:
            return
        new_tag, accepted = QtWidgets.QInputDialog.getText(self, "Rename Tag", "New tag name:", text=tag)
        if not accepted:
            return
        normalized = normalize_tags([new_tag])
        if not normalized:
            _show_error_popup(self, "Rename Tag", "Tag name cannot be blank.", "Settings → Tag management")
            return
        new_tag = normalized[0]
        old_key = tag.casefold()
        new_key = new_tag.casefold()
        if new_key != old_key:
            existing_keys = {
                item.casefold()
                for entry in self._tag_entries()
                for item in entry.tags
                if item.casefold() != old_key
            }
            if new_key in existing_keys:
                _show_error_popup(
                    self,
                    "Rename Tag",
                    f"The tag '{new_tag}' already exists. Choose a different name.",
                    "Settings → Tag management",
                )
                return

        matching: list[VideoEntry] = []
        for entry in self._tag_entries():
            if any(item.casefold() == old_key for item in entry.tags):
                entry.tags = normalize_tags([new_tag if item.casefold() == old_key else item for item in entry.tags])
                matching.append(entry)
        if not matching:
            self._refresh_tag_management()
            return
        for entry in matching:
            self.pending_tag_updates[entry.id] = list(entry.tags)

        disabled = self._disabled_tags()
        if old_key in disabled:
            disabled.remove(old_key)
            disabled.add(new_key)
            self._set_value("DISABLED_SIMILARITY_TAGS", ", ".join(sorted(disabled)))
        self._refresh_tag_management(new_tag)

    def _delete_selected_tag(self) -> None:
        tag = self._selected_tag_name()
        if not tag:
            return
        matching = [entry for entry in self._tag_entries() if any(item.casefold() == tag.casefold() for item in entry.tags)]
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove Tag",
            f"Remove '{tag}' from {len(matching)} video(s)?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        for entry in matching:
            entry.tags = [item for item in entry.tags if item.casefold() != tag.casefold()]
        for entry in matching:
            self.pending_tag_updates[entry.id] = list(entry.tags)
        disabled = self._disabled_tags()
        disabled.discard(tag.casefold())
        self._set_value("DISABLED_SIMILARITY_TAGS", ", ".join(sorted(disabled)))
        self._refresh_tag_management()

    def _remove_tag_from_selected_video(self) -> None:
        tag = self._selected_tag_name()
        item = self.tag_videos.currentItem()
        if not tag or not item:
            return
        video_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole))
        entry = next((item for item in self._tag_entries() if item.id == video_id), None)
        if not entry:
            return
        entry.tags = [value for value in entry.tags if value.casefold() != tag.casefold()]
        self.pending_tag_updates[entry.id] = list(entry.tags)
        self._refresh_tag_management(tag)

    def _update_algorithm_field_enabled(self) -> None:
        enabled = self.values.get("SIMILARITY_USE_BPM", "true").lower() in {"1", "true", "yes", "on"}
        for key in ("SIMILARITY_USE_HALF_DOUBLE_TIME", "SIMILARITY_BPM_MAX_DISTANCE", "SIMILARITY_BPM_WEIGHT"):
            control = self.controls.get(key)
            if control:
                control.setEnabled(enabled)

    def _build_advanced_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(QtWidgets.QLabel("All supported settings"))
        self.advanced = QtWidgets.QTableWidget(len(SETTING_SPECS), 4)
        self.advanced.setHorizontalHeaderLabels(["Key", "Value", "Default", "Category"])
        self.advanced.horizontalHeader().setStretchLastSection(True)
        for row, spec in enumerate(SETTING_SPECS):
            self.advanced_rows[spec.key] = row
            for col, value in ((0, spec.key), (2, spec.default), (3, spec.category)):
                item = QtWidgets.QTableWidgetItem(value)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.advanced.setItem(row, col, item)
            value_item = QtWidgets.QTableWidgetItem(self.values.get(spec.key, spec.default))
            if spec.key in STORAGE_SETTING_KEYS:
                value_item.setFlags(value_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                value_item.setToolTip("Managed inside the library data folder. Use Library → Move data… to relocate it.")
            self.advanced.setItem(row, 1, value_item)
        self.advanced.itemChanged.connect(self._advanced_known_changed)
        layout.addWidget(self.advanced, 1)
        layout.addWidget(QtWidgets.QLabel("Other [Settings] entries (preserved unless you edit or remove them)"))
        self.raw_table = QtWidgets.QTableWidget(0, 2)
        self.raw_table.setHorizontalHeaderLabels(["Key", "Value"])
        self.raw_table.horizontalHeader().setStretchLastSection(True)
        for key, value in self.raw_initial.items():
            if key.upper() not in {spec.key for spec in SETTING_SPECS}:
                self._add_raw_row(key, value)
        raw_controls = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add entry")
        add.clicked.connect(lambda: self._add_raw_row("", ""))
        remove = QtWidgets.QPushButton("Remove selected")
        remove.clicked.connect(lambda: self.raw_table.removeRow(self.raw_table.currentRow()) if self.raw_table.currentRow() >= 0 else None)
        raw_controls.addWidget(add)
        raw_controls.addWidget(remove)
        raw_controls.addStretch(1)
        layout.addWidget(self.raw_table)
        layout.addLayout(raw_controls)
        return page

    def _add_raw_row(self, key: str, value: str) -> None:
        row = self.raw_table.rowCount()
        self.raw_table.insertRow(row)
        self.raw_table.setItem(row, 0, QtWidgets.QTableWidgetItem(key))
        self.raw_table.setItem(row, 1, QtWidgets.QTableWidgetItem(value))

    def _advanced_known_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() == 1:
            key = self.advanced.item(item.row(), 0).text()
            self._set_value(key, item.text(), update_advanced=False)

    def _set_value(self, key: str, value: str, *, update_advanced: bool = True) -> None:
        self.values[key] = value
        self.reset_keys.discard(key)
        if update_advanced and key in self.advanced_rows:
            item = self.advanced.item(self.advanced_rows[key], 1)
            blocker = QtCore.QSignalBlocker(self.advanced)
            item.setText(value)
            del blocker
        if key == "SIMILARITY_USE_BPM":
            self._update_algorithm_field_enabled()
        if key == "DISCORD_RPC":
            enabled = value.lower() in {"1", "true", "yes", "on"}
            toggle = getattr(self, "discord_presence_settings_toggle", None)
            if toggle:
                blocker = QtCore.QSignalBlocker(toggle)
                toggle.setChecked(enabled)
                del blocker
            self._set_discord_presence_settings_open(enabled)
        if key == "SERVER_AUTOSTART":
            enabled = value.lower() in {"1", "true", "yes", "on"}
            toggle = getattr(self, "subsonic_settings_toggle", None)
            if toggle:
                blocker = QtCore.QSignalBlocker(toggle)
                toggle.setChecked(enabled)
                del blocker
            self._set_subsonic_settings_open(enabled)
        if key in {"DISCORD_PRESENCE_IMAGE_MODE", "DISCORD_PRESENCE_SMALL_IMAGE_MODE"}:
            self._update_presence_image_value_enabled(key)
        self._refresh_setting_label(key)

    def _update_presence_default_image_value_enabled(self) -> None:
        self._update_presence_image_value_enabled("DISCORD_PRESENCE_IMAGE_MODE")

    def _update_presence_image_value_enabled(self, mode_key: str) -> None:
        value_key = mode_key.removesuffix("_MODE") + "_VALUE"
        control = self.controls.get(value_key)
        default_mode = "default" if mode_key == "DISCORD_PRESENCE_SMALL_IMAGE_MODE" else "empty"
        mode = self.values.get(mode_key, default_mode)
        if control:
            supported = mode in {"url", "discord_key"}
            control.setVisible(supported)
            control.setEnabled(supported)

    def _refresh_setting_label(self, key: str) -> None:
        label = self.setting_labels.get(key)
        if not label:
            return
        spec = next(item for item in SETTING_SPECS if item.key == key)
        value = self.values.get(key, spec.default)
        changed = value != self.original_values.get(key, spec.default) or key in self.reset_keys
        non_default = value != spec.default
        text = html.escape(spec.label)
        if non_default:
            text = f"<i>{text}</i>"
        if changed:
            text = f"<b>{text} *</b>"
        label.setText(text)

    def _reset_key(self, key: str) -> None:
        default = next(spec.default for spec in SETTING_SPECS if spec.key == key)
        self.values[key] = default
        control = self.controls.get(key)
        if key == "USE_BROWSER_COOKIES" and hasattr(self, "browser_cookie_enabled"):
            self.browser_cookie_enabled.setChecked(False)
            self.browser_cookie_box.setCurrentIndex(max(0, self.browser_cookie_box.findData("chrome")))
            self.browser_profile_box.setEditText("Default")
        elif isinstance(control, QtWidgets.QCheckBox):
            control.setChecked(default == "true")
        elif isinstance(control, QtWidgets.QComboBox):
            control.setCurrentIndex(max(0, control.findData(default)))
        elif isinstance(control, QtWidgets.QSpinBox):
            control.setValue(int(default))
        elif isinstance(control, QtWidgets.QLineEdit):
            control.setText(default)
        if key in self.advanced_rows:
            blocker = QtCore.QSignalBlocker(self.advanced)
            self.advanced.item(self.advanced_rows[key], 1).setText(default)
            del blocker
        # Updating a normal Qt control emits its valueChanged signal.  Mark the
        # reset last so an explicit default in config.ini is removed on Apply.
        self.reset_keys.add(key)
        self._refresh_setting_label(key)

    def _reset_current_tab(self) -> None:
        category = self.navigation.currentItem().text()
        if category == "Library":
            return
        if category == "Advanced":
            return
        if category == "Tags":
            self.pending_tag_updates.clear()
            self._reset_key("DISABLED_SIMILARITY_TAGS")
            self._refresh_tag_management()
            return
        for spec in SETTING_SPECS:
            if spec.category == category:
                self._reset_key(spec.key)

    def _validate(self) -> Optional[str]:
        if not self.values["SERVER_HOST"].strip():
            return "Server host cannot be blank."
        if not self.values["SERVER_USERNAME"].strip():
            return "Server username cannot be blank."
        for key in ("DOWNLOAD_DIRECTORY", "EXPORTS_DIRECTORY", "THUMBNAILS_DIRECTORY"):
            if not self.values[key].strip():
                return f"{key} cannot be blank."
        if self.values.get("DISCORD_PRESENCE_IMAGE_MODE", "empty") not in {"empty", "url", "discord_key"}:
            return "Discord default large image mode must be Empty, Image URL, or Discord Image Key."
        if self.values.get("DISCORD_PRESENCE_SMALL_IMAGE_MODE", "default") not in {
            "default", "empty", "url", "discord_key"
        }:
            return "Discord small image mode must be Default, Empty, Image URL, or Discord Image Key."
        raw_keys: set[str] = set()
        for row in range(self.raw_table.rowCount()):
            key_item, value_item = self.raw_table.item(row, 0), self.raw_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            if not key:
                return "Advanced entry keys cannot be blank."
            normalized = key.upper()
            if normalized in raw_keys or normalized in {spec.key for spec in SETTING_SPECS}:
                return f"Duplicate settings key: {key}"
            raw_keys.add(normalized)
            if value_item is None:
                return f"Advanced entry {key} needs a value."
        return None

    def _apply(self) -> bool:
        error = self._validate()
        if error:
            _show_error_popup(self, "Settings", error, "Settings → Validation")
            return False
        updates: dict[str, Optional[str]] = {}
        for spec in SETTING_SPECS:
            value = self.values[spec.key]
            if value != self.original_values.get(spec.key) or spec.key in self.reset_keys:
                updates[spec.key] = None if value == spec.default else value
        raw_now: dict[str, str] = {}
        for row in range(self.raw_table.rowCount()):
            raw_now[self.raw_table.item(row, 0).text().strip()] = self.raw_table.item(row, 1).text()
        initial_unknown = {key: value for key, value in self.raw_initial.items() if key.upper() not in {spec.key for spec in SETTING_SPECS}}
        for key in set(initial_unknown) | set(raw_now):
            if raw_now.get(key) != initial_unknown.get(key):
                updates[key] = raw_now.get(key)
        if not updates and not self.pending_tag_updates:
            return True
        tags_changed = bool(self.pending_tag_updates)
        try:
            save_ini_settings(self.config.root_dir, updates)
            if self.pending_tag_updates:
                entries = [
                    entry
                    for entry in self.metadata.entries()
                    if entry.id in self.pending_tag_updates
                ]
                for entry in entries:
                    entry.tags = list(self.pending_tag_updates[entry.id])
                self.metadata.bulk_upsert(entries)
            self.apply_callback()
            if tags_changed:
                self.tags_changed_callback()
        except Exception as exc:
            _show_error_popup(self, "Settings", f"Could not apply settings: {exc}", "Settings → Apply")
            return False
        self.raw_initial = read_ini_settings(self.config.root_dir)
        self.values = effective_settings(self.config.root_dir)
        self.original_values = dict(self.values)
        self.reset_keys.clear()
        self.pending_tag_updates.clear()
        for spec in SETTING_SPECS:
            self._refresh_setting_label(spec.key)
        self.changed.emit()
        return True

    def _apply_and_close(self) -> None:
        if self._apply():
            self.accept()


class DirectAddWorker(QtCore.QThread):
    progress = QtCore.Signal(dict)
    completed = QtCore.Signal(dict)

    def __init__(self, config: AppConfig, metadata: MetadataStore, raw_input: str):
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.raw_input = raw_input
        self.stop_event = threading.Event()

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        try:
            result = updater.add_video(self.raw_input, self.progress.emit, self.stop_event)
        except DownloadCancelled:
            self.completed.emit({"ok": False, "message": "Cancelled"})
        except (Exception, SystemExit) as exc:
            self.completed.emit({"ok": False, "message": str(exc)})
        else:
            self.completed.emit(
                {
                    "ok": True,
                    "status": result.status,
                    "message": result.message,
                    "selected_id": result.resolved_id,
                }
            )


class BatchDirectAddWorker(QtCore.QThread):
    progress = QtCore.Signal(dict)
    completed = QtCore.Signal(dict)

    def __init__(self, config: AppConfig, metadata: MetadataStore, raw_inputs: list[str]):
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.raw_inputs = raw_inputs
        self.stop_event = threading.Event()

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        updater = CacheUpdater(self.config, self.metadata)
        added = 0
        existing = 0
        failed = 0
        last_selected_id = ""
        try:
            for index, raw_input in enumerate(self.raw_inputs, start=1):
                if self.stop_event.is_set():
                    raise DownloadCancelled()
                self.progress.emit(
                    {
                        "phase": "scan",
                        "message": f"Adding {index}/{len(self.raw_inputs)}: {raw_input}",
                        "queue_total": len(self.raw_inputs),
                        "queue_done": index - 1,
                        "download_total": len(self.raw_inputs),
                        "download_done": added + existing + failed,
                    }
                )
                try:
                    result = updater.add_video(raw_input, self.progress.emit, self.stop_event)
                except (Exception, SystemExit) as exc:
                    failed += 1
                    self.progress.emit({"phase": "error", "message": f"{raw_input}: {exc}"})
                    continue
                last_selected_id = result.resolved_id or last_selected_id
                if result.status == "downloaded":
                    added += 1
                else:
                    existing += 1
                self.progress.emit(
                    {
                        "phase": "done",
                        "message": result.message,
                        "queue_total": len(self.raw_inputs),
                        "queue_done": index,
                        "download_total": len(self.raw_inputs),
                        "download_done": added + existing + failed,
                    }
                )
        except DownloadCancelled:
            self.completed.emit({"ok": False, "message": "Cancelled"})
            return
        self.completed.emit(
            {
                "ok": True,
                "selected_id": last_selected_id,
                "message": f"Finished batch add: {added} added, {existing} already present, {failed} failed",
            }
        )


class LocalImportWorker(QtCore.QThread):
    completed = QtCore.Signal(dict)

    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataStore,
        src: Path,
        title: str,
        author: str,
        upload_date: str,
        tags: list[str],
    ):
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.src = src
        self.title = title
        self.author = author
        self.upload_date = upload_date
        self.tags = tags

    def run(self) -> None:
        try:
            digest = sha256_prefix(self.src)
            video_id = canonical_local_id(digest)
            existing = self.metadata.get(video_id)
            if existing:
                self.completed.emit(
                    {
                        "ok": True,
                        "status": "exists",
                        "message": f"Already in library: {existing.title or video_id}",
                        "selected_id": video_id,
                    }
                )
                return
            self.config.download_dir.mkdir(parents=True, exist_ok=True)
            suffix = self.src.suffix.lower()
            if suffix not in MEDIA_EXTS:
                raise ValueError("Selected file must be a supported audio or video format")
            out_path = self.config.download_dir / f"{video_id}{suffix}"
            shutil.copy2(self.src, out_path)
            entry = VideoEntry(
                id=video_id,
                title=self.title,
                upload_date=self.upload_date,
                author=self.author,
                tags=self.tags,
                source_type="local",
                source_id=video_id,
                path=out_path,
            )
            apply_bpm_analysis(entry)
            self.metadata.upsert(entry, save=True)
            self.completed.emit(
                {
                    "ok": True,
                    "status": "imported",
                    "message": f"Imported {entry.title}",
                    "selected_id": video_id,
                }
            )
        except (Exception, SystemExit) as exc:
            self.completed.emit({"ok": False, "message": str(exc)})


class BatchLocalImportWorker(QtCore.QThread):
    completed = QtCore.Signal(dict)

    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataStore,
        sources: list[tuple[Path, str, str, str, list[str]]],
        archives: list[Path] | None = None,
        author_override: str = "",
        upload_date_override: str = "",
        tags_override: list[str] | None = None,
    ):
        super().__init__()
        self.config = config
        self.metadata = metadata
        self.sources = sources
        self.archives = archives or []
        self.author_override = author_override
        self.upload_date_override = upload_date_override
        self.tags_override = tags_override or []

    def run(self) -> None:
        added = 0
        existing = 0
        failed = 0
        last_selected_id = ""
        try:
            with tempfile.TemporaryDirectory(prefix="ytarchive-import-") as temp_dir:
                sources = list(self.sources)
                for archive in self.archives:
                    for src in _extract_zip_media_files(archive, Path(temp_dir)):
                        prefill = _local_import_defaults(src)
                        sources.append(
                            (
                                src,
                                prefill["title"],
                                self.author_override or prefill["author"],
                                self.upload_date_override or prefill["upload_date"],
                                self.tags_override,
                            )
                        )
                for src, title, author, upload_date, tags in sources:
                    digest = sha256_prefix(src)
                    video_id = canonical_local_id(digest)
                    last_selected_id = video_id or last_selected_id
                    if self.metadata.get(video_id):
                        existing += 1
                        continue
                    self.config.download_dir.mkdir(parents=True, exist_ok=True)
                    suffix = src.suffix.lower()
                    if suffix not in MEDIA_EXTS:
                        failed += 1
                        continue
                    out_path = self.config.download_dir / f"{video_id}{suffix}"
                    shutil.copy2(src, out_path)
                    entry = VideoEntry(
                        id=video_id,
                        title=title,
                        upload_date=upload_date,
                        author=author,
                        tags=tags,
                        source_type="local",
                        source_id=video_id,
                        path=out_path,
                    )
                    apply_bpm_analysis(entry)
                    self.metadata.upsert(entry, save=True)
                    added += 1
        except Exception as exc:
            self.completed.emit({"ok": False, "message": str(exc)})
            return
        self.completed.emit(
            {
                "ok": True,
                "selected_id": last_selected_id,
                "message": f"Finished local import: {added} added, {existing} already present, {failed} skipped",
            }
        )


class AddNewDialog(QtWidgets.QDialog):
    def __init__(self, config: AppConfig, metadata: MetadataStore, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.metadata = metadata
        self.selected_id: Optional[str] = None
        self.direct_worker: Optional[DirectAddWorker] = None
        self.batch_direct_worker: Optional[BatchDirectAddWorker] = None
        self.import_worker: Optional[LocalImportWorker] = None
        self.batch_import_worker: Optional[BatchLocalImportWorker] = None
        self.local_paths: list[Path] = []
        self.library_may_have_changed = False
        self._close_requested = False
        self._accept_when_finished = False

        self.setWindowTitle("Add New")
        self.resize(720, 460)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_youtube_tab(), "Online")
        self.tabs.addTab(self._build_local_tab(), "Local File")

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self._request_close)

        controls = QtWidgets.QHBoxLayout()
        controls.addStretch(1)
        controls.addWidget(close_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(controls)

    def _require_dependencies(self, title: str, items: list[str], detail: str, source: str = "Add New") -> bool:
        missing_now = _missing_dependencies()
        missing = [item for item in items if item in missing_now["required"] or item in missing_now["optional"]]
        if not missing:
            return True
        _show_error_popup(self, title, _format_dependency_message(title, missing, detail), source)
        return False

    def _build_youtube_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        self.youtube_input = QtWidgets.QPlainTextEdit()
        self.youtube_input.setPlaceholderText("One YouTube/SoundCloud/Spotify/Bandcamp/Monochrome URL, direct media file URL, or YouTube ID per line")
        self.youtube_input.setMaximumBlockCount(5000)

        self.youtube_status = ElidedLabel("Ready")
        self.youtube_progress = QtWidgets.QProgressBar()
        self.youtube_progress.setRange(0, 1)
        self.youtube_progress.setValue(0)
        self.youtube_log = QtWidgets.QPlainTextEdit()
        self.youtube_log.setReadOnly(True)
        self.youtube_log.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.youtube_log.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.youtube_log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.youtube_log.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.youtube_log.setMinimumWidth(0)
        self.youtube_log.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        self.youtube_add_btn = QtWidgets.QPushButton("Add")
        self.youtube_add_btn.clicked.connect(self._start_direct_add)
        online_missing = set(_missing_dependencies()["required"])
        if online_missing.intersection({"yt-dlp", "yt-dlp-ejs", "yt-dlp-js-runtime", "ffmpeg"}):
            self.youtube_status.set_full_text("Online download dependencies are missing; direct media file links can still be added.")

        controls = QtWidgets.QHBoxLayout()
        controls.addStretch(1)
        controls.addWidget(self.youtube_add_btn)

        layout.addWidget(QtWidgets.QLabel("Links or IDs"))
        layout.addWidget(self.youtube_input)
        layout.addWidget(self.youtube_status)
        layout.addWidget(self.youtube_progress)
        layout.addWidget(self.youtube_log, stretch=1)
        layout.addLayout(controls)
        return page

    def _build_local_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        form = QtWidgets.QFormLayout()
        self.file_path = QtWidgets.QLineEdit()
        self.file_path.setReadOnly(True)
        browse_btn = QtWidgets.QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_files)
        file_row = QtWidgets.QHBoxLayout()
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.addWidget(self.file_path, stretch=1)
        file_row.addWidget(browse_btn)
        file_widget = QtWidgets.QWidget()
        file_widget.setLayout(file_row)
        form.addRow("File", file_widget)

        self.local_title = QtWidgets.QLineEdit()
        self.local_author = QtWidgets.QLineEdit()
        self.local_date = QtWidgets.QLineEdit()
        self.local_tags = QtWidgets.QLineEdit()
        self.local_date.setPlaceholderText("YYYY-MM-DD")
        self.local_tags.setPlaceholderText("Comma-separated")
        self.local_author.setToolTip("When importing multiple files, this replaces the author for every selected file. Leave blank to keep each file's detected author.")
        self.local_date.setToolTip("When importing multiple files, this replaces the date for every selected file. Leave blank to keep each file's detected date.")
        self.local_tags.setToolTip("Tags are comma-separated. When importing multiple files, they are added to every selected file.")
        form.addRow("Title", self.local_title)
        form.addRow("Author", self.local_author)
        form.addRow("Date", self.local_date)
        form.addRow("Tags", self.local_tags)
        layout.addLayout(form)

        self.local_note = QtWidgets.QLabel("Imported files use a stable ID based on the file hash.")
        self.local_status = QtWidgets.QLabel("Choose local audio/video files or ZIP archives of songs to import.")
        self.local_import_btn = QtWidgets.QPushButton("Import")
        self.local_import_btn.clicked.connect(self._start_local_import)

        controls = QtWidgets.QHBoxLayout()
        controls.addStretch(1)
        controls.addWidget(self.local_import_btn)

        layout.addWidget(self.local_note)
        layout.addWidget(self.local_status)
        layout.addStretch(1)
        layout.addLayout(controls)
        return page

    def _register_add_worker(self, worker: QtCore.QThread) -> None:
        worker.finished.connect(self._on_add_worker_finished, QtCore.Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)

    def _add_workers(self) -> tuple[QtCore.QThread, ...]:
        return tuple(
            worker
            for worker in (
                self.direct_worker,
                self.batch_direct_worker,
                self.import_worker,
                self.batch_import_worker,
            )
            if worker is not None
        )

    def _on_add_worker_finished(self) -> None:
        for attribute in (
            "direct_worker",
            "batch_direct_worker",
            "import_worker",
            "batch_import_worker",
        ):
            worker = getattr(self, attribute)
            if worker is not None and not worker.isRunning():
                setattr(self, attribute, None)
        if self._add_workers():
            return
        if self._close_requested:
            self._close_requested = False
            self.reject()
        elif self._accept_when_finished:
            self._accept_when_finished = False
            self.accept()
        else:
            self._set_busy(False)

    def _browse_files(self) -> None:
        filters = "Media or ZIP Files (*.mp4 *.mkv *.webm *.mov *.m4a *.mp3 *.flac *.wav *.opus *.avi *.zip);;All Files (*)"
        selected, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Choose Media Files or ZIP Archives", str(self.config.root_dir), filters)
        if not selected:
            return
        self.local_paths = [Path(item) for item in selected]
        if len(self.local_paths) == 1:
            path = self.local_paths[0]
            self.file_path.setText(str(path))
            if path.suffix.lower() == ".zip":
                self.local_title.clear()
                self.local_author.clear()
                self.local_date.clear()
                self.local_tags.clear()
                self.local_title.setEnabled(False)
                self.local_author.setEnabled(True)
                self.local_date.setEnabled(True)
                self.local_tags.setEnabled(True)
                self.local_author.setPlaceholderText("Optional: apply to every song")
                self.local_date.setPlaceholderText("Optional: YYYY-MM-DD (apply to every song)")
                self.local_tags.setPlaceholderText("Optional: comma-separated (apply to every song)")
                self.local_status.setText(f"Ready to import supported media from {path.name}")
                return
            prefill = _local_import_defaults(path)
            self.local_title.setText(prefill["title"])
            self.local_author.setText(prefill["author"])
            self.local_date.setText(format_date(prefill["upload_date"]))
            self.local_title.setEnabled(True)
            self.local_author.setEnabled(True)
            self.local_date.setEnabled(True)
            self.local_tags.setEnabled(True)
            self.local_status.setText(f"Ready to import {path.name}")
            return
        self.file_path.setText(f"{len(self.local_paths)} files selected")
        self.local_title.clear()
        self.local_author.clear()
        self.local_date.clear()
        self.local_tags.clear()
        self.local_title.setEnabled(False)
        self.local_author.setEnabled(True)
        self.local_date.setEnabled(True)
        self.local_tags.setEnabled(True)
        self.local_author.setPlaceholderText("Optional: apply to all selected files")
        self.local_date.setPlaceholderText("Optional: YYYY-MM-DD (apply to all)")
        self.local_tags.setPlaceholderText("Optional: comma-separated (apply to all)")
        self.local_status.setText(
            f"Ready to import {len(self.local_paths)} files. Author, date, and tags apply to all when provided."
        )

    def _start_direct_add(self) -> None:
        raw_inputs = [line.strip() for line in self.youtube_input.toPlainText().splitlines() if line.strip()]
        if not raw_inputs:
            _show_error_popup(
                self,
                "Add New",
                "Enter a YouTube, SoundCloud, Spotify, Bandcamp, Monochrome, or direct media file link, or a YouTube video ID.",
                "Add New → Online download",
            )
            return
        online_inputs = [line for line in raw_inputs if not _is_direct_media_input(line)]
        required = ["yt-dlp", "ffmpeg"]
        if any(is_youtube_media_input(line) for line in online_inputs):
            required.extend(["yt-dlp-ejs", "yt-dlp-js-runtime"])
        if online_inputs and not self._require_dependencies(
            "Online Add Unavailable",
            required,
            "Online downloads require yt-dlp and ffmpeg. Explicit YouTube downloads also require yt-dlp's challenge solver and a JavaScript runtime.",
            source="Add New → Online download dependencies",
        ):
            self.youtube_status.set_full_text("Online download dependencies are missing; direct media file links can still be added.")
            return
        self.youtube_log.clear()
        self.youtube_progress.setRange(0, 1)
        self.youtube_progress.setValue(0)
        self.youtube_status.set_full_text("Preparing download...")
        self._set_busy(True)
        self.library_may_have_changed = True
        if len(raw_inputs) == 1:
            worker = DirectAddWorker(self.config, self.metadata, raw_inputs[0])
            self.direct_worker = worker
        else:
            worker = BatchDirectAddWorker(self.config, self.metadata, raw_inputs)
            self.batch_direct_worker = worker
        worker.progress.connect(self._on_direct_progress)
        worker.completed.connect(self._on_direct_completed)
        self._register_add_worker(worker)
        worker.start()

    def _on_direct_progress(self, data: dict) -> None:
        if self._close_requested:
            return
        message = data.get("message", "")
        if message:
            self.youtube_status.set_full_text(message)
        if data.get("phase") in {"confirmed", "native", "skip", "error", "done"} and message:
            self.youtube_log.appendPlainText(message)
        active = data.get("active") or []
        if active:
            row = active[0]
            total = int(row.get("total") or 0)
            completed = int(row.get("completed") or 0)
            if total > 0:
                self.youtube_progress.setRange(0, total)
                self.youtube_progress.setValue(min(completed, total))
            else:
                self.youtube_progress.setRange(0, 0)

    def _on_direct_completed(self, result: dict) -> None:
        if self._close_requested:
            return
        if not result.get("ok"):
            _show_error_popup(
                self,
                "Add New",
                result.get("message", "Download failed"),
                "Add New → Online download",
            )
            self.youtube_status.set_full_text(result.get("message", "Download failed"))
            return
        self.selected_id = result.get("selected_id") or self.selected_id
        self.youtube_status.set_full_text(result.get("message", "Done"))
        self.youtube_log.appendPlainText(result.get("message", "Done"))
        self._accept_when_finished = True

    def _start_local_import(self) -> None:
        if not self.local_paths:
            _show_error_popup(self, "Add New", "Choose one or more local files to import.", "Add New → Local import")
            return
        self.local_status.setText("Importing into library...")
        self._set_busy(True)
        self.library_may_have_changed = True
        archives = [path for path in self.local_paths if path.suffix.lower() == ".zip"]
        media_paths = [path for path in self.local_paths if path.suffix.lower() in MEDIA_EXTS]
        if len(self.local_paths) == 1 and not archives:
            src = self.local_paths[0]
            if not src.exists() or not src.is_file():
                _show_error_popup(self, "Add New", "Selected file does not exist.", "Add New → Local import")
                self._set_busy(False)
                return
            title = self.local_title.text().strip() or src.stem
            author = self.local_author.text().strip()
            tags = normalize_tags(self.local_tags.text().split(","))
            if not self.local_date.text().strip():
                _show_error_popup(self, "Add New", "Enter a date for the imported file.", "Add New → Local import → Date")
                self._set_busy(False)
                return
            try:
                upload_date = normalize_upload_date(self.local_date.text().strip())
            except ValueError as exc:
                _show_error_popup(self, "Add New", str(exc), "Add New → Local import → Date")
                self._set_busy(False)
                return
            worker = LocalImportWorker(self.config, self.metadata, src, title, author, upload_date, tags)
            self.import_worker = worker
        else:
            author_override = self.local_author.text().strip()
            tags = normalize_tags(self.local_tags.text().split(","))
            date_override = self.local_date.text().strip()
            if date_override:
                try:
                    upload_date_override = normalize_upload_date(date_override)
                except ValueError as exc:
                    _show_error_popup(self, "Add New", str(exc), "Add New → Local import → Date")
                    self._set_busy(False)
                    return
            else:
                upload_date_override = ""
            sources = []
            for src in media_paths:
                if not src.exists() or not src.is_file():
                    continue
                prefill = _local_import_defaults(src)
                sources.append(
                    (
                        src,
                        prefill["title"],
                        author_override or prefill["author"],
                        upload_date_override or prefill["upload_date"],
                        tags,
                    )
                )
            if not sources and not archives:
                _show_error_popup(self, "Add New", "No usable local files were selected.", "Add New → Local import")
                self._set_busy(False)
                return
            worker = BatchLocalImportWorker(
                self.config,
                self.metadata,
                sources,
                archives,
                author_override,
                upload_date_override,
                tags,
            )
            self.batch_import_worker = worker
        worker.completed.connect(self._on_local_completed)
        self._register_add_worker(worker)
        worker.start()

    def _on_local_completed(self, result: dict) -> None:
        if self._close_requested:
            return
        if not result.get("ok"):
            _show_error_popup(
                self,
                "Add New",
                result.get("message", "Import failed"),
                "Add New → Local import",
            )
            self.local_status.setText(result.get("message", "Import failed"))
            return
        self.selected_id = result.get("selected_id") or self.selected_id
        self.local_status.setText(result.get("message", "Import complete"))
        self._accept_when_finished = True

    def _set_busy(self, busy: bool) -> None:
        self.youtube_add_btn.setEnabled(not busy)
        self.local_import_btn.setEnabled(not busy)
        self.tabs.setEnabled(not busy)

    def _begin_close_request(self) -> None:
        if self._close_requested:
            return
        self._close_requested = True
        self._accept_when_finished = False
        for worker in self._add_workers():
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        if self.direct_worker or self.batch_direct_worker:
            self.youtube_status.set_full_text("Stopping download...")
        if self.import_worker or self.batch_import_worker:
            self.local_status.setText("Finishing import...")
        self._set_busy(True)

    def _request_close(self) -> None:
        if self._add_workers():
            self._begin_close_request()
            return
        super().reject()

    def reject(self) -> None:
        """Defer button-driven rejection until an add worker has stopped."""
        if self._add_workers():
            self._begin_close_request()
            return
        super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        workers = self._add_workers()
        if workers:
            self._begin_close_request()
            event.ignore()
            return
        event.accept()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        config: Optional[AppConfig] = None,
        *,
        dependencies_checked: bool = False,
        remember_root_on_move: bool = True,
    ):
        super().__init__()
        icon = _application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.config = config or load_config()
        self._remember_root_on_move = remember_root_on_move
        self._ensure_app_dirs()
        self.metadata = MetadataStore(self.config.metadata_path)
        self.metadata.load()
        self.library = LibraryIndex(self.config.download_dir)
        self.current: Optional[VideoEntry] = None
        self.export_workers: set[ExportWorker] = set()
        self.bpm_worker: Optional[BpmAnalysisWorker] = None
        self.cache_worker: Optional[CacheUpdateWorker] = None
        self._update_worker: Optional[UpdateCheckWorker] = None
        self._update_check_scheduled = False
        self._available_update_url: Optional[str] = None
        self.cache_progress: dict = {}
        self._cache_dialog: Optional[CacheDialog] = None
        self.network = QtNetwork.QNetworkAccessManager(self)
        # Thumbnail fetching is intentionally disabled for now. Keep the cache
        # directory and implementation available so it can be re-enabled later.
        # self.network.finished.connect(self._thumbnail_reply_finished)
        self.discord_rpc = None
        self._presence_thumbnail_cache: dict[str, Optional[str]] = {}
        self._presence_update_timer = QtCore.QTimer(self)
        self._presence_update_timer.setSingleShot(True)
        self._presence_update_timer.timeout.connect(self._flush_presence_update)
        self._presence_update_pending = False
        self._presence_retry_delay_ms = 1000
        self._mpv_preheated = False
        self._dependency_notice_shown = dependencies_checked
        self._startup_automation_pending = True
        self._startup_window_shown = False
        self._playback_entry_id: Optional[str] = None
        self._tracked_position = 0.0
        self._tracked_seconds_pending = 0.0
        self._player_duration = 0.0
        self._player_paused = False
        self._play_queue: list[str] = []
        self._queue_loop_enabled = False
        # The queue retains its playback history.  This cursor identifies the
        # queued row currently being played; rows after it are still pending.
        self._queue_current_index: Optional[int] = None
        self._preserve_similar_list = False
        self._similar_seed_id: Optional[str] = None
        self._artist_seed_id: Optional[str] = None
        self._eof_handled_for_entry_id: Optional[str] = None
        self._library_song_summary_cache = "Library not loaded"
        self._similarity_catalog_cache = SimilarityCatalogCache()
        self._last_metadata_flush_monotonic = time.monotonic()
        self._metadata_flush_interval = 15.0
        self.managed_server = ManagedServer(self.config)
        self.managed_server.statusChanged.connect(self._server_status_changed)
        self.tray: Optional[QtWidgets.QSystemTrayIcon] = None
        self.tray_status_action: Optional[QtGui.QAction] = None
        self.stop_server_action: Optional[QtGui.QAction] = None
        self._settings_dialog: Optional[SettingsDialog] = None
        self._quitting = False
        app = QtWidgets.QApplication.instance()
        self._quit_on_last_window_closed_before_startup = True
        if app is not None:
            self._quit_on_last_window_closed_before_startup = app.quitOnLastWindowClosed()
            # A startup dialog may be the only visible top-level widget. Keep
            # its close from ending the process before startup can finish.
            app.setQuitOnLastWindowClosed(False)

        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        apply_color_scheme(self.config.color_theme)
        self._build_ui()
        self._refresh_playback_icons()
        self._setup_tray()
        self._reload_library()
        self._setup_timer()
        QtCore.QTimer.singleShot(0, self._run_startup_automation)

    def _ensure_app_dirs(self) -> None:
        self._ensure_config_dirs(self.config)

    @staticmethod
    def _ensure_config_dirs(config: AppConfig) -> None:
        config.download_dir.mkdir(parents=True, exist_ok=True)
        config.exports_dir.mkdir(parents=True, exist_ok=True)
        config.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        config.artist_thumbnails_dir.mkdir(parents=True, exist_ok=True)

    def _setup_tray(self) -> None:
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QtWidgets.QSystemTrayIcon(self)
        self.tray.setIcon(self.windowIcon() or self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray.setToolTip(APP_NAME)

        menu = QtWidgets.QMenu(self)
        show_action = menu.addAction(f"Open {APP_NAME}")
        show_action.triggered.connect(self._show_from_tray)
        self.tray_status_action = menu.addAction(self.managed_server.status)
        self.tray_status_action.setEnabled(False)
        self.stop_server_action = menu.addAction("Stop Server")
        self.stop_server_action.triggered.connect(self.managed_server.stop)
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_from_tray)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._open_available_update_page)
        self.tray.show()

    def _tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        """Quit explicitly instead of treating this as a normal window close."""
        self._quitting = True
        self.close()
        QtWidgets.QApplication.quit()

    def _server_status_changed(self, status: str) -> None:
        if self.tray_status_action:
            self.tray_status_action.setText(status)
        if self.stop_server_action:
            self.stop_server_action.setEnabled(bool(self.managed_server.httpd or self.managed_server.external))
        subsonic_action = getattr(self, "subsonic_action", None)
        if subsonic_action is not None:
            blocker = QtCore.QSignalBlocker(subsonic_action)
            subsonic_action.setChecked(bool(self.managed_server.httpd or self.managed_server.external))
            del blocker
        self.statusBar().showMessage(status)
        if status == SERVER_PASSWORD_REQUIRED_STATUS:
            _show_error_popup(
                self,
                "Subsonic Server Not Started",
                "Enter a password in Settings → Integrations before starting the Subsonic server.",
                "Integrations → Subsonic Server",
            )

    def _toggle_subsonic(self, enabled: bool) -> None:
        """Start or stop the Subsonic server for this session only."""
        if enabled:
            self.managed_server.start_if_available()
        else:
            self.managed_server.stop()

    def _update_integration_action_visibility(self) -> None:
        """Show runtime integration toggles only when enabled in Settings."""
        discord_action = getattr(self, "discord_action", None)
        if discord_action is not None:
            discord_action.setVisible(bool(self.config.discord_enabled))
        subsonic_action = getattr(self, "subsonic_action", None)
        if subsonic_action is not None:
            subsonic_action.setVisible(bool(self.config.server_autostart))

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        left = QtWidgets.QWidget()
        left.setMinimumWidth(360)
        left.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search title, artist, tag, or media ID")
        self.search.setAccessibleName("Search library")
        self.search.setToolTip("Search titles, artists, tags, and media IDs. Press Escape to clear the selection.")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render_results)
        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.setToolTip("Sort library results")
        self.sort_combo.addItems(
            [
                "Relevance",
                "Alphabetical",
                "Most Recently Downloaded",
                "Oldest Downloaded",
                "Most Recently Uploaded",
                "Oldest Uploaded",
                "Last Played",
                "Most Played (by time)",
                "Most Played (by plays)",
                "BPM (descending)",
                "BPM (ascending)",
            ]
        )
        self.sort_combo.currentTextChanged.connect(self._render_results)
        self.table_model = VideoTableModel()
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 86)
        self.table.setColumnWidth(3, 112)
        self.table.setColumnWidth(4, 58)
        self.table.setColumnHidden(4, True)
        self.table.setSortingEnabled(False)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _index: self._play_selected())
        search_row = QtWidgets.QHBoxLayout()
        search_row.addWidget(self.search, stretch=1)
        self.result_count = QtWidgets.QLabel("0 songs")
        self.result_count.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.result_count.setMinimumWidth(74)
        self.result_count.setStyleSheet("color: palette(mid);")
        search_row.addWidget(self.result_count)
        search_row.addWidget(self.sort_combo)
        left_layout.addLayout(search_row)
        self.empty_state = QtWidgets.QLabel()
        self.empty_state.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.empty_state.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.empty_state.setStyleSheet("color: palette(mid); padding: 24px;")
        self.library_view = QtWidgets.QStackedWidget()
        self.library_view.addWidget(self.table)
        self.library_view.addWidget(self.empty_state)
        left_layout.addWidget(self.library_view, stretch=1)

        right = QtWidgets.QWidget()
        right.setMinimumWidth(460)
        right.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.player = MpvWidget()
        self.player_frame = AspectRatioWidget(self.player)
        right_layout.addWidget(self.player_frame)
        self.playback_bar = self._player_controls()
        right_layout.addWidget(self.playback_bar)
        self._set_playback_bar_visible(self.config.show_playback_bar)
        right_layout.addWidget(self._details_layout(), stretch=1)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([520, 660])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.splitter = splitter
        root.addWidget(splitter)
        self.setCentralWidget(central)

        update_action = QtGui.QAction("Update Cache", self)
        update_action.triggered.connect(self._update_cache)
        self.update_cache_action = update_action
        reload_action = QtGui.QAction("Reload Library", self)
        reload_action.setShortcut(QtGui.QKeySequence("Ctrl+R"))
        reload_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        reload_action.triggered.connect(
            lambda _checked=False: self._reload_library(force_file_scan=True)
        )
        analyze_bpm_action = QtGui.QAction("Analyze Missing BPM", self)
        analyze_bpm_action.triggered.connect(self._analyze_missing_bpm)
        self.discord_action = QtGui.QAction("Discord Presence", self)
        self.discord_action.setCheckable(True)
        self.discord_action.setChecked(self.config.discord_enabled)
        self.discord_action.triggered.connect(self._toggle_discord)
        self.subsonic_action = QtGui.QAction("Subsonic Server", self)
        self.subsonic_action.setCheckable(True)
        self.subsonic_action.setChecked(bool(self.managed_server.httpd or self.managed_server.external))
        self.subsonic_action.setToolTip("Start or stop the Subsonic server for this session; does not change startup settings.")
        self.subsonic_action.triggered.connect(self._toggle_subsonic)
        clear_selection_action = QtGui.QAction("Clear Selection", self)
        clear_selection_action.setShortcut(QtGui.QKeySequence("Escape"))
        clear_selection_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        clear_selection_action.setToolTip("Deselect the library track. Show the playing track instead, if there is one.")
        clear_selection_action.triggered.connect(self._clear_library_selection)
        queue_action = QtGui.QAction("Queue Selected", self)
        queue_action.setShortcut(QtGui.QKeySequence("Ctrl+Return"))
        queue_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        queue_action.triggered.connect(self._queue_selected)
        self.queue_action = queue_action
        self.loop_queue_action = QtGui.QAction("Loop Queue", self)
        self.loop_queue_action.setCheckable(True)
        self.loop_queue_action.setToolTip("Repeat the queue after its last track finishes.")
        self.loop_queue_action.toggled.connect(self._set_loop_queue)
        add_new_action = QtGui.QAction("Add New", self)
        add_new_action.setShortcut(QtGui.QKeySequence("Ctrl+N"))
        add_new_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        add_new_action.triggered.connect(self._open_add_new)
        settings_action = QtGui.QAction("Settings", self)
        settings_action.triggered.connect(lambda _checked=False: self._open_settings())
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(add_new_action)
        file_menu.addSeparator()
        close_action = QtGui.QAction("Close", self)
        close_action.setShortcut(QtGui.QKeySequence("Ctrl+Q"))
        close_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        close_action.triggered.connect(self.close)
        file_menu.addAction(close_action)
        quit_action = QtGui.QAction("Quit", self)
        quit_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+Q"))
        quit_action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        quit_action.triggered.connect(self._quit_from_tray)
        file_menu.addAction(quit_action)

        library_menu = self.menuBar().addMenu("Library")
        library_settings_action = QtGui.QAction("Library Settings…", self)
        library_settings_action.setToolTip("Open Settings on the Library page.")
        library_settings_action.triggered.connect(
            lambda _checked=False: self._open_settings("Library")
        )
        self.library_settings_action = library_settings_action
        library_menu.addAction(library_settings_action)
        library_menu.addSeparator()
        library_menu.addAction(reload_action)
        library_menu.addAction(update_action)
        library_menu.addSeparator()
        library_menu.addAction(analyze_bpm_action)

        playback_menu = self.menuBar().addMenu("Playback")
        playback_menu.addAction(queue_action)
        playback_menu.addAction(self.loop_queue_action)
        playback_menu.addAction(clear_selection_action)

        integrations_menu = self.menuBar().addMenu("Integrations")
        integrations_menu.addAction(self.discord_action)
        integrations_menu.addAction(self.subsonic_action)
        integrations_menu.addSeparator()
        integration_settings_action = QtGui.QAction("Integration Settings…", self)
        integration_settings_action.setToolTip("Open Settings on the Integrations page.")
        integration_settings_action.triggered.connect(
            lambda _checked=False: self._open_settings("Integrations")
        )
        integrations_menu.addAction(integration_settings_action)
        self._update_integration_action_visibility()
        self.menuBar().addAction(settings_action)
        self.menuBar().setNativeMenuBar(False)

        self._update_action_states()
        self.statusBar().showMessage(self._dependency_status())

    def _player_controls(self) -> QtWidgets.QWidget:
        """Build the small, discoverable transport bar below the embedded player."""
        controls = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.play_pause_btn = QtWidgets.QToolButton()
        self.play_pause_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.play_pause_btn.setFixedSize(32, 28)
        self.play_pause_btn.setIconSize(QtCore.QSize(18, 18))
        self._refresh_playback_icons()
        self.play_pause_btn.setIcon(self._play_icon)
        self.play_pause_btn.setAccessibleName("Play selected track")
        self.play_pause_btn.setToolTip("Play the selected track")
        self.play_pause_btn.clicked.connect(self._toggle_pause)

        self.position_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.setAccessibleName("Playback position")
        self.position_slider.setToolTip("Seek through the current track")
        self.position_slider.sliderMoved.connect(self._preview_seek_position)
        self.position_slider.sliderReleased.connect(self._seek_player)

        self.player_time_label = QtWidgets.QLabel("0:00 / 0:00")
        self.player_time_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.player_time_label.setMinimumWidth(82)
        self.player_time_label.setStyleSheet("color: palette(mid);")

        volume_label = QtWidgets.QLabel("Vol")
        volume_label.setToolTip("Playback volume")
        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(88)
        self.volume_slider.setAccessibleName("Playback volume")
        self.volume_slider.setToolTip("Playback volume")
        self.volume_slider.valueChanged.connect(self.player.set_volume)

        layout.addWidget(self.play_pause_btn)
        layout.addWidget(self.position_slider, stretch=1)
        layout.addWidget(self.player_time_label)
        layout.addWidget(volume_label)
        layout.addWidget(self.volume_slider)
        self._update_player_controls(0.0, 0.0, False)
        return controls

    def _playback_icon(self, show_pause: bool) -> QtGui.QIcon:
        size = 24
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self.config.color_theme == "system":
            background = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window)
            light_theme = background.lightness() >= 128
        else:
            light_theme = self.config.color_theme == "light"
        color = QtGui.QColor("#000000" if light_theme else "#ffffff")
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        if show_pause:
            painter.drawRoundedRect(QtCore.QRectF(5.0, 4.0, 5.0, 16.0), 1.0, 1.0)
            painter.drawRoundedRect(QtCore.QRectF(14.0, 4.0, 5.0, 16.0), 1.0, 1.0)
        else:
            path = QtGui.QPainterPath()
            path.moveTo(6.0, 4.0)
            path.lineTo(19.0, 12.0)
            path.lineTo(6.0, 20.0)
            path.closeSubpath()
            painter.drawPath(path)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _refresh_playback_icons(self) -> None:
        self._play_icon = self._playback_icon(False)
        self._pause_icon = self._playback_icon(True)
        play_pause_btn = getattr(self, "play_pause_btn", None)
        if play_pause_btn is not None:
            play_pause_btn.setIcon(self._play_icon if self._player_paused or not self._playback_entry_id else self._pause_icon)

    def _set_playback_bar_visible(self, visible: bool) -> None:
        playback_bar = getattr(self, "playback_bar", None)
        if playback_bar is not None:
            playback_bar.setVisible(bool(visible))

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        config = getattr(self, "config", None)
        if (
            event.type() == QtCore.QEvent.Type.ApplicationPaletteChange
            and getattr(config, "color_theme", "") == "system"
        ):
            self._refresh_playback_icons()

    def _show_dependency_notice(self) -> None:
        if self._dependency_notice_shown:
            return
        self._dependency_notice_shown = True
        missing = _missing_dependencies()
        if not missing["required"]:
            return
        parts = []
        if missing["required"]:
            parts.append("Required tools are missing:")
            for item in missing["required"]:
                parts.append(f"- {DEPENDENCY_LABELS.get(item, item)}")
        parts.append("")
        parts.append("Rerun the setup helper or follow the Install section in README.md, then restart the app.")
        QtWidgets.QMessageBox.information(self, "Missing Required Tools", "\n".join(parts))

    def _require_dependencies(self, title: str, items: list[str], detail: str, source: str = "Dependencies") -> bool:
        missing = [item for item in items if item in _missing_dependencies()["required"] or item in _missing_dependencies()["optional"]]
        if not missing:
            return True
        _show_error_popup(self, title, _format_dependency_message(title, missing, detail), source)
        return False

    def _details_layout(self) -> QtWidgets.QWidget:
        details_panel = QtWidgets.QWidget()
        details_panel.setMinimumWidth(0)
        wrapper = QtWidgets.QVBoxLayout(details_panel)
        wrapper.setContentsMargins(8, 6, 8, 6)
        wrapper.setSpacing(8)
        # Thumbnail preview disabled for now. The cache directory remains
        # available and the fetch/display methods below are left in place.
        # self.thumbnail = QtWidgets.QLabel()
        # self.thumbnail.setFixedSize(180, 135)
        # self.thumbnail.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        # self.thumbnail.setStyleSheet("background: #111; color: #aaa;")
        # wrapper.addWidget(self.thumbnail)

        details_grid = QtWidgets.QGridLayout()
        details_grid.setContentsMargins(0, 0, 0, 0)
        details_grid.setHorizontalSpacing(14)
        details_grid.setVerticalSpacing(6)
        details_grid.setColumnMinimumWidth(0, 84)
        details_grid.setColumnStretch(1, 1)

        self.title_label = self._detail_value_label(wrap=False)
        self.author_label = self._detail_value_label(wrap=False)
        self.date_label = self._detail_value_label(wrap=False)
        self.id_label = self._detail_value_label(wrap=False)
        self.path_label = self._detail_value_label(wrap=False)
        self.title_edit = QtWidgets.QLineEdit()
        self.author_edit = QtWidgets.QLineEdit()
        self.date_edit = QtWidgets.QLineEdit()
        self.date_edit.setPlaceholderText("YYYY-MM-DD")
        self.bpm_label = self._detail_value_label(wrap=False)
        self.bpm_edit = QtWidgets.QLineEdit()
        self.bpm_edit.setPlaceholderText("Leave blank to clear")
        self.presence_image_label = self._detail_value_label(wrap=False)
        self.presence_image_mode_edit = QtWidgets.QComboBox()
        for mode, label in PRESENCE_IMAGE_MODES.items():
            self.presence_image_mode_edit.addItem(label, mode)
        self.presence_image_value_edit = QtWidgets.QLineEdit()
        self.presence_image_value_edit.setPlaceholderText("URL, YouTube ID, or Discord image key")
        presence_image_editor = QtWidgets.QWidget()
        presence_image_layout = QtWidgets.QHBoxLayout(presence_image_editor)
        presence_image_layout.setContentsMargins(0, 0, 0, 0)
        presence_image_layout.setSpacing(5)
        presence_image_layout.addWidget(self.presence_image_mode_edit)
        presence_image_layout.addWidget(self.presence_image_value_edit, 1)
        self.presence_image_mode_edit.currentIndexChanged.connect(self._presence_image_mode_changed)
        self.tap_tempo = TapTempo()
        self.tap_bpm_btn = QtWidgets.QPushButton("Tap BPM")
        self.tap_bpm_btn.setToolTip("Tap once per beat. A pause of three seconds starts a new measurement.")
        self.tap_bpm_btn.clicked.connect(self._tap_bpm)
        self.redetect_bpm_btn = QtWidgets.QPushButton("Re-detect BPM")
        self.redetect_bpm_btn.setFixedWidth(130)
        self.redetect_bpm_btn.clicked.connect(self._redetect_selected_bpm)
        bpm_edit_container = QtWidgets.QWidget()
        bpm_edit_row = QtWidgets.QHBoxLayout(bpm_edit_container)
        bpm_edit_row.setContentsMargins(0, 0, 0, 0)
        bpm_edit_row.setSpacing(5)
        bpm_edit_row.addWidget(self.bpm_edit, stretch=1)
        bpm_edit_row.addWidget(self.tap_bpm_btn)
        bpm_edit_row.addWidget(self.redetect_bpm_btn)
        self.tag_editor = TagEditor()
        self.tag_editor.tagsChanged.connect(self._save_tags)
        self.tag_editor.tagsCopied.connect(self._show_tags_copied)
        for editor in (self.title_edit, self.author_edit, self.date_edit, self.bpm_edit, self.presence_image_value_edit):
            editor.setMinimumHeight(24)
            editor.setMaximumHeight(28)
            editor.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.title_stack = self._detail_stack(self.title_label, self.title_edit)
        self.author_stack = self._detail_stack(self.author_label, self.author_edit)
        self.date_stack = self._detail_stack(self.date_label, self.date_edit)
        self.bpm_stack = self._detail_stack(self.bpm_label, bpm_edit_container)
        self.presence_image_stack = self._detail_stack(self.presence_image_label, presence_image_editor)
        self._set_detail_editing(False)

        for row, (name, widget) in enumerate(
            [
                ("Title", self.title_stack),
                ("Author", self.author_stack),
                ("Upload Date", self.date_stack),
                ("BPM", self.bpm_stack),
                ("Presence Image", self.presence_image_stack),
                ("Tags", self.tag_editor),
                ("ID", self.id_label),
                ("File", self.path_label),
            ]
        ):
            label = QtWidgets.QLabel(name)
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop)
            if name == "Presence Image":
                self.presence_image_row_label = label
            details_grid.addWidget(label, row, 0)
            details_grid.addWidget(widget, row, 1)
            if name == "Presence Image":
                self._set_presence_image_row_visible(self.config.discord_enabled)

        self.meta_text = QtWidgets.QPlainTextEdit()
        self.meta_text.setReadOnly(True)
        self.meta_text.setMinimumHeight(72)
        self.meta_text.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.lyrics_text = QtWidgets.QPlainTextEdit()
        self.lyrics_text.setReadOnly(True)
        self.lyrics_text.setPlaceholderText("[00:12.34]Lyric line [.lrc]")
        self.lyrics_text.setMinimumHeight(120)
        self.lyrics_text.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.current_lyric_label = QtWidgets.QLabel("")
        self._configure_tab_status_label(self.current_lyric_label)
        self.insert_lyric_time_btn = QtWidgets.QPushButton("Insert Time")
        self.insert_lyric_time_btn.setFixedWidth(110)
        self.insert_lyric_time_btn.clicked.connect(self._insert_lyric_timestamp)
        self.insert_lyric_time_btn.setVisible(False)
        self.save_lyrics_btn = QtWidgets.QPushButton("Save Lyrics")
        self.save_lyrics_btn.setFixedWidth(110)
        self.save_lyrics_btn.clicked.connect(self._save_lyrics)
        self.save_lyrics_btn.setVisible(False)
        self.export_ext = QtWidgets.QComboBox()
        self.export_ext.setFixedWidth(110)
        self.export_ext.addItems(["mp3", "m4a", "flac", "opus", "wav", "mkv", "mp4", "webm"])
        self.export_btn = QtWidgets.QPushButton("Export")
        self.export_btn.setFixedWidth(110)
        self.export_btn.setToolTip("Convert the selected local file to another format")
        self.export_btn.clicked.connect(self._export_selected)
        self.copy_selected_btn = QtWidgets.QPushButton("Copy")
        self.copy_selected_btn.setFixedWidth(90)
        self.copy_selected_btn.setToolTip("Copy the selected file to the clipboard")
        self.copy_selected_btn.clicked.connect(self._copy_selected_file)
        self.open_external_btn = QtWidgets.QPushButton("Open Pop-Out")
        self.open_external_btn.setFixedWidth(120)
        self.open_external_btn.clicked.connect(self._open_external)
        self.show_in_folder_btn = QtWidgets.QPushButton("Show in Folder")
        self.show_in_folder_btn.setFixedWidth(120)
        self.show_in_folder_btn.clicked.connect(self._show_in_folder)
        self.edit_btn = QtWidgets.QPushButton("Edit")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setFixedWidth(90)
        self.edit_btn.toggled.connect(self._toggle_detail_editing)
        self.delete_btn = QtWidgets.QPushButton("Delete")
        self.delete_btn.setFixedWidth(100)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.hide_subsonic_btn = QtWidgets.QPushButton("Hide")
        self.hide_subsonic_btn.setCheckable(True)
        self.hide_subsonic_btn.setFixedWidth(90)
        self.hide_subsonic_btn.setToolTip("Hide this track from Subsonic and related song lists")
        self.hide_subsonic_btn.toggled.connect(self._toggle_hidden_from_subsonic)
        self.hide_subsonic_btn.setVisible(False)
        self.audio_only_btn = QtWidgets.QPushButton("Audio Only")
        self.audio_only_btn.setCheckable(True)
        self.audio_only_btn.setFixedWidth(110)
        self.audio_only_btn.toggled.connect(self._set_audio_only)
        self.queue_btn = QtWidgets.QPushButton("Add to Queue")
        self.queue_btn.setFixedWidth(120)
        self.queue_btn.clicked.connect(self._queue_selected)
        export_row = QtWidgets.QHBoxLayout()
        export_row.setContentsMargins(0, 0, 0, 0)
        export_row.setSpacing(8)
        export_row.addWidget(QtWidgets.QLabel("Export"))
        export_row.addWidget(self.export_ext)
        export_row.addWidget(self.export_btn)
        export_row.addWidget(self.copy_selected_btn)
        export_row.addStretch(1)

        lyrics_page = QtWidgets.QWidget()
        lyrics_layout = QtWidgets.QVBoxLayout(lyrics_page)
        lyrics_layout.setContentsMargins(0, 0, 0, 0)
        lyrics_layout.setSpacing(0)
        lyrics_layout.addWidget(self._tab_status_bar(self.current_lyric_label))
        lyrics_layout.addWidget(self.lyrics_text)
        lyrics_controls = QtWidgets.QHBoxLayout()
        lyrics_controls.setContentsMargins(0, 0, 0, 0)
        lyrics_controls.addStretch(1)
        lyrics_controls.addWidget(self.insert_lyric_time_btn)
        lyrics_controls.addWidget(self.save_lyrics_btn)
        lyrics_layout.addLayout(lyrics_controls)

        technical_page = QtWidgets.QWidget()
        technical_layout = QtWidgets.QVBoxLayout(technical_page)
        technical_layout.setContentsMargins(0, 0, 0, 0)
        technical_layout.addWidget(self.meta_text)

        queue_page = QtWidgets.QWidget()
        queue_layout = QtWidgets.QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(0)
        self.queue_time_label = QtWidgets.QLabel()
        self._configure_tab_status_label(self.queue_time_label)
        queue_layout.addWidget(self._tab_status_bar(self.queue_time_label))
        self.queue_list = TrackListWidget()
        self.queue_list.setAlternatingRowColors(True)
        self.queue_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.queue_list.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.queue_list.setDropIndicatorShown(True)
        self.queue_list.setToolTip("Double-click to play. Drag tracks to reorder the queue.")
        self.queue_list.orderChanged.connect(self._queue_order_changed)
        self.queue_list.itemSelectionChanged.connect(self._select_queue_item)
        queue_layout.addWidget(self.queue_list)
        queue_controls = QtWidgets.QGridLayout()
        queue_controls.setContentsMargins(8, 8, 8, 6)
        queue_controls.setHorizontalSpacing(8)
        queue_controls.setColumnStretch(0, 1)
        queue_controls.setColumnStretch(2, 1)
        self.loop_queue_btn = QtWidgets.QCheckBox("Loop Queue")
        self.loop_queue_btn.setToolTip("Repeat the queue after its last track finishes.")
        self.loop_queue_btn.toggled.connect(self._set_loop_queue)
        self.clear_queue_btn = QtWidgets.QPushButton("Clear Queue")
        self.clear_queue_btn.setMinimumWidth(120)
        self.clear_queue_btn.clicked.connect(self._clear_queue)
        queue_controls.addWidget(
            self.loop_queue_btn,
            0,
            0,
            alignment=QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
        )
        center_actions = QtWidgets.QWidget()
        center_layout = QtWidgets.QHBoxLayout(center_actions)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        center_layout.addWidget(self.queue_btn)
        center_layout.addWidget(self.clear_queue_btn)
        queue_controls.addWidget(center_actions, 0, 1, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        queue_layout.addLayout(queue_controls)

        similar_page = QtWidgets.QWidget()
        similar_layout = QtWidgets.QVBoxLayout(similar_page)
        similar_layout.setContentsMargins(0, 0, 0, 0)
        similar_layout.setSpacing(0)
        self.similar_status_label = QtWidgets.QLabel("Select a song to find similar tracks.")
        self._configure_tab_status_label(self.similar_status_label)
        similar_layout.addWidget(self._tab_status_bar(self.similar_status_label))
        self.similar_list = QtWidgets.QListWidget()
        self.similar_list.setAlternatingRowColors(True)
        self.similar_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.similar_list.itemSelectionChanged.connect(self._select_similar_item)
        similar_layout.addWidget(self.similar_list)

        artist_page = QtWidgets.QWidget()
        artist_layout = QtWidgets.QVBoxLayout(artist_page)
        artist_layout.setContentsMargins(0, 0, 0, 0)
        artist_layout.setSpacing(0)
        self.artist_status_label = QtWidgets.QLabel("Select a song to find more tracks by the same artist.")
        self._configure_tab_status_label(self.artist_status_label)
        artist_layout.addWidget(self._tab_status_bar(self.artist_status_label))
        self.artist_list = QtWidgets.QListWidget()
        self.artist_list.setAlternatingRowColors(True)
        self.artist_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.artist_list.itemSelectionChanged.connect(self._select_artist_item)
        artist_layout.addWidget(self.artist_list)

        self.details_tabs = QtWidgets.QTabWidget()
        # Give queue, lyrics, and related-song views enough room to be useful
        # while allowing the surrounding detail pane to scroll on short windows.
        self.details_tabs.setMinimumHeight(240)
        self.queue_tab_index = self.details_tabs.addTab(queue_page, "Queue (0)")
        self.lyrics_tab_index = self.details_tabs.addTab(lyrics_page, "Lyrics")
        self.similar_tab_index = self.details_tabs.addTab(similar_page, "Similar")
        self.artist_tab_index = self.details_tabs.addTab(artist_page, "By Artist")
        self.technical_tab_index = self.details_tabs.addTab(technical_page, "Technical")
        self.details_tabs.currentChanged.connect(self._details_tab_changed)
        self._render_queue()
        self._update_lyrics_tab_enabled()

        self.detail_fields_panel = QtWidgets.QWidget()
        self.detail_fields_panel.setLayout(details_grid)
        wrapper.addWidget(self.detail_fields_panel)

        export_row_widget = QtWidgets.QWidget()
        export_row_widget.setLayout(export_row)
        self.export_row_widget = export_row_widget
        wrapper.addWidget(export_row_widget)

        actions_row = QtWidgets.QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(8)
        actions_row.addWidget(self.open_external_btn)
        actions_row.addWidget(self.show_in_folder_btn)
        actions_row.addWidget(self.edit_btn)
        actions_row.addWidget(self.delete_btn)
        actions_row.addWidget(self.audio_only_btn)
        actions_row.addWidget(self.hide_subsonic_btn)
        actions_row.addStretch(1)
        actions_row_widget = QtWidgets.QWidget()
        actions_row_widget.setLayout(actions_row)
        self.actions_row_widget = actions_row_widget
        wrapper.addWidget(actions_row_widget)
        wrapper.addWidget(self.details_tabs, stretch=1)
        self._update_action_states()
        details_scroll = QtWidgets.QScrollArea()
        details_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        details_scroll.setWidgetResizable(True)
        details_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        details_scroll.setWidget(details_panel)
        self.details_scroll = details_scroll
        return details_scroll

    def _configure_tab_status_label(self, label: QtWidgets.QLabel) -> None:
        """Give tab headers a consistent, vertically centered status bar."""
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.setContentsMargins(0, 0, 0, 0)
        label.setMargin(0)
        label.setWordWrap(False)
        # Let the parent layout center the font-height widget instead of
        # centering a label with extra empty space inside it.
        label.setFixedHeight(label.fontMetrics().height())
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def _tab_status_bar(self, label: QtWidgets.QLabel) -> QtWidgets.QWidget:
        """Return a fixed-height tab header with its label centered on both axes."""
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(28)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        return bar

    def _detail_value_label(self, wrap: bool) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel()
        label.setWordWrap(wrap)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        label.setMinimumHeight(22)
        label.setMaximumHeight(44 if wrap else 24)
        label.setMinimumWidth(1)
        label.setMargin(0)
        return label

    def _detail_stack(self, label: QtWidgets.QLabel, editor: QtWidgets.QWidget) -> QtWidgets.QStackedWidget:
        stack = QtWidgets.QStackedWidget()
        stack.addWidget(label)
        stack.addWidget(editor)
        stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        stack.setMinimumHeight(24)
        stack.setMaximumHeight(44 if label.wordWrap() else 28)
        stack.setMinimumWidth(1)
        return stack

    def _set_detail_label_text(self, label: QtWidgets.QLabel, text: str) -> None:
        label.setText(text)
        label.setToolTip(text)

    def _set_detail_content_visible(self, visible: bool) -> None:
        """Hide track-specific fields and actions when no track is active."""
        for name in ("detail_fields_panel", "export_row_widget", "actions_row_widget"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(visible)

    def _update_action_states(self) -> None:
        """Keep controls honest about whether the current selection supports them."""
        entry = self.current
        has_entry = entry is not None
        self._set_detail_content_visible(has_entry)
        has_file = bool(entry and entry.path and entry.path.exists())
        for name, enabled in (
            ("open_external_btn", has_file),
            ("show_in_folder_btn", has_file),
            ("export_btn", has_file),
            ("copy_selected_btn", has_file),
            ("edit_btn", has_entry),
            ("delete_btn", has_entry),
            ("audio_only_btn", has_file),
            ("queue_btn", has_file),
            ("hide_subsonic_btn", has_entry),
            ("tap_bpm_btn", has_entry and bool(getattr(self, "edit_btn", None) and self.edit_btn.isChecked())),
            ("redetect_bpm_btn", has_file and bool(getattr(self, "edit_btn", None) and self.edit_btn.isChecked())),
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(enabled)
        queue_action = getattr(self, "queue_action", None)
        if queue_action is not None:
            queue_action.setEnabled(has_file)

    def _update_player_controls(self, position: float, duration: float, paused: bool) -> None:
        if not hasattr(self, "play_pause_btn"):
            return
        active = bool(self._playback_entry_id)
        self._player_paused = paused
        self._player_duration = max(0.0, duration)
        self.play_pause_btn.setEnabled(active)
        self.position_slider.setEnabled(active and self._player_duration > 0)
        self.volume_slider.setEnabled(active)
        duration_ms = max(0, min(2_147_483_647, round(self._player_duration * 1000)))
        if self.position_slider.maximum() != duration_ms:
            blocker = QtCore.QSignalBlocker(self.position_slider)
            self.position_slider.setRange(0, duration_ms)
            del blocker
        display_position = max(0.0, position)
        if self._player_duration > 0 and self.position_slider.isSliderDown():
            display_position = self.position_slider.value() / 1000.0
        display_position = min(self._player_duration, display_position) if self._player_duration > 0 else 0.0
        if not self.position_slider.isSliderDown():
            value = round(display_position * 1000)
            blocker = QtCore.QSignalBlocker(self.position_slider)
            self.position_slider.setValue(value)
            del blocker
        self.player_time_label.setText(
            f"{_format_playback_duration(display_position)} / {_format_playback_duration(self._player_duration)}"
        )
        if paused:
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setAccessibleName("Resume playback")
            self.play_pause_btn.setToolTip("Resume playback")
        elif active:
            self.play_pause_btn.setIcon(self._pause_icon)
            self.play_pause_btn.setAccessibleName("Pause playback")
            self.play_pause_btn.setToolTip("Pause playback")
        else:
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setAccessibleName("Play selected track")
            self.play_pause_btn.setToolTip("Play the selected track")

    def _toggle_pause(self) -> None:
        if not self._playback_entry_id:
            self.statusBar().showMessage("Select a local track to play")
            return
        paused = self.player.toggle_pause()
        position = self.position_slider.value() / 1000.0
        self._update_player_controls(position, self._player_duration, paused)

    def _preview_seek_position(self, value: int) -> None:
        if not self._playback_entry_id or self._player_duration <= 0:
            return
        position = max(0.0, min(self._player_duration, value / 1000.0))
        self.player_time_label.setText(
            f"{_format_playback_duration(position)} / {_format_playback_duration(self._player_duration)}"
        )

    def _seek_player(self, value: Optional[int] = None) -> None:
        if not self._playback_entry_id or self._player_duration <= 0:
            return
        value = self.position_slider.value() if value is None else value
        target = max(0.0, min(self._player_duration, value / 1000.0))
        self._tracked_position = target
        position = target
        duration = self._player_duration
        if self.player.seek_seconds(target):
            actual_position, actual_duration = self.player.query_position()
            if actual_duration > 0:
                duration = actual_duration
            if abs(actual_position - target) <= 0.5:
                position = actual_position
        self._update_player_controls(position, duration, self.player.is_paused())
        if hasattr(self, "timer"):
            self.timer.start(500)

    def _setup_timer(self) -> None:
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick_player)
        self.timer.start(500)

    def _reload_library(
        self,
        selected_id: Optional[str] = None,
        *,
        force_file_scan: bool = False,
    ) -> None:
        if selected_id is None:
            selected_id = self._selected_video_id()
        # In-app writes already update MetadataStore's loaded revision.  Always
        # rebuild the lightweight metadata catalog here so those writes are not
        # mistaken for an unchanged library.  LibraryIndex.refresh_file_index
        # still skips the directory walk unless the directory revision changed
        # (or the caller explicitly knows files were added/removed).
        self.metadata.load_if_changed()
        stats = self.library.rebuild(
            self.metadata.entries(),
            force_file_scan=force_file_scan,
        )
        self._refresh_library_song_summary()
        self._render_results(selected_id=selected_id)
        self.statusBar().showMessage(
            f"{self._library_song_summary()}; {stats.files_found} files found, {stats.missing_files} missing. {self._dependency_status()}"
        )

    def _library_song_summary(self) -> str:
        return self._library_song_summary_cache

    def _refresh_library_song_summary(self) -> None:
        total = len(self.library.entries)
        tagged = sum(1 for entry in self.library.entries if entry.tags)
        visible_tracks = sum(
            1 for entry in self.library.entries if entry.path and not entry.hidden_from_subsonic
        )
        storage_bytes = self.library.storage_bytes()
        total_label = "song" if total == 1 else "songs"
        tagged_label = "song" if tagged == 1 else "songs"
        visible_label = "song" if visible_tracks == 1 else "songs"
        self._library_song_summary_cache = (
            f"{total} {total_label} in library ({human_size(storage_bytes)} total); "
            f"{tagged} {tagged_label} tagged; "
            f"{visible_tracks} {visible_label} visible in similar songs"
        )

    def _render_results(self, selected_id: Optional[str] = None) -> None:
        query = self.search.text().strip()
        if query:
            rows = self.library.search(query, limit=len(self.library.entries))
        else:
            rows = list(self.library.entries)
        rows = self._sort_entries(rows, query)
        self.table_model.set_rows(rows)
        total = len(self.library.entries)
        if query:
            self.result_count.setText(f"{len(rows)} of {total} song{'s' if total != 1 else ''}")
            self.empty_state.setText(
                f"No tracks match \u201c{query}\u201d.\nTry a different search or clear the search field."
            )
        else:
            self.result_count.setText(f"{total} song{'s' if total != 1 else ''}")
            self.empty_state.setText(
                "Your library is empty.\nUse File \u2192 Add New to import or download your first track."
            )
        self.library_view.setCurrentWidget(self.table if rows else self.empty_state)
        self._restore_selection(selected_id)

    def _sort_entries(self, rows: list[VideoEntry], query: str) -> list[VideoEntry]:
        sort_mode = self._current_sort_mode()
        if sort_mode == "Relevance":
            if query:
                return rows
            return sorted(rows, key=_downloaded_time, reverse=True)
        if sort_mode == "Alphabetical":
            return sorted(rows, key=lambda entry: (entry.title.casefold(), entry.author.casefold(), entry.id))
        if sort_mode == "Most Recently Downloaded":
            return sorted(rows, key=_downloaded_time, reverse=True)
        if sort_mode == "Oldest Downloaded":
            return sorted(rows, key=_downloaded_time)
        if sort_mode == "Most Recently Uploaded":
            return sorted(rows, key=_upload_date_value, reverse=True)
        if sort_mode == "Oldest Uploaded":
            return sorted(rows, key=_upload_date_value)
        if sort_mode == "Last Played":
            return sorted(rows, key=_last_played_value, reverse=True)
        if sort_mode == "Most Played (by time)":
            return sorted(rows, key=_playback_seconds_value, reverse=True)
        if sort_mode == "Most Played (by plays)":
            return sorted(rows, key=_play_count_value, reverse=True)
        if sort_mode == "BPM (descending)":
            return sorted((entry for entry in rows if entry.bpm > 0), key=lambda entry: entry.bpm, reverse=True)
        if sort_mode == "BPM (ascending)":
            return sorted((entry for entry in rows if entry.bpm > 0), key=lambda entry: entry.bpm)
        return rows

    def _current_sort_mode(self) -> str:
        return self.sort_combo.currentText() if hasattr(self, "sort_combo") else "Relevance"

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            self._show_playing_entry_or_clear_details()
            return
        source_row = selected[0].row()
        entry = self.table_model.entry_at(source_row)
        if entry:
            self._show_entry(entry)

    def _clear_library_selection(self) -> None:
        """Deselect the library row without interrupting playback."""
        if self.edit_btn.isChecked() and not self._save_detail_edits(refresh_results=False):
            return
        if self.edit_btn.isChecked():
            blocker = QtCore.QSignalBlocker(self.edit_btn)
            self.edit_btn.setChecked(False)
            del blocker
            self.edit_btn.setText("Edit")
            self._set_detail_editing(False)
        selection_model = self.table.selectionModel()
        if selection_model:
            selection_model.clearSelection()
            selection_model.setCurrentIndex(
                QtCore.QModelIndex(),
                QtCore.QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        self._show_playing_entry_or_clear_details()

    def _show_playing_entry_or_clear_details(self) -> None:
        """Use the now-playing track as the detail fallback for no selection."""
        entry = self._entry_for_id(self._playback_entry_id) if self._playback_entry_id else None
        if entry:
            self._show_entry(entry)
        else:
            self._clear_detail_fields()

    def _clear_detail_fields(self) -> None:
        self.current = None
        self.tag_editor.setEnabled(False)
        self.tag_editor.set_tags([], self._tag_counts(), [])
        for label in (
            self.title_label,
            self.author_label,
            self.date_label,
            self.bpm_label,
            self.presence_image_label,
            self.id_label,
            self.path_label,
        ):
            self._set_detail_label_text(label, "")
        for editor in (
            self.title_edit,
            self.author_edit,
            self.date_edit,
            self.bpm_edit,
            self.presence_image_value_edit,
        ):
            editor.clear()
        self.lyrics_text.clear()
        self.current_lyric_label.clear()
        self.lyrics_text.setExtraSelections([])
        self.meta_text.clear()
        self._update_lyrics_tab_enabled()
        self._update_action_states()

    def _show_entry(self, entry: VideoEntry) -> None:
        editing = bool(getattr(self, "edit_btn", None) and self.edit_btn.isChecked())
        if editing and self.current and self.current.id != entry.id:
            if not self._save_detail_edits(refresh_results=False):
                return
        self.current = entry
        self.tag_editor.setEnabled(True)
        self.tag_editor.set_tags(
            entry.tags, self._tag_counts(), self._suggested_tags(entry.tags), self.config.disabled_similarity_tags,
        )
        self._set_detail_label_text(self.title_label, entry.title)
        self._set_detail_label_text(self.author_label, entry.author or "")
        self.date_label.setText(format_date(entry.upload_date))
        self._set_detail_label_text(self.bpm_label, str(entry.bpm) if entry.bpm > 0 else "")
        self.title_edit.setText(entry.title)
        self.author_edit.setText(entry.author or "")
        self.date_edit.setText(format_date(entry.upload_date))
        self.bpm_edit.setText(str(entry.bpm) if entry.bpm > 0 else "")
        self._set_presence_image_editor(entry)
        self._set_detail_editing(editing)
        self.lyrics_text.setPlainText(entry.lyrics or "")
        self._update_lyrics_tab_enabled()
        position, _duration = self.player.query_position()
        self._update_synced_lyrics(position)
        self._set_detail_label_text(self.id_label, entry.id)
        self._set_detail_label_text(self.path_label, str(entry.path) if entry.path else "(missing)")
        if getattr(self, "hide_subsonic_btn", None):
            blocker = QtCore.QSignalBlocker(self.hide_subsonic_btn)
            self.hide_subsonic_btn.setChecked(entry.hidden_from_subsonic)
            self.hide_subsonic_btn.setText("Show" if entry.hidden_from_subsonic else "Hide")
            del blocker
        # Thumbnail preview/fetch is disabled for now.
        # self.thumbnail.setText("Loading")
        # self._load_thumbnail(entry.id)
        self._load_technical(entry)
        if self.details_tabs.currentIndex() == self.similar_tab_index and not self._preserve_similar_list:
            self._render_similar_songs(entry)
        elif self.details_tabs.currentIndex() == self.artist_tab_index and not self._preserve_similar_list:
            self._render_artist_songs(entry)
        self._update_action_states()

    def _set_presence_image_editor(self, entry: VideoEntry) -> None:
        mode = entry.presence_image_mode if entry.presence_image_mode in PRESENCE_IMAGE_MODES else "default"
        blocker = QtCore.QSignalBlocker(self.presence_image_mode_edit)
        self.presence_image_mode_edit.setCurrentIndex(max(0, self.presence_image_mode_edit.findData(mode)))
        del blocker
        self.presence_image_value_edit.setText(entry.presence_image_value or "")
        if mode == "default":
            display = "Default (Presence Settings)"
        else:
            display = PRESENCE_IMAGE_MODES[mode]
            if entry.presence_image_value:
                display += f": {entry.presence_image_value}"
        self._set_detail_label_text(self.presence_image_label, display)
        self._presence_image_mode_changed()

    def _set_presence_image_row_visible(self, visible: bool) -> None:
        if hasattr(self, "presence_image_row_label"):
            self.presence_image_row_label.setVisible(visible)
        if hasattr(self, "presence_image_stack"):
            self.presence_image_stack.setVisible(visible)

    def _presence_image_mode_changed(self, _index: int = -1) -> None:
        mode = str(self.presence_image_mode_edit.currentData() or "default")
        needs_value = mode in {"url", "custom_youtube", "discord_key"}
        self.presence_image_value_edit.setVisible(needs_value)
        self.presence_image_value_edit.setEnabled(needs_value)
        self.presence_image_value_edit.setPlaceholderText(
            "Image URL" if mode == "url" else
            "11-character YouTube ID" if mode == "custom_youtube" else
            "Discord image asset key" if mode == "discord_key" else
            ""
        )

    def _tag_counts(self) -> dict[str, int]:
        return tag_counts(self.library.entries)

    def _suggested_tags(self, current_tags: list[str]) -> list[str]:
        return suggested_tags(self.library.entries, current_tags)

    def _save_tags(self, tags: list[str]) -> None:
        entry = self.current
        if not entry:
            return
        tags = normalize_tags(tags)
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.tags = tags
        self.metadata.upsert(stored)
        self._reload_library(selected_id=entry.id)
        self.statusBar().showMessage(f"Updated tags for {entry.title}")

    def _show_tags_copied(self, _tags: str) -> None:
        if self.current:
            self.statusBar().showMessage(f"Copied tags for {self.current.title} to clipboard")

    def _load_thumbnail(self, video_id: str) -> None:
        youtube_id = thumbnail_video_id(video_id)
        if not youtube_id:
            return
        path = thumbnail_path(self.config.thumbnails_dir, youtube_id)
        if path.exists():
            self._set_thumbnail(path)
            return
        self.thumbnail.setText("Loading")
        self._request_thumbnail(youtube_id, "maxresdefault")

    def _request_thumbnail(self, video_id: str, quality: str) -> None:
        url = QtCore.QUrl(f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg")
        request = QtNetwork.QNetworkRequest(url)
        reply = self.network.get(request)
        reply.setProperty("video_id", video_id)
        reply.setProperty("quality", quality)

    def _thumbnail_reply_finished(self, reply: QtNetwork.QNetworkReply) -> None:
        video_id = reply.property("video_id")
        quality = reply.property("quality")
        if not video_id:
            reply.deleteLater()
            return
        if reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
            if data:
                path = thumbnail_path(self.config.thumbnails_dir, str(video_id))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                if self.current and self.current.id == video_id:
                    self._set_thumbnail(path)
                reply.deleteLater()
                return
        reply.deleteLater()
        if quality == "maxresdefault":
            self._request_thumbnail(str(video_id), "hqdefault")
        elif self.current and self.current.id == video_id:
            self.thumbnail.setText("No thumbnail")

    def _set_thumbnail(self, path: Path) -> None:
        pixmap = QtGui.QPixmap(path)
        if pixmap.isNull():
            self.thumbnail.setText("No thumbnail")
            return
        self.thumbnail.setPixmap(pixmap.scaled(self.thumbnail.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))

    def _load_technical(self, entry: VideoEntry) -> None:
        summary = []
        if entry.source_type:
            summary.append(f"source: {entry.source_type}")
        if entry.resolved_from:
            summary.append(f"resolved: {entry.resolved_from}")
        if entry.audio_quality:
            summary.append(f"quality: {entry.audio_quality}")
        if entry.bpm > 0:
            source = f" ({entry.bpm_source})" if entry.bpm_source else ""
            summary.append(f"BPM: {entry.bpm}{source}")
        path = entry.path
        if not path or not path.exists():
            if entry.bitrate_kbps > 0:
                summary.append(f"bitrate: {_format_bitrate(entry.bitrate_kbps * 1000)}")
            if entry.audio_bitrate_kbps > 0 and entry.audio_bitrate_kbps != entry.bitrate_kbps:
                summary.append(f"audio bitrate: {_format_bitrate(entry.audio_bitrate_kbps * 1000)}")
            self.meta_text.setPlainText("\n".join(summary))
            return
        size = human_size(path.stat().st_size)
        if not shutil.which("ffprobe"):
            summary.append(f"size: {size}")
            if entry.bitrate_kbps > 0:
                summary.append(f"bitrate: {_format_bitrate(entry.bitrate_kbps * 1000)}")
            if entry.audio_bitrate_kbps > 0 and entry.audio_bitrate_kbps != entry.bitrate_kbps:
                summary.append(f"audio bitrate: {_format_bitrate(entry.audio_bitrate_kbps * 1000)}")
            summary.append("ffprobe is missing, so codec details are unavailable.")
            self.meta_text.setPlainText("\n".join(summary))
            return
        info = ffprobe_json(path)
        if not info:
            summary.append(f"size: {size}")
            self.meta_text.setPlainText("\n".join(summary))
            return
        streams = info.get("streams", [])
        summary.append(f"size: {size}")
        duration = _duration_seconds_from_info(info)
        bitrate = entry.bitrate_kbps * 1000 if entry.bitrate_kbps > 0 else _bitrate_bps(info)
        if not bitrate and duration and duration > 0:
            bitrate = round(path.stat().st_size * 8 / duration)
        audio_bitrate = entry.audio_bitrate_kbps * 1000 if entry.audio_bitrate_kbps > 0 else _audio_bitrate_bps(info)
        if bitrate:
            summary.append(f"bitrate: {_format_bitrate(bitrate)}")
        if audio_bitrate and _bitrate_kbps(audio_bitrate) != _bitrate_kbps(bitrate):
            summary.append(f"audio bitrate: {_format_bitrate(audio_bitrate)}")
        _cache_media_fields(entry, self.metadata, duration, _bitrate_kbps(bitrate), _bitrate_kbps(audio_bitrate))
        for stream in streams:
            codec = stream.get("codec_name", "?")
            kind = stream.get("codec_type", "?")
            if kind == "video":
                summary.append(f"video: {codec} {stream.get('width', '?')}x{stream.get('height', '?')}")
            elif kind == "audio":
                summary.append(f"audio: {codec}")
        self.meta_text.setPlainText("\n".join(summary))

    def _set_detail_editing(self, editing: bool) -> None:
        for stack in (self.title_stack, self.author_stack, self.date_stack, self.bpm_stack, self.presence_image_stack):
            stack.setCurrentIndex(1 if editing else 0)
        self.tag_editor.set_editing(editing)
        self.tap_bpm_btn.setVisible(editing)
        self.redetect_bpm_btn.setVisible(editing)
        if hasattr(self, "hide_subsonic_btn"):
            self.hide_subsonic_btn.setVisible(editing)
        if hasattr(self, "lyrics_text"):
            self.lyrics_text.setReadOnly(not editing)
        if hasattr(self, "insert_lyric_time_btn"):
            self.insert_lyric_time_btn.setVisible(editing)
        if hasattr(self, "save_lyrics_btn"):
            self.save_lyrics_btn.setVisible(editing)
        self._update_lyrics_tab_enabled()
        self._update_action_states()

    def _update_lyrics_tab_enabled(self) -> None:
        if not hasattr(self, "details_tabs"):
            return
        has_lyrics = bool(self.current and self.current.lyrics.strip())
        editing = bool(getattr(self, "edit_btn", None) and self.edit_btn.isChecked())
        enabled = editing or has_lyrics
        was_lyrics_tab = self.details_tabs.currentIndex() == self.lyrics_tab_index
        self.details_tabs.setTabEnabled(self.lyrics_tab_index, enabled)
        self.details_tabs.setTabToolTip(
            self.lyrics_tab_index,
            "" if enabled else "Enter Edit mode to add lyrics.",
        )
        if not enabled and was_lyrics_tab:
            self.details_tabs.setCurrentIndex(self.queue_tab_index)

    def _toggle_detail_editing(self, editing: bool) -> None:
        if editing:
            if not self.current:
                self.edit_btn.setChecked(False)
                self.statusBar().showMessage("No track selected")
                return
            self.title_edit.setText(self.current.title)
            self.author_edit.setText(self.current.author or "")
            self.date_edit.setText(format_date(self.current.upload_date))
            self.bpm_edit.setText(str(self.current.bpm) if self.current.bpm > 0 else "")
            self._set_presence_image_editor(self.current)
            self.tap_tempo.reset()
            self.tap_bpm_btn.setText("Tap BPM")
            self.edit_btn.setText("Save")
            self._set_detail_editing(True)
            self.title_edit.setFocus()
            self.title_edit.selectAll()
            return
        if self.title_stack.currentIndex() == 1 and not self._save_detail_edits():
            QtCore.QSignalBlocker(self.edit_btn)
            self.edit_btn.setChecked(True)
            return
        self.edit_btn.setText("Edit")
        self._set_detail_editing(False)

    def _save_detail_edits(self, refresh_results: bool = True) -> bool:
        entry = self.current
        if not entry:
            return True
        title = self.title_edit.text().strip()
        if not title:
            _show_error_popup(self, "Edit Track", "Title cannot be blank.", "Library → Track editor")
            return False
        try:
            upload_date = normalize_upload_date(self.date_edit.text().strip())
        except ValueError as exc:
            _show_error_popup(self, "Edit Track", str(exc), "Library → Track editor → Upload date")
            return False
        author = self.author_edit.text().strip()
        bpm_text = self.bpm_edit.text().strip()
        try:
            bpm = int(bpm_text) if bpm_text else 0
        except ValueError:
            _show_error_popup(self, "Edit Track", "BPM must be a positive whole number.", "Library → Track editor → BPM")
            return False
        if bpm < 0:
            _show_error_popup(self, "Edit Track", "BPM must be a positive whole number.", "Library → Track editor → BPM")
            return False
        presence_image_mode = str(self.presence_image_mode_edit.currentData() or "default")
        presence_image_value = self.presence_image_value_edit.text().strip()
        if presence_image_mode in {"url", "custom_youtube", "discord_key"} and not presence_image_value:
            _show_error_popup(
                self,
                "Edit Track",
                "A value is required for the selected Presence Image mode.",
                "Library → Track editor → Presence image",
            )
            return False
        if presence_image_mode == "custom_youtube" and not is_youtube_raw_id(presence_image_value):
            _show_error_popup(
                self,
                "Edit Track",
                "Custom YouTube Thumbnail requires an 11-character YouTube video ID.",
                "Library → Track editor → Presence image",
            )
            return False
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.title = title
        stored.author = author
        stored.upload_date = upload_date
        stored.bpm = bpm
        stored.bpm_source = "manual" if bpm else ""
        stored.bpm_confidence = 1.0 if bpm else 0.0
        stored.bpm_analysis_version = 0
        stored.presence_image_mode = presence_image_mode
        stored.presence_image_value = presence_image_value if presence_image_mode in {"url", "custom_youtube", "discord_key"} else ""
        self.metadata.upsert(stored)
        entry.title = title
        entry.author = author
        entry.upload_date = upload_date
        entry.bpm = bpm
        entry.bpm_source = stored.bpm_source
        entry.bpm_confidence = stored.bpm_confidence
        entry.bpm_analysis_version = 0
        entry.presence_image_mode = stored.presence_image_mode
        entry.presence_image_value = stored.presence_image_value
        self._update_library_entry_metadata(entry.id, title=title, author=author, upload_date=upload_date, bpm=bpm, bpm_source=stored.bpm_source, bpm_confidence=stored.bpm_confidence, bpm_analysis_version=0)
        self._set_detail_label_text(self.title_label, title)
        self._set_detail_label_text(self.author_label, author)
        self.date_label.setText(format_date(upload_date))
        self._set_detail_label_text(self.bpm_label, str(bpm) if bpm else "")
        if refresh_results:
            self._reload_library(selected_id=entry.id)
        self.statusBar().showMessage(f"Updated {title}")
        self._update_presence()
        return True

    def _tap_bpm(self) -> None:
        """Fill the BPM editor with a tempo measured from user taps."""
        bpm = self.tap_tempo.tap()
        taps = self.tap_tempo.tap_count
        self.tap_bpm_btn.setText(f"Tap BPM ({taps})")
        if bpm is None:
            self.statusBar().showMessage("Tap again on the next beat to measure BPM")
            return
        self.bpm_edit.setText(str(bpm))
        self.statusBar().showMessage(f"Tap tempo: {bpm} BPM from {taps} beats")

    def _save_lyrics(self) -> None:
        entry = self.current
        if not entry:
            self.statusBar().showMessage("No track selected")
            return
        lyrics = self.lyrics_text.toPlainText()
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.lyrics = lyrics
        self.metadata.upsert(stored)
        entry.lyrics = lyrics
        self._reload_library(selected_id=entry.id)
        self.statusBar().showMessage(f"Saved lyrics for {entry.title}")
        position, _duration = self.player.query_position()
        self._update_synced_lyrics(position)

    def _insert_lyric_timestamp(self) -> None:
        position, _duration = self.player.query_position()
        timestamp = _format_lrc_timestamp(position)
        cursor = self.lyrics_text.textCursor()
        cursor.insertText(timestamp)
        self.lyrics_text.setTextCursor(cursor)
        self.lyrics_text.setFocus()
        self._update_synced_lyrics(position)

    def _update_synced_lyrics(self, position: float) -> None:
        active = _active_lrc_line(self.lyrics_text.toPlainText(), position)
        if active is None:
            self.current_lyric_label.setText("")
            self.lyrics_text.setExtraSelections([])
            return
        line_number, text = active
        self.current_lyric_label.setText(text)
        selection = QtWidgets.QTextEdit.ExtraSelection()
        selection.cursor = QtGui.QTextCursor(self.lyrics_text.document().findBlockByLineNumber(line_number))
        selection.cursor.select(QtGui.QTextCursor.SelectionType.LineUnderCursor)
        palette = self.lyrics_text.palette()
        selection.format.setBackground(palette.color(QtGui.QPalette.ColorRole.Highlight))
        selection.format.setForeground(palette.color(QtGui.QPalette.ColorRole.HighlightedText))
        self.lyrics_text.setExtraSelections([selection])

    def _play_selected(self) -> None:
        if not self.current:
            self.statusBar().showMessage("No local video selected")
            return
        self._queue_current_index = None
        self._play_entry(self.current)

    def _play_entry(self, entry: VideoEntry) -> None:
        if not entry.path:
            self.statusBar().showMessage("The selected track has no local media file")
            return
        if not self._require_dependencies(
            "Playback Unavailable",
            ["mpv"],
            "Embedded playback requires mpv.",
            source="Playback → Embedded player",
        ):
            return
        self._flush_playback_progress()
        self._eof_handled_for_entry_id = None
        self.player.play(entry.path)
        if self.player.last_error:
            self.statusBar().showMessage(f"mpv: {self.player.last_error}")
        else:
            self._start_playback_tracking(entry)
            self.statusBar().showMessage(f"Playing {entry.title} - {entry.author or 'Unknown Artist'}")
        self._update_presence()

    def _queue_selected(self) -> None:
        if not self.current:
            self.statusBar().showMessage("No track selected")
            return
        if not self.current.path:
            self.statusBar().showMessage("The selected track has no local media file")
            return
        self._cache_queue_duration(self.current)
        self._play_queue.append(self.current.id)
        self._render_queue()
        self.statusBar().showMessage(f"Queued {self.current.title} ({self._queued_track_count()} waiting)")

    def _set_loop_queue(self, enabled: bool) -> None:
        self._queue_loop_enabled = bool(enabled)
        for control_name in ("loop_queue_btn", "loop_queue_action"):
            control = getattr(self, control_name, None)
            if control is None or control.isChecked() == self._queue_loop_enabled:
                continue
            blocker = QtCore.QSignalBlocker(control)
            control.setChecked(self._queue_loop_enabled)
            del blocker
        self.statusBar().showMessage(
            "Queue looping enabled" if self._queue_loop_enabled else "Queue looping disabled"
        )

    def _play_queued_item(self, item: QtWidgets.QListWidgetItem) -> None:
        entry = self._entry_for_id(item.data(QtCore.Qt.ItemDataRole.UserRole))
        if not entry:
            self.statusBar().showMessage("That queued track is no longer in the library")
            return
        self._queue_current_index = self.queue_list.row(item)
        self._play_entry(entry)

    def _play_track_from_id(self, entry_id: str) -> None:
        # A double-click starts a new Similar-song view. Do not carry the old
        # card's selection into that view while the new track is loading.
        blocker = QtCore.QSignalBlocker(self.similar_list)
        self.similar_list.clearSelection()
        self.similar_list.setCurrentRow(-1)
        del blocker
        entry = self._entry_for_id(entry_id)
        if not entry:
            self.statusBar().showMessage("That track is no longer in the library")
            return
        self._queue_current_index = None
        self._play_entry(entry)
        # Similar cards intentionally preserve their current results on a
        # single click. Once one is played, it becomes the new similarity seed.
        # Rebuild after the double-click event completes: clearing the clicked
        # card during its own event handler can make Qt apply the rest of that
        # event to the first card in the replacement list.
        if self.details_tabs.currentIndex() == self.similar_tab_index:
            QtCore.QTimer.singleShot(0, lambda entry_id=entry.id: self._render_similar_songs(self._entry_for_id(entry_id)))
        elif self.details_tabs.currentIndex() == self.artist_tab_index:
            QtCore.QTimer.singleShot(0, lambda entry_id=entry.id: self._render_artist_songs(self._entry_for_id(entry_id)))

    def _select_entry_from_id(self, entry_id: str) -> None:
        """Keep track cards and the main library selection in sync."""
        entry = self._entry_for_id(entry_id)
        if not entry:
            return
        self._preserve_similar_list = True
        try:
            self._restore_selection(entry_id)
            # A current search can exclude the entry from the table model.
            if not self.current or self.current.id != entry_id:
                self._show_entry(entry)
        finally:
            self._preserve_similar_list = False

    def _select_queue_item(self) -> None:
        item = self.queue_list.currentItem()
        if item:
            self._select_entry_from_id(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _select_similar_item(self) -> None:
        item = self.similar_list.currentItem()
        if item:
            self._select_entry_from_id(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _select_artist_item(self) -> None:
        item = self.artist_list.currentItem()
        if item:
            self._select_entry_from_id(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _select_card_item(self, list_widget: QtWidgets.QListWidget, item: QtWidgets.QListWidgetItem, entry_id: str) -> None:
        list_widget.setCurrentItem(item)
        self._select_entry_from_id(entry_id)

    def _remove_selected_queue_item(self) -> None:
        item = self.queue_list.currentItem()
        if not item:
            return
        self._remove_queue_item_at(self.queue_list.row(item))

    def _remove_queue_item_at(self, row: int) -> None:
        # The active track must remain anchored, but completed entries are
        # otherwise ordinary queue rows and may be removed.
        if not 0 <= row < len(self._play_queue) or row == self._queue_current_index:
            return
        self._play_queue.pop(row)
        if self._queue_current_index is not None and row < self._queue_current_index:
            self._queue_current_index -= 1
        self._render_queue()

    def _queue_order_changed(self, row_tokens: list[int]) -> None:
        """Persist a drag reorder while keeping the active row in place."""
        visible_tokens = [
            row for row, entry_id in enumerate(self._play_queue)
            if self._entry_for_id(entry_id)
        ]
        if row_tokens == visible_tokens:
            return
        if sorted(row_tokens) != visible_tokens:
            self._render_queue()
            return
        reordered_queue = list(self._play_queue)
        for destination, source in zip(visible_tokens, row_tokens):
            reordered_queue[destination] = self._play_queue[source]
        self._play_queue = reordered_queue
        if self._queue_current_index is not None:
            current_position = row_tokens.index(self._queue_current_index)
            self._queue_current_index = visible_tokens[current_position]
        self._render_queue()

    def _queue_similar(self, entry_id: str, *, play_next: bool) -> None:
        entry = self._entry_for_id(entry_id)
        if not entry or not entry.path:
            self.statusBar().showMessage("That track has no local media file")
            return
        self._cache_queue_duration(entry)
        if play_next:
            insert_at = self._queue_current_index + 1 if self._queue_current_index is not None else 0
            self._play_queue.insert(insert_at, entry.id)
            action = "Queued next"
        else:
            self._play_queue.append(entry.id)
            action = "Queued"
        self._render_queue()
        self.statusBar().showMessage(f"{action} {entry.title} ({self._queued_track_count()} waiting)")

    def _hide_similar_entry(self, entry_id: str) -> None:
        entry = self._entry_for_id(entry_id)
        if not entry:
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Hide Track",
            f"Hide '{entry.title}' from Subsonic and remove it from Similar songs?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        selected_id = self._selected_video_id()
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.hidden_from_subsonic = True
        self.metadata.upsert(stored)
        self._reload_library(selected_id=selected_id)
        self.statusBar().showMessage(
            f"Hidden from Subsonic and related lists: {entry.title} - {self._library_song_summary()}"
        )

    def _clear_queue(self) -> None:
        if not self._play_queue:
            return
        if self._queue_current_index is None:
            self._play_queue.clear()
        else:
            self._play_queue = [self._play_queue[self._queue_current_index]]
            self._queue_current_index = 0
        self._render_queue()
        self.statusBar().showMessage(
            "Cleared upcoming tracks; the current track was kept."
            if self._queue_current_index is not None
            else "Playback queue cleared"
        )

    def _entry_for_id(self, entry_id: str) -> Optional[VideoEntry]:
        return next((entry for entry in self.library.entries if entry.id == entry_id), None)

    def _details_tab_changed(self, index: int) -> None:
        """Load result tabs only when they become visible."""
        if index == self.similar_tab_index:
            self._render_similar_songs(self.current)
        elif index == self.artist_tab_index:
            self._render_artist_songs(self.current)

    def _render_similar_songs(self, entry: Optional[VideoEntry]) -> None:
        """Populate the Similar tab using the same ranking as the server API."""
        blocker = QtCore.QSignalBlocker(self.similar_list)
        self.similar_list.clear()
        if not entry:
            self._similar_seed_id = None
            self.similar_status_label.setText("Select a song to find similar tracks.")
        else:
            self._similar_seed_id = entry.id
            catalog = self._similarity_catalog_cache.get(
                self.library.entries,
                self.library.generation,
                self.config.disabled_similarity_tags,
            )
            results = similar_song_entries(
                self.library.entries,
                entry,
                entry.author or "",
                entry.id,
                self.config,
                catalog=catalog,
            )
            if not results:
                self.similar_status_label.setText("No similar songs found.")
            else:
                self.similar_status_label.setText(f"Songs similar to {entry.title}")
                self._populate_track_list(self.similar_list, results)
        self.similar_list.clearSelection()
        self.similar_list.setCurrentRow(-1)
        del blocker

    def _render_artist_songs(self, entry: Optional[VideoEntry]) -> None:
        blocker = QtCore.QSignalBlocker(self.artist_list)
        self.artist_list.clear()
        if not entry:
            self._artist_seed_id = None
            self.artist_status_label.setText("Select a song to find more tracks by the same artist.")
        else:
            self._artist_seed_id = entry.id
            artist = (entry.author or "").strip()
            results = [
                candidate for candidate in self.library.entries
                if candidate.id != entry.id
                and not candidate.hidden_from_subsonic
                and artist
                and (candidate.author or "").strip().casefold() == artist.casefold()
            ]
            results.sort(key=lambda candidate: ((candidate.title or candidate.id).casefold(), candidate.id))
            results = results[: self.config.similarity_max_results]
            if not artist:
                self.artist_status_label.setText("This song has no artist metadata.")
            elif not results:
                self.artist_status_label.setText(f"No other songs found by {artist}.")
            else:
                self.artist_status_label.setText(f"Songs by {artist}")
                self._populate_track_list(self.artist_list, results)
        self.artist_list.clearSelection()
        self.artist_list.setCurrentRow(-1)
        del blocker

    def _populate_track_list(self, list_widget: QtWidgets.QListWidget, results: list[VideoEntry]) -> None:
        for result in results:
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, result.id)
            item.setToolTip(str(result.path) if result.path else "Missing local media")
            item.setSizeHint(QtCore.QSize(0, 92))
            list_widget.addItem(item)
            card = TrackCard(
                result,
                self.config.thumbnails_dir,
                actions=[
                    ("Play Next", "Add to the top of the queue", lambda _checked=False, entry_id=result.id: self._queue_similar(entry_id, play_next=True)),
                    ("+", "Add to the end of the queue", lambda _checked=False, entry_id=result.id: self._queue_similar(entry_id, play_next=False)),
                    ("Hide", "Hide from Subsonic and related song lists", lambda _checked=False, entry_id=result.id: self._hide_similar_entry(entry_id)),
                ],
            )
            card.set_list_item(item)
            card.selected.connect(lambda entry_id, row_item=item, target=list_widget: self._select_card_item(target, row_item, entry_id))
            card.activated.connect(self._play_track_from_id)
            list_widget.setItemWidget(item, card)

    def _cache_queue_duration(self, entry: VideoEntry) -> None:
        """Populate duration once for older or locally imported media."""
        if entry.duration_seconds > 0 or not entry.path or not entry.path.exists():
            return
        info = ffprobe_json(entry.path)
        if info:
            _cache_media_fields(entry, self.metadata, _duration_seconds_from_info(info), None, None)

    def _render_queue(self) -> None:
        self.queue_list.clear()
        for index, entry_id in enumerate(self._play_queue, start=1):
            entry = self._entry_for_id(entry_id)
            if not entry:
                continue
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, entry.id)
            item.setData(QUEUE_ROW_TOKEN_ROLE, index - 1)
            item.setToolTip(str(entry.path) if entry.path else "Missing local media")
            item.setSizeHint(QtCore.QSize(0, 92))
            row = index - 1
            is_current = (
                row == self._queue_current_index
                and entry.id == self._playback_entry_id
            )
            if row != self._queue_current_index:
                item.setFlags(
                    item.flags()
                    | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    | QtCore.Qt.ItemFlag.ItemIsDropEnabled
                )
            else:
                item.setFlags(
                    item.flags()
                    & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled
                    | QtCore.Qt.ItemFlag.ItemIsDropEnabled
                )
            self.queue_list.addItem(item)
            actions = [("X", "Remove from queue", lambda _checked=False, row=row: self._remove_queue_item_at(row))] if row != self._queue_current_index else []
            card = TrackCard(
                entry,
                self.config.thumbnails_dir,
                actions=actions,
                now_playing=is_current,
            )
            card.set_list_item(item)
            card.selected.connect(lambda entry_id, row_item=item: self._select_card_item(self.queue_list, row_item, entry_id))
            card.activated.connect(lambda entry_id, row_item=item: self._play_queued_item(row_item))
            self.queue_list.setItemWidget(item, card)
        pending = self._queued_track_count()
        self.details_tabs.setTabText(
            self.queue_tab_index,
            f"Queue ({pending} up next)" if pending else "Queue",
        )
        self.clear_queue_btn.setText("Clear Upcoming" if self._queue_current_index is not None else "Clear Queue")
        self.clear_queue_btn.setToolTip(
            "Remove tracks after the current track; the current track keeps playing."
            if self._queue_current_index is not None
            else "Remove all tracks from the playback queue."
        )
        self.clear_queue_btn.setEnabled(bool(self._play_queue))
        self._update_queue_time()

    def _is_pending_queue_row(self, row: int) -> bool:
        return 0 <= row < len(self._play_queue) and (
            self._queue_current_index is None or row > self._queue_current_index
        )

    def _queued_track_count(self) -> int:
        """Number of tracks still waiting after the active queue row."""
        if self._queue_current_index is None:
            return len(self._play_queue)
        return max(0, len(self._play_queue) - self._queue_current_index - 1)

    def _update_queue_time(self, position: Optional[float] = None, duration: Optional[float] = None) -> None:
        """Show the remaining listening time, including the active track."""
        if position is None or duration is None:
            position, duration = self.player.query_position()
        queued_duration = 0.0
        unknown_count = 0
        pending_start = self._queue_current_index + 1 if self._queue_current_index is not None else 0
        for entry_id in self._play_queue[pending_start:]:
            entry = self._entry_for_id(entry_id)
            if entry and entry.duration_seconds > 0:
                queued_duration += entry.duration_seconds
            else:
                unknown_count += 1
        current_remaining = max(0.0, duration - position) if self._playback_entry_id and duration > 0 else 0.0
        remaining = current_remaining + queued_duration
        if self._queue_loop_enabled and self._play_queue:
            detail = f"Remaining listening time: {_format_playback_duration(remaining)} (looping)"
        else:
            detail = f"Remaining listening time: {_format_playback_duration(remaining)}"
        if unknown_count:
            detail += f" (+ {unknown_count} track{'s' if unknown_count != 1 else ''} with unknown duration)"
        self.queue_time_label.setText(detail)
        self.queue_time_label.setToolTip(
            "The queue repeats after this pass while Loop Queue is enabled."
            if self._queue_loop_enabled and self._play_queue
            else ""
        )

    def _play_next_queued(self) -> None:
        next_row = self._queue_current_index + 1 if self._queue_current_index is not None else 0
        wrapped = False
        while True:
            while next_row < len(self._play_queue):
                entry_id = self._play_queue[next_row]
                self._queue_current_index = next_row
                entry = self._entry_for_id(entry_id)
                self._render_queue()
                if entry and entry.path and entry.path.exists():
                    self._play_entry(entry)
                    return
                next_row += 1
            if not self._queue_loop_enabled or wrapped or not self._play_queue:
                break
            wrapped = True
            next_row = 0
        self._queue_current_index = None
        self._reset_playback_tracking()
        self._render_queue()
        if not self.table.selectionModel().selectedRows():
            self._show_playing_entry_or_clear_details()
        self.statusBar().showMessage("Playback queue finished")

    def _open_external(self) -> None:
        if not self.current or not self.current.path:
            return
        path = str(self.current.path)
        if os.name == "nt":
            try:
                os.startfile(path)
                return
            except Exception:
                pass

        candidates: list[list[str]]
        if sys.platform == "darwin":
            candidates = [["open", path], ["mpv", path], ["vlc", path]]
        else:
            candidates = [["mpv", path], ["vlc", path], ["xdg-open", path]]
        for cmd in candidates:
            exe = shutil.which(cmd[0])
            if not exe:
                continue
            try:
                subprocess.Popen(
                    [exe, *cmd[1:]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **windows_no_console_kwargs(),
                )
                return
            except Exception:
                continue

    def _show_in_folder(self) -> None:
        if not self.current or not self.current.path:
            self.statusBar().showMessage("No local video selected")
            return
        path = self.current.path.resolve()
        opened = False
        if os.name == "nt":
            try:
                subprocess.Popen(["explorer", "/select,", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = True
            except Exception:
                opened = False
        elif sys.platform == "darwin":
            try:
                subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = True
            except Exception:
                opened = False
        else:
            uri = QtCore.QUrl.fromLocalFile(str(path)).toString()
            for cmd in (
                ["dbus-send", "--session", "--dest=org.freedesktop.FileManager1", "--type=method_call", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems", f"array:string:{uri}", "string:"],
                ["gio", "open", str(path.parent)],
                ["xdg-open", str(path.parent)],
            ):
                exe = shutil.which(cmd[0])
                if not exe:
                    continue
                try:
                    subprocess.Popen([exe, *cmd[1:]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    opened = True
                    break
                except Exception:
                    continue
        if opened:
            self.statusBar().showMessage(f"Opened folder for {path.name}")
        else:
            self.statusBar().showMessage("Could not open file browser")

    def _export_selected(self) -> None:
        if not self.current or not self.current.path:
            return
        if not self._require_dependencies(
            "Export Unavailable",
            ["ffmpeg"],
            "Export requires ffmpeg.",
            source="Library → Export",
        ):
            return
        ext = self.export_ext.currentText()
        default_path = self.config.exports_dir / f"{export_filename_base(self.current.title, self.current.id)}.{ext}"
        filters = [
            f"{ext.upper()} (*.{ext})",
            "Audio (*.mp3 *.m4a *.flac *.opus *.wav)",
            "Video (*.mp4 *.mkv *.webm)",
            "All Files (*)",
        ]
        selected, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export File",
            str(default_path),
            ";;".join(filters),
        )
        if not selected:
            return
        out = Path(selected)
        if not out.suffix:
            out = out.with_suffix(f".{ext}")
        worker = ExportWorker(self.current.path, out)
        self.export_workers.add(worker)
        worker.finished.connect(self._export_finished)
        worker.finished.connect(lambda _ok, _message, w=worker: self.export_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.statusBar().showMessage(f"Exporting {out.name}...")

    def _copy_selected_file(self) -> None:
        if not self.current or not self.current.path:
            self.statusBar().showMessage("No local video selected")
            return
        path = self.current.path.resolve()
        mime = QtCore.QMimeData()
        file_url = QtCore.QUrl.fromLocalFile(str(path))
        mime.setUrls([file_url])
        mime.setText(str(path))
        mime.setData("x-special/gnome-copied-files", f"copy\n{file_url.toString()}".encode())
        QtWidgets.QApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage(f"Copied {path.name} to clipboard")

    def _export_finished(self, ok: bool, message: str) -> None:
        self.statusBar().showMessage(message)
        if not ok:
            _show_error_popup(self, "Export failed", message, "Library → Export")

    def _update_cache(self) -> None:
        if self._cache_dialog is not None:
            self._cache_dialog.showNormal()
            self._cache_dialog.raise_()
            self._cache_dialog.activateWindow()
            return
        if self.cache_worker and self.cache_worker.isRunning():
            self._show_cache_progress()
            return
        if not self._require_dependencies(
            "Cache Update Unavailable",
            ["yt-dlp", "yt-dlp-ejs", "yt-dlp-js-runtime", "ffmpeg"],
            "Online cache updates require yt-dlp, its YouTube challenge solver/runtime, and ffmpeg. aria2c is optional and only improves download performance.",
            source="Library → Cache update",
        ):
            return
        dialog = CacheDialog(self.config, self.metadata, self)
        dialog.worker.finished.connect(
            lambda: self._reload_library(force_file_scan=True)
        )
        self._show_cache_dialog(dialog)

    def _show_cache_progress(self) -> None:
        if not self.cache_worker:
            return
        dialog = CacheDialog(self.config, self.metadata, self, worker=self.cache_worker)
        if self.cache_progress:
            dialog._on_progress(self.cache_progress)
        self._show_cache_dialog(dialog)

    def _show_cache_dialog(self, dialog: CacheDialog) -> None:
        dialog.setModal(False)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._cache_dialog_closed)
        self._cache_dialog = dialog
        dialog.show()

    def _cache_dialog_closed(self, _result: int) -> None:
        self._cache_dialog = None

    def _start_background_cache_update(self) -> None:
        if self.cache_worker and self.cache_worker.isRunning():
            return
        missing = set(_missing_dependencies()["required"])
        if missing.intersection({"yt-dlp", "yt-dlp-ejs", "yt-dlp-js-runtime", "ffmpeg"}):
            self.statusBar().showMessage("Startup cache update skipped: online download dependencies are required")
            return
        self.cache_worker = CacheUpdateWorker(self.config, self.metadata)
        self.cache_worker.progress.connect(self._background_cache_progress)
        self.cache_worker.finished.connect(self._background_cache_finished)
        self.update_cache_action.setText("Update progress")
        self.cache_worker.start()
        self.statusBar().showMessage("Updating cache in the background…")

    def _background_cache_progress(self, data: dict) -> None:
        self.cache_progress = dict(data)
        message = data.get("message")
        if message:
            self.statusBar().showMessage(f"Cache update: {message}")

    def _background_cache_finished(self) -> None:
        failed = self.cache_progress.get("phase") in {"error", "cancelled"}
        message = self.cache_progress.get("message", "Cache update finished")
        self._reload_library(force_file_scan=True)
        self.update_cache_action.setText("Update Cache")
        self.cache_worker = None
        if failed and self.tray:
            self.tray.showMessage(f"{APP_NAME} cache update", message, QtWidgets.QSystemTrayIcon.MessageIcon.Warning)
        self.statusBar().showMessage(message)

    def _start_update_check(self) -> None:
        """Start the optional release lookup after normal startup is ready."""
        if not getattr(self.config, "check_for_updates_on_startup", True):
            return
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        worker = UpdateCheckWorker(__version__)
        self._update_worker = worker
        worker.update_available.connect(self._show_update_available)
        worker.finished.connect(self._update_check_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _update_check_finished(self) -> None:
        self._update_worker = None

    def _open_available_update_page(self) -> None:
        if self._available_update_url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._available_update_url))

    def _show_update_available(self, release: object) -> None:
        if self._quitting or not isinstance(release, ReleaseInfo):
            return
        self._available_update_url = release.url
        version = release.tag_name or f"v{release.version}"
        message = (
            f"A newer version of {APP_NAME} is available: {version}.\n\n"
            "Open the GitHub release page to download it?"
        )
        if self.tray and not self.isVisible():
            self.tray.showMessage(
                f"{APP_NAME} update available",
                f"Version {version} is ready to download. Click this message to open GitHub.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                10000,
            )
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Update available",
            message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self._open_available_update_page()

    def _run_startup_automation(self) -> None:
        # Give the normal startup window one event-loop turn to show and paint
        # before any modal startup popup can be opened.  This keeps a missing
        # dependency notice or cache prompt from making the app appear stuck
        # at launch.  Tray-minimized startup intentionally stays hidden.
        if not self._startup_window_shown:
            self._startup_window_shown = True
            if not (self.config.start_minimized_to_tray and self.tray):
                self.show()
                QtCore.QTimer.singleShot(0, self._run_startup_automation)
                return

        self._startup_automation_pending = False
        self._show_dependency_notice()
        if self.config.server_autostart:
            self.managed_server.start_if_available()
        if self.config.discord_enabled:
            self._toggle_discord(True)
        if self.config.cache_update_on_startup == "automatic":
            self._start_background_cache_update()
        elif self.config.cache_update_on_startup == "ask":
            answer = QtWidgets.QMessageBox.question(
                self,
                "Update Download Cache",
                "Update the download cache now? It will run in the background.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self._start_background_cache_update()
        if self.config.start_minimized_to_tray:
            if self.tray:
                self.hide()
            else:
                self.statusBar().showMessage("Start minimized was ignored because no system tray is available")

        if not (self.config.start_minimized_to_tray and self.tray):
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.setQuitOnLastWindowClosed(self._quit_on_last_window_closed_before_startup)

        # A tray-hidden startup does not produce a showEvent, so queue the
        # same player initialization that normally happens there.
        if not self._mpv_preheated:
            self._mpv_preheated = True
            QtCore.QTimer.singleShot(0, self._preheat_player)

        # Keep the network request out of the critical startup path.  The
        # small delay also lets any dependency or cache prompt finish first.
        if hasattr(self, "_start_update_check") and not getattr(self, "_update_check_scheduled", False):
            self._update_check_scheduled = True
            QtCore.QTimer.singleShot(1000, self._start_update_check)

    def _open_settings(self, category: Optional[str] = None) -> None:
        if self._settings_dialog is not None:
            if category:
                self._settings_dialog.select_category(category)
            self._settings_dialog.showNormal()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(
            self.config,
            self.metadata,
            self,
            self._apply_settings,
            self._reload_library,
            self._select_video_from_settings,
            self._move_data,
            self._switch_data_root,
            library=self.library,
        )
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._settings_dialog_closed)
        self._settings_dialog = dialog
        if category:
            dialog.select_category(category)
        dialog.show()

    def _settings_dialog_closed(self, _result: int) -> None:
        self._settings_dialog = None

    def _select_video_from_settings(self, video_id: str) -> None:
        self._restore_selection(video_id)

    def _prepare_for_library_switch(self) -> bool:
        """Stop library-specific activity before replacing the active root."""
        if getattr(self, "edit_btn", None) and self.edit_btn.isChecked():
            if not self._save_detail_edits(refresh_results=False):
                return False
            blocker = QtCore.QSignalBlocker(self.edit_btn)
            self.edit_btn.setChecked(False)
            del blocker
            self.edit_btn.setText("Edit")
            self._set_detail_editing(False)

        if self._playback_entry_id:
            self.player.command(["stop"])
        self._reset_playback_tracking()
        self._play_queue.clear()
        self._queue_current_index = None
        self._set_loop_queue(False)
        self._render_queue()

        self.current = None
        selection_model = self.table.selectionModel()
        if selection_model:
            selection_model.clearSelection()
        self._clear_detail_fields()
        self._render_similar_songs(None)
        self._render_artist_songs(None)
        return True

    def _switch_data_root(self) -> None:
        """Switch to another existing data root without moving any files."""
        if self.cache_worker and self.cache_worker.isRunning():
            _show_error_popup(
                self,
                "Open Existing Library",
                "Stop the active cache update before switching libraries.",
                "Library → Open Existing Library",
            )
            return
        if self.bpm_worker and self.bpm_worker.isRunning():
            _show_error_popup(
                self,
                "Open Existing Library",
                "Wait for BPM analysis to finish before switching libraries.",
                "Library → Open Existing Library",
            )
            return
        if self._settings_dialog is not None and not self._settings_dialog._apply():
            return

        destination = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            f"Choose existing {APP_NAME} library",
            str(self.config.root_dir.parent),
        )
        if not destination:
            return
        destination_path = Path(destination).expanduser().resolve()
        try:
            new_config = load_existing_config(destination_path)
        except (OSError, ValueError) as exc:
            _show_error_popup(
                self,
                "Open Existing Library",
                str(exc),
                "Library → Open Existing Library → Validate folder",
            )
            return
        if new_config.root_dir == self.config.root_dir:
            self.statusBar().showMessage("That library is already open")
            return

        answer = QtWidgets.QMessageBox.question(
            self,
            "Open Existing Library",
            f"Switch from:\n{self.config.root_dir}\n\nTo:\n{new_config.root_dir}\n\n"
            "Nothing will be moved or copied. Playback and the current queue will be stopped. Continue?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        try:
            self._ensure_config_dirs(new_config)
            new_metadata = MetadataStore(new_config.metadata_path)
            new_metadata.load()
            new_library = LibraryIndex(new_config.download_dir)
            new_library.rebuild(new_metadata.entries())
        except Exception as exc:
            _show_error_popup(
                self,
                "Open Existing Library",
                f"Could not prepare the selected library: {exc}",
                "Library → Open Existing Library → Load folder",
            )
            return
        if not self._prepare_for_library_switch():
            return

        old_config = self.config
        was_external_server = self.managed_server.external
        discord_was_active = bool(self.discord_rpc)
        startup_log_was_closed = False
        try:
            # Keep startup diagnostics beside whichever root is active. This
            # also avoids leaving the old root's log handle open on Windows.
            from .cli import _install_startup_log, close_startup_log

            close_startup_log()
            startup_log_was_closed = True
            _install_startup_log(new_config.root_dir)

            if discord_was_active:
                self._toggle_discord(False)
            if not was_external_server:
                self.managed_server.stop()
            self.config = new_config
            self.metadata = new_metadata
            self.library = new_library
            self._similarity_catalog_cache = SimilarityCatalogCache()
            self.managed_server.config = self.config
            self._update_integration_action_visibility()
            apply_color_scheme(self.config.color_theme)
            self._refresh_playback_icons()

            blocker = QtCore.QSignalBlocker(self.discord_action)
            self.discord_action.setChecked(self.config.discord_enabled)
            del blocker
            if self.config.discord_enabled:
                self._toggle_discord(True)

            self._reload_library(selected_id=None)
            if self.config.server_autostart and not was_external_server:
                self.managed_server.start_if_available()
            remember_error: Optional[Exception] = None
            if self._remember_root_on_move:
                try:
                    remember_root_dir(self.config.root_dir)
                except Exception as exc:
                    remember_error = exc
            if self._settings_dialog is not None:
                self._settings_dialog.accept()
            message = f"Opened library: {self.config.root_dir}"
            if was_external_server:
                message += " (the externally running server was left unchanged)"
            if remember_error is not None:
                message += f" (could not remember this root: {remember_error})"
            self.statusBar().showMessage(message)
        except Exception as exc:
            if startup_log_was_closed:
                try:
                    _install_startup_log(old_config.root_dir)
                except Exception:
                    pass
            _show_error_popup(
                self,
                "Open Existing Library",
                f"Could not switch libraries: {exc}",
                "Library → Open Existing Library → Activate folder",
            )

    def _move_data(self) -> None:
        """Move the complete data root, then reconnect the live application."""
        if self.cache_worker and self.cache_worker.isRunning():
            _show_error_popup(
                self,
                "Move data",
                "Stop the active cache update before moving the library data folder.",
                "Settings → Library → Move data",
            )
            return
        if self.bpm_worker and self.bpm_worker.isRunning():
            _show_error_popup(
                self,
                "Move data",
                "Wait for BPM analysis to finish before moving the library data folder.",
                "Settings → Library → Move data",
            )
            return

        destination = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            f"Choose a new {APP_NAME} library data folder",
            str(self.config.root_dir.parent),
        )
        if not destination:
            return
        destination_path = Path(destination).expanduser().resolve()
        answer = QtWidgets.QMessageBox.question(
            self,
            f"Move {APP_NAME} data",
            f"Move the complete archive from:\n{self.config.root_dir}\n\nTo:\n{destination_path}\n\nThe destination must be empty. Continue?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        old_config = self.config
        was_external_server = self.managed_server.external
        startup_log_was_closed = False
        try:
            # Windows may keep an open log file from being renamed while the
            # root is moved. The CLI logger is optional when app.main() is
            # called directly, so keep this local and best-effort.
            from .cli import _install_startup_log, close_startup_log

            close_startup_log()
            startup_log_was_closed = True
            new_config = move_data_root(
                old_config.root_dir,
                destination_path,
                remember=self._remember_root_on_move,
            )
            _install_startup_log(new_config.root_dir)
        except Exception as exc:
            if startup_log_was_closed:
                try:
                    _install_startup_log(old_config.root_dir)
                except Exception:
                    pass
            _show_error_popup(self, "Move data", f"Could not move the library data folder: {exc}", "Settings → Library → Move data")
            return

        self.managed_server.stop()
        self.config = new_config
        self._update_integration_action_visibility()
        self._ensure_app_dirs()
        self.metadata = MetadataStore(self.config.metadata_path)
        self.metadata.load()
        self.library = LibraryIndex(self.config.download_dir)
        self.managed_server.config = self.config
        self._reload_library(selected_id=self.current.id if self.current else None)
        if self.config.server_autostart and not was_external_server:
            self.managed_server.start_if_available()
        if self._settings_dialog is not None:
            self._settings_dialog.accept()
        self.statusBar().showMessage(f"Data moved to {self.config.root_dir}")

    def _apply_settings(self) -> None:
        old_config = self.config
        new_config = load_config(old_config.root_dir)
        library_changed = (
            old_config.download_dir != new_config.download_dir
            or old_config.metadata_path != new_config.metadata_path
        )
        disabled_tags_changed = old_config.disabled_similarity_tags != new_config.disabled_similarity_tags
        server_changed = any(
            getattr(old_config, field) != getattr(new_config, field)
            for field in (
                "server_host",
                "server_port",
                "server_username",
                "server_password",
                "server_password_hash",
                "server_timing",
                "server_autostart",
                "download_dir",
                "metadata_path",
                "thumbnails_dir",
                "artist_profiles_path",
                "artist_thumbnails_dir",
                "disabled_similarity_tags",
                "similarity_use_artist",
                "similarity_use_tags",
                "similarity_use_rarity",
                "similarity_use_bpm",
                "similarity_use_half_double_time",
                "similarity_artist_weight",
                "similarity_tag_weight",
                "similarity_bpm_max_distance",
                "similarity_bpm_weight",
                "similarity_min_score",
                "similarity_max_results",
            )
        )
        self.config = new_config
        self._update_integration_action_visibility()
        self.tag_editor.set_disabled_tags(self.config.disabled_similarity_tags)
        self._set_presence_image_row_visible(self.config.discord_enabled)
        if old_config.show_playback_bar != new_config.show_playback_bar:
            self._set_playback_bar_visible(new_config.show_playback_bar)
        if old_config.color_theme != new_config.color_theme:
            apply_color_scheme(new_config.color_theme)
            self._refresh_playback_icons()
        self._ensure_app_dirs()
        if library_changed:
            self.metadata = MetadataStore(self.config.metadata_path)
            self.metadata.load()
            self.library = LibraryIndex(self.config.download_dir)
            self._reload_library()
        elif disabled_tags_changed:
            self._reload_library()
        if server_changed:
            was_external = self.managed_server.external
            self.managed_server.stop()
            self.managed_server.config = self.config
            if self.config.server_autostart and not was_external:
                self.managed_server.start_if_available()
            elif was_external:
                self.statusBar().showMessage("Server settings saved; the externally running server was not changed")
        current_discord = bool(self.discord_rpc)
        discord_identity_changed = old_config.discord_application_id != new_config.discord_application_id
        blocker = QtCore.QSignalBlocker(self.discord_action)
        self.discord_action.setChecked(self.config.discord_enabled)
        del blocker
        if discord_identity_changed and current_discord:
            self._toggle_discord(False)
            current_discord = False
        if self.config.discord_enabled != current_discord:
            self._toggle_discord(self.config.discord_enabled)
        elif self.discord_rpc:
            self._update_presence()
        self.statusBar().showMessage("Settings applied")

    def _analyze_missing_bpm(self) -> None:
        if not aubio_available():
            _show_error_popup(
                self,
                "BPM Analysis Unavailable",
                f"Install the aubio Python package, then restart {APP_NAME} to analyze BPM automatically. You can still enter BPM values manually.",
                "Library → BPM analysis",
            )
            return
        if self.bpm_worker and self.bpm_worker.isRunning():
            self.statusBar().showMessage("BPM analysis is already running")
            return
        pending = [entry for entry in self.library.entries if entry.path and entry.bpm <= 0]
        if not pending:
            self.statusBar().showMessage("Every local track already has a BPM")
            return
        self.bpm_worker = BpmAnalysisWorker(pending, self.metadata)
        self.bpm_worker.progress.connect(self._bpm_analysis_progress)
        self.bpm_worker.completed.connect(self._bpm_analysis_completed)
        self.bpm_worker.start()
        self.statusBar().showMessage(f"Analyzing BPM for {len(pending)} tracks...")

    def _redetect_selected_bpm(self) -> None:
        entry = self.current
        if not entry or not entry.path:
            self.statusBar().showMessage("Select a local track to analyze its BPM")
            return
        if not aubio_available():
            _show_error_popup(
                self,
                "BPM Analysis Unavailable",
                f"Install the aubio Python package, then restart {APP_NAME} to analyze BPM automatically.",
                "Library → BPM analysis",
            )
            return
        if self.bpm_worker and self.bpm_worker.isRunning():
            self.statusBar().showMessage("BPM analysis is already running")
            return
        self.bpm_worker = BpmAnalysisWorker([entry], self.metadata, force=True)
        self.bpm_worker.progress.connect(self._bpm_analysis_progress)
        self.bpm_worker.completed.connect(self._bpm_analysis_completed)
        self.bpm_worker.start()
        self.statusBar().showMessage(f"Re-detecting BPM for {entry.title or entry.id}...")

    def _bpm_analysis_progress(self, done: int, total: int, title: str, saved: int) -> None:
        self.statusBar().showMessage(f"Analyzing BPM {done}/{total}: {title} ({saved} saved)")

    def _bpm_analysis_completed(self, saved: int, total: int) -> None:
        self._reload_library(selected_id=self.current.id if self.current else None)
        if self.bpm_worker and self.bpm_worker.force and total == 1:
            message = "BPM re-detected" if saved else "No confident BPM estimate; existing value was kept"
            self.statusBar().showMessage(message)
            return
        self.statusBar().showMessage(f"BPM analysis finished: {saved} confident estimates from {total} tracks")

    def _open_add_new(self) -> None:
        dialog = AddNewDialog(self.config, self.metadata, self)
        result = dialog.exec()
        if result or dialog.library_may_have_changed:
            selected_id = dialog.selected_id or None
            self._reload_library(selected_id=selected_id, force_file_scan=True)
            if result:
                entry = self.metadata.get(selected_id) if selected_id else None
                if entry:
                    self.statusBar().showMessage(f"Selected {entry.title or entry.id}")

    def _delete_selected(self) -> None:
        entry = self.current
        if not entry:
            self.statusBar().showMessage("No track selected")
            return
        remove_file = entry.path and entry.path.exists()
        message = f"Delete '{entry.title}' from the library?"
        if remove_file:
            message += "\n\nThe local media file will also be removed."
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Track",
            message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if self._playback_entry_id == entry.id:
            self.player.command(["stop"])
            self._reset_playback_tracking()
        if remove_file:
            try:
                entry.path.unlink()
            except OSError as exc:
                _show_error_popup(self, "Delete Track", f"Could not delete file: {exc}", "Library → Delete track")
                return
        removed = self.metadata.delete(entry.id, save=True)
        if not removed:
            _show_error_popup(self, "Delete Track", "Entry was not found in metadata.", "Library → Delete track")
            return
        if self._queue_current_index is not None:
            current_id = self._play_queue[self._queue_current_index]
            if current_id == entry.id:
                self._queue_current_index = None
            else:
                self._queue_current_index = sum(
                    entry_id != entry.id
                    for entry_id in self._play_queue[:self._queue_current_index]
                )
        self._play_queue = [entry_id for entry_id in self._play_queue if entry_id != entry.id]
        self._render_queue()
        self.current = None
        if self.edit_btn.isChecked():
            self.edit_btn.setChecked(False)
        self._reload_library(selected_id=None, force_file_scan=True)
        self._clear_detail_fields()
        self.statusBar().showMessage(f"Deleted {entry.title} - {self._library_song_summary()}")

    def _toggle_hidden_from_subsonic(self, hidden: bool) -> None:
        entry = self.current
        if not entry:
            blocker = QtCore.QSignalBlocker(self.hide_subsonic_btn)
            self.hide_subsonic_btn.setChecked(False)
            self.hide_subsonic_btn.setText("Hide")
            del blocker
            self.statusBar().showMessage("No track selected")
            return
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.hidden_from_subsonic = hidden
        self.metadata.upsert(stored)
        self._reload_library(selected_id=entry.id)
        self.statusBar().showMessage(
            f"{'Hidden from' if hidden else 'Shown in'} Subsonic and related lists: "
            f"{entry.title} - {self._library_song_summary()}"
        )

    def _toggle_discord(self, enabled: bool) -> None:
        if enabled:
            application_id = self.config.discord_application_id
            if not application_id:
                self.discord_action.setChecked(False)
                _show_error_popup(
                    self,
                    "Discord Presence",
                    "Set a Discord Application ID in Settings → Integrations before enabling Discord Presence.",
                    "Integrations → Discord Presence → Application ID",
                )
                return
            if not application_id.isdigit():
                self.discord_action.setChecked(False)
                _show_error_popup(
                    self,
                    "Discord Presence",
                    "Discord Application ID must contain only digits. Copy the Application ID from the app's General Information page in the Discord Developer Portal.",
                    "Integrations → Discord Presence → Application ID",
                )
                return
            if not self._require_dependencies(
                "Discord Presence Unavailable",
                ["pypresence"],
                "Discord Rich Presence requires the pypresence Python package.",
                source="Integrations → Discord Presence → Dependencies",
            ):
                self.discord_action.setChecked(False)
                return
            try:
                from pypresence import Presence

                self.discord_rpc = Presence(application_id)
                self.discord_rpc.connect()
                self.statusBar().showMessage("Discord presence enabled")
                self._update_presence()
            except Exception as exc:
                self.discord_action.setChecked(False)
                self.discord_rpc = None
                _show_error_popup(
                    self,
                    "Discord Presence",
                    f"Unavailable: {exc}",
                    "Integrations → Discord Presence → Connection",
                )
        else:
            self._presence_update_timer.stop()
            self._presence_update_pending = False
            if self.discord_rpc:
                try:
                    self.discord_rpc.clear()
                    self.discord_rpc.close()
                except Exception:
                    pass
            self.discord_rpc = None

    def _update_presence(self) -> None:
        if not self.discord_rpc:
            return
        # Discord can close or rate-limit its IPC pipe when several tracks are
        # started in quick succession. Coalesce those requests so only the
        # final track in a burst is sent.
        self._presence_update_pending = True
        self._presence_update_timer.start(250)

    def _flush_presence_update(self) -> None:
        if not self._presence_update_pending:
            return
        if not self.discord_rpc:
            if not self.discord_action.isChecked() or not self._reconnect_discord_presence():
                self._presence_update_timer.start(self._presence_retry_delay_ms)
                self._presence_retry_delay_ms = min(self._presence_retry_delay_ms * 2, 30000)
                return
        self._presence_update_pending = False
        entry = self._entry_for_id(self._playback_entry_id) if self._playback_entry_id else self.current
        if not entry:
            self.statusBar().showMessage("Discord presence enabled; select or play a track to publish it")
            return
        try:
            activity = dict(
                details=entry.title[:128],
                state=(f"by {entry.author}" if entry.author else APP_NAME)[:128],
            )
            image = self._presence_image_for_entry(entry)
            if image:
                activity["large_image"] = image
            default_image = self._global_presence_image()
            small_image = self._presence_small_image_for_entry(entry, image, default_image)
            if small_image:
                activity["small_image"] = small_image
            self.discord_rpc.update(**activity)
            self._presence_retry_delay_ms = 1000
        except Exception as exc:
            self.statusBar().showMessage(f"Discord presence update failed: {exc}")
            self._presence_update_pending = True
            if self._presence_connection_error(exc) and self._reconnect_discord_presence():
                try:
                    self.discord_rpc.update(**activity)
                    self._presence_update_pending = False
                    self._presence_retry_delay_ms = 1000
                    return
                except Exception as retry_exc:
                    self.statusBar().showMessage(f"Discord presence reconnect failed: {retry_exc}")
            self._presence_update_timer.start(self._presence_retry_delay_ms)
            self._presence_retry_delay_ms = min(self._presence_retry_delay_ms * 2, 30000)

    @staticmethod
    def _presence_connection_error(exc: Exception) -> bool:
        """Whether pypresence should be reconnected for this failure."""
        return type(exc).__name__ in {
            "ConnectionTimeout",
            "PipeClosed",
            "ResponseTimeout",
        } or isinstance(exc, OSError)

    def _reconnect_discord_presence(self) -> bool:
        """Reconnect Discord quietly after its IPC pipe is closed."""
        old_rpc = self.discord_rpc
        if old_rpc:
            try:
                old_rpc.close()
            except Exception:
                pass
        try:
            from pypresence import Presence

            rpc = Presence(self.config.discord_application_id)
            rpc.connect()
        except Exception as exc:
            self.discord_rpc = None
            self.statusBar().showMessage(f"Discord presence reconnect failed: {exc}")
            return False
        self.discord_rpc = rpc
        self.statusBar().showMessage("Discord presence reconnected")
        return True

    def _presence_image_for_entry(self, entry: VideoEntry) -> Optional[str]:
        """Resolve the per-song image mode, falling back to Presence Settings."""
        mode = entry.presence_image_mode if entry.presence_image_mode in PRESENCE_IMAGE_MODES else "default"
        value = entry.presence_image_value.strip()
        if mode == "default":
            if self.config.discord_presence_default_youtube_thumbnail:
                youtube_id = thumbnail_video_id(entry.id)
                image = _youtube_presence_image(youtube_id, self._presence_thumbnail_cache) if youtube_id else None
                if image:
                    return image
            return self._global_presence_image()
        if mode == "default_large":
            return self._global_presence_image()
        if mode == "url":
            return value or None
        if mode == "discord_key":
            return value or None
        if mode == "custom_youtube":
            youtube_id = value if is_youtube_raw_id(value) else None
            image = _youtube_presence_image(youtube_id, self._presence_thumbnail_cache) if youtube_id else None
            return image or self._global_presence_image()
        if mode == "youtube":
            youtube_id = thumbnail_video_id(entry.id)
            image = _youtube_presence_image(youtube_id, self._presence_thumbnail_cache) if youtube_id else None
            return image or self._global_presence_image()
        if mode == "empty":
            return None
        return self._global_presence_image()

    def _entry_has_custom_presence_image(
        self,
        entry: VideoEntry,
        image: Optional[str],
        default_image: Optional[str],
    ) -> bool:
        """True when an explicit per-song image has replaced the default one."""
        return bool(
            default_image
            and image
            and image != default_image
            and entry.presence_image_mode in {"url", "discord_key", "youtube", "custom_youtube"}
        )

    def _presence_small_image_for_entry(
        self,
        entry: VideoEntry,
        image: Optional[str],
        default_image: Optional[str],
    ) -> Optional[str]:
        """Resolve the independently configurable small presence image."""
        mode = self.config.discord_presence_small_image_mode
        value = self.config.discord_presence_small_image_value.strip()
        # The override option is intentionally authoritative. This preserves
        # its original behavior even when a separate small image is configured.
        if (
            self.config.discord_presence_show_default_as_small_on_override
            and self._entry_has_custom_presence_image(entry, image, default_image)
        ):
            return default_image
        if mode in {"url", "discord_key"}:
            return value or None
        if mode == "empty":
            return None
        return None

    def _global_presence_image(self) -> Optional[str]:
        mode = self.config.discord_presence_image_mode
        value = self.config.discord_presence_image_value.strip()
        if mode in {"url", "discord_key"}:
            return value or None
        return None

    def _tick_player(self) -> None:
        position, duration = self.player.query_position()
        paused = bool(self._playback_entry_id and self.player.is_paused())
        self._update_player_controls(position, duration, paused)
        self._update_queue_time(position, duration)
        self._update_synced_lyrics(position)
        if not self._playback_entry_id:
            return
        if self.player.has_reached_eof() and self._eof_handled_for_entry_id != self._playback_entry_id:
            self._eof_handled_for_entry_id = self._playback_entry_id
            self._flush_playback_progress(force=True)
            self._play_next_queued()
            return
        if paused:
            self._tracked_position = max(0.0, position)
            return
        delta = position - self._tracked_position
        if 0 < delta <= 2.0:
            self._tracked_seconds_pending += delta
        self._tracked_position = max(0.0, position)
        if self._tracked_seconds_pending >= 5.0:
            self._flush_playback_progress()

    def _set_audio_only(self, enabled: bool) -> None:
        self.player.set_audio_only(enabled)
        self.statusBar().showMessage("Audio-only mode enabled" if enabled else "Audio-only mode disabled")

    def _start_playback_tracking(self, entry: VideoEntry) -> None:
        selected_id = self._selected_video_id()
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        entry.last_played_at = now
        self.metadata.load()
        stored = self.metadata.get(entry.id) or entry
        stored.last_played_at = now
        self.metadata.upsert(stored)
        self._update_library_entry_state(entry.id, last_played_at=now)
        self._playback_entry_id = entry.id
        self._tracked_position = 0.0
        self._tracked_seconds_pending = 0.0
        self._update_player_controls(0.0, 0.0, False)
        self._render_queue()
        if not self.table.selectionModel().selectedRows():
            self._show_entry(entry)
        if self._current_sort_mode() in {
            "Last Played",
            "Most Played (by time)",
            "Most Played (by plays)",
        }:
            self._render_results(selected_id=selected_id)

    def _flush_playback_progress(self, *, force: bool = False) -> None:
        if not self._playback_entry_id:
            return
        self.metadata.load_if_changed()
        entry = self.metadata.get(self._playback_entry_id)
        progress_changed = False
        if entry and self._tracked_seconds_pending > 0:
            entry.playback_seconds += self._tracked_seconds_pending
            self.metadata.upsert(entry, save=False)
            self._update_library_entry_state(
                entry.id,
                last_played_at=entry.last_played_at,
                playback_seconds=entry.playback_seconds,
            )
            self._tracked_seconds_pending = 0.0
            progress_changed = True
        if force or time.monotonic() - self._last_metadata_flush_monotonic >= self._metadata_flush_interval:
            self.metadata.save()
            self._last_metadata_flush_monotonic = time.monotonic()
        if progress_changed and self._current_sort_mode() == "Most Played (by time)":
            self._render_results(selected_id=self._selected_video_id())

    def _reset_playback_tracking(self) -> None:
        self._flush_playback_progress(force=True)
        self._playback_entry_id = None
        self._tracked_position = 0.0
        self._tracked_seconds_pending = 0.0
        self._update_player_controls(0.0, 0.0, False)

    def _update_library_entry_state(
        self,
        video_id: str,
        *,
        last_played_at: Optional[str] = None,
        playback_seconds: Optional[float] = None,
        hidden_from_subsonic: Optional[bool] = None,
    ) -> None:
        for entry in self.library.entries:
            if entry.id != video_id:
                continue
            if last_played_at is not None:
                entry.last_played_at = last_played_at
            if playback_seconds is not None:
                entry.playback_seconds = playback_seconds
            if hidden_from_subsonic is not None:
                entry.hidden_from_subsonic = hidden_from_subsonic
        if hidden_from_subsonic is not None:
            self.library.invalidate_search()
        if self.current and self.current.id == video_id:
            if last_played_at is not None:
                self.current.last_played_at = last_played_at
            if playback_seconds is not None:
                self.current.playback_seconds = playback_seconds
            if hidden_from_subsonic is not None:
                self.current.hidden_from_subsonic = hidden_from_subsonic

    def _update_library_entry_metadata(
        self,
        video_id: str,
        *,
        title: str,
        author: str,
        upload_date: str,
        lyrics: Optional[str] = None,
        bpm: Optional[int] = None,
        bpm_source: Optional[str] = None,
        bpm_confidence: Optional[float] = None,
        bpm_analysis_version: Optional[int] = None,
    ) -> None:
        for entry in self.library.entries:
            if entry.id != video_id:
                continue
            entry.title = title
            entry.author = author
            entry.upload_date = upload_date
            if lyrics is not None:
                entry.lyrics = lyrics
            if bpm is not None:
                entry.bpm = bpm
            if bpm_source is not None:
                entry.bpm_source = bpm_source
            if bpm_confidence is not None:
                entry.bpm_confidence = bpm_confidence
            if bpm_analysis_version is not None:
                entry.bpm_analysis_version = bpm_analysis_version
        if self.current and self.current.id == video_id:
            self.current.title = title
            self.current.author = author
            self.current.upload_date = upload_date
            if lyrics is not None:
                self.current.lyrics = lyrics
            if bpm is not None:
                self.current.bpm = bpm
            if bpm_source is not None:
                self.current.bpm_source = bpm_source
            if bpm_confidence is not None:
                self.current.bpm_confidence = bpm_confidence
            if bpm_analysis_version is not None:
                self.current.bpm_analysis_version = bpm_analysis_version
        self.library.invalidate_search()

    def _selected_video_id(self) -> Optional[str]:
        selected = self.table.selectionModel().selectedRows() if hasattr(self, "table") else []
        if selected:
            entry = self.table_model.entry_at(selected[0].row())
            if entry:
                return entry.id
        return self.current.id if self.current else None

    def _restore_selection(self, selected_id: Optional[str]) -> None:
        if not selected_id:
            return
        for row, entry in enumerate(self.table_model.rows):
            if entry.id != selected_id:
                continue
            index = self.table_model.index(row, 0)
            selection_model = self.table.selectionModel()
            if selection_model:
                selection_model.select(
                    index,
                    QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QtCore.QItemSelectionModel.SelectionFlag.Rows,
                )
                self.table.setCurrentIndex(index)
                self.table.scrollTo(index, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
            self._show_entry(entry)
            return

    def _dependency_status(self) -> str:
        missing = _missing_dependencies()
        if missing["required"]:
            return "Required tools missing: " + ", ".join(missing["required"])
        return "Playback and download tools ready"

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.config.close_to_tray and self.tray and self.tray.isVisible() and not self._quitting:
            event.ignore()
            self.hide()
            return
        if hasattr(self, "timer"):
            self.timer.stop()
        if self.bpm_worker and self.bpm_worker.isRunning():
            self.bpm_worker.cancel()
            self.bpm_worker.wait(3000)
        if self.cache_worker and self.cache_worker.isRunning():
            self.cache_worker.cancel()
            if not self.cache_worker.wait(30000):
                self.statusBar().showMessage("Waiting for the background cache update to stop…")
                event.ignore()
                return
        if self._update_worker and self._update_worker.isRunning():
            # urllib uses a short timeout, so normal closing remains quick
            # while still avoiding a QThread being destroyed mid-request.
            self._update_worker.requestInterruption()
            self._update_worker.wait(6000)
        self._reset_playback_tracking()
        for worker in list(self.export_workers):
            worker.wait(30000)
        self.player.close()
        if self.discord_rpc:
            try:
                self.discord_rpc.clear()
                self.discord_rpc.close()
            except Exception:
                pass
        self.managed_server.stop()
        event.accept()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if not self._startup_automation_pending and not self._dependency_notice_shown:
            QtCore.QTimer.singleShot(0, self._show_dependency_notice)
        if not self._mpv_preheated:
            self._mpv_preheated = True
            QtCore.QTimer.singleShot(0, self._preheat_player)

    def _preheat_player(self) -> None:
        if self.player.start():
            self.statusBar().showMessage(f"Player ready. {self._dependency_status()}")
        elif self.player.last_error:
            self.statusBar().showMessage(f"mpv: {self.player.last_error}")


def _downloaded_time(entry: VideoEntry) -> float:
    if not entry.path:
        return 0.0
    try:
        return entry.path.stat().st_mtime
    except OSError:
        return 0.0


def _upload_date_value(entry: VideoEntry) -> int:
    try:
        return int(entry.upload_date)
    except (TypeError, ValueError):
        return 0


def _last_played_value(entry: VideoEntry) -> str:
    return entry.last_played_at or ""


def _playback_seconds_value(entry: VideoEntry) -> float:
    return entry.playback_seconds


def _play_count_value(entry: VideoEntry) -> int:
    return entry.play_count


def _bitrate_bps(info: dict) -> Optional[int]:
    try:
        value = int(float(info.get("format", {}).get("bit_rate")))
    except (TypeError, ValueError):
        return None
    if value > 0:
        return value
    return None


def _audio_bitrate_bps(info: dict) -> Optional[int]:
    for stream in info.get("streams", []):
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        try:
            value = int(float(stream.get("bit_rate")))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _bitrate_kbps(bits_per_second: Optional[int]) -> Optional[int]:
    if not bits_per_second or bits_per_second <= 0:
        return None
    return max(1, round(bits_per_second / 1000))


def _format_bitrate(bits_per_second: int) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.2f} Mbps"
    return f"{max(1, round(bits_per_second / 1000))} kbps"


def _duration_seconds_from_info(info: dict) -> Optional[float]:
    raw_duration = info.get("format", {}).get("duration")
    try:
        return float(raw_duration)
    except (TypeError, ValueError):
        pass
    for stream in info.get("streams", []):
        if not isinstance(stream, dict):
            continue
        try:
            return float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
    return None


def _cache_media_fields(
    entry: VideoEntry,
    metadata: MetadataStore,
    duration: Optional[float],
    bitrate_kbps: Optional[int],
    audio_bitrate_kbps: Optional[int],
) -> None:
    changed = False
    if duration and duration > 0 and entry.duration_seconds <= 0:
        entry.duration_seconds = duration
        changed = True
    if bitrate_kbps and bitrate_kbps > 0 and entry.bitrate_kbps <= 0:
        entry.bitrate_kbps = bitrate_kbps
        changed = True
    if audio_bitrate_kbps and audio_bitrate_kbps > 0 and entry.audio_bitrate_kbps <= 0:
        entry.audio_bitrate_kbps = audio_bitrate_kbps
        changed = True
    if changed:
        metadata.upsert(entry)


def _format_playback_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def _format_lrc_timestamp(seconds: float) -> str:
    total_centiseconds = max(0, int(round(seconds * 100)))
    minutes, remainder = divmod(total_centiseconds, 60 * 100)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"[{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}]"


def _active_lrc_line(lyrics: str, position: float) -> Optional[tuple[int, str]]:
    active: Optional[tuple[float, int, str]] = None
    for line_number, line in enumerate(lyrics.splitlines()):
        matches = list(re.finditer(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]", line))
        if not matches:
            continue
        text = re.sub(r"^(?:\[\d{1,3}:\d{2}(?:\.\d{1,3})?\])+", "", line).strip()
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            timestamp = minutes * 60 + seconds + int(fraction.ljust(3, "0")[:3]) / 1000.0
            if timestamp <= position and (active is None or timestamp >= active[0]):
                active = (timestamp, line_number, text)
    if active is None:
        return None
    return active[1], active[2]


def _local_import_defaults(path: Path) -> dict[str, str]:
    tags = detect_media_tags(path)
    upload_date = tags.get("upload_date") or dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")
    return {
        "title": tags.get("title") or path.stem,
        "author": tags.get("author") or "",
        "upload_date": upload_date,
    }


def _extract_zip_media_files(archive: Path, destination: Path) -> list[Path]:
    """Extract supported media from an archive without trusting member paths."""
    extracted: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as contents:
        members = [
            member
            for member in contents.infolist()
            if not member.is_dir() and Path(member.filename).suffix.lower() in MEDIA_EXTS
        ]
        if not members:
            return extracted
        if len(members) > 10_000:
            raise ValueError("ZIP archive contains too many media files")
        if sum(member.file_size for member in members) > 20 * 1024 * 1024 * 1024:
            raise ValueError("ZIP archive contains more than 20 GB of media")
        for index, member in enumerate(members):
            # ZIP symlinks can point outside the temporary extraction directory.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("ZIP archive contains a symbolic link")
            target_dir = destination / f"{index:05d}"
            target_dir.mkdir()
            target = target_dir / Path(member.filename).name
            with contents.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(target)
    return extracted


def _is_direct_media_input(value: str) -> bool:
    raw = (value or "").strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return Path(unquote(parsed.path)).suffix.lower() in MEDIA_EXTS


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} desktop media library GUI.")
    parser.add_argument("--root", type=Path, default=None, help="Application data/config root directory.")
    args = parser.parse_args(argv)
    _set_windows_app_identity()
    app = QtWidgets.QApplication([])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    _ensure_consistent_qt_style(app)
    try:
        instance_lock = _acquire_single_instance_lock()
    except OSError as exc:
        QtWidgets.QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    if instance_lock is None:
        QtWidgets.QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.")
        return 1

    try:
        icon = _application_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)
        initial_root = resolve_root(args.root)
        dependencies_checked = False
        if (initial_root / "config.ini").is_file():
            config = load_config(initial_root)
        else:
            config = _run_first_run_setup(
                initial_root,
                remember_root=args.root is None,
                root_locked=args.root is not None,
            )
            if config is None:
                return 0
            dependencies_checked = True
        window = MainWindow(
            config=config,
            dependencies_checked=dependencies_checked,
            remember_root_on_move=args.root is None,
        )
        return app.exec()
    finally:
        instance_lock.unlock()
