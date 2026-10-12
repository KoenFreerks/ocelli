"""Video preview and backend benchmark, for one or more cameras side by side.

    python -m video.bench --list
    python -m video.bench --device A                         # camera A from config, GStreamer 1080p60
    python -m video.bench --device A --device B --seconds 30 # both feeds, then print stats
    python -m video.bench --device 1A2B --backend qt         # by Cam Link serial, Qt Multimedia
    python -m video.bench --device A --clock --seconds 20    # glass-to-glass test (needs a lens)

Keys: D toggles the debug overlays, F fullscreen, Esc quits.
--clock shows a millisecond clock next to the video and saves a window grab
every 2 s to logs/latency/. Point a camera at the clock: in each grab,
(clock on the right) - (clock seen in the video) = glass-to-glass latency.
"""

from __future__ import annotations

import argparse
import resource
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

import config

from . import devices
from .capture import BACKENDS, FORMATS, Capture
from .view import VideoView

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "latency"


def cpu_seconds() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def resolve_device(spec: str, cfg: config.AppConfig) -> tuple[str, devices.CaptureDevice | None]:
    """'A'/'B' (from config) or a serial/by-id path -> (label, device)."""
    if spec.upper() in config.CAMERA_IDS:
        cid = spec.upper()
        by_id = cfg.cameras[cid].video_device
        if not by_id:
            sys.exit(f"Camera {cid} has no video_device in {config.default_path()}")
        dev = devices.find(by_id)
        return f"Camera {cid}", dev
    dev = devices.find(spec)
    return (dev.serial if dev else spec), dev


class BenchWindow(QWidget):
    def __init__(self, args, feeds: list[tuple[str, devices.CaptureDevice]]) -> None:
        super().__init__()
        self.args = args
        self.setWindowTitle(f"Ocelli bench [{args.backend}]: " + ", ".join(l for l, _ in feeds))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.feeds: list[tuple[str, Capture, VideoView, list[float]]] = []
        for label, dev in feeds:
            cap = BACKENDS[args.backend](dev.by_id, args.width, args.height, args.fps,
                                         args.format, parent=self)
            view = VideoView(label, opengl=args.gl)
            view.attach(cap)
            view.set_debug(True)
            cap.stateChanged.connect(
                lambda s, d, label=label: print(f"{datetime.now():%H:%M:%S.%f}"[:-3]
                                                + f"  {label}: {s} {d}".rstrip()))
            lay.addWidget(view, 1)
            self.feeds.append((label, cap, view, []))

        self.clock: QLabel | None = None
        if args.clock:
            self.clock = QLabel()
            self.clock.setFont(QFont("Monospace", 40, QFont.Weight.Bold))
            self.clock.setStyleSheet("background: white; color: black; padding: 20px")
            self.clock.setMinimumWidth(460)
            self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(self.clock)
            QTimer(self, interval=1, timeout=self._tick).start()
            QTimer(self, interval=2000, timeout=self._grab).start()
            LOG_DIR.mkdir(parents=True, exist_ok=True)

        QShortcut(QKeySequence("D"), self, self._toggle_debug)
        QShortcut(QKeySequence("F"), self, self._toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, self.close)

        self.cpu0 = cpu_seconds()
        self.t0 = time.monotonic()
        QTimer(self, interval=1000, timeout=self._sample).start()
        if args.seconds:
            QTimer.singleShot(int(args.seconds * 1000), self.close)
        n = len(feeds)
        self.resize(960 * n + (460 if args.clock else 0), 540 if n > 1 else 720)
        for _, cap, _, _ in self.feeds:
            cap.start()

    def _toggle_debug(self) -> None:
        on = not self.feeds[0][2].debug_visible()
        for _, _, view, _ in self.feeds:
            view.set_debug(on)

    def _tick(self) -> None:
        self.clock.setText(datetime.now().strftime("%H:%M:%S\n.%f")[:-3])

    def _grab(self) -> None:
        self.grab().save(str(LOG_DIR / f"{self.args.backend}-{datetime.now():%H%M%S}.png"))

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _sample(self) -> None:
        for _, cap, _, samples in self.feeds:
            if cap.state == "streaming":
                samples.append(cap.stats().fps)

    def closeEvent(self, event) -> None:
        if self.args.snapshot:
            self.grab().save(self.args.snapshot)
        wall = time.monotonic() - self.t0
        cpu = cpu_seconds() - self.cpu0
        print()
        for label, cap, _, samples in self.feeds:
            cap.stop()
            s = cap.stats()
            fps = samples[1:] or samples  # skip warm-up second
            print(f"== {label} [{self.args.backend} {self.args.format}] {s.width}x{s.height} "
                  f"({cap.by_id.rsplit('/', 1)[-1]})")
            line = f"   frames {s.frames} in {wall:.1f}s"
            if fps:
                line += f"; fps mean {statistics.mean(fps):.1f} min {min(fps):.0f} max {max(fps):.0f}"
                low = sum(1 for f in fps if f < self.args.fps - 2)
                if low:
                    line += f"; {low} second(s) below {self.args.fps - 2} fps"
            print(line)
            lat = sorted(cap.latency_samples())
            if lat:
                print(f"   capture->app latency mean {statistics.mean(lat):.1f} ms, "
                      f"p95 {lat[max(0, int(len(lat) * 0.95) - 1)]:.1f} ms")
        print(f"CPU {100 * cpu / wall:.0f}% of one core (whole process, {len(self.feeds)} feed(s))")
        super().closeEvent(event)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list capture devices and exit")
    ap.add_argument("--device", action="append", default=[],
                    help="A/B (from config), a serial (or its last 4+ chars) or a by-id path; repeatable")
    ap.add_argument("--backend", choices=list(BACKENDS), default="gst")
    ap.add_argument("--format", choices=list(FORMATS), default="YUYV")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--seconds", type=float, help="close after N seconds and print stats")
    ap.add_argument("--clock", action="store_true", help="glass-to-glass latency clock + grabs")
    ap.add_argument("--gl", action="store_true",
                    help="OpenGL viewport (renders white on Wayland+NVIDIA here)")
    ap.add_argument("--snapshot", help="save a window grab to this PNG just before closing")
    args = ap.parse_args()

    cfg = config.load()
    if args.list or not args.device:
        assigned = {c.video_device: cid for cid, c in cfg.cameras.items() if c.video_device}
        for d in devices.list_devices():
            cid = assigned.get(d.by_id, "-")
            print(f"{cid}  {d.serial}  {d.node:<12} {d.by_id}")
        if not args.device:
            return
    feeds = []
    for spec in args.device:
        label, dev = resolve_device(spec, cfg)
        if dev is None:
            sys.exit(f"No capture device matches '{spec}'. Try --list.")
        feeds.append((label, dev))

    app = QApplication(sys.argv)
    win = BenchWindow(args, feeds)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
