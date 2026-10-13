"""Record the HDMI feed to a file: a branch that can be attached to a running capture pipeline.

The capture pipeline ends in a `tee` (see GstCapture). Starting a recording adds

    queue (leaky) ! videoconvert ! NV12 ! <encoder> ! parse ! mux ! filesink

to a free tee pad; stopping unlinks it, sends EOS down the branch and waits for the muxer to
finish the file. The preview branch is never touched, and the record queue is leaky so a slow
encoder can never stall the preview (dropped frames are counted instead).
"""

from __future__ import annotations

import logging
import os
import threading
import time

import config

log = logging.getLogger(__name__)

# codec -> encoders in order of preference (hardware first)
ENCODERS = {
    "h264": [("nvh264enc", "NVIDIA NVENC"), ("x264enc", "x264 (CPU)")],
    "h265": [("nvh265enc", "NVIDIA NVENC"), ("x265enc", "x265 (CPU)")],
}
PARSERS = {"h264": "h264parse", "h265": "h265parse"}
EXTENSIONS = {"mkv": "mkv", "mp4": "mp4"}


def _gst():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst


def pick_encoder(codec: str) -> tuple[str, str] | None:
    """(element, label) of the best encoder installed for `codec`, or None."""
    Gst = _gst()
    for element, label in ENCODERS.get(codec, []):
        if Gst.ElementFactory.find(element) is not None:
            return element, label
    return None


def describe(codec: str) -> str:
    found = pick_encoder(codec)
    if found is None:
        return f"no {codec.upper()} encoder installed"
    return found[1]


def _encoder_props(element: str, bitrate_kbps: int) -> str:
    if element.startswith("nv"):
        return f"{element} bitrate={bitrate_kbps} rc-mode=vbr gop-size=60"
    return f"{element} bitrate={bitrate_kbps} speed-preset=superfast key-int-max=60"


def build_description(rc: config.RecordConfig) -> str:
    found = pick_encoder(rc.codec)
    if found is None:
        raise RuntimeError(f"no {rc.codec.upper()} encoder installed (gstreamer1.0-plugins-ugly/bad)")
    element = found[0]
    mux = "matroskamux" if rc.container == "mkv" else "mp4mux fragment-duration=2000"
    return (
        "queue name=rq max-size-buffers=30 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        "videoconvert ! video/x-raw,format=NV12 ! "
        f"{_encoder_props(element, rc.bitrate_mbps * 1000)} ! {PARSERS[rc.codec]} ! "
        f"{mux} ! filesink name=fs"
    )


class StreamRecorder:
    """One recording branch on a pipeline's tee. Not thread-safe: call from the GUI thread."""

    def __init__(self) -> None:
        self.Gst = _gst()
        self._pipeline = None
        self._tee = None
        self._tee_pad = None
        self._bin = None
        self._sink_pad = None
        self._eos = threading.Event()
        self._unlinked = threading.Event()
        self.path = ""
        self.started = 0.0
        self.dropped = 0
        self.encoder = ""

    @property
    def active(self) -> bool:
        return self._bin is not None

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.active else 0.0

    def size(self) -> int:
        try:
            return os.path.getsize(self.path) if self.active else 0
        except OSError:
            return 0

    def start(self, pipeline, path: str, rc: config.RecordConfig) -> None:
        Gst = self.Gst
        if self.active:
            raise RuntimeError("already recording")
        tee = pipeline.get_by_name("t")
        if tee is None:
            raise RuntimeError("pipeline has no tee")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        branch = Gst.parse_bin_from_description(build_description(rc), True)
        fs = branch.get_by_name("fs")
        fs.set_property("location", path)
        self.encoder = pick_encoder(rc.codec)[1]  # type: ignore[index]
        self.dropped = 0
        self._eos.clear()
        self._unlinked.clear()
        branch.get_by_name("rq").connect("overrun", self._on_overrun)
        fs.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_event)

        pipeline.add(branch)
        sink_pad = branch.get_static_pad("sink")
        # Start the file at t=0 rather than at the pipeline's running time.
        clock = pipeline.get_clock()
        if clock is not None:
            sink_pad.set_offset(-(clock.get_time() - pipeline.get_base_time()))
        branch.sync_state_with_parent()
        tee_pad = tee.request_pad_simple("src_%u")
        if tee_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            tee.release_request_pad(tee_pad)
            branch.set_state(Gst.State.NULL)
            pipeline.remove(branch)
            raise RuntimeError("could not attach the record branch")
        self._pipeline, self._tee, self._tee_pad = pipeline, tee, tee_pad
        self._bin, self._sink_pad = branch, sink_pad
        self.path, self.started = path, time.monotonic()
        log.info("stream recording to %s (%s)", path, self.encoder)

    def stop(self, timeout: float = 3.0) -> bool:
        """Finish the file. True if the muxer saw EOS (file is complete)."""
        if not self.active:
            return True
        Gst = self.Gst
        pipeline, tee, tee_pad, branch, sink_pad = (
            self._pipeline, self._tee, self._tee_pad, self._bin, self._sink_pad)

        def detach(pad, _info):
            pad.unlink(sink_pad)
            sink_pad.send_event(Gst.Event.new_eos())
            self._unlinked.set()
            return Gst.PadProbeReturn.REMOVE

        # Normally detach between two frames; if no frames are flowing, do it directly.
        probe = tee_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, detach)
        if not self._unlinked.wait(0.5):
            tee_pad.remove_probe(probe)
            if not self._unlinked.is_set():
                tee_pad.unlink(sink_pad)
                sink_pad.send_event(Gst.Event.new_eos())
        complete = self._eos.wait(timeout)
        if not complete:
            log.warning("recording %s: encoder did not finish in %.0fs", self.path, timeout)
        tee.release_request_pad(tee_pad)
        branch.set_state(Gst.State.NULL)
        pipeline.remove(branch)
        log.info("stream recording stopped: %s (%d MB, %d dropped frames)", self.path,
                 self._final_size() // 2**20, self.dropped)
        self._bin = self._sink_pad = self._tee_pad = self._tee = self._pipeline = None
        return complete

    def _final_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _on_event(self, _pad, info):  # streaming thread
        ev = info.get_event()
        if ev is not None and ev.type == self.Gst.EventType.EOS:
            self._eos.set()
        return self.Gst.PadProbeReturn.OK

    def _on_overrun(self, _queue) -> None:  # streaming thread
        self.dropped += 1
