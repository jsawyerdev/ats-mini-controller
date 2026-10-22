#!/usr/bin/env bash
# One-line installer for ats-mini-controller (RTL-SDR station browser).
#
#   curl -fsSL https://raw.githubusercontent.com/jsawyerdev/ats-mini-controller/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --port 8731 --no-reboot
#
# What it does:
#   1. Detects apt (.deb) vs dnf/zypper (.rpm) and installs the matching
#      GitHub Release asset; falls back to pip+systemd on other distros.
#   2. Installs udev rules + DVB modprobe blacklist so librtlsdr can claim
#      the dongle (needs one reboot afterwards on first install).
#   3. Enables the ats-mini-controller systemd service on port 8731.
set -euo pipefail

REPO="${REPO:-jsawyerdev/ats-mini-controller}"
PORT="${PORT:-8731}"
VERSION="${VERSION:-latest}"
NEED_REBOOT=1
MANUAL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:-8731}"; shift 2 ;;
        --version) VERSION="${2:-latest}"; shift 2 ;;
        --no-reboot) NEED_REBOOT=0; shift ;;
        --manual) MANUAL=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  DEB_ARCH="amd64"; RPM_ARCH="x86_64" ;;
    aarch64) DEB_ARCH="arm64"; RPM_ARCH="aarch64" ;;
    armv7l|armv6l) DEB_ARCH="armhf"; RPM_ARCH="armhfp" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

release_tag() {
    if [ "$VERSION" != "latest" ]; then echo "$VERSION"; return; fi
    curl -fsSL -o /dev/null -w "%{url_effective}" \
        "https://github.com/${REPO}/releases/latest" | sed 's#.*/tag/##'
}

install_pkg() {
    TAG="$(release_tag)"
    echo "Installing ats-mini-controller ${TAG} for ${ARCH} ..."
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    if have apt-get && [ "$MANUAL" -eq 0 ]; then
        URL="https://github.com/${REPO}/releases/download/${TAG}/ats-mini-controller_${TAG#v}_${DEB_ARCH}.deb"
        curl -fsSL -o "$TMP/pkg.deb" "$URL"
        $SUDO apt-get update
        $SUDO apt-get install -y "$TMP/pkg.deb" rtl-sdr libusb-1.0-0
    elif (have dnf || have zypper) && [ "$MANUAL" -eq 0 ]; then
        URL="https://github.com/${REPO}/releases/download/${TAG}/ats-mini-controller-${TAG#v}-1.${RPM_ARCH}.rpm"
        curl -fsSL -o "$TMP/pkg.rpm" "$URL"
        if have dnf; then $SUDO dnf install -y "$TMP/pkg.rpm" rtl-sdr libusb1
        else $SUDO zypper install -y "$TMP/pkg.rpm" rtl-sdr libusb-1_0-0; fi
    else
        echo "No supported package manager asset; falling back to pip install."
        $SUDO apt-get update 2>/dev/null || true
        $SUDO apt-get install -y python3-venv rtl-sdr libusb-1.0-0 2>/dev/null || \
        $SUDO dnf install -y python3 rtl-sdr libusb1 2>/dev/null || true
        $SUDO mkdir -p /opt/ats-mini-controller /etc/ats-mini-controller
        $SUDO chown -R "$(id -u):$(id -g)" /opt/ats-mini-controller
        SRC="$(mktemp -d)"
        curl -fsSL "https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz" | tar -xz -C "$SRC" --strip-components=1
        python3 -m venv /opt/ats-mini-controller/venv
        /opt/ats-mini-controller/venv/bin/pip install --no-cache-dir -r "$SRC/requirements.txt"
        cp "$SRC"/app.py "$SRC"/radio_bridge.py "$SRC"/cw_decoder.py "$SRC"/cw_monitor.py \
           "$SRC"/audio_passthrough.py "$SRC"/stations_db.py /opt/ats-mini-controller/
        mkdir -p /opt/ats-mini-controller/static /opt/ats-mini-controller/data/recordings
        cp "$SRC/static/index.html" /opt/ats-mini-controller/static/
        cp "$SRC/data/stations.db" /opt/ats-mini-controller/data/ 2>/dev/null || true
        $SUDO cp "$SRC/packaging/ats-mini-controller.service" /lib/systemd/system/
        $SUDO cp "$SRC/packaging/66-rtl-sdr.rules" /lib/udev/rules.d/
        $SUDO cp "$SRC/packaging/blacklist-rtlsdr.conf" /etc/modprobe.d/blacklist-rtlsdr.conf
        $SUDO sh "$SRC/packaging/postinst"
        rm -rf "$SRC" "$TMP"
    fi
    # Ensure the configured port is picked up (service defaults to 8731).
    if [ "$PORT" != "8731" ]; then
        echo "PORT=$PORT" | $SUDO tee /etc/ats-mini-controller/env >/dev/null
        $SUDO systemctl restart ats-mini-controller.service || true
    fi
}

install_pkg

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "Done. Open http://${IP:-<host>:}:${PORT} on your LAN."
if [ "$NEED_REBOOT" -eq 1 ]; then
    echo "First install usually needs one reboot to release the DVB kernel modules."
fi
