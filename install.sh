#!/usr/bin/env bash

# The final call keeps an incomplete download from starting the installation.
main() {
    set -euo pipefail

    local -a options=(--no-integrate)
    local arg integrate=false start=true
    for arg in "$@"; do
        case "$arg" in
            --integrate) integrate=true ;;
            --no-start) start=false ;;
            -h|--help)
                printf '%s\n' 'Usage: bash install.sh [--integrate] [--no-start]'
                return
                ;;
            *) fail "Unknown option: $arg" ;;
        esac
    done
    if "$integrate"; then
        options=()
    fi
    if ! "$start" || [[ -z ${WAYLAND_DISPLAY:-} ]]; then
        options+=(--no-start)
    fi

    [[ $(uname -s) == Linux ]] || fail 'Luch requires Linux.'
    (( EUID != 0 )) || fail 'Run as your regular user. The installer uses sudo for system packages if needed.'
    for arg in curl tar flock; do
        command -v "$arg" >/dev/null || fail "Install $arg and run this command again."
    done

    if ! check_runtime 2>/dev/null; then
        install_dependencies
        check_runtime || fail 'Luch needs system Python 3.11+, PyGObject, Cairo, GTK 3 and GTK Layer Shell.'
    fi

    local root=${XDG_DATA_HOME:-"$HOME/.local/share"}/luch
    local source="$root/source"
    mkdir -p -- "$root"
    local lock
    exec {lock}>"$root/install.lock"
    flock -n "$lock" || fail 'Another Luch installation is already running.'

    if [[ -e $source || -L $source ]]; then
        [[ ! -L $source && -f $source/.luch-managed ]] || fail "$source already exists and is not managed by this installer."
    fi

    local stage cleanup_command
    stage=$(mktemp -d "$root/.install.XXXXXXXX")
    printf -v cleanup_command 'cleanup_install "$?" %q %q' "$stage" "$source"
    trap "$cleanup_command" EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    curl --fail --silent --show-error --location --retry 2 \
        'https://codeload.github.com/butukay/luch/tar.gz/refs/heads/master' \
        --output "$stage/source.tar.gz"
    mkdir -- "$stage/source"
    tar -xzf "$stage/source.tar.gz" -C "$stage/source" --strip-components=1
    [[ -x $stage/source/bin/luch && -f $stage/source/scripts/install ]] ||
        fail 'The downloaded archive is missing Luch files.'
    touch -- "$stage/source/.luch-managed"

    if [[ -d $source ]]; then
        mv -T -- "$source" "$stage/previous"
    fi
    mv -T -- "$stage/source" "$source"
    /usr/bin/python3 -I "$source/scripts/install" "${options[@]}"
    printf '\nOpen Luch with "%s toggle" in a compatible Wayland session.\n' "$HOME/.local/bin/luch"
    cleanup_install 0 "$stage" "$source"
    trap - EXIT INT TERM
}

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

check_runtime() {
    /usr/bin/python3 -I - <<'PYTHON'
import sys

if sys.version_info < (3, 11):
    sys.exit('Python 3.11 or newer is required.')

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('GtkLayerShell', '0.1')
gi.require_foreign('cairo')
from gi.repository import Gtk, GtkLayerShell
PYTHON
}

install_dependencies() {
    local ID= ID_LIKE= PRETTY_NAME=
    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
    fi
    if [[ -x /usr/bin/python3 ]] &&
        ! /usr/bin/python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        fail "${PRETTY_NAME:-This distribution} provides an older system Python. Luch requires Python 3.11 or newer."
    fi

    local -a setup=() install
    case " $ID $ID_LIKE " in
        *' arch '*)
            install=(pacman -S --needed --noconfirm python python-gobject python-cairo gtk3 gtk-layer-shell librsvg)
            ;;
        *' debian '*|*' ubuntu '*)
            setup=(apt-get update)
            install=(apt-get install -y --no-install-recommends python3 python3-gi python3-gi-cairo
                gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1 librsvg2-common)
            ;;
        *' fedora '*)
            install=(dnf install -y python3 python3-gobject python3-cairo gtk3 gtk-layer-shell librsvg2)
            ;;
        *)
            fail 'Install system Python 3.11+, PyGObject, Cairo, GTK 3 and GTK Layer Shell, then run this command again.'
            ;;
    esac
    command -v "${install[0]}" >/dev/null || fail "Package manager unavailable: ${install[0]}"
    command -v sudo >/dev/null || fail "Install the system packages as an administrator: ${install[*]}"
    if (( ${#setup[@]} )); then
        sudo "${setup[@]}" </dev/null
    fi
    sudo "${install[@]}" </dev/null
}

cleanup_install() {
    local status=$1 stage=$2 source=$3
    if (( status != 0 )) && [[ -d $stage/previous ]]; then
        if [[ -d $source ]]; then
            mv -T -- "$source" "$stage/failed" ||
                fail "Previous installation is saved at $stage/previous"
        fi
        mv -T -- "$stage/previous" "$source" ||
            fail "Previous installation is saved at $stage/previous"
    fi
    rm -rf -- "$stage"
}

main "$@"
