from __future__ import annotations

import configparser
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import APP_NAME

APP_DATA_DIR_NAME = "ytarchive"
ROOT_POINTER_FILENAME = "data-root.txt"

# These settings are retained for compatibility with existing config.ini files,
# but the GUI presents their shared root as one library data folder.
STORAGE_SETTING_KEYS = frozenset(
    {
        "DOWNLOAD_DIRECTORY",
        "EXPORTS_DIRECTORY",
        "THUMBNAILS_DIRECTORY",
        "ARTIST_PROFILES_PATH",
        "ARTIST_THUMBNAILS_DIRECTORY",
        "METADATA_PATH",
        "PLAYLISTS_PATH",
    }
)
STORAGE_SETTING_FIELDS = {
    "DOWNLOAD_DIRECTORY": "download_dir",
    "EXPORTS_DIRECTORY": "exports_dir",
    "THUMBNAILS_DIRECTORY": "thumbnails_dir",
    "ARTIST_PROFILES_PATH": "artist_profiles_path",
    "ARTIST_THUMBNAILS_DIRECTORY": "artist_thumbnails_dir",
    "METADATA_PATH": "metadata_path",
    "PLAYLISTS_PATH": "playlists_path",
}


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: str
    category: str
    label: str
    sensitive: bool = False
    internal: bool = False


