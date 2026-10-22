# fpm-based builder for ats-mini-controller native packages.
# Usage:
#   packaging/build-packages.sh [--version 2.12.0] [--arch amd64|arm64]
# Requires: fpm (gem install fpm), python3-venv, dpkg-deb (for .deb checks).
#
# The single source of truth for the version is app.py (APP_VERSION).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
if [ -z "$VERSION" ] || [ "$VERSION" = "--version" ]; then
    VERSION="$(python3 -c "import re; print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('app.py').read()).group(1))")"
fi
# Allow: build-packages.sh --version X.Y.Z  or  build-packages.sh X.Y.Z
if [ "${1:-}" = "--version" ]; then VERSION="${2:-$VERSION}"; fi
ARCH_FLAG="${3:-}"
UNAME_M="$(uname -m)"
case "$UNAME_M" in
    x86_64)  DEB_ARCH="amd64"; RPM_ARCH="x86_64" ;;
    aarch64) DEB_ARCH="arm64"; RPM_ARCH="aarch64" ;;
    armv7l)  DEB_ARCH="armhf"; RPM_ARCH="armhfp" ;;
    *)       DEB_ARCH="amd64"; RPM_ARCH="x86_64" ;;
esac
if [ -n "$ARCH_FLAG" ]; then
    case "$ARCH_FLAG" in
        amd64|x86_64) DEB_ARCH="amd64"; RPM_ARCH="x86_64" ;;
        arm64|aarch64) DEB_ARCH="arm64"; RPM_ARCH="aarch64" ;;
        armhf|armv7l) DEB_ARCH="armhf"; RPM_ARCH="armhfp" ;;
    esac
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP_DIR="$STAGE/opt/ats-mini-controller"

echo "Building ats-mini-controller $VERSION ($DEB_ARCH / $RPM_ARCH) ..."

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --no-cache-dir -r requirements.txt

# Application payload (keep image lean: no raw FCC dumps, recordings, caches).
mkdir -p "$APP_DIR/static" "$APP_DIR/data/recordings"
cp app.py radio_bridge.py cw_decoder.py cw_monitor.py audio_passthrough.py stations_db.py "$APP_DIR/"
cp -r static/index.html "$APP_DIR/static/"
cp data/stations.db "$APP_DIR/data/"
touch "$APP_DIR/data/.packaged"

mkdir -p "$STAGE/lib/systemd/system" "$STAGE/lib/udev/rules.d" "$STAGE/etc/modprobe.d" "$STAGE/usr/bin"
cp packaging/ats-mini-controller.service "$STAGE/lib/systemd/system/"
cp packaging/66-rtl-sdr.rules "$STAGE/lib/udev/rules.d/"
cp packaging/blacklist-rtlsdr.conf "$STAGE/etc/modprobe.d/blacklist-rtlsdr.conf"
printf '#!/bin/sh\nexec /opt/ats-mini-controller/venv/bin/python -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8731}"\n' > "$STAGE/usr/bin/ats-mini-controller"
chmod 755 "$STAGE/usr/bin/ats-mini-controller" packaging/postinst packaging/prerm
mkdir -p dist

if command -v fpm >/dev/null 2>&1; then
    fpm -s dir -t deb -C "$STAGE" \
        -n ats-mini-controller -v "$VERSION" -a "$DEB_ARCH" \
        --description "RTL-SDR station browser, signal scanner and CW decoder" \
        --url "https://github.com/jsawyerdev/ats-mini-controller" \
        --license MIT \
        --depends libusb-1.0-0 --depends python3 \
        --after-install packaging/postinst --before-remove packaging/prerm \
        --deb-systemd packaging/ats-mini-controller.service \
        -p "dist/ats-mini-controller_${VERSION}_${DEB_ARCH}.deb" \
        opt lib usr etc
    fpm -s dir -t rpm -C "$STAGE" \
        -n ats-mini-controller -v "$VERSION" -a "$RPM_ARCH" \
        --description "RTL-SDR station browser, signal scanner and CW decoder" \
        --url "https://github.com/jsawyerdev/ats-mini-controller" \
        --license MIT \
        --depends libusb1 --depends python3 \
        --after-install packaging/postinst --before-remove packaging/prerm \
        -p "dist/ats-mini-controller-${VERSION}-1.${RPM_ARCH}.rpm" \
        opt lib usr etc
    echo "Wrote dist/:"
    ls -lh dist/
else
    echo "fpm not found (gem install fpm). Staging tree left at: $STAGE"
    echo "Install fpm and re-run to emit .deb/.rpm into dist/."
    trap - EXIT
    echo "$STAGE"
fi
