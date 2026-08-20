"""ytarchive Library desktop media archive GUI."""

APP_NAME = "ytarchive Library"
APP_SLUG = "ytarchive-lib"
APP_COMMAND = "ytarchive-lib"
SERVER_COMMAND = "ytarchive-lib-server"
# The public repository is also the source of release announcements.  Keep
# this separate from the package name so the update checker can continue to
# work if the distribution name ever changes.
PROJECT_REPOSITORY = "kitdotrocks/ytarchive-library"
PROJECT_REPOSITORY_URL = f"https://github.com/{PROJECT_REPOSITORY}"
__version__ = "0.1.0"

from .runtime import prepare_external_tool_path

prepare_external_tool_path()
