"""Create opt-in desktop and application-menu shortcuts."""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, APP_SLUG


def _launcher_command() -> tuple[Path, list[str]]:
    """Return the executable and arguments a shortcut should launch."""
    if os.name == "nt":
        # A GUI shortcut should not open a console window. A normal CPython
        # virtual environment contains pythonw.exe beside python.exe.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            return pythonw.resolve(), ["-m", "ytarchive"]

    invoked = Path(sys.argv[0])
    if not invoked.is_absolute():
        located = shutil.which(str(invoked))
        if located:
            invoked = Path(located)
    if invoked.is_file() and invoked.suffix.lower() not in {".py", ".pyw"}:
        return invoked.resolve(), []
    return Path(sys.executable).resolve(), ["-m", "ytarchive"]


def _shortcut_arguments(root: Path | None) -> tuple[Path, list[str]]:
    target, arguments = _launcher_command()
    arguments = list(arguments)
    if root is not None:
        arguments.extend(("--root", str(root)))
    return target, arguments


def _desktop_exec_arg(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _desktop_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def _desktop_entry(target: Path, arguments: list[str], icon: Path) -> str:
    command = " ".join(
        _desktop_exec_arg(value)
        for value in (target, *arguments)
    )
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Desktop media library and player\n"
        f"Exec={command}\n"
        f"Icon={_desktop_value(icon)}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
    )


def _write_linux_shortcut(
    destination: Path,
    target: Path,
    arguments: list[str],
    icon: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_desktop_entry(target, arguments, icon), encoding="utf-8")
    destination.chmod(destination.stat().st_mode | 0o111)


def _linux_destinations() -> list[Path]:
    destinations = [
        Path.home() / ".local" / "share" / "applications" / f"{APP_SLUG}.desktop",
    ]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        destinations.append(desktop / f"{APP_SLUG}.desktop")
    return destinations


def _powershell_executable() -> str:
    for name in ("powershell.exe", "powershell", "pwsh"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise OSError("PowerShell is required to create a Windows shortcut.")


def _write_windows_shortcut(
    destination: Path,
    target: Path,
    arguments: list[str],
    icon: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "YTARCHIVE_LIB_SHORTCUT_PATH": str(destination),
            "YTARCHIVE_LIB_SHORTCUT_TARGET": str(target),
            "YTARCHIVE_LIB_SHORTCUT_ARGUMENTS": subprocess.list2cmdline(arguments),
            "YTARCHIVE_LIB_SHORTCUT_WORKING_DIRECTORY": str(target.parent),
            "YTARCHIVE_LIB_SHORTCUT_ICON": f"{icon},0",
            "YTARCHIVE_LIB_SHORTCUT_DESCRIPTION": APP_NAME,
        }
    )
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:YTARCHIVE_LIB_SHORTCUT_PATH)
$shortcut.TargetPath = $env:YTARCHIVE_LIB_SHORTCUT_TARGET
$shortcut.Arguments = $env:YTARCHIVE_LIB_SHORTCUT_ARGUMENTS
$shortcut.WorkingDirectory = $env:YTARCHIVE_LIB_SHORTCUT_WORKING_DIRECTORY
$shortcut.IconLocation = $env:YTARCHIVE_LIB_SHORTCUT_ICON
$shortcut.Description = $env:YTARCHIVE_LIB_SHORTCUT_DESCRIPTION
$shortcut.Save()
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [
            _powershell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown PowerShell error").strip()
        raise OSError(f"Could not create {destination}: {detail}")


def _windows_destinations() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return [
        Path.home() / "Desktop" / f"{APP_NAME}.lnk",
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk",
    ]


def create_shortcuts(root: Path | None = None) -> list[Path]:
    """Create shortcuts for the current platform and return their paths."""
    root = root.expanduser().resolve() if root is not None else None
    target, arguments = _shortcut_arguments(root)
    if not target.is_file():
        raise OSError(f"Could not find the application launcher: {target}")

    icon_name = "logo.ico" if os.name == "nt" else "logo.svg"
    icon = Path(__file__).with_name(icon_name)
    if not icon.is_file():
        raise OSError(f"Could not find the bundled application icon: {icon}")

    destinations = _windows_destinations() if os.name == "nt" else _linux_destinations()
    for destination in destinations:
        if os.name == "nt":
            _write_windows_shortcut(destination, target, arguments, icon)
        else:
            _write_linux_shortcut(destination, target, arguments, icon)
    return destinations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create desktop and application-menu shortcuts for ytarchive Library."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Open this data root when the shortcut is launched.",
    )
    args = parser.parse_args(argv)
    try:
        destinations = create_shortcuts(args.root)
    except OSError as exc:
        print(f"Could not create shortcuts: {exc}", file=sys.stderr)
        return 1
    for destination in destinations:
        print(f"Created {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
