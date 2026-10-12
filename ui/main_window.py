"""Main window: layouts, top bar, alerts, keyboard shortcuts, feed-per-window mode."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import config

from .controller import CameraController
from .tile import CameraTile, ControlPanel

log = logging.getLogger(__name__)

LAYOUTS = {"pip": "Main + thumbnail", "side": "Side by side", "single": "Single"}

BAR_CSS = """
QWidget#topbar { background: #151515; border-bottom: 1px solid #2a2a2a; }
QWidget#topbar QPushButton { color: #ddd; background: #262626; border: 1px solid #3a3a3a;
    padding: 3px 10px; font: 12px 'Sans'; }
QWidget#topbar QPushButton:hover { background: #333; }
QWidget#topbar QPushButton:checked { background: #3d5a80; border-color: #5b7fb0; }
QWidget#topbar QPushButton#recboth { font-weight: bold; }
QWidget#topbar QPushButton#recboth[recording="true"] { background: #b3121b; border-color: #e33; }
QWidget#topbar QLabel { color: #aaa; font: 12px 'Sans'; padding: 0 6px; }
QLabel#alert { color: white; font: bold 13px 'Sans'; padding: 6px 14px; border-radius: 4px; }
QWidget#stage { background: black; }
QLabel#placeholder { color: #888; font: 16px 'Sans'; }
QLabel#help { color: #eee; background: rgba(15,15,15,235); border: 1px solid #555;
    border-radius: 6px; padding: 18px 24px; font: 14px 'Monospace'; }
