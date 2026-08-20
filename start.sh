#!/usr/bin/env bash
# Starts the ATS Mini controller. Run this instead of invoking uvicorn
# directly: it clears a stale process holding the serial port first, which
# otherwise fails silently with "device reports readiness to read but
# returned no data" or an empty /api/status.
#
# Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
set -euo pipefail
cd "$(dirname "$0")"

HOST="127.0.0.1"
HTTP_PORT="8731"

# Kill any previous instance of this app, by process pattern rather than by
# which serial port it's holding: the ATS Mini's /dev/cu.usbmodemNNNN path
# changes across reconnects (the app now auto-detects it by USB vendor ID
# instead of a fixed path), so that's not a reliable thing to check here.
stale_pids="$(pgrep -f "uvicorn app:app.*--port $HTTP_PORT" || true)"
if [ -n "$stale_pids" ]; then
    echo "Killing stale server (pid $(echo $stale_pids | tr '\n' ' '))"
    kill -9 $stale_pids
    sleep 1.5
fi

if ! ls /dev/cu.usbmodem* >/dev/null 2>&1; then
    echo "Warning: no USB serial device found. Is the ATS Mini plugged in? Starting anyway; tuning will fail until it's connected." >&2
fi

echo "Starting ATS Mini controller at http://$HOST:$HTTP_PORT"
( sleep 2 && open "http://$HOST:$HTTP_PORT" ) &
exec python3 -m uvicorn app:app --host "$HOST" --port "$HTTP_PORT"
