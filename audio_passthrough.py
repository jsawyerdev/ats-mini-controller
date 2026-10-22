"""Streams the RTL-SDR bridge's live demodulated audio to the browser over
WebSocket. Replaces the old local USB-audio-interface passthrough: an
RTL-SDR has no analog audio output to capture from a sound card - the
demodulated audio only exists as PCM inside this process - so getting it to
the listener now means sending it over the network to be decoded and played
by the browser's Web Audio API, instead of copying it to a local speaker.

Unlike the old passthrough, this needs no exclusive access to anything: any
number of browser tabs can subscribe at once, and it runs alongside the CW
monitor without contention, since both just read the same underlying stream.

Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from radio_bridge import RtlSdrBridge

logger = logging.getLogger(__name__)


async def serve_audio_websocket(websocket: WebSocket, bridge: RtlSdrBridge) -> None:
    """Accept a browser's WebSocket connection and push it 16-bit mono PCM
    frames at radio_bridge.AUDIO_RATE until it disconnects."""
    await websocket.accept()
    loop = asyncio.get_running_loop()
    subscriber_id, queue = bridge.subscribe_audio(loop)
    try:
        while True:
            chunk = await queue.get()
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        logger.debug("audio websocket client disconnected")
    finally:
        bridge.unsubscribe_audio(subscriber_id)
