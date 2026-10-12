#!/bin/sh
# Adds Ocelli to the desktop's app menu (per user, no sudo). Re-run after moving the folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/ocelli.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Ocelli
Comment=Monitor and control two Blackmagic Pocket 4K cameras
Exec="$DIR/.venv/bin/python" "$DIR/main.py"
Path=$DIR
Icon=$DIR/assets/ocelli.svg
Terminal=false
Categories=AudioVideo;Video;
Keywords=camera;blackmagic;microscope;record;
StartupWMClass=ocelli
StartupNotify=true
Actions=fullscreen;

[Desktop Action fullscreen]
Name=Start fullscreen
Exec="$DIR/.venv/bin/python" "$DIR/main.py" --fullscreen
DESKTOP
command -v desktop-file-validate >/dev/null && desktop-file-validate "$APPS/ocelli.desktop" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed $APPS/ocelli.desktop"
