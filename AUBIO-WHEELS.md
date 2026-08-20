# Windows aubio wheels

The guided Windows setup bundle includes prebuilt Windows x64 `aubio` wheels so
automatic BPM analysis does not require Microsoft C++ Build Tools on the end
user's computer.

## Source and license

The wheels are unmodified builds of the following upstream source revision:

- Project: <https://github.com/aubio/aubio>
- Commit: `ad5cf975aed08cc4562dd008cf9f83b12b82ffb8`
- Package version: `0.5.0a0` (`0.5.0-alpha` upstream)
- License: GNU General Public License, version 3 or later

Each wheel contains aubio's `AUTHORS` and `COPYING` files. Every release that
contains these wheels also publishes `aubio-0.5.0a0-source.zip`, an archive of
the exact corresponding source revision, and places that archive inside the
guided setup ZIP under `third-party/aubio/`, beside the build recipe and
records. ytarchive Library does not patch the aubio source.

The bundled `aubio-0.5.0a0-build.yml` workflow snapshot and per-wheel
`aubio-0.5.0a0-cp*-build.txt` records are the script and build details used to
produce the binaries. They are stored under `third-party/aubio/`; the wheels
are stored under `wheels/`. Keep the source archive, license files, build
recipe, build records, and wheels together when redistributing the setup bundle.

## Rebuilding

The release workflow builds one x64 wheel for each supported CPython version
from 3.10 through 3.14 on a GitHub-hosted Windows runner. It checks out the
pinned commit, installs the recorded NumPy, setuptools, and wheel versions,
then runs:

```powershell
python -m pip wheel --no-deps --no-build-isolation --wheel-dir wheelhouse ./aubio-source
```

The workflow installs each resulting wheel and constructs an
`aubio.tempo` detector before publishing it. See
`.github/workflows/release.yml` (also distributed as
`third-party/aubio/aubio-0.5.0a0-build.yml` in the setup bundle) for the
complete build recipe and the NumPy header version used for each Python
version.

FFmpeg and NumPy are not copied into the guided setup ZIP. Setup obtains them
separately, and their upstream licenses continue to apply.