# This is the single list used by both the configuration loader and the GUI.
# Keep values as INI strings here; load_config is responsible for converting
# them into the strongly-typed AppConfig fields used by the application.
SETTING_SPECS = (
    SettingSpec("DOWNLOAD_DIRECTORY", "videos", "Library", "Download directory"),
    SettingSpec("EXPORTS_DIRECTORY", "exports", "Library", "Exports directory"),
    SettingSpec("THUMBNAILS_DIRECTORY", "thumbnails", "Library", "Thumbnails directory"),
    SettingSpec("ARTIST_PROFILES_PATH", "artist_profiles.json", "Library", "Artist profiles file"),
    SettingSpec("ARTIST_THUMBNAILS_DIRECTORY", "artist_thumbnails", "Library", "Artist thumbnails directory"),
    SettingSpec("METADATA_PATH", "video_metadata.json", "Library", "Metadata file"),
    SettingSpec("PLAYLISTS_PATH", "playlists.txt", "Library", "Playlists file"),
    SettingSpec("NUM_WORKERS", "4", "Downloads", "Download workers"),
    SettingSpec("USE_ARIA2C", "true", "Downloads", "Use aria2c"),
    SettingSpec("USE_BROWSER_COOKIES", "", "Downloads", "Browser cookies"),
    SettingSpec("BROWSER_COOKIES_MODE", "required", "Downloads", "Use browser cookies"),
    SettingSpec("SPOTIFY_LOSSLESS_COMMAND", "", "Downloads", "Spotify lossless command"),
    SettingSpec("SPOTDL_COMMAND", "", "Downloads", "spotDL command"),
    SettingSpec("DISCORD_RPC", "false", "Integrations", "Discord Rich Presence"),
    SettingSpec("DISCORD_APPLICATION_ID", "", "Integrations", "Application ID"),
    SettingSpec("DISCORD_PRESENCE_IMAGE_MODE", "empty", "Integrations", "Default Large Image"),
    SettingSpec("DISCORD_PRESENCE_IMAGE_VALUE", "", "Integrations", "Default Large Image Value"),
    SettingSpec("DISCORD_PRESENCE_SMALL_IMAGE_MODE", "default", "Integrations", "Small Image"),
    SettingSpec("DISCORD_PRESENCE_SMALL_IMAGE_VALUE", "", "Integrations", "Small Image Value"),
    SettingSpec("DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL", "true", "Integrations", "Use YouTube thumbnail by default for YouTube songs"),
    SettingSpec("DISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE", "false", "Integrations", "Show default large image as small image on song override"),
    SettingSpec("SERVER_HOST", "0.0.0.0", "Integrations", "Network address"),
    SettingSpec("SERVER_PORT", "4533", "Integrations", "Port"),
    SettingSpec("SERVER_USERNAME", "ytarchive", "Integrations", "Username"),
    SettingSpec("SERVER_PASSWORD", "", "Integrations", "Password", sensitive=True),
    SettingSpec("SERVER_PASSWORD_HASH", "", "Integrations", "Password hash", sensitive=True),
    SettingSpec("SERVER_TIMING", "false", "Integrations", "Timing logs"),
    SettingSpec("SERVER_AUTOSTART", "false", "Integrations", "Listen on another device"),
    SettingSpec("START_MINIMIZED_TO_TRAY", "false", "Startup", "Start minimized to tray"),
    SettingSpec("CLOSE_TO_TRAY", "true", "Startup", "Keep running in background when closing window"),
    SettingSpec("CACHE_UPDATE_ON_STARTUP", "never", "Startup", "Cache update on startup"),
    SettingSpec("CHECK_FOR_UPDATES_ON_STARTUP", "true", "Startup", "Show update prompts on startup"),
    SettingSpec("SKIPPED_UPDATE_VERSION", "", "Startup", "Skipped update version", internal=True),
    SettingSpec("COLOR_THEME", "system", "Appearance", "Color palette"),
    SettingSpec("SHOW_PLAYBACK_BAR", "false", "Appearance", "Show playback bar"),
    # Kept as a compatibility setting for existing config.ini files. The GUI
    # exposes COLOR_THEME instead.
    SettingSpec("DARK_MODE", "false", "Appearance", "Use dark mode"),
    SettingSpec("DISABLED_SIMILARITY_TAGS", "", "Tags", "Disabled similarity tags"),
    SettingSpec("SIMILARITY_USE_ARTIST", "true", "Algorithm", "Use matching artists"),
    SettingSpec("SIMILARITY_USE_TAGS", "true", "Algorithm", "Use matching tags"),
    SettingSpec("SIMILARITY_USE_RARITY", "true", "Algorithm", "Use rarity-based scoring"),
    SettingSpec("SIMILARITY_USE_BPM", "true", "Algorithm", "Use BPM proximity"),
    SettingSpec("SIMILARITY_USE_HALF_DOUBLE_TIME", "true", "Algorithm", "Treat half/double-time BPM as equivalent"),
    SettingSpec("SIMILARITY_ARTIST_WEIGHT", "100", "Algorithm", "Artist score weight (%)"),
    SettingSpec("SIMILARITY_TAG_WEIGHT", "100", "Algorithm", "Tag score weight (%)"),
    SettingSpec("SIMILARITY_BPM_MAX_DISTANCE", "30", "Algorithm", "Maximum BPM distance"),
    SettingSpec("SIMILARITY_BPM_WEIGHT", "30", "Algorithm", "Maximum BPM score"),
    SettingSpec("SIMILARITY_MIN_SCORE", "1", "Algorithm", "Minimum score for a result"),
    SettingSpec("SIMILARITY_MAX_RESULTS", "50", "Algorithm", "Maximum similar-song results"),
)
SETTING_BY_KEY = {spec.key: spec for spec in SETTING_SPECS}


