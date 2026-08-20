#!/bin/sh

# Friendly per-user installer for the Linux release bundle and source archive.
# The application itself is kept separate from the user's media library.

set -u

APP_NAME="ytarchive Library"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
INSTALL_DIR="$DATA_HOME/ytarchive-lib/app"
APP_LAUNCHER="$INSTALL_DIR/bin/ytarchive-lib"

say() {
    printf '%s\n' "$*"
}

confirm() {
    prompt=$1
    printf '%s [Y/n] ' "$prompt"
    if ! read -r answer; then
        return 1
    fi
    case "$answer" in
        ""|y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

confirm_no() {
    prompt=$1
    printf '%s [y/N] ' "$prompt"
    if ! read -r answer; then
        return 1
    fi
    case "$answer" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

pause_on_error() {
    say ""
    say "Setup could not finish. The message above explains what went wrong."
    say "You can correct the problem and run this file again."
    exit 1
}

refresh_deno_path() {
    if [ -f "$HOME/.deno/env" ]; then
        # The official Deno installer writes this small PATH helper.
        # shellcheck disable=SC1091
        . "$HOME/.deno/env"
    elif [ -d "$HOME/.deno/bin" ]; then
        PATH="$HOME/.deno/bin:$PATH"
        export PATH
    fi
}

install_optional_package() {
    package=$1
    label=$2
    say "Installing $label..."
    if ! "$INSTALL_DIR/bin/python" -m pip install --upgrade "$package"; then
        say "$label could not be installed. The app will continue without it."
        say "Rerun this setup helper later to try again."
    fi
}

install_optional_dependencies() {
    discord_ready=0
    aubio_ready=0
    if "$INSTALL_DIR/bin/python" -c "import pypresence" >/dev/null 2>&1; then
        discord_ready=1
    fi
    if "$INSTALL_DIR/bin/python" -c "import aubio" >/dev/null 2>&1; then
        aubio_ready=1
    fi
    if [ "$discord_ready" -eq 1 ] && [ "$aubio_ready" -eq 1 ]; then
        say ""
        say "Optional integrations are already installed."
        return 0
    fi

    say ""
    say "Optional integrations"
    if [ "$discord_ready" -eq 0 ]; then
        say "  1) Discord Rich Presence"
    fi
    if [ "$aubio_ready" -eq 0 ]; then
        say "  2) Automatic BPM analysis"
    fi
    if [ "$discord_ready" -eq 0 ] && [ "$aubio_ready" -eq 0 ]; then
        say "  3) Both"
    fi
    say "  0) Skip and keep any optional packages already installed"
    printf "Choose an option [0]: "
    optional_choice=""
    if ! read -r optional_choice; then
        optional_choice=""
    fi
    case "$optional_choice" in
        1)
            if [ "$discord_ready" -eq 0 ]; then
                install_optional_package "pypresence" "Discord Rich Presence"
            fi
            ;;
        2)
            if [ "$aubio_ready" -eq 0 ]; then
                install_optional_package "aubio" "automatic BPM analysis"
            fi
            ;;
        3)
            if [ "$discord_ready" -eq 0 ]; then
                install_optional_package "pypresence" "Discord Rich Presence"
            fi
            if [ "$aubio_ready" -eq 0 ]; then
                install_optional_package "aubio" "automatic BPM analysis"
            fi
            ;;
        ""|0)
            say "Skipping optional integrations. Existing optional packages are kept."
            ;;
        *)
            say "No optional integrations were selected. Existing optional packages are kept."
            ;;
    esac
}

close_running_app() {
    lock_file="$DATA_HOME/ytarchive Library/ytarchive-lib.lock"
    if [ ! -f "$lock_file" ]; then
        return 0
    fi

    app_pid=$(sed -n '1p' "$lock_file" 2>/dev/null || true)
    case "$app_pid" in
        ""|*[!0-9]*)
            return 0
            ;;
    esac
    if [ ! -r "/proc/$app_pid/cmdline" ]; then
        return 0
    fi

    app_command=$(tr '\0' ' ' < "/proc/$app_pid/cmdline" 2>/dev/null || true)
    case "$app_command" in
        *ytarchive*)
            if ! confirm_no "An existing $APP_NAME is running. Close it and launch the updated app?"; then
                say "Leaving the existing $APP_NAME running. The updated app was not launched."
                return 1
            fi
            say "Closing the existing $APP_NAME before launching the updated app..."
            kill -TERM "$app_pid" 2>/dev/null || true
            for attempt in 1 2 3 4 5; do
                if ! kill -0 "$app_pid" 2>/dev/null; then
                    return 0
                fi
                sleep 1
            done
            say "The existing $APP_NAME did not close in time. The updated app was not launched."
            return 1
            ;;
    esac
    return 0
}

