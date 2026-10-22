Name:           ats-mini-controller
Version:        2.12.0
Release:        1%{?dist}
Summary:        RTL-SDR station browser, signal scanner and CW decoder
License:        MIT
URL:            https://github.com/jsawyerdev/ats-mini-controller
BuildArch:      x86_64

Requires:       python3, libusb1, rtl-sdr

%description
Web app that tunes an RTL-SDR USB dongle in software, with a searchable
station database, live signal scanning and an automatic Morse (CW) decoder.

%install
APP_DIR=%{buildroot}/opt/ats-mini-controller
mkdir -p $APP_DIR/static $APP_DIR/data/recordings
mkdir -p %{buildroot}/lib/systemd/system %{buildroot}/lib/udev/rules.d
mkdir -p %{buildroot}/etc/modprobe.d %{buildroot}/usr/bin
python3 -m venv $APP_DIR/venv
$APP_DIR/venv/bin/pip install --no-cache-dir -r %{_sourcedir}/requirements.txt
cp %{_sourcedir}/app.py %{_sourcedir}/radio_bridge.py %{_sourcedir}/cw_decoder.py \
   %{_sourcedir}/cw_monitor.py %{_sourcedir}/audio_passthrough.py \
   %{_sourcedir}/stations_db.py $APP_DIR/
cp %{_sourcedir}/static/index.html $APP_DIR/static/
cp %{_sourcedir}/data/stations.db $APP_DIR/data/
cp %{_sourcedir}/packaging/ats-mini-controller.service %{buildroot}/lib/systemd/system/
cp %{_sourcedir}/packaging/66-rtl-sdr.rules %{buildroot}/lib/udev/rules.d/
cp %{_sourcedir}/packaging/blacklist-rtlsdr.conf %{buildroot}/etc/modprobe.d/blacklist-rtlsdr.conf
printf '#!/bin/sh\nexec /opt/ats-mini-controller/venv/bin/python -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8731}"\n' > %{buildroot}/usr/bin/ats-mini-controller
chmod 755 %{buildroot}/usr/bin/ats-mini-controller

%post
sh %{_sourcedir}/packaging/postinst || true

%preun
sh %{_sourcedir}/packaging/prerm || true

%files
/opt/ats-mini-controller/
/lib/systemd/system/ats-mini-controller.service
/lib/udev/rules.d/66-rtl-sdr.rules
/etc/modprobe.d/blacklist-rtlsdr.conf
/usr/bin/ats-mini-controller

%changelog
* Thu Oct 22 2026 JSLabs <james@jamessawyer.co.uk> - 2.12.0-1
- Native systemd/udev packaging for .rpm installs.
