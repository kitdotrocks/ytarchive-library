from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.updates import (  # noqa: E402
    ReleaseInfo,
    fetch_latest_release,
    find_update,
    is_newer_version,
    is_valid_repository,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class UpdateVersionTestCase(unittest.TestCase):
    def test_repository_identifier_is_validated(self) -> None:
        self.assertTrue(is_valid_repository("kitdotrocks/ytarchive-library"))
        self.assertFalse(is_valid_repository("https://github.com/kitdotrocks/ytarchive-library"))
        self.assertFalse(is_valid_repository("owner/repo/releases/latest"))

    def test_version_comparison_handles_v_prefix_and_trailing_zeroes(self) -> None:
        self.assertTrue(is_newer_version("0.1.0", "v0.2.0"))
        self.assertFalse(is_newer_version("v0.2.0", "0.2"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.0-beta"))
        self.assertTrue(is_newer_version("1.0.0-beta", "1.0.0"))

    def test_fetch_latest_release_uses_public_github_api(self) -> None:
        payload = {
            "tag_name": "v0.2.0",
            "name": "First public release",
            "html_url": "https://github.com/kitdotrocks/ytarchive-library/releases/tag/v0.2.0",
            "draft": False,
            "prerelease": False,
        }
        with mock.patch("ytarchive.updates.urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            release = fetch_latest_release("kitdotrocks/ytarchive-library")

        self.assertEqual(
            release,
            ReleaseInfo(
                tag_name="v0.2.0",
                version="0.2.0",
                url="https://github.com/kitdotrocks/ytarchive-library/releases/tag/v0.2.0",
                name="First public release",
            ),
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/kitdotrocks/ytarchive-library/releases/latest")
        self.assertEqual(request.headers["Accept"], "application/vnd.github+json")

    def test_find_update_returns_only_a_newer_release(self) -> None:
        release = ReleaseInfo("v0.2.0", "0.2.0", "https://github.com/kitdotrocks/ytarchive-library/releases/tag/v0.2.0")
        with mock.patch("ytarchive.updates.fetch_latest_release", return_value=release):
            self.assertEqual(find_update("0.1.0"), release)
            self.assertIsNone(find_update("0.2.0"))


if __name__ == "__main__":
    unittest.main()
