"""Live audio passthrough: copies the USB audio interface's input (the ATS
Mini's headphone-out) straight to the Mac's speakers in real time, so
whatever the radio is tuned to is audible without touching anything else -
retuning the radio (e.g. clicking a station) just changes what comes out,
since this is a dumb continuous copy, not something that needs to know
about frequencies at all.

Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
"""

from __future__ import annotations

import logging

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioPassthrough:
    def __init__(self, input_device: int, output_device: int | None = None):
        self._input_device = input_device
        self._output_device = output_device
        self._stream: sd.Stream | None = None

    def _callback(self, indata: np.ndarray, outdata: np.ndarray, frames, time_info, status) -> None:
        if status:
            logger.debug("audio passthrough stream status: %s", status)
        # Input is mono (1 channel from the USB adapter); broadcast it to
        # however many output channels the speakers expect (usually 2).
        outdata[:] = np.repeat(indata, outdata.shape[1], axis=1)

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.Stream(
            device=(self._input_device, self._output_device),
            channels=(1, 2),
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def running(self) -> bool:
        return self._stream is not None
