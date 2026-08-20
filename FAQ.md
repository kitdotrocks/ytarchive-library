# FAQ

### Why does ytarchive Library say that mpv is missing?

Rerun the setup helper and allow it to install the missing requirements. You
can also install mpv from its [official installation
page](https://mpv.io/installation/). Close and reopen ytarchive Library after
installing it. No separate mpv configuration is normally required.

If you installed the app manually, run `ytarchive-lib doctor` in the same
Python environment to check whether mpv can be found.

### Can I delete the downloaded setup folder after installation?

Yes. The setup helper copies the application into a private folder for your
user account. You can delete the downloaded ZIP and extracted setup folder
after ytarchive Library opens successfully.

Your library is stored separately and is not affected. Keep the setup ZIP only
if you want an offline copy of that application version; installing Python
dependencies still requires an internet connection.

### How do I update ytarchive Library?

Download the newest setup ZIP, extract it, and run the setup helper again. It
updates the app without changing the library folder or settings.

### How do I loop the current video or track?

Focus the embedded player and press `Shift+L`. This toggles mpv's loop mode.
Press `Shift+L` again to turn it off.

### Do I need `aria2c`?

No. `aria2c` is optional. When it is installed, ytarchive Library can use it
to accelerate some direct media downloads; otherwise downloads use yt-dlp
normally.

### Where is my library stored?

Your media, library information, artwork, playlists, exports, settings, and
logs are stored in the library data folder you chose during first-run setup.
See [Where your files are stored](README.md#where-your-files-are-stored) for
the suggested locations. Back up that folder as a unit.

### Why did a download fail?

Run the dependency check described under
[Troubleshooting](README.md#troubleshooting) first. Some sites or media require
browser cookies; cookie selection and testing are available under **Settings →
Downloads**. The application log is stored as `ytarchive.log` in the active
library data folder.

### Can I listen to the library on another device?

Yes. Enable and configure the server in **Settings → Integrations → Subsonic
server**, then connect with a Subsonic client on the same network.
