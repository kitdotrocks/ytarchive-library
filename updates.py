"""Small, best-effort GitHub release update checker.

The application uses this module from a background Qt thread.  Keeping the
HTTP and version logic here makes the startup UI code small and easy to test,
and means an unavailable network never needs to interrupt normal startup.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import APP_SLUG, PROJECT_REPOSITORY

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(
    r"(?<![0-9])v?(?P<release>[0-9]+(?:\.[0-9]+)*)(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReleaseInfo:
    """The user-facing details needed to announce one GitHub release."""

    tag_name: str
    version: str
    url: str
    name: str = ""


def is_valid_repository(repository: str) -> bool:
    """Return whether *repository* is a safe ``owner/name`` identifier."""
    return bool(_REPOSITORY_RE.fullmatch(str(repository).strip()))


def _version_parts(value: str) -> tuple[tuple[int, ...], bool] | None:
    """Return numeric release parts and whether a suffix is present."""
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        return None
    try:
        release = tuple(int(part) for part in match.group("release").split("."))
    except ValueError:
        return None
    return release or (0,), bool(match.group("pre"))


def normalize_version(value: str) -> Optional[str]:
    """Return a clean numeric version from a package version or release tag."""
    parts = _version_parts(value)
    if parts is None:
        return None
    release, has_suffix = parts
    text = ".".join(str(part) for part in release)
    return f"{text}-pre" if has_suffix else text


def is_newer_version(current_version: str, latest_version: str) -> bool:
    """Compare two release versions without requiring an extra dependency."""
    current = _version_parts(current_version)
    latest = _version_parts(latest_version)
    if current is None or latest is None:
        return False
    current_release, current_pre = current
    latest_release, latest_pre = latest
    # Padding makes ``1.0`` and ``1.0.0`` compare as the same release.
    width = max(len(current_release), len(latest_release))
    current_key = current_release + (0,) * (width - len(current_release))
    latest_key = latest_release + (0,) * (width - len(latest_release))
    if latest_key != current_key:
        return latest_key > current_key
    # A stable release is newer than its matching pre-release.  GitHub's
    # ``latest`` endpoint excludes prereleases, but this also keeps the helper
    # correct when it is used with a manually supplied tag in a test.
    return current_pre and not latest_pre


def is_update_skipped(skipped_version: str, latest_version: str) -> bool:
    """Return whether *latest_version* is covered by a skipped release.

    A skipped release remains suppressed until a strictly newer release is
    found. Invalid stored state is ignored so a malformed configuration cannot
    silence update prompts forever.
    """
    if not str(skipped_version).strip():
        return False
    if _version_parts(skipped_version) is None or _version_parts(latest_version) is None:
        return False
    return not is_newer_version(skipped_version, latest_version)


def _release_from_payload(payload: object, repository: str) -> Optional[ReleaseInfo]:
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag_name = str(payload.get("tag_name", "")).strip()
    version = normalize_version(tag_name)
    if not tag_name or version is None:
        return None
    url = str(payload.get("html_url", "")).strip()
    if not url:
        url = f"https://github.com/{repository}/releases/latest"
    return ReleaseInfo(
        tag_name=tag_name,
        version=version,
        url=url,
        name=str(payload.get("name", "")).strip(),
    )


def fetch_latest_release(repository: str = PROJECT_REPOSITORY, *, timeout: float = 5.0) -> Optional[ReleaseInfo]:
    """Fetch the latest published full release for a public GitHub repository.

    Any network or response error is allowed to reach the caller so the UI
    worker can record it as a silent, best-effort failure.
    """
    repository = str(repository).strip()
    if not is_valid_repository(repository):
        raise ValueError(f"Invalid GitHub repository: {repository!r}")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_SLUG}-update-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _release_from_payload(payload, repository)


def find_update(current_version: str, repository: str = PROJECT_REPOSITORY, *, timeout: float = 5.0) -> Optional[ReleaseInfo]:
    """Return release information only when a newer release is available."""
    release = fetch_latest_release(repository, timeout=timeout)
    if release is None or not is_newer_version(current_version, release.version):
        return None
    return release