"""
ALERT_COLORS = {"info": "rgba(40,90,160,220)", "warn": "rgba(190,120,0,230)",
                "error": "rgba(180,20,30,235)"}


class Stage(QWidget):
    """Lays the camera tiles out by hand so the thumbnail can float over the main feed."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.tiles: dict[str, CameraTile] = {}
        self.mode = "pip"
        self.main = "A"
        self.placeholder = QLabel("Feeds are in separate windows (W to bring them back)", self)
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.hide()
        self.alert = QLabel(self)
        self.alert.setObjectName("alert")
        self.alert.hide()
        self.help = QLabel(SHORTCUT_HELP + "\n\nclick, Esc or ? to close", self)
        self.help.setObjectName("help")
        self.help.hide()
        self._alert_timer = QTimer(self, singleShot=True, timeout=self.alert.hide)

    def other(self) -> str:
        return "B" if self.main == "A" else "A"

    def show_alert(self, level: str, text: str) -> None:
        self.alert.setText(text)
        self.alert.setStyleSheet(f"background: {ALERT_COLORS.get(level, ALERT_COLORS['info'])}")
        self.alert.adjustSize()
        self.alert.show()
        self.alert.raise_()
        self._place_alert()
        self._alert_timer.start(4000 if level == "info" else 8000)

    def _place_alert(self) -> None:
        self.alert.move((self.width() - self.alert.width()) // 2, 10)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.relayout()

    def relayout(self) -> None:
        w, h = self.width(), self.height()
        self.placeholder.setGeometry(0, 0, w, h)
        here = {cid: t for cid, t in self.tiles.items() if t.parent() is self}
        self.placeholder.setVisible(not here)
        main, other = here.get(self.main), here.get(self.other())
        if main is None and other is not None:
            main, other = other, None
        if main is None:
            return
        if self.mode == "side" and other is not None:
            gap = 2
            half = (w - gap) // 2
            main.set_compact(False)
            other.set_compact(False)
            main.setGeometry(0, 0, half, h)
            other.setGeometry(half + gap, 0, w - half - gap, h)
            main.show()
            other.show()
        else:
            main.set_compact(False)
            main.setGeometry(0, 0, w, h)
            main.show()
            if other is not None:
                if self.mode == "pip":
                    tw = max(240, w // 4)
                    th = tw * 9 // 16 + other.status.sizeHint().height()
                    margin = 12
                    bottom = main.status.sizeHint().height() + margin
                    panel = main.panel.width() if main.panel.isVisible() else 0
                    other.set_compact(True)
                    other.setGeometry(w - panel - tw - margin, h - bottom - th, tw, th)
                    other.show()
                    other.raise_()
                else:
                    other.hide()
        self._place_alert()
        self.alert.raise_()
        if self.help.isVisible():
            self.help.adjustSize()
            self.help.move((w - self.help.width()) // 2, (h - self.help.height()) // 2)
            self.help.raise_()

    def toggle_help(self) -> None:
        self.help.setVisible(not self.help.isVisible())
        self.relayout()

    def mousePressEvent(self, event) -> None:
        if self.help.isVisible():
            self.toggle_help()
        super().mousePressEvent(event)


class FeedWindow(QWidget):
    """One camera in its own top-level window (feed-per-window mode)."""

    def __init__(self, tile: CameraTile, owner: "MainWindow") -> None:
        super().__init__()
        self.owner = owner
        self.setWindowTitle(f"Ocelli: {tile.ctl.name}")
        self.setObjectName("feedwindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("QWidget#feedwindow { background: black; }")  # not inherited by children
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(tile)
        tile.set_compact(False)
        tile.show()
        owner.install_shortcuts(self)

    def closeEvent(self, event) -> None:
        if not self.owner.closing:
            QTimer.singleShot(0, lambda: self.owner.set_feed_windows(False))
        super().closeEvent(event)


class MainWindow(QWidget):
    def __init__(self, cfg: config.AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.closing = False
        self._shutdown_done = False
        self.setWindowTitle("Ocelli")
        self.setStyleSheet(BAR_CSS)
        self.resize(1600, 960)

        self.stage = Stage(self)
        self.stage.mode = cfg.ui.layout if cfg.ui.layout in LAYOUTS else "pip"
        self.stage.main = cfg.ui.main_camera if cfg.ui.main_camera in config.CAMERA_IDS else "A"
        self.ctls: dict[str, CameraController] = {}
        self._build_cameras()
        self.feed_windows: list[FeedWindow] = []
        self._settings = None
        self._bar_wanted = True  # user preference (H); fullscreen hides the bar anyway
        self._bar_hide = QTimer(self, singleShot=True, interval=1200, timeout=self._hide_hover_bar)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._make_bar())
        lay.addWidget(self.stage, 1)
        self.install_shortcuts(self)
        QApplication.instance().installEventFilter(self)
        app = QGuiApplication.instance()
        app.screenRemoved.connect(self._screen_removed)
        app.screenAdded.connect(self._screen_added)
        self._refresh_bar()

    def _build_cameras(self) -> None:
        self.ctls = {cid: CameraController(cid, self.cfg, self) for cid in config.CAMERA_IDS}
        self.stage.tiles = {}
        for cid, ctl in self.ctls.items():
            tile = CameraTile(ctl, self.stage)
            tile.clicked.connect(lambda cid=cid: self._tile_clicked(cid))
            tile.view.doubleClicked.connect(lambda tile=tile: self.toggle_fullscreen(tile.window()))
            self.stage.tiles[cid] = tile
            ctl.alert.connect(self.stage.show_alert)
            ctl.alert.connect(lambda level, text: log.info("[%s] %s", level, text))
            ctl.changed.connect(self._refresh_bar)
            tile.view.set_debug(self.cfg.ui.show_debug)
            tile.show()

    async def _reload_cameras(self) -> None:
        """Stop both cameras and start them again with the current assignments."""
        self.set_feed_windows(False)
        await asyncio.gather(*(c.stop() for c in self.ctls.values()))
        for tile in self.stage.tiles.values():
            tile.hide()
            tile.deleteLater()
        for ctl in self.ctls.values():
            ctl.deleteLater()
        self._build_cameras()
        self.start()
        self._refresh_bar()
        self.stage.show_alert("info", "Camera assignments applied")

    # --- top bar ------------------------------------------------------------------------

    def _make_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topbar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.bar = bar
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 4)
        self.layout_group = QButtonGroup(self)
        for i, (key, label) in enumerate(LAYOUTS.items(), start=1):
            b = QPushButton(f"{label} ({i})")
            b.setCheckable(True)
            b.setChecked(key == self.stage.mode)
            b.clicked.connect(lambda _c, key=key: self.set_layout(key))
            self.layout_group.addButton(b)
            lay.addWidget(b)
        lay.addSpacing(16)
        self.rec_both = QPushButton("● REC both")
        self.rec_both.setObjectName("recboth")
        self.rec_both.setToolTip("Start/stop recording on both cameras (Ctrl+R)")
        self.rec_both.clicked.connect(self.toggle_record_both)
        lay.addWidget(self.rec_both)
        sync = QPushButton("Sync time")
        sync.setToolTip("Set both camera clocks to this PC's time and timezone (Ctrl+T)")
        sync.clicked.connect(self.sync_time_both)
        lay.addWidget(sync)
        lay.addStretch(1)
        self.clock = QLabel()
        lay.addWidget(self.clock)
        QTimer(self, interval=250, timeout=lambda: self.clock.setText(
            f"PC {datetime.now():%a %d %b  %H:%M:%S}")).start()
        self.windows_btn = QPushButton("Separate windows (W)")
        self.windows_btn.setCheckable(True)
        self.windows_btn.clicked.connect(self.set_feed_windows)
        lay.addWidget(self.windows_btn)
        settings = QPushButton("Settings")
        settings.clicked.connect(self.open_settings)
        lay.addWidget(settings)
        help_btn = QPushButton("?")
        help_btn.setToolTip("Keyboard shortcuts (? or F1)")
        help_btn.clicked.connect(self.stage.toggle_help)
        lay.addWidget(help_btn)
        for b in bar.findChildren(QPushButton):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return bar

    def _refresh_bar(self) -> None:
        any_rec = any(c.status.recording for c in self.ctls.values())
        self.rec_both.setText("■ STOP both" if any_rec else "● REC both")
        self.rec_both.setProperty("recording", "true" if any_rec else "false")
        self.rec_both.style().unpolish(self.rec_both)
        self.rec_both.style().polish(self.rec_both)

    # --- actions ------------------------------------------------------------------------

    def set_layout(self, mode: str) -> None:
        if self.feed_windows:
            self.set_feed_windows(False)
        self.stage.mode = mode
        self.cfg.ui.layout = mode
        for b, key in zip(self.layout_group.buttons(), LAYOUTS):
            b.setChecked(key == mode)
        self.stage.relayout()

    def cycle_layout(self, step: int) -> None:
        keys = list(LAYOUTS)
        self.set_layout(keys[(keys.index(self.stage.mode) + step) % len(keys)])

    def swap(self) -> None:
        self.stage.main = self.stage.other()
        self.cfg.ui.main_camera = self.stage.main
        self.stage.relayout()

    def _tile_clicked(self, cid: str) -> None:
        if self.stage.help.isVisible():
            self.stage.toggle_help()
            return
        if self.stage.mode == "pip" and cid != self.stage.main:
            self.swap()

    def toggle_record_both(self) -> None:
        start = not any(c.status.recording for c in self.ctls.values())
        for ctl in self.ctls.values():
            ctl.set_recording(start)  # both sent concurrently

    def sync_time_both(self) -> None:
        for ctl in self.ctls.values():
            ctl.sync_time()

    def toggle_debug(self) -> None:
        on = not self.cfg.ui.show_debug
        self.cfg.ui.show_debug = on
        for t in self.stage.tiles.values():
            t.view.set_debug(on)

    def toggle_focus_view(self) -> None:
        """Z: 1:1 pixel view on the tile under the mouse (or the main camera)."""
        tiles = [t for t in self.stage.tiles.values() if t.isVisible() and not t._compact]
        tile = next((t for t in tiles if t.underMouse()), None) or self.stage.tiles[self.stage.main]
        if tile._compact:
            return
        tile.view.toggle_focus_view()

    def toggle_panels(self) -> None:
        tiles = [t for t in self.stage.tiles.values() if t.isVisible() and not t._compact]
        on = not any(t.panel.isVisible() for t in tiles)
        for t in tiles:
            t.set_panel(on)
        self.stage.relayout()

    def toggle_fullscreen(self, widget: QWidget | None = None) -> None:
        w = widget or self
        if w.isFullScreen():
            self.leave_fullscreen(w)
        else:
            w.showFullScreen()
            if w is self:
                self.bar.hide()
                self.stage.show_alert("info", "Fullscreen: move the mouse to the top edge for the "
                                              "menu bar · F, Esc or double-click to leave")

    def leave_fullscreen(self, w: QWidget | None = None) -> None:
        w = w or self
        if w.isFullScreen():
            w.showNormal()
        if w is self:
            self._bar_hide.stop()
            self.bar.setVisible(self._bar_wanted)

    def toggle_bar(self) -> None:
        self._bar_wanted = not self.bar.isVisible()
        self.bar.setVisible(self._bar_wanted)

    def _hide_hover_bar(self) -> None:
        if self.isFullScreen() and not self.bar.underMouse():
            self.bar.hide()

    def set_feed_windows(self, on: bool) -> None:
        if on and not self.feed_windows:
            # Left-to-right desk order, so camera A always lands on the left monitor.
            screens = sorted(QGuiApplication.screens(), key=lambda sc: (sc.geometry().x(), sc.geometry().y()))
            for i, cid in enumerate(("A", "B")):
                win = FeedWindow(self.stage.tiles[cid], self)
                screen = screens[i % len(screens)]
                win.setScreen(screen)
                geo = screen.availableGeometry()
                if len(screens) > 1:
                    win.move(geo.topLeft())
                    win.showFullScreen()
                else:
                    win.setGeometry(geo.x() + i * geo.width() // 2, geo.y(), geo.width() // 2,
                                    geo.width() // 2 * 9 // 16)
                    win.show()
                self.feed_windows.append(win)
        elif not on and self.feed_windows:
            wins, self.feed_windows = self.feed_windows, []
            for tile in self.stage.tiles.values():
                tile.setParent(self.stage)
            for win in wins:
                win.close()
                win.deleteLater()
            for tile in self.stage.tiles.values():
                tile.show()
        self.windows_btn.setChecked(bool(self.feed_windows))
        self.stage.relayout()

    def _screen_removed(self, _screen) -> None:
        # A monitor went away (switched off, display settings changed, remote session...):
        # don't leave a feed on a screen that no longer exists.
        if self.feed_windows and len(QGuiApplication.screens()) < 2:
            QTimer.singleShot(0, lambda: self.set_feed_windows(False))
            self.stage.show_alert("warn", "A monitor was disconnected: both feeds are back in this window")

    def _screen_added(self, _screen) -> None:
        if not self.feed_windows and len(QGuiApplication.screens()) >= 2:
            self.stage.show_alert("info", "Second monitor connected: press W to put one camera on each")

    def open_settings(self) -> None:
        from .settings import SettingsDialog

        # A dialog can end up hidden behind a fullscreen window on Wayland, so leave
        # fullscreen first, and keep the dialog non-modal so it can never lock the app.
        self.leave_fullscreen(self)
        for win in self.feed_windows:
            self.leave_fullscreen(win)
        if self._settings is None:
            self._settings = SettingsDialog(self.cfg, self)
            self._settings.setModal(False)
            self._settings.saved.connect(self._settings_saved)
            self._settings.finished.connect(lambda _r: setattr(self, "_settings", None))
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _settings_saved(self, new: config.AppConfig) -> None:
        devices_changed = new.cameras != self.cfg.cameras or new.ble != self.cfg.ble
        # Update in place: controllers keep a reference to this config object.
        self.cfg.cameras = new.cameras
        self.cfg.ble = new.ble
        try:
            config.save(self.cfg)
        except OSError as e:
            self.stage.show_alert("error", f"Could not save settings: {e}")
            return
        log.info("settings saved; cameras: %s", {c: (v.ble_address, v.video_device and
                 v.video_device.rsplit("_", 1)[-1]) for c, v in self.cfg.cameras.items()})
        if devices_changed:
            self.stage.show_alert("info", "Applying new camera assignments…")
            asyncio.ensure_future(self._reload_cameras())
        else:
            self.stage.show_alert("info", "Settings saved")

    # --- keyboard -----------------------------------------------------------------------

    def install_shortcuts(self, w: QWidget) -> None:
        keys = {
            "1": lambda: self.set_layout("pip"),
            "2": lambda: self.set_layout("side"),
            "3": lambda: self.set_layout("single"),
            "F": lambda: self.toggle_fullscreen(w),
            "Esc": lambda: self.stage.toggle_help() if self.stage.help.isVisible()
                           else self.leave_fullscreen(w),
            "?": self.stage.toggle_help,
            "F1": self.stage.toggle_help,
            "Z": self.toggle_focus_view,
            "D": self.toggle_debug,
            "C": self.toggle_panels,
            "H": self.toggle_bar,
            "W": lambda: self.set_feed_windows(not self.feed_windows),
            "Ctrl+R": self.toggle_record_both,
            "Ctrl+T": self.sync_time_both,
            "Ctrl+,": self.open_settings,
        }
        for key, fn in keys.items():
            QShortcut(QKeySequence(key), w, fn)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove and self.isFullScreen():
            y = self.mapFromGlobal(event.globalPosition().toPoint()).y()
            if y <= 4 and not self.bar.isVisible():
                self.bar.show()
                self._bar_hide.stop()
            elif self.bar.isVisible() and y > self.bar.height() + 30:
                self._bar_hide.start()
        # Tab swaps cameras and Left/Right cycle layouts, unless the user is working in a
        # control-panel field (where those keys move focus or change the value).
        if event.type() == QEvent.Type.KeyPress and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier \
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Left, Qt.Key.Key_Right):
            focus = QApplication.focusWidget()
            in_panel = focus is not None and any(
                isinstance(p, ControlPanel) for p in _ancestors(focus))
            active = QApplication.activeWindow()
            if not in_panel and (active is self or active in self.feed_windows):
                if event.key() == Qt.Key.Key_Tab:
                    self.swap()
                else:
                    self.cycle_layout(1 if event.key() == Qt.Key.Key_Right else -1)
                return True
        return super().eventFilter(obj, event)

    # --- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        for ctl in self.ctls.values():
            ctl.start()
        self.stage.relayout()

    def closeEvent(self, event) -> None:
        if self._shutdown_done:
            super().closeEvent(event)
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            asyncio.ensure_future(self._shutdown())

    async def _shutdown(self) -> None:
        try:
            config.save(self.cfg)
        except OSError as e:
            log.warning("saving config: %s", e)
        for win in self.feed_windows:
            win.close()
        try:
            await asyncio.wait_for(asyncio.gather(*(c.stop() for c in self.ctls.values())), 10)
        except asyncio.TimeoutError:
            log.warning("BLE shutdown timed out")
        self._shutdown_done = True
        self.close()
        QApplication.instance().quit()


def _ancestors(w: QWidget):
    while w is not None:
        yield w
        w = w.parentWidget()


SHORTCUT_HELP = """Keyboard
1 / 2 / 3   main + thumbnail / side by side / single
← / →       previous / next layout
Tab         swap cameras (or click the thumbnail)
F, Esc      fullscreen (video only; mouse to top edge for this bar)
dbl-click   fullscreen on / off
Z           1:1 focus view (exact camera pixels; wheel zoom, drag)
W           each camera in its own window
C           camera control panels (or hover + ⚙)
D           debug overlay (capture size, fps, latency)
H           hide/show this bar
Ctrl+R      record both on/off
Ctrl+T      sync both camera clocks to this PC
Ctrl+,      settings
?, F1       this help"""
