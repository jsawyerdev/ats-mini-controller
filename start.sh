#!/usr/bin/env bash
# Starts the RTL-SDR station browser for local development. The intended
# deployment path is Docker (see compose.yaml); this script is for running
# it directly on a host that already has the RTL-SDR driver stack set up
# (librtlsdr installed, kernel DVB drivers blacklisted - see README).
#
# Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
set -euo pipefail
cd "$(dirname "$0")"

HOST="127.0.0.1"
HTTP_PORT="8731"

if ! python3 -c "from rtlsdr import RtlSdr; RtlSdr().close()" >/dev/null 2>&1; then
    echo "Warning: no RTL-SDR dongle detected (or driver permissions aren't set up - see README). Starting anyway; tuning will fail until it's connected." >&2
fi

echo "Starting RTL-SDR station browser at http://$HOST:$HTTP_PORT"
( sleep 2 && open "http://$HOST:$HTTP_PORT" ) &
exec python3 -m uvicorn app:app --host "$HOST" --port "$HTTP_PORT"
