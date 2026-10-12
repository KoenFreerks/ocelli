"""Capture backends that deliver QVideoFrames from a Cam Link.

Two interchangeable backends:
  GstCapture - GStreamer v4l2src -> appsink, frames copied into QVideoFrame
  QtCapture  - Qt Multimedia QCamera (FFmpeg backend)
Both emit `frame(QVideoFrame)` on the GUI thread and report state changes, and
both retry on their own when the device disappears and comes back.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QSize, QTimer, Signal
from PySide6.QtMultimedia import (
    QCamera,
    QMediaCaptureSession,
    QMediaDevices,
    QVideoFrame,
    QVideoFrameFormat,
    QVideoSink,
)

from . import devices

log = logging.getLogger(__name__)

PF = QVideoFrameFormat.PixelFormat
# our name -> (GStreamer caps format, Qt pixel format)
FORMATS = {
    "YUYV": ("YUY2", PF.Format_YUYV),
    "NV12": ("NV12", PF.Format_NV12),
}

NO_SIGNAL_AFTER = 2.0  # seconds without a frame
RETRY_EVERY = 2.0


@dataclass
class Stats:
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    fps: float = 0.0
    frames: int = 0
    latency_ms: float | None = None  # capture -> app, GStreamer only


class Capture(QObject):
    frame = Signal(object)  # QVideoFrame
    # "starting", "streaming", "no-signal", "missing", "error", "stopped"
    stateChanged = Signal(str, str)

    def __init__(self, by_id: str, width: int = 1920, height: int = 1080, fps: int = 60,
                 pixel_format: str = "YUYV", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.by_id = by_id
        self.width, self.height, self.fps = width, height, fps
        self.pixel_format = pixel_format
        self.state = "stopped"
        self._want_running = False
        self._times: collections.deque[float] = collections.deque(maxlen=240)
        self._last_frame = 0.0
        self._latencies: collections.deque[float] = collections.deque(maxlen=120)
        self._stats = Stats()
        self._watchdog = QTimer(self, interval=500, timeout=self._check)

    # --- public ---

    def start(self) -> None:
        self._want_running = True
        self._watchdog.start()
        self._try_open()

    def stop(self) -> None:
        self._want_running = False
        self._watchdog.stop()
        self._close()
        self._set_state("stopped")

    def stats(self) -> Stats:
        now = time.monotonic()
        recent = [t for t in self._times if now - t <= 1.0]
        self._stats.fps = float(len(recent)) if recent and now - recent[0] > 0.5 else 0.0
        lat = list(self._latencies)
        self._stats.latency_ms = sum(lat) / len(lat) if lat else None
        return self._stats

    def latency_samples(self) -> list[float]:
        return list(self._latencies)

    # --- for subclasses ---

    def _open(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    def _frame_arrived(self, frame: QVideoFrame, latency_ms: float | None = None) -> None:
        if not self._want_running:
            return  # a queued frame that arrived after stop()
        now = time.monotonic()
        self._times.append(now)
        self._last_frame = now
        if latency_ms is not None:
            self._latencies.append(latency_ms)
        s = self._stats
        s.frames += 1
        s.width, s.height = frame.width(), frame.height()
        s.pixel_format = frame.pixelFormat().name.removeprefix("Format_")
        if self.state != "streaming":
            self._set_state("streaming", f"{s.width}x{s.height}")
        self.frame.emit(frame)

    def _failed(self, detail: str) -> None:
        log.warning("%s: %s", self.by_id, detail)
        self._close()
        self._set_state("error", detail)

    # --- internals ---

    def _set_state(self, state: str, detail: str = "") -> None:
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state, detail)

    def _try_open(self) -> None:
        if devices.resolve(self.by_id) is None:
            self._set_state("missing", self.by_id)
            return
        self._last_frame = time.monotonic()
        self._set_state("starting")
        try:
            self._open()
        except Exception as e:  # backend-specific failures
            self._failed(str(e))

    def _check(self) -> None:
        if not self._want_running:
            return
        if self.state in ("missing", "error", "stopped"):
            if time.monotonic() - self._last_frame >= RETRY_EVERY:
                self._last_frame = time.monotonic()
                self._try_open()
            return
        if devices.resolve(self.by_id) is None:
            self._close()
            self._set_state("missing", self.by_id)
            return
        if time.monotonic() - self._last_frame > NO_SIGNAL_AFTER:
            self._set_state("no-signal", "no frames: camera off or HDMI out?")


class GstCapture(Capture):
    backend = "gstreamer"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        Gst.init(None)
        self._Gst, self._GstVideo = Gst, GstVideo
        self._pipeline = None
        self._bus_timer = QTimer(self, interval=100, timeout=self._poll_bus)

    def _open(self) -> None:
        Gst = self._Gst
        gst_fmt, self._qt_fmt = FORMATS[self.pixel_format]
        desc = (
            f'v4l2src device="{self.by_id}" ! '
            f"video/x-raw,format={gst_fmt},width={self.width},height={self.height},"
            f"framerate={self.fps}/1 ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(desc)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to start (device busy or unsupported format?)")
        self._bus_timer.start()

    def _close(self) -> None:
        self._bus_timer.stop()
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None

    def _on_sample(self, sink) -> int:
        # GStreamer streaming thread
        Gst, GstVideo = self._Gst, self._GstVideo
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        latency = None
        pipeline = self._pipeline
        if pipeline is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            clock = pipeline.get_clock()
            if clock is not None:
                running = clock.get_time() - pipeline.get_base_time()
                latency = (running - buf.pts) / 1e6
        frame = QVideoFrame(QVideoFrameFormat(QSize(info.width, info.height), self._qt_fmt))
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            if frame.map(QVideoFrame.MapMode.WriteOnly):
                src = mapinfo.data
                for plane in range(frame.planeCount()):
                    dst = frame.bits(plane)
                    dst_stride = frame.bytesPerLine(plane)
                    src_stride = info.stride[plane]
                    off = info.offset[plane]
                    rows = info.height if plane == 0 else info.height // 2
                    if dst_stride == src_stride:
                        n = rows * dst_stride
                        dst[:n] = src[off:off + n]
                    else:
                        w = min(dst_stride, src_stride)
                        for r in range(rows):
                            dst[r * dst_stride:r * dst_stride + w] = \
                                src[off + r * src_stride:off + r * src_stride + w]
                frame.unmap()
        finally:
            buf.unmap(mapinfo)
        # Hand over to the GUI thread (queued: this QObject lives there).
        self._sampleReady.emit(frame, latency)
        return Gst.FlowReturn.OK

    _sampleReady = Signal(object, object)

    def start(self) -> None:
        if not getattr(self, "_connected", False):
            self._sampleReady.connect(self._frame_arrived)
            self._connected = True
        super().start()

    def _poll_bus(self) -> None:
        if self._pipeline is None:
            return
        Gst = self._Gst
        bus = self._pipeline.get_bus()
        while True:
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                return
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                log.debug("gst error debug: %s", dbg)
                self._failed(err.message)
            else:
                self._failed("end of stream")
            return


class QtCapture(Capture):
    backend = "qt"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._camera: QCamera | None = None
        self._session = QMediaCaptureSession(self)
        self._sink = QVideoSink(self)
        self._session.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)

    def _open(self) -> None:
        node = devices.resolve(self.by_id)
        dev = next((d for d in QMediaDevices.videoInputs() if bytes(d.id()).decode() == node), None)
        if dev is None:
            raise RuntimeError(f"Qt does not list {node}")
        _, qt_fmt = FORMATS[self.pixel_format]
        fmt = next((f for f in dev.videoFormats()
                    if f.resolution() == QSize(self.width, self.height)
                    and round(f.maxFrameRate()) == self.fps and f.pixelFormat() == qt_fmt), None)
        if fmt is None:
            raise RuntimeError(f"no {self.width}x{self.height}@{self.fps} {self.pixel_format} format")
        self._camera = QCamera(dev, self)
        self._camera.setCameraFormat(fmt)
        self._camera.errorOccurred.connect(lambda _e, text: self._failed(text))
        self._session.setCamera(self._camera)
        self._camera.start()

    def _close(self) -> None:
        if self._camera is not None:
            self._camera.stop()
            self._session.setCamera(None)
            self._camera.deleteLater()
            self._camera = None

    def _on_frame(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            self._frame_arrived(frame)


BACKENDS = {"gst": GstCapture, "qt": QtCapture}
