"""Runtime environment helpers for installed desktop launches."""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional


_WINDOWS_TOOL_NAMES = frozenset(
    {
        "aria2c.exe",
        "deno.exe",
        "ffmpeg.exe",
        "ffprobe.exe",
        "mpv.exe",
        "spotdl.exe",
    }
)


def _windows_registry_tool_paths() -> tuple[Path, ...]:
    """Return executable paths registered through Windows App Paths."""
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    paths: list[Path] = []
    seen: set[str] = set()
    access_modes = [getattr(winreg, "KEY_READ", 0)]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag and flag not in access_modes:
            access_modes.append(flag | getattr(winreg, "KEY_READ", 0))

    for executable in sorted(_WINDOWS_TOOL_NAMES):
        subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for access in access_modes:
                try:
                    key = winreg.OpenKey(hive, subkey, 0, access)
                except OSError:
                    continue
                try:
                    value, _value_type = winreg.QueryValueEx(key, "")
                except OSError:
                    value = ""
                finally:
                    winreg.CloseKey(key)
                raw_value = os.path.expandvars(str(value or "")).strip()
                if raw_value.startswith('"'):
                    raw_value = raw_value[1:].split('"', 1)[0]
                elif raw_value:
                    raw_value = raw_value.split()[0]
                if not raw_value:
                    continue
                candidate = Path(raw_value)
                try:
                    if candidate.is_file():
                        resolved = os.path.normcase(os.path.abspath(str(candidate)))
                        if resolved not in seen:
                            seen.add(resolved)
                            paths.append(candidate)
                except OSError:
                    continue
    return tuple(paths)


@lru_cache(maxsize=1)
def _windows_runtime_directories() -> tuple[Path, ...]:
    """Find common per-user Windows package locations for external tools."""
    if os.name != "nt":
        return ()

    directories: list[Path] = []
    seen: set[str] = set()

    def add(directory: Path) -> None:
        try:
            if not directory.is_dir():
                return
        except OSError:
            return
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key not in seen:
            seen.add(key)
            directories.append(directory)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        local_root = Path(local_app_data)
        # WinGet exposes portable package executables through this directory,
        # which is not present in every process's inherited PATH.
        add(local_root / "Microsoft" / "WinGet" / "Links")
        add(local_root / "Microsoft" / "WindowsApps")

        packages = local_root / "Microsoft" / "WinGet" / "Packages"
        try:
            for candidate in packages.rglob("*"):
                if candidate.is_file() and candidate.name.casefold() in _WINDOWS_TOOL_NAMES:
                    add(candidate.parent)
        except OSError:
            pass

        for directory in (
            local_root / "Programs" / "mpv",
            local_root / "Programs" / "mpv" / "bin",
            local_root / "Programs" / "ffmpeg",
            local_root / "Programs" / "ffmpeg" / "bin",
            local_root / "Programs" / "Deno",
            local_root / "Programs" / "Deno" / "bin",
        ):
            add(directory)

    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        raw_root = os.environ.get(variable, "").strip()
        if not raw_root:
            continue
        root = Path(raw_root)
        for directory in (
            root / "mpv",
            root / "mpv" / "bin",
            root / "MPV Player",
            root / "MPV Player" / "bin",
            root / "ffmpeg" / "bin",
            root / "Deno",
            root / "Deno" / "bin",
        ):
            add(directory)

    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        user_root = Path(user_profile)
        for directory in (
            user_root / "scoop" / "shims",
            user_root / "scoop" / "apps" / "mpv" / "current",
            user_root / "scoop" / "apps" / "mpv" / "current" / "bin",
            user_root / ".deno" / "bin",
        ):
            add(directory)

    program_data = os.environ.get("ProgramData", "").strip()
    if program_data:
        data_root = Path(program_data)
        for directory in (
            data_root / "chocolatey" / "bin",
            data_root / "scoop" / "shims",
        ):
            add(directory)

    for executable in _windows_registry_tool_paths():
        add(executable.parent)

    return tuple(directories)


def prepare_external_tool_path() -> None:
    """Make installed Windows tool locations visible to this app process."""
    # A setup helper or package manager can install a tool after this module
    # was first imported. Refresh the cached directory scan whenever a caller
    # asks us to prepare the process again.
    _windows_runtime_directories.cache_clear()
    directories = _windows_runtime_directories()
    if not directories:
        return

    current_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    seen = {os.path.normcase(os.path.abspath(entry)) for entry in current_entries}
    additions: list[str] = []
    for directory in directories:
        value = str(directory)
        key = os.path.normcase(os.path.abspath(value))
        if key not in seen:
            seen.add(key)
            additions.append(value)
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, *current_entries])


def find_external_tool(name: str) -> Optional[str]:
    """Return an external tool path, including Windows installs outside PATH."""
    prepare_external_tool_path()
    located = shutil.which(name)
    if located:
        return located
    if os.name != "nt":
        return None

    executable = Path(name).name
    executable_names = [executable]
    if not executable.casefold().endswith(".exe"):
        executable_names.append(f"{executable}.exe")
    registry_paths = _windows_registry_tool_paths()
    for candidate in registry_paths:
        if candidate.name.casefold() in {item.casefold() for item in executable_names}:
            return str(candidate)
    for directory in _windows_runtime_directories():
        for executable_name in executable_names:
            candidate = directory / executable_name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def windows_no_console_kwargs() -> dict[str, object]:
    """Return subprocess options that keep Windows console tools windowless."""
    if os.name != "nt":
        return {}
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": creation_flags} if creation_flags else {}


__all__ = ["find_external_tool", "prepare_external_tool_path", "windows_no_console_kwargs"]
