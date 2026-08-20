"""Console entry points for the GUI, server, and diagnostics commands."""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from . import APP_COMMAND, APP_NAME
from .config import default_root_dir


_startup_log_closer: Optional[Callable[[], None]] = None


def _print_help() -> None:
    """Print concise, user-facing help before GUI startup redirects output."""
    print(
        f"""{APP_NAME}

Usage:
  {APP_COMMAND} [--root PATH]             Open the desktop library
  {APP_COMMAND} doctor [--json]           Check required and optional dependencies
  {APP_COMMAND} shortcuts [--root PATH]   Create desktop/application shortcuts
  {APP_COMMAND} serve [OPTIONS]           Run the read-only Subsonic server

Options:
  --root PATH    Use PATH for configuration and library data
  -h, --help     Show this help and exit

Examples:
  {APP_COMMAND}
  {APP_COMMAND} --root ~/Music/ytarchive
  {APP_COMMAND} doctor
  {APP_COMMAND} doctor --json
  {APP_COMMAND} shortcuts

Run '{APP_COMMAND} serve --help' for server options.
"""
    )


def _install_startup_log(root_dir: Optional[Path] = None) -> None:
    """Capture GUI startup output beside application data when possible."""
    global _startup_log_closer
    if _startup_log_closer is not None:
        _startup_log_closer()
    log = None
    root = (root_dir or default_root_dir()).resolve()
    candidates = (root / "ytarchive.log", Path(tempfile.gettempdir()) / "ytarchive.log")
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            log = candidate.open("w", encoding="utf-8", buffering=1)
            log_path = candidate
            break
        except OSError:
            continue
    if log is None:
        return
    log.write(f"{APP_NAME} log: {log_path}\n")
    log.write(f"cwd: {Path.cwd()}\n")
    log.write(f"argv: {' '.join(sys.argv)}\n")
    log.write("=" * 72 + "\n")
    stdout_fd = _duplicate_fd(1)
    stderr_fd = _duplicate_fd(2)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    try:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    except OSError:
        pass
    sys.stdout = os.fdopen(1, "w", encoding=getattr(original_stdout, "encoding", None) or "utf-8", buffering=1, closefd=False)
    sys.stderr = os.fdopen(2, "w", encoding=getattr(original_stderr, "encoding", None) or "utf-8", buffering=1, closefd=False)

    closed = False

    def close_log() -> None:
        nonlocal closed
        global _startup_log_closer
        if closed:
            return
        closed = True
        sys.stdout.flush()
        sys.stderr.flush()
        _restore_output_fd(stdout_fd, 1)
        _restore_output_fd(stderr_fd, 2)
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        _close_fd(stdout_fd)
        _close_fd(stderr_fd)
        log.close()
        if _startup_log_closer is close_log:
            _startup_log_closer = None

    _startup_log_closer = close_log
    atexit.register(close_log)


def close_startup_log() -> None:
    """Close the redirected GUI log before an operation moves its root."""
    closer = _startup_log_closer
    if closer is not None:
        closer()


def _duplicate_fd(fd: int) -> Optional[int]:
    try:
        return os.dup(fd)
    except OSError:
        return None


def _restore_output_fd(saved_fd: Optional[int], fd: int) -> None:
    if saved_fd is None:
        return
    try:
        os.dup2(saved_fd, fd)
    except OSError:
        pass


def _close_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _root_argument(argv: list[str]) -> Optional[Path]:
    for index, value in enumerate(argv):
        if value == "--root" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith("--root="):
            return Path(value.partition("=")[2])
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command == "serve":
        from .server import main as server_main

        return server_main(args[1:])
    if command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(args[1:])
    if command == "shortcuts":
        from .shortcuts import main as shortcuts_main

        return shortcuts_main(args[1:])
    if any(value in {"-h", "--help"} for value in args):
        _print_help()
        return 0

    _install_startup_log(_root_argument(args))
    from .app import main as app_main

    return app_main(args)


__all__ = ["main"]
