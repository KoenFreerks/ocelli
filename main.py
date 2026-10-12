"""Ocelli: monitor and control two Blackmagic Pocket 4K cameras.

    .venv/bin/python main.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import qasync
from PySide6.QtCore import QDir, QLockFile, QStandardPaths, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

import config
from ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "ocelli.log"


def setup_logging(verbose: bool) -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # bleak/dbus are chatty at INFO
    for name in ("bleak", "dbus_fast"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--snapshot", help=argparse.SUPPRESS)  # testing: grab the window to PNG...
    ap.add_argument("--seconds", type=float, help=argparse.SUPPRESS)  # ...after N s, then quit
    args = ap.parse_args()
    setup_logging(args.verbose)

    app = QApplication(sys.argv)
    app.setApplicationName("Ocelli")
    app.setDesktopFileName("ocelli")  # Wayland app id: matches ocelli.desktop (dock icon)
    app.setWindowIcon(QIcon(str(ROOT / "assets" / "ocelli.svg")))

    # One instance only: a second one would find the Cam Links busy and fight over Bluetooth.
    runtime = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation) \
        or QDir.tempPath()
    lock = QLockFile(str(Path(runtime) / "ocelli.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, "Ocelli", "Ocelli is already running.")
        return
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow(config.load())
    win.showFullScreen() if args.fullscreen else win.show()
    win.start()
    if args.seconds:
        def finish():
            if args.snapshot:
                win.grab().save(args.snapshot)
            win.close()
        QTimer.singleShot(int(args.seconds * 1000), finish)

    quit_event = asyncio.Event()
    app.aboutToQuit.connect(quit_event.set)
    with loop:
        loop.run_until_complete(quit_event.wait())


if __name__ == "__main__":
    main()
