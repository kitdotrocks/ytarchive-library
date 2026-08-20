"""Dependency diagnostics for installed ytarchive Library environments."""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import sys
from typing import Any

from . import APP_COMMAND, APP_NAME, APP_SLUG
from .bpm import aubio_available
from .downloader import yt_dlp_available, yt_dlp_ejs_available, yt_dlp_js_runtime
from .runtime import find_external_tool


DEPENDENCY_LABELS = {
    "PySide6": "PySide6 Python package",
    "yt-dlp": "yt-dlp Python package",
    "yt-dlp-ejs": "yt-dlp EJS challenge solver",
    "yt-dlp-js-runtime": "JavaScript runtime for yt-dlp",
    "pypresence": "pypresence Python package",
    "mpv": "mpv executable",
    "ffmpeg": "ffmpeg executable",
    "ffprobe": "ffprobe executable",
    "aubio": "aubio Python package (BPM analysis)",
}

DEPENDENCY_HELP = {
    "PySide6": f"Rerun the {APP_NAME} setup helper, or install PySide6 in the Python environment used by the app.",
    "yt-dlp": f"Rerun the {APP_NAME} setup helper, or run: python -m pip install -U \"yt-dlp[default]\".",
    "yt-dlp-ejs": "Rerun the setup helper, or reinstall yt-dlp with its default extras.",
    "yt-dlp-js-runtime": "Install Deno from https://docs.deno.com/runtime/getting_started/installation/, then restart the app.",
    "pypresence": "Install pypresence only if you want Discord Rich Presence.",
    "mpv": "Rerun the setup helper, or install mpv from https://mpv.io/installation/, then restart the app.",
    "ffmpeg": "Rerun the setup helper, or install FFmpeg from https://ffmpeg.org/download.html, then restart the app.",
    "ffprobe": "Install FFmpeg, which normally includes ffprobe, then restart the app.",
    "aubio": "Install aubio only if you want automatic BPM analysis; BPM can still be entered manually.",
}


def _python_package_available(name: str) -> bool:
    if name == "yt-dlp":
        return yt_dlp_available()
    return importlib.util.find_spec(name) is not None


def missing_dependencies() -> dict[str, list[str]]:
    """Return missing required and optional runtime dependencies."""
    required: list[str] = []
    optional: list[str] = []

    def add(target: list[str], item: str) -> None:
        if item not in target:
            target.append(item)

    if not _python_package_available("PySide6"):
        add(required, "PySide6")
    if not yt_dlp_available():
        add(required, "yt-dlp")
    else:
        if not yt_dlp_ejs_available():
            add(required, "yt-dlp-ejs")
        if yt_dlp_js_runtime() is None:
            add(required, "yt-dlp-js-runtime")

    for executable in ("mpv", "ffmpeg", "ffprobe"):
        if not shutil.which(executable) and not find_external_tool(executable):
            add(required, executable)
    if not _python_package_available("pypresence"):
        add(optional, "pypresence")
    if not aubio_available():
        add(optional, "aubio")
    return {"required": required, "optional": optional}


def dependency_report() -> dict[str, Any]:
    missing = missing_dependencies()
    return {
        "application": APP_SLUG,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "missing": missing,
    }


def format_dependency_report(report: dict[str, Any]) -> str:
    missing = report["missing"]
    lines = [
        f"{APP_COMMAND} doctor",
        f"Python: {report['python']}",
        f"Platform: {report['platform']}",
        "",
    ]
    for category, title in (("required", "Required tools and components"), ("optional", "Optional features")):
        items = missing.get(category) or []
        lines.append(f"{title}:")
        if not items:
            lines.append("  all detected")
            continue
        for item in items:
            lines.append(f"  - {DEPENDENCY_LABELS.get(item, item)}")
            lines.append(f"    {DEPENDENCY_HELP.get(item, 'Install it and make it available to the application.')}")
    if not missing["required"]:
        lines.extend(["", "Everything needed for playback and downloads is ready."])
    else:
        lines.extend(["", f"Install the required items, then run {APP_COMMAND} doctor again."])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Check {APP_NAME}'s Python and external runtime dependencies.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a human report.")
    args = parser.parse_args(argv)
    report = dependency_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_dependency_report(report))
    return 1 if report["missing"]["required"] else 0


__all__ = [
    "DEPENDENCY_HELP",
    "DEPENDENCY_LABELS",
    "dependency_report",
    "format_dependency_report",
    "main",
    "missing_dependencies",
]
