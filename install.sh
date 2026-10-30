#!/bin/sh
# Installs Custom Resolution Panel. Usage: sudo ./install.sh [--remove]
#
# The app runs straight from this folder; this script only installs what needs
# root (the helper and the udev rule) plus the menu entry.
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
ID=io.github.wellfz.CustomResolutionPanel
HELPER=/usr/local/libexec/custom-resolution-panel-helper
RULE=/etc/udev/rules.d/61-custom-resolution-panel.rules
BIN=/usr/local/bin/custom-resolution-panel
DESKTOP=/usr/local/share/applications/$ID.desktop
ICON=/usr/local/share/icons/hicolor/scalable/apps/$ID.svg

if [ "$(id -u)" -ne 0 ]; then
    echo "run it with sudo" >&2
    exit 1
fi

if [ "$1" = "--remove" ]; then
    rm -f "$HELPER" "$RULE" "$BIN" "$DESKTOP" "$ICON"
    udevadm control --reload
    echo "Removed. Saved EDIDs (/usr/lib/firmware/edid/custom-resolution-panel-*) and"
    echo "drm.edid_firmware in GRUB are left alone; use \"Restore\" in the app before removing."
    exit 0
fi

install -D -o root -g root -m 755 helper/custom-resolution-panel-helper "$HELPER"
install -D -o root -g root -m 644 helper/61-custom-resolution-panel.rules "$RULE"
install -D -o root -g root -m 644 "data/$ID.desktop" "$DESKTOP"
install -D -o root -g root -m 644 "data/$ID.svg" "$ICON"
ln -sf "$SRC/custom-resolution-panel" "$BIN"
udevadm control --reload
"$HELPER" sync-service
gtk-update-icon-cache -q -t /usr/local/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/local/share/applications 2>/dev/null || true
echo "Installed. Open \"Custom Resolution Panel\" from the app menu or run: custom-resolution-panel"
