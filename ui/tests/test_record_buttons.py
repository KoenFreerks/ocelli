"""Per-camera and both-camera REC logic, with fake captures (no devices, no Bluetooth)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import config
from ui import controller as controller_mod
from ui.main_window import MainWindow
from video.capture import Capture


class FakeCapture(Capture):
    supports_recording = True

    def __init__(self, by_id, *a, **kw):
        super().__init__(by_id, *a, **kw)
        self._rec = False
        self.state = "streaming"

    def _open(self): ...
    def _close(self): ...
    def start(self): ...
    def stop(self): ...

    @property
    def recording(self):
        return self._rec

    def start_recording(self, rc, camera_id):
        self._rec = True
        self.recordingChanged.emit(True, f"/tmp/{camera_id}.mkv")

    def stop_recording(self):
        self._rec = False
        self.recordingChanged.emit(False, "/tmp/x.mkv")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make_window(monkeypatch, cams: str):
    monkeypatch.setitem(controller_mod.BACKENDS, "gst", FakeCapture)
    cfg = config.AppConfig()
    cfg.record.target = "stream"
    for cid in cams:
        cfg.cameras[cid].video_device = f"/dev/v4l/by-id/fake-{cid}"
    return MainWindow(cfg)


def rec(win):
    return {cid: c.status.stream_rec for cid, c in win.ctls.items()}


def test_cameras_record_independently(app, monkeypatch):
    win = make_window(monkeypatch, "AB")
    assert win.rec_both.text() == "● REC both"
    win.toggle_record_camera("A")
    assert rec(win) == {"A": True, "B": False}
    win.toggle_record_camera("B")
    assert rec(win) == {"A": True, "B": True}
    win.toggle_record_camera("A")
    assert rec(win) == {"A": False, "B": True}
    assert win.rec_both.text() == "■ STOP both"
    win.toggle_record_both()  # something is recording: stops it, does not start A
    assert rec(win) == {"A": False, "B": False}
    win.toggle_record_both()
    assert rec(win) == {"A": True, "B": True}
    win.toggle_record_both()
    assert rec(win) == {"A": False, "B": False}


def test_status_bar_buttons_follow_state(app, monkeypatch):
    win = make_window(monkeypatch, "AB")
    a = win.stage.tiles["A"].status.recbtn
    assert a.text() == "● Rec" and a.isEnabled()
    a.click()
    assert a.text() == "■ Stop" and a.property("recording") == "true"
    assert win.stage.tiles["B"].status.recbtn.text() == "● Rec"
    a.click()
    assert a.text() == "● Rec"


def test_one_camera_setup(app, monkeypatch):
    win = make_window(monkeypatch, "A")
    assert win.rec_both.text() == "● REC"  # not "both"
    assert win.stage.tiles["B"].status.recbtn.isHidden()
    win.toggle_record_both()
    assert rec(win) == {"A": True, "B": False}
    win.toggle_record_camera("B")  # not set up: warns, does nothing
    assert rec(win) == {"A": True, "B": False}
    win.toggle_record_both()
    assert rec(win) == {"A": False, "B": False}


def test_button_disabled_when_nothing_to_record(app, monkeypatch):
    win = make_window(monkeypatch, "A")
    win.cfg.record.target = "camera"  # camera media needs a Bluetooth link; there is none
    win.ctls["A"].changed.emit()
    assert not win.stage.tiles["A"].status.recbtn.isEnabled()