say "$APP_NAME setup"
say ""
say "This installs the app for your user account and creates an application-menu shortcut."
say "Your music and settings are not changed by setup."
say ""

if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
    say "This helper is for Linux. See README.md for the manual installation steps."
    pause_on_error
fi

missing_packages=""
if ! command -v python3 >/dev/null 2>&1; then
    missing_packages="$missing_packages python3"
fi
if ! command -v mpv >/dev/null 2>&1; then
    missing_packages="$missing_packages mpv"
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    missing_packages="$missing_packages ffmpeg"
fi
if ! command -v deno >/dev/null 2>&1; then
    if ! command -v curl >/dev/null 2>&1; then
        missing_packages="$missing_packages curl"
    fi
    if ! command -v unzip >/dev/null 2>&1; then
        missing_packages="$missing_packages unzip"
    fi
fi

if [ -n "$missing_packages" ]; then
    say "Missing system tools:$missing_packages"
    if command -v apt-get >/dev/null 2>&1; then
        say ""
        if confirm "Install these tools with apt now?"; then
            if command -v sudo >/dev/null 2>&1; then
                if ! sudo apt-get update || ! sudo apt-get install -y $missing_packages python3-venv; then
                    say "apt could not install all required tools."
                fi
            elif [ "$(id -u)" = "0" ]; then
                if ! apt-get update || ! apt-get install -y $missing_packages python3-venv; then
                    say "apt could not install all required tools."
                fi
            else
                say "Administrator access is required. Ask an administrator to run:"
                say "  apt install$missing_packages python3-venv"
            fi
        fi
    else
        say "Install the missing tools with your distribution's software manager."
        say "The package names shown above are also common on other distributions."
    fi
    say ""
fi

if ! command -v python3 >/dev/null 2>&1; then
    say "Python 3.10 or newer is required before the app can be installed."
    say "See https://www.python.org/downloads/ or use your distribution's software manager."
    pause_on_error
fi

if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"; then
    say "The installed Python is too old. Python 3.10 or newer is required."
    pause_on_error
fi

refresh_deno_path
if ! command -v deno >/dev/null 2>&1; then
    say "Deno is needed for reliable online downloads."
    if command -v curl >/dev/null 2>&1 && confirm "Install Deno for your user account now?"; then
        if ! curl -fsSL https://deno.land/install.sh | sh; then
            say "Deno could not be installed. You can retry later from https://deno.com/."
        fi
        refresh_deno_path
    else
        say "You can install it later from https://docs.deno.com/runtime/getting_started/installation/."
    fi
    say ""
fi

say "Installing $APP_NAME..."
if [ ! -x "$INSTALL_DIR/bin/python" ]; then
    if ! python3 -m venv "$INSTALL_DIR"; then
        say "Python could not create the private app environment."
        say "On Ubuntu or Debian, install python3-venv and run setup again:"
        say "  sudo apt install python3-venv"
        pause_on_error
    fi
fi

if ! "$INSTALL_DIR/bin/python" -m pip install --upgrade pip; then
    say "The Python installer could not be updated. Check your internet connection."
    pause_on_error
fi

PACKAGE_SOURCE=$SCRIPT_DIR
for wheel in "$SCRIPT_DIR"/ytarchive_lib-*.whl "$SCRIPT_DIR"/wheels/ytarchive_lib-*.whl; do
    if [ -f "$wheel" ]; then
        PACKAGE_SOURCE=$wheel
        break
    fi
done

if ! "$INSTALL_DIR/bin/python" -m pip install --upgrade "$PACKAGE_SOURCE"; then
    say "The app could not be installed. Check your internet connection and run setup again."
    pause_on_error
fi

install_optional_dependencies

if ! "$APP_LAUNCHER" shortcuts; then
    say "The app was installed, but its desktop shortcut could not be created."
    say "You can still start it with:"
    say "  $APP_LAUNCHER"
fi

say ""
say "Checking the tools used for playback and downloads..."
if ! "$APP_LAUNCHER" doctor; then
    say ""
    say "The app is installed, but one or more required tools are still missing."
    say "Install the items listed above, then open $APP_NAME again."
fi

say ""
say "$APP_NAME is installed."
say "Open it from your application menu, or run:"
say "  $APP_LAUNCHER"
say ""
say "To update later, download a newer setup bundle and run this file again."

if confirm "Start $APP_NAME now?"; then
    if close_running_app; then
        "$APP_LAUNCHER" >/dev/null 2>&1 &
    fi
fi

exit 0