@dataclass
class AppConfig:
    root_dir: Path
    download_dir: Path
    exports_dir: Path
    thumbnails_dir: Path
    artist_profiles_path: Path
    artist_thumbnails_dir: Path
    metadata_path: Path
    playlists_path: Path
    workers: int
    browser_cookies: Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]
    discord_enabled: bool
    spotify_lossless_command: str
    spotdl_command: str
    server_host: str
    server_port: int
    server_username: str
    server_password: str
    server_password_hash: str
    use_aria2c: bool = True
    browser_cookies_mode: str = "required"
    cookies_file: Optional[Path] = None
    server_timing: bool = False
    server_autostart: bool = False
    start_minimized_to_tray: bool = False
    close_to_tray: bool = True
    cache_update_on_startup: str = "never"
    check_for_updates_on_startup: bool = True
    skipped_update_version: str = ""
    dark_mode: bool = False
    color_theme: str = "system"
    show_playback_bar: bool = False
    disabled_similarity_tags: frozenset[str] = frozenset()
    similarity_use_artist: bool = True
    similarity_use_tags: bool = True
    similarity_use_rarity: bool = True
    similarity_use_bpm: bool = True
    similarity_use_half_double_time: bool = True
    similarity_artist_weight: int = 100
    similarity_tag_weight: int = 100
    similarity_bpm_max_distance: int = 30
    similarity_bpm_weight: int = 30
    similarity_min_score: int = 1
    similarity_max_results: int = 50
    discord_application_id: str = ""
    discord_presence_image_mode: str = "empty"
    discord_presence_image_value: str = ""
    discord_presence_small_image_mode: str = "default"
    discord_presence_small_image_value: str = ""
    discord_presence_default_youtube_thumbnail: bool = True
    discord_presence_show_default_as_small_on_override: bool = False


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        # Normalize absolute settings before comparing them with a resolved
        # data root. Windows can spell the same directory with a long user
        # name or an 8.3 alias, which would otherwise make a moved path look
        # as if it were outside the old root.
        return path.resolve()
    return root / path


def _legacy_source_root_dir() -> Optional[Path]:
    """Find the pre-packaging source checkout's adjacent data directory."""
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "config.ini").is_file():
        return candidate
    return None


