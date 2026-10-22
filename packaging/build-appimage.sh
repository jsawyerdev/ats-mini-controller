#!/usr/bin/env bash
# Build the ats-mini-controller AppImage.
# Usage: packaging/build-appimage.sh [--version 2.12.0]
# Requires: python3-venv, appimagetool (auto-downloaded if missing).
#
# NOTE: the AppImage bundles the Python app but cannot install system
# integration (udev rules, modprobe blacklist, systemd unit). The host
# still needs the one-time setup from README / install.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(python3 -c "import re; print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('app.py').read()).group(1))")"
if [ "${1:-}" = "--version" ]; then VERSION="${2:-$VERSION}"; fi
UNAME_M="$(uname -m)"
case "$UNAME_M" in
    x86_64) APPIMAGE_ARCH="x86_64" ;;
    aarch64) APPIMAGE_ARCH="aarch64" ;;
    *) APPIMAGE_ARCH="x86_64" ;;
esac

APPDIR="dist/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/opt/ats-mini-controller/static" "$APPDIR/opt/ats-mini-controller/data" "$APPDIR/usr/bin"

python3 -m venv "$APPDIR/opt/ats-mini-controller/venv"
"$APPDIR/opt/ats-mini-controller/venv/bin/pip" install --no-cache-dir -r requirements.txt
cp app.py radio_bridge.py cw_decoder.py cw_monitor.py audio_passthrough.py stations_db.py "$APPDIR/opt/ats-mini-controller/"
cp static/index.html "$APPDIR/opt/ats-mini-controller/static/"
cp data/stations.db "$APPDIR/opt/ats-mini-controller/data/"
cp packaging/ats-mini-controller.desktop "$APPDIR/"
cp packaging/AppRun "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"
# Minimal icon placeholder (appimagetool requires one); packagers can replace.
printf '\x89PNG\r\n\x1a\n' > "$APPDIR/ats-mini-controller.png" || true

if ! command -v appimagetool >/dev/null 2>&1; then
    echo "Downloading appimagetool ..."
    mkdir -p dist
    curl -fsSL -o dist/appimagetool "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${APPIMAGE_ARCH}.AppImage"
    chmod +x dist/appimagetool
    APPIMAGETOOL="dist/appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

mkdir -p dist
"$APPIMAGETOOL" "$APPDIR" "dist/ats-mini-controller-${VERSION}-${APPIMAGE_ARCH}.AppImage"
ls -lh dist/*.AppImage
