# Third-Party Notices

ytarchive Library is licensed under the GNU General Public License, version 3
or any later version. See [LICENSE](LICENSE) for the project's license.

This file identifies third-party software and assets used by ytarchive Library.
The current Python package and source distribution do not bundle the external
executables, Python dependencies, JavaScript runtimes, or font listed below;
users install those components separately. If a future installer or portable
bundle includes any of them, it must ship the applicable license files and
notices for the exact versions and builds included.

## Python dependencies

### Qt for Python (PySide6)

ytarchive Library uses [Qt for Python (PySide6)](https://doc.qt.io/qtforpython-6/)
for its user interface. Qt for Python is available under the LGPL version 3,
the GPL version 3, and Qt commercial licenses. See the official [Qt licensing
information](https://doc.qt.io/qt/6/licensing.html) and [Qt for Python license
information](https://doc.qt.io/qtforpython-6/licenses.html).

When redistributing PySide6 or Qt binaries, include the applicable license and
third-party notices supplied with the exact PySide6/Qt distribution.

### yt-dlp

Online media downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp), which is
released under [The Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE).

The `yt-dlp[default]` dependency may also install the separate
[`yt-dlp-ejs`](https://github.com/yt-dlp/ejs) package. Its prebuilt wheels
include code under the ISC and MIT licenses; see its [licensing
information](https://github.com/yt-dlp/ejs#licensing) when bundling it.

### aubio

The optional BPM-analysis integration uses [aubio](https://github.com/aubio/aubio),
licensed under the GNU General Public License, version 3 or any later version.
See aubio's [COPYING file](https://github.com/aubio/aubio/blob/master/COPYING).

### pypresence

The optional Discord Rich Presence integration uses
[pypresence](https://github.com/qwertyquerty/pypresence), licensed under the
MIT License. See the [upstream license](https://github.com/qwertyquerty/pypresence/blob/master/LICENSE).

## External tools

These tools are invoked from `PATH` and are not included with the current
distribution.

### mpv

Embedded playback uses [mpv](https://mpv.io/). The upstream project is GPLv2
or later by default, or LGPLv2.1 or later when built with `-Dgpl=false`. See
[mpv's license information](https://github.com/mpv-player/mpv#license).

### FFmpeg

Media conversion and inspection use [FFmpeg](https://ffmpeg.org/). FFmpeg is
LGPLv2.1 or later by default, but optional components can be GPL-licensed. The
license and source obligations depend on the exact build being distributed;
see [FFmpeg's legal information](https://ffmpeg.org/legal.html).

### aria2

The optional accelerated-download integration uses
[aria2](https://github.com/aria2/aria2), licensed under the GNU General Public
License, version 2 or any later version. See the [upstream license
files](https://github.com/aria2/aria2).

### Deno and other JavaScript runtimes

yt-dlp can use [Deno](https://github.com/denoland/deno) as its recommended
JavaScript runtime. Deno is MIT-licensed. Other supported runtimes are not
bundled by ytarchive Library and retain their own licenses.

## Logo and banner

The `logo.svg` and `banner.svg` assets specify the [Roboto Mono
font](https://github.com/googlefonts/RobotoMono). The font file itself is not
included in this repository. If it is bundled in a future distribution, include
the [SIL Open Font License 1.1](https://github.com/googlefonts/RobotoMono/blob/main/OFL.txt)
and the accompanying font notices.

## Release note

This inventory is an index, not a substitute for the license files distributed
with third-party components. Before publishing a bundled release, record the
exact component versions, build options, license texts, and corresponding
source offers required by those components.
