"""One camera, as the GUI sees it: BLE link + video capture + current status.

All BLE work runs as asyncio tasks on the qasync loop; nothing here blocks the
Qt event loop. Widgets read `status` and listen to `changed`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

import config
from ble import protocol as p
from ble.client import CameraLink, maintain
from video.capture import BACKENDS, Capture

log = logging.getLogger(__name__)

# One lock per Bluetooth adapter: cameras sharing an adapter never connect at the
# same time. A camera with its own adapter (config) gets its own lock.
_adapter_locks: dict[str, asyncio.Lock] = {}


def adapter_lock(adapter: str) -> asyncio.Lock:
    return _adapter_locks.setdefault(adapter, asyncio.Lock())


@dataclass
class CameraStatus:
    ble: str = "off"  # off, connecting, connected, waiting, lost, stale, failed
    ble_detail: str = ""
    model: str = ""
    recording: bool = False
    rec_pending: str = ""  # "start" / "stop" while waiting for the camera to confirm
    stream_rec: bool = False  # recording the HDMI feed to a file on this PC
    stream_file: str = ""
    transport: p.TransportInfo | None = None
    timecode: str = ""
    iso: int | None = None
    wb_kelvin: int | None = None
    wb_tint: int | None = None
    shutter_angle: float | None = None  # degrees
    shutter_speed: int | None = None  # 1/x s
    aperture: float | None = None  # f-number; None = unknown
    has_lens: bool | None = None
    focus: float | None = None
    codec: str = ""
    rec_format: str = ""
    battery: p.Battery | None = None
    media: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    last_sync: datetime | None = None


class CameraController(QObject):
    changed = Signal()
    alert = Signal(str, str)  # level ("info"/"warn"/"error"), text

    def __init__(self, cid: str, cfg: config.AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cid = cid
        self.cfg = cfg
        self.status = CameraStatus()
        self.link: CameraLink | None = None
        self.capture: Capture | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._was_connected = False
        self._rec_timer = QTimer(self, interval=1000, timeout=self.changed.emit)  # elapsed/size
        self._make_capture()

    @property
    def name(self) -> str:
        return f"Camera {self.cid}"

    @property
    def cam_cfg(self) -> config.CameraConfig:
        return self.cfg.cameras[self.cid]

    # --- lifecycle -------------------------------------------------------------------

    def _make_capture(self) -> None:
        pv = self.cfg.preview
        if self.cam_cfg.video_device:
            self.capture = BACKENDS[pv.backend](self.cam_cfg.video_device, pv.width, pv.height,
                                                pv.fps, pv.pixel_format, parent=self)
            self.capture.stateChanged.connect(self._on_video_state)
            self.capture.recordingChanged.connect(self._on_stream_recording)
            self.capture.recordingProblem.connect(lambda t: self.alert.emit("warn", f"{self.name}: {t}"))

    def start(self) -> None:
        if self.capture:
            self.capture.start()
        if self.cam_cfg.ble_address:
            self._stop.clear()
            self.link = CameraLink(self.cam_cfg.ble_address, self.cam_cfg.ble_adapter,
                                   self.cfg.ble.host_name)
            self.link.on_command = self._on_command
            self.link.on_timecode = self._on_timecode
            self._task = asyncio.ensure_future(self._run_ble())

    async def stop(self) -> None:
        if self.capture:
            self.capture.stop()
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run_ble(self) -> None:
        b = self.cfg.ble
        try:
            await maintain(
                self.link, self._stop,
                timeout=b.connect_timeout,
                initial_delay=b.retry_initial,
                max_delay=b.retry_max,
                on_state=self._on_ble_state,
                connect_lock=adapter_lock(self.cam_cfg.ble_adapter),
                settle=b.stagger_seconds,
                stale_after=b.stale_seconds,
                after_connect=self._after_connect if b.auto_sync_time else None,
            )
        except Exception as e:  # keep the GUI alive whatever happens
            log.exception("%s BLE task crashed", self.name)
            self.alert.emit("error", f"{self.name}: Bluetooth task stopped ({e})")

    async def _after_connect(self, link: CameraLink) -> None:
        await self._do_sync(link)

    # --- actions (fire and forget from the GUI) ---------------------------------------

    def run(self, coro, what: str) -> None:
        async def wrapper():
            try:
                await coro
            except Exception as e:
                log.warning("%s: %s failed: %s", self.name, what, e)
                self.alert.emit("error", f"{self.name}: {what} failed: {e or type(e).__name__}")
        asyncio.ensure_future(wrapper())

    def connected(self) -> bool:
        return self.link is not None and self.status.ble == "connected"

    def _need_link(self) -> CameraLink:
        if not self.connected():
            raise RuntimeError("not connected")
        return self.link  # type: ignore[return-value]

    def set_recording(self, on: bool) -> None:
        """Start/stop recording where the Recording setting says: camera, stream or both."""
        target = self.cfg.record.target
        # A camera without a video feed or Bluetooth address (one-camera setups, or no
        # Bluetooth at all) just sits that part out instead of raising an error.
        if target in ("stream", "both") and self.capture and (on or self.status.stream_rec):
            self._set_stream_recording(on)
        if target in ("camera", "both") and self.cam_cfg.ble_address:
            self._set_camera_recording(on)

    def recording_any(self) -> bool:
        cam = self.cfg.record.target in ("camera", "both") and self.status.recording
        return cam or self.status.stream_rec

    def _set_camera_recording(self, on: bool) -> None:
        async def go():
            link = self._need_link()
            self.status.rec_pending = "start" if on else "stop"
            self.changed.emit()
            try:
                await link.set_recording(on, timeout=8)
            except asyncio.TimeoutError:
                raise RuntimeError("camera did not confirm (media ready?)") from None
            finally:
                self.status.rec_pending = ""
                self.changed.emit()
        self.run(go(), "start recording" if on else "stop recording")

    def _set_stream_recording(self, on: bool) -> None:
        cap = self.capture
        try:
            if cap is None or not cap.supports_recording:
                raise RuntimeError("no video feed to record")
            if on:
                cap.start_recording(self.cfg.record, self.cid)
            else:
                cap.stop_recording()
        except Exception as e:
            log.warning("%s: stream recording: %s", self.name, e)
            self.alert.emit("error", f"{self.name}: recording the stream failed: {e}")

    def _on_stream_recording(self, active: bool, path: str) -> None:
        st = self.status
        was = st.stream_rec
        st.stream_rec, st.stream_file = active, path if active else st.stream_file
        if active:
            self._rec_timer.start()
            name = path.rsplit("/", 1)[-1]
            self.alert.emit("info", f"{self.name}: recording to {name}")
        else:
            self._rec_timer.stop()
            if was and path:
                self.alert.emit("info", f"{self.name}: saved {path.rsplit('/', 1)[-1]}")
        self.changed.emit()

    def toggle_recording(self) -> None:
        self.set_recording(not self.recording_any())

    def send(self, cmd: p.Command, what: str) -> None:
        async def go():
            await self._need_link().send(cmd)
        self.run(go(), what)

    def sync_time(self) -> None:
        async def go():
            await self._do_sync(self._need_link())
        self.run(go(), "time sync")

    async def _do_sync(self, link: CameraLink) -> None:
        sent, minutes, rtt = await link.sync_clock()
        self.status.last_sync = sent.astimezone()
        self.changed.emit()
        self.alert.emit("info", f"{self.name}: clock set to {self.status.last_sync:%H:%M:%S} "
                                f"(UTC{minutes / 60:+g}h)")

    # --- incoming ---------------------------------------------------------------------

    def _on_ble_state(self, state: str, detail: str) -> None:
        log.info("%s BLE %s %s", self.name, state, detail)
        st = self.status
        st.ble, st.ble_detail = state, detail
        if state == "connected":
            st.model = detail
            if self._was_connected:
                self.alert.emit("info", f"{self.name}: Bluetooth reconnected")
            self._was_connected = True
        elif state in ("lost", "stale") and self._was_connected:
            why = "link lost" if state == "lost" else "no data from camera"
            self.alert.emit("warn", f"{self.name}: Bluetooth {why}, reconnecting…")
        elif state == "warning":
            self.alert.emit("warn", f"{self.name}: {detail}")
        self.changed.emit()

    def _on_video_state(self, state: str, detail: str) -> None:
        log.info("%s video %s %s", self.name, state, detail)
        if state == "missing":
            self.alert.emit("warn", f"{self.name}: capture device unplugged, waiting for it")
        elif state == "no-signal":
            self.alert.emit("warn", f"{self.name}: no HDMI signal")
        elif state == "error":
            self.alert.emit("warn", f"{self.name}: capture error ({detail}), retrying")
        self.changed.emit()

    def _on_timecode(self, tc: p.Timecode | None, _raw: bytes) -> None:
        text = str(tc) if tc else ""
        if text != self.status.timecode:
            self.status.timecode = text
            self.changed.emit()

    def _on_command(self, cmd) -> None:
        if not isinstance(cmd, p.Command) or cmd.malformed:
            return
        st, v = self.status, cmd.values
        try:
            match cmd.key:
                case (10, 1):
                    st.transport = p.TransportInfo.from_values(v)
                    st.recording = st.transport.recording
                case (1, 14):
                    st.iso = int(v[0])
                case (1, 2):
                    st.wb_kelvin, st.wb_tint = int(v[0]), int(v[1])
                case (1, 11):
                    st.shutter_angle, st.shutter_speed = v[0] / 100, None
                case (1, 12):
                    st.shutter_speed, st.shutter_angle = int(v[0]), None
                case (0, 2):
                    st.has_lens = v[0] != -16.0  # 0x8000 = no lens
                    st.aperture = p.aperture_fstop_from_av(v[0]) if st.has_lens else None
                case (0, 0):
                    st.focus = float(v[0])
                case (10, 0):
                    st.codec = p.codec_label(v)
                case (1, 9):
                    fps, _sensor, w, h = v[:4]
                    st.rec_format = f"{w}x{h} {fps}p"
                case (9, 0):
                    st.battery = p.Battery.from_values(v)
                case (9, 1):
                    names = {0: "none", 1: "ready", -1: "mount error", -2: "record error"}
                    st.media = [names.get(int(x), str(x)) for x in v]
                case (9, 2):
                    st.remaining = [p.RemainingTime.from_raw(int(x)).label() for x in v]
                case _:
                    return
        except (IndexError, ValueError, p.ProtocolError):
            return
        self.changed.emit()

    # --- summaries for widgets ----------------------------------------------------------

    def active_media(self) -> tuple[str, str]:
        """(medium name, remaining time) of the active slot, if known."""
        t = self.status.transport
        if t is None:
            return "", ""
        names = {0: "CFast", 1: "SD", 2: "SSD", 3: "SSD"}
        for i, m in enumerate(t.media):
            if t.slot_active(i):
                rem = self.status.remaining[i] if i < len(self.status.remaining) else ""
                return names.get(m, str(m)), rem
        return "", ""