def _platform_data_root_dir() -> Path:
    """Return the platform data root before a user-selected root is applied."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return (base.expanduser() / APP_DATA_DIR_NAME).resolve()


def _root_pointer_path() -> Path:
    return _platform_data_root_dir() / ROOT_POINTER_FILENAME


def _read_root_pointer() -> Optional[Path]:
    pointer = _root_pointer_path()
    try:
        target = Path(pointer.read_text(encoding="utf-8").strip()).expanduser().resolve()
    except (OSError, ValueError):
        return None
    return target if (target / "config.ini").is_file() else None


def default_root_dir() -> Path:
    """Return the selected data root used when ``--root`` is not supplied.

    A source checkout with an existing adjacent ``config.ini`` keeps using
    that legacy root so an upgrade does not silently hide an existing library.
    A first-run choice outside the platform directory is remembered by a small
    pointer file under the platform directory.
    """
    legacy = _legacy_source_root_dir()
    if legacy is not None:
        return legacy
    return _read_root_pointer() or _platform_data_root_dir()


def remember_root_dir(root_dir: Path) -> None:
    """Remember a user-selected data root for launches without ``--root``."""
    target = Path(root_dir).expanduser().resolve()
    platform_root = _platform_data_root_dir()
    pointer = _root_pointer_path()
    if target == platform_root:
        try:
            pointer.unlink()
        except FileNotFoundError:
            pass
        return
    platform_root.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_suffix(pointer.suffix + ".tmp")
    temporary.write_text(str(target) + "\n", encoding="utf-8")
    temporary.replace(pointer)


def resolve_root(root_dir: Path | None = None) -> Path:
    """Resolve an explicit application data root or the platform default."""
    return Path(root_dir).expanduser().resolve() if root_dir is not None else default_root_dir().resolve()


def parse_browser_cookies(value: str | bool | None) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    if not value or value is False:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"0", "false", "no", "off", "none"}:
        return None

    # yt-dlp's Python API expects (browser, profile, keyring, container).
    if ":" in raw:
        browser, profile = raw.split(":", 1)
        return browser.strip(), profile.strip() or None, None, None
    return raw, None, None, None


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed) if minimum is not None else parsed


def parse_disabled_tags(value: str | None) -> frozenset[str]:
    return frozenset(part.strip().casefold() for part in str(value or "").split(",") if part.strip())


def read_ini_settings(root_dir: Path | None = None) -> dict[str, str]:
    """Return the literal [Settings] values, including unknown extension keys."""
    root = resolve_root(root_dir)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    parser.optionxform = str
    parser.read(root / "config.ini", encoding="utf-8")
    return dict(parser["Settings"]) if parser.has_section("Settings") else {}


def effective_settings(root_dir: Path | None = None) -> dict[str, str]:
    values = {spec.key: spec.default for spec in SETTING_SPECS}
    configured = read_ini_settings(root_dir)
    values.update(configured)
    if "COLOR_THEME" not in configured and _as_bool(values.get("DARK_MODE")):
        values["COLOR_THEME"] = "dark"
    return values


def save_ini_settings(root_dir: Path, updates: dict[str, Optional[str]]) -> None:
    """Patch [Settings] in place, preserving comments, blank lines, and ordering.

    A None value removes the key, allowing the loader's built-in default to take
    effect.  This deliberately avoids ConfigParser.write(), which discards user
    comments and normalizes the rest of the file.
    """
    path = Path(root_dir).resolve() / "config.ini"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    if not lines:
        lines = ["[Settings]\n"]
    section_start = next((i for i, line in enumerate(lines) if line.strip().lower() == "[settings]"), None)
    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        section_start = len(lines)
        lines.append("[Settings]\n")
    section_end = next((i for i in range(section_start + 1, len(lines)) if re.match(r"^\s*\[[^]]+\]", lines[i])), len(lines))
    pending = {key.upper(): value for key, value in updates.items()}
    assignment = re.compile(r"^(\s*)([^=;#\s][^=]*?)(\s*=\s*)(.*?)(\r?\n)?$")
    output: list[str] = []
    for i, line in enumerate(lines):
        if section_start < i < section_end:
            match = assignment.match(line)
            key = match.group(2).strip().upper() if match else ""
            if key in pending:
                value = pending.pop(key)
                if value is not None:
                    ending = match.group(5) or "\n"
                    # Retain a conventional inline comment when replacing a
                    # value.  Commands may legitimately contain '#' or ';',
                    # so only whitespace-prefixed markers are treated as a
                    # comment delimiter.
                    trailing = re.search(r"(\s[;#].*)$", match.group(4))
                    suffix = trailing.group(1) if trailing else ""
                    output.append(f"{match.group(1)}{match.group(2)}{match.group(3)}{value}{suffix}{ending}")
                continue
        output.append(line)
    insert_at = next((i for i, line in enumerate(output) if i > section_start and re.match(r"^\s*\[[^]]+\]", line)), len(output))
    additions = [f"{key} = {value}\n" for key, value in pending.items() if value is not None]
    output[insert_at:insert_at] = additions
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    temporary.replace(path)


def load_config(root_dir: Path | None = None) -> AppConfig:
    root = resolve_root(root_dir)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    parser.read(root / "config.ini", encoding="utf-8")
    settings = parser["Settings"] if parser.has_section("Settings") else {}

    configured_theme = str(settings.get("COLOR_THEME", "dark" if _as_bool(settings.get("DARK_MODE")) else "system")).strip().lower()
    color_theme = configured_theme if configured_theme in {"system", "light", "dark", "ocean", "forest", "sunset", "purple", "oled"} else "system"

    return AppConfig(
        root_dir=root,
        download_dir=_resolve(root, settings.get("DOWNLOAD_DIRECTORY", "videos")),
        exports_dir=_resolve(root, settings.get("EXPORTS_DIRECTORY", "exports")),
        thumbnails_dir=_resolve(root, settings.get("THUMBNAILS_DIRECTORY", "thumbnails")),
        artist_profiles_path=_resolve(root, settings.get("ARTIST_PROFILES_PATH", "artist_profiles.json")),
        artist_thumbnails_dir=_resolve(root, settings.get("ARTIST_THUMBNAILS_DIRECTORY", "artist_thumbnails")),
        metadata_path=_resolve(root, settings.get("METADATA_PATH", "video_metadata.json")),
        playlists_path=_resolve(root, settings.get("PLAYLISTS_PATH", "playlists.txt")),
        workers=_as_int(settings.get("NUM_WORKERS"), 4, 1),
        use_aria2c=_as_bool(settings.get("USE_ARIA2C"), True),
        browser_cookies=parse_browser_cookies(settings.get("USE_BROWSER_COOKIES")),
        browser_cookies_mode=(
            str(settings.get("BROWSER_COOKIES_MODE", "required")).strip().lower()
            if str(settings.get("BROWSER_COOKIES_MODE", "required")).strip().lower() in {"always", "required", "never"}
            else "required"
        ),
        cookies_file=(
            _resolve(root, str(settings.get("COOKIES_FILE", "")).strip())
            if str(settings.get("COOKIES_FILE", "")).strip()
            else None
        ),
        discord_enabled=_as_bool(settings.get("DISCORD_RPC")),
        discord_application_id=str(settings.get("DISCORD_APPLICATION_ID", "")).strip(),
        discord_presence_image_mode=(
            str(settings.get("DISCORD_PRESENCE_IMAGE_MODE", "empty")).strip().lower()
            if str(settings.get("DISCORD_PRESENCE_IMAGE_MODE", "empty")).strip().lower() in {"url", "discord_key", "empty"}
            else "empty"
        ),
        discord_presence_image_value=str(settings.get("DISCORD_PRESENCE_IMAGE_VALUE", "")).strip(),
        discord_presence_small_image_mode=(
            str(settings.get("DISCORD_PRESENCE_SMALL_IMAGE_MODE", "default")).strip().lower()
            if str(settings.get("DISCORD_PRESENCE_SMALL_IMAGE_MODE", "default")).strip().lower()
            in {"default", "empty", "url", "discord_key"}
            else "default"
        ),
        discord_presence_small_image_value=str(settings.get("DISCORD_PRESENCE_SMALL_IMAGE_VALUE", "")).strip(),
        discord_presence_default_youtube_thumbnail=_as_bool(settings.get("DISCORD_PRESENCE_DEFAULT_YOUTUBE_THUMBNAIL"), True),
        discord_presence_show_default_as_small_on_override=_as_bool(
            settings.get("DISCORD_PRESENCE_SHOW_DEFAULT_AS_SMALL_ON_OVERRIDE"), False
        ),
        spotify_lossless_command=str(settings.get("SPOTIFY_LOSSLESS_COMMAND", "")).strip(),
        spotdl_command=str(settings.get("SPOTDL_COMMAND", "")).strip(),
        server_host=str(settings.get("SERVER_HOST", "0.0.0.0")).strip() or "0.0.0.0",
        server_port=_as_int(settings.get("SERVER_PORT"), 4533),
        server_username=str(settings.get("SERVER_USERNAME", "ytarchive")).strip() or "ytarchive",
        server_password=str(settings.get("SERVER_PASSWORD", "")).strip(),
        server_password_hash=str(settings.get("SERVER_PASSWORD_HASH", "")).strip(),
        server_timing=_as_bool(settings.get("SERVER_TIMING")),
        server_autostart=_as_bool(settings.get("SERVER_AUTOSTART"), False),
        start_minimized_to_tray=_as_bool(settings.get("START_MINIMIZED_TO_TRAY")),
        close_to_tray=_as_bool(settings.get("CLOSE_TO_TRAY"), True),
        cache_update_on_startup=str(settings.get("CACHE_UPDATE_ON_STARTUP", "never")).strip().lower()
        if str(settings.get("CACHE_UPDATE_ON_STARTUP", "never")).strip().lower() in {"ask", "never", "automatic"}
        else "never",
        check_for_updates_on_startup=_as_bool(settings.get("CHECK_FOR_UPDATES_ON_STARTUP"), True),
        skipped_update_version=str(settings.get("SKIPPED_UPDATE_VERSION", "")).strip(),
        color_theme=color_theme,
        dark_mode=color_theme == "dark",
        show_playback_bar=_as_bool(settings.get("SHOW_PLAYBACK_BAR"), False),
        disabled_similarity_tags=parse_disabled_tags(settings.get("DISABLED_SIMILARITY_TAGS")),
        similarity_use_artist=_as_bool(settings.get("SIMILARITY_USE_ARTIST"), True),
        similarity_use_tags=_as_bool(settings.get("SIMILARITY_USE_TAGS"), True),
        similarity_use_rarity=_as_bool(settings.get("SIMILARITY_USE_RARITY"), True),
        similarity_use_bpm=_as_bool(settings.get("SIMILARITY_USE_BPM"), True),
        similarity_use_half_double_time=_as_bool(settings.get("SIMILARITY_USE_HALF_DOUBLE_TIME"), True),
        similarity_artist_weight=_as_int(settings.get("SIMILARITY_ARTIST_WEIGHT"), 100, 0),
        similarity_tag_weight=_as_int(settings.get("SIMILARITY_TAG_WEIGHT"), 100, 0),
        similarity_bpm_max_distance=_as_int(settings.get("SIMILARITY_BPM_MAX_DISTANCE"), 30, 0),
        similarity_bpm_weight=_as_int(settings.get("SIMILARITY_BPM_WEIGHT"), 30, 0),
        similarity_min_score=_as_int(settings.get("SIMILARITY_MIN_SCORE"), 1, 0),
        similarity_max_results=_as_int(settings.get("SIMILARITY_MAX_RESULTS"), 50, 1),
    )


def load_existing_config(root_dir: Path) -> AppConfig:
    """Load a data root that already belongs to ytarchive Library.

    Unlike ``load_config``, this helper never treats a missing configuration
    as a new empty archive. It is used when the user explicitly chooses an
    existing library so selecting the wrong directory cannot silently create
    a second, empty library there.
    """
    root = Path(root_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"The selected library directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"The selected library path is not a directory: {root}")
    if not (root / "config.ini").is_file():
        raise FileNotFoundError(
            f"No {APP_NAME} library exists in {root}. Select the complete library data folder, not its videos subfolder."
        )
    try:
        return load_config(root)
    except configparser.Error as exc:
        raise ValueError(f"Could not read the {APP_NAME} configuration in {root}: {exc}") from exc


def move_data_root(old_root: Path, new_root: Path, *, remember: bool = True) -> AppConfig:
    """Move an application's complete data root and return its new config.

    Storage settings that point inside the old root are rewritten as relative
    paths so the moved archive remains self-contained. Legacy absolute paths
    outside the root are preserved for compatibility.
    """
    source = Path(old_root).expanduser().resolve()
    target = Path(new_root).expanduser().resolve()
    if source == target:
        raise ValueError("Choose a different data directory.")
    if not (source / "config.ini").is_file():
        raise FileNotFoundError(f"No {APP_NAME} configuration exists in {source}")
    if target.is_relative_to(source):
        raise ValueError("The new data directory cannot be inside the current data directory.")
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"The new data directory is not a directory: {target}")
    existing_target_entries = (
        [entry for entry in target.iterdir() if entry.name != ROOT_POINTER_FILENAME]
        if target.exists()
        else []
    )
    if existing_target_entries:
        raise FileExistsError("Choose an empty folder for the library data.")

    old_config = load_config(source)
    updates: dict[str, Optional[str]] = {}
    for key, field in STORAGE_SETTING_FIELDS.items():
        path = getattr(old_config, field)
        try:
            relative = path.relative_to(source)
        except ValueError:
            continue
        updates[key] = str(relative) or "."

    # A source checkout keeps its package directory beside the legacy data
    # root. It is code, not application data, and must never be moved with the
    # archive.
    package_dir = Path(__file__).resolve().parent
    protected = package_dir if source == package_dir.parent else None
    if source == package_dir or (package_dir.is_relative_to(source) and protected is None):
        raise ValueError("The current data directory overlaps the installed source package.")

    target.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    children = [child for child in source.iterdir() if child != protected]

    def rollback() -> None:
        for moved_path, original_path in reversed(moved):
            if moved_path.exists() and not original_path.exists():
                shutil.move(str(moved_path), str(original_path))
        try:
            target.rmdir()
        except OSError:
            pass

    try:
        for child in children:
            moved_path = target / child.name
            shutil.move(str(child), str(moved_path))
            moved.append((moved_path, child))
        save_ini_settings(target, updates)
        if remember:
            remember_root_dir(target)
    except Exception:
        rollback()
        raise

    if protected is None:
        try:
            source.rmdir()
        except OSError:
            pass
    return load_config(target)
