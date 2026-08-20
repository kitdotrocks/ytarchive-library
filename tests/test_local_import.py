from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ytarchive.app import _extract_zip_media_files


class ZipLocalImportTestCase(unittest.TestCase):
    def test_extracts_supported_media_and_keeps_track_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "album.zip"
            with zipfile.ZipFile(archive, "w") as contents:
                contents.writestr("Album/01 Intro.mp3", b"intro")
                contents.writestr("Album/02 Song.flac", b"song")
                contents.writestr("Album/notes.txt", b"ignored")

            extracted = _extract_zip_media_files(archive, root / "extracted")

            self.assertEqual([path.name for path in extracted], ["01 Intro.mp3", "02 Song.flac"])
            self.assertEqual([path.read_bytes() for path in extracted], [b"intro", b"song"])

    def test_rejects_zip_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            link = zipfile.ZipInfo("song.mp3")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            with zipfile.ZipFile(archive, "w") as contents:
                contents.writestr(link, "outside.mp3")

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                _extract_zip_media_files(archive, root / "extracted")
