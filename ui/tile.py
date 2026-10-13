"""A camera tile: video view + always-visible status bar + collapsible control panel."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ble import protocol as p
from video.view import VideoView

from .controller import CameraController

ISO_PRESETS = [100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000,
               2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 16000, 20000, 25600]
ANGLE_PRESETS = [11.25, 22.5, 45, 60, 90, 120, 150, 172.8, 180, 216, 270, 324, 360]
SPEED_PRESETS = [24, 25, 30, 48, 50, 60, 100, 120, 125, 250, 500, 1000, 2000]
FSTOP_PRESETS = [1.4, 2, 2.8, 4, 5.6, 8, 11, 16, 22]
UNKNOWN = "—"  # shown until the camera reports a value
KELVIN_UNKNOWN = 2450  # spinbox minimum used as the "—" special value
TINT_UNKNOWN = -51

OVERLAY_CSS = """
QFrame#statusbar { background: rgba(0,0,0,165); }
QFrame#statusbar QLabel { color: #e6e6e6; font: 11px 'Sans'; padding: 0 4px; }
QFrame#panel { background: rgba(18,18,18,225); border-left: 1px solid #333; }
QFrame#panel QLabel, QFrame#panel QPushButton, QFrame#panel QComboBox, QFrame#panel QSpinBox {
    color: #e6e6e6; font: 12px 'Sans'; }
QFrame#panel QPushButton { background: #2c2c2c; border: 1px solid #444; padding: 4px 8px; }
QFrame#panel QPushButton:hover { background: #3a3a3a; }
QFrame#panel QPushButton:disabled { color: #666; }
QFrame#panel QPushButton#rec { font-weight: bold; }
QFrame#panel QPushButton#rec[recording="true"] { background: #b3121b; border-color: #e33; }
QFrame#panel QComboBox, QFrame#panel QSpinBox { background: #222; border: 1px solid #444; }
QToolButton#gear { color: white; background: rgba(0,0,0,140); border: none; font-size: 16px;
    padding: 2px 6px; }
"""


def _dot(color: str) -> str:
    return f"<span style='color:{color}'>●</span>"


class StatusBar(QFrame):
    """REC, timecode, battery, media, settings summary, BLE + video indicators."""

    def __init__(self, ctl: CameraController, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("statusbar")
        self.ctl = ctl
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self.name = QLabel(ctl.name)
        self.rec = QLabel()
        self.srec = QLabel()  # recording the HDMI feed to this PC
        self.tc = QLabel()
        self.settings = QLabel()
        self.media = QLabel()
        self.battery = QLabel()
        self.links = QLabel()
        for w in (self.name, self.rec, self.srec, self.tc, self.settings):
            lay.addWidget(w)
        lay.addStretch(1)
        for w in (self.media, self.battery, self.links):
            lay.addWidget(w)
        self.name.setStyleSheet("font-weight: bold")
        self.tc.setStyleSheet("font-family: monospace")
        ctl.changed.connect(self.refresh)
        if ctl.capture:
            ctl.capture.stateChanged.connect(lambda *_: self.refresh())
        self.refresh()

    def set_compact(self, compact: bool) -> None:
        self.settings.setVisible(not compact)
        self.media.setVisible(not compact)

    def refresh(self) -> None:
        st = self.ctl.status
        if st.rec_pending:
            self.rec.setText(f"{_dot('#f5b400')} {'REC…' if st.rec_pending == 'start' else 'STOP…'}")
        elif st.recording:
            self.rec.setText(f"{_dot('#ff2a2a')} <b>REC</b>")
        elif st.transport is not None:
            self.rec.setText(f"{_dot('#777')} STBY")
        else:
            self.rec.setText("")
        if st.stream_rec:
            _file, secs, size, dropped = self.ctl.capture.record_info()
            h, rem = divmod(int(secs), 3600)
            text = (f"{_dot('#ff2a2a')} <b>REC PC</b> {h}:{rem // 60:02d}:{rem % 60:02d} "
                    f"{size / 2**20:,.0f} MB")
            if dropped:
                text += f" <span style='color:#f5b400'>({dropped} dropped)</span>"
            self.srec.setText(text)
            self.srec.setToolTip(st.stream_file)
        else:
            self.srec.setText("")
        self.tc.setText(st.timecode)

        parts = []
        if st.iso:
            parts.append(f"ISO {st.iso}")
        if st.shutter_angle is not None:
            parts.append(f"{st.shutter_angle:g}°")
        elif st.shutter_speed:
            parts.append(f"1/{st.shutter_speed}")
        if st.wb_kelvin:
            parts.append(f"{st.wb_kelvin}K")
        if st.rec_format:
            parts.append(st.rec_format)
        self.settings.setText("  ".join(parts))

        medium, rem = self.ctl.active_media()
        self.media.setText(f"{medium} {rem}".strip())
        if rem == "FULL":
            self.media.setStyleSheet("color: #ff5050")
        else:
            self.media.setStyleSheet("")

        b = st.battery
        if b is not None:
            low = b.percent <= 15 and not b.external_power
            color = "#ff5050" if low else "#e6e6e6"
            self.battery.setText(f"<span style='color:{color}'>🔋 {b.label()}</span>")
        else:
            self.battery.setText("")

        ble_color = {"connected": "#3ddc84", "connecting": "#f5b400", "waiting": "#f5b400"}.get(
            st.ble, "#ff5050" if st.ble != "off" else "#777")
        vstate = self.ctl.capture.state if self.ctl.capture else "off"
        vid_color = {"streaming": "#3ddc84", "starting": "#f5b400"}.get(
            vstate, "#ff5050" if vstate != "off" else "#777")
        self.links.setText(f"BT {_dot(ble_color)}  VID {_dot(vid_color)}")
        self.links.setToolTip(f"Bluetooth: {st.ble} {st.ble_detail}\nVideo: {vstate}")


class ControlPanel(QFrame):
    """Camera settings. Edits are sent immediately; camera reports update the widgets."""

    WIDTH = 270

    def __init__(self, ctl: CameraController, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.ctl = ctl
        self.setFixedWidth(self.WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.title = QLabel(f"<b>{ctl.name}</b>")
        outer.addWidget(self.title)

        self.rec = QPushButton("● Record")
        self.rec.setObjectName("rec")
        self.rec.clicked.connect(ctl.toggle_recording)
        outer.addWidget(self.rec)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.iso = QComboBox()
        self.iso.addItems([UNKNOWN] + [str(i) for i in ISO_PRESETS])
        self.iso.activated.connect(self._iso_chosen)
        form.addRow("ISO", self.iso)

        self.shutter = QComboBox()
        self.shutter.activated.connect(self._shutter_chosen)
        form.addRow("Shutter", self.shutter)
        self._shutter_mode = ""

        wb_row = QHBoxLayout()
        self.kelvin = QSpinBox(minimum=KELVIN_UNKNOWN, maximum=10000, singleStep=50, suffix=" K")
        self.kelvin.setSpecialValueText(UNKNOWN)
        self.tint = QSpinBox(minimum=TINT_UNKNOWN, maximum=50, prefix="tint ")
        self.tint.setSpecialValueText(UNKNOWN)
        self.kelvin.editingFinished.connect(self._wb_edited)
        self.tint.editingFinished.connect(self._wb_edited)
        wb_row.addWidget(self.kelvin)
        wb_row.addWidget(self.tint)
        form.addRow("WB", wb_row)
        self.auto_wb = QPushButton("Auto white balance")
        self.auto_wb.clicked.connect(lambda: ctl.send(p.auto_white_balance(), "auto white balance"))
        form.addRow("", self.auto_wb)

        self.aperture = QComboBox()
        self.aperture.addItem(UNKNOWN)
        for f in FSTOP_PRESETS:
            self.aperture.addItem(f"f/{f:g}", f)
        self.aperture.activated.connect(self._aperture_chosen)
        form.addRow("Iris", self.aperture)
        lens_row = QHBoxLayout()
        self.af = QPushButton("Autofocus")
        self.af.clicked.connect(lambda: ctl.send(p.autofocus(), "autofocus"))
        self.auto_iris = QPushButton("Auto iris")
        self.auto_iris.clicked.connect(lambda: ctl.send(p.auto_aperture(), "auto iris"))
        lens_row.addWidget(self.af)
        lens_row.addWidget(self.auto_iris)
        form.addRow("Lens", lens_row)
        outer.addLayout(form)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #aaa; font-size: 11px")
        outer.addWidget(self.info)

        self.sync = QPushButton("Sync time to this PC")
        self.sync.clicked.connect(ctl.sync_time)
        outer.addWidget(self.sync)
        outer.addStretch(1)

        ctl.changed.connect(self.refresh)
        self.refresh()

    def _iso_chosen(self, index: int) -> None:
        text = self.iso.itemText(index)
        if text.isdigit():
            self.ctl.send(p.set_iso(int(text)), "set ISO")

    def _aperture_chosen(self, index: int) -> None:
        f = self.aperture.itemData(index)
        if f:
            self.ctl.send(p.set_aperture_fstop(f), "set aperture")

    def _shutter_chosen(self, index: int) -> None:
        value = self.shutter.itemData(index)
        if value is None:
            return
        if self._shutter_mode == "speed":
            self.ctl.send(p.set_shutter_speed(int(value)), "set shutter speed")
        else:
            self.ctl.send(p.set_shutter_angle(float(value)), "set shutter angle")

    def _wb_edited(self) -> None:
        st = self.ctl.status
        k, t = self.kelvin.value(), self.tint.value()
        if k == KELVIN_UNKNOWN or t == TINT_UNKNOWN:
            return
        if (k, t) != (st.wb_kelvin, st.wb_tint):
            self.ctl.send(p.set_white_balance(self.kelvin.value(), self.tint.value()),
                          "set white balance")

    @staticmethod
    def _select(combo: QComboBox, text: str | None) -> None:
        if combo.view().isVisible():
            return  # user is choosing
        if text is None:
            combo.setCurrentIndex(0)  # "—"
            return
        i = combo.findText(text)
        if i < 0:
            combo.addItem(text)
            i = combo.count() - 1
        combo.setCurrentIndex(i)

    def refresh(self) -> None:
        st, ctl = self.ctl.status, self.ctl
        live = ctl.connected()
        target = ctl.cfg.record.target
        self.rec.setEnabled(live if target == "camera" else ctl.capture is not None)
        for w in (self.iso, self.shutter, self.kelvin, self.tint, self.auto_wb, self.sync):
            w.setEnabled(live)
        lens = live and bool(st.has_lens)
        for w in (self.aperture, self.af, self.auto_iris):
            w.setEnabled(lens)
            w.setToolTip("" if st.has_lens else "No electronic lens reported by the camera")

        rec_on = ctl.recording_any()
        if st.rec_pending:
            self.rec.setText("waiting for camera…")
        else:
            where = {"camera": "", "stream": " to PC", "both": " (camera + PC)"}[target]
            self.rec.setText(f"■ Stop recording{where}" if rec_on else f"● Record{where}")
        self.rec.setProperty("recording", "true" if rec_on else "false")
        self.rec.style().unpolish(self.rec)
        self.rec.style().polish(self.rec)

        self._select(self.iso, str(st.iso) if st.iso is not None else None)

        mode = "speed" if st.shutter_speed else "angle"
        if mode != self._shutter_mode:
            self._shutter_mode = mode
            self.shutter.clear()
            self.shutter.addItem(UNKNOWN, None)
            for v in (SPEED_PRESETS if mode == "speed" else ANGLE_PRESETS):
                self.shutter.addItem(f"1/{v}" if mode == "speed" else f"{v:g}°", v)
        if st.shutter_speed:
            self._select(self.shutter, f"1/{st.shutter_speed}")
        elif st.shutter_angle is not None:
            self._select(self.shutter, f"{st.shutter_angle:g}°")
        else:
            self._select(self.shutter, None)

        if not self.kelvin.hasFocus():
            self.kelvin.setValue(st.wb_kelvin if st.wb_kelvin is not None else KELVIN_UNKNOWN)
        if not self.tint.hasFocus():
            self.tint.setValue(st.wb_tint if st.wb_tint is not None else TINT_UNKNOWN)
        self._select(self.aperture,
                     f"f/{st.aperture:.1f}".replace(".0", "") if st.has_lens and st.aperture else None)

        lines = []
        if st.model:
            lines.append(st.model)
        if st.codec or st.rec_format:
            lines.append(f"{st.codec}  {st.rec_format}".strip())
        if st.media:
            slots = []
            for i, m in enumerate(st.media):
                rem = st.remaining[i] if i < len(st.remaining) else ""
                slots.append(f"slot {i + 1}: {m}{' ' + rem if m == 'ready' else ''}")
            lines.append(" · ".join(slots))
        if st.battery:
            lines.append(f"Power: {st.battery.label()}")
        lines.append("Clock synced: " + (f"{st.last_sync:%a %H:%M:%S}" if st.last_sync else "not this session"))
        lines.append(f"Bluetooth: {st.ble} {st.ble_detail}".strip())
        self.info.setText("\n".join(lines))


class CameraTile(QWidget):
    """Video + overlays. Emits `clicked` (used to swap the thumbnail)."""

    clicked = Signal()

    def __init__(self, ctl: CameraController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.setStyleSheet(OVERLAY_CSS)
        self.view = VideoView(ctl.name, parent=self)
        if ctl.capture:
            self.view.attach(ctl.capture)
        else:
            self.view.show_message(f"{ctl.name}\nno capture device assigned (Settings)")
        self.view.viewport().installEventFilter(self)
        self.status = StatusBar(ctl, self)
        self.panel = ControlPanel(ctl, self)
        self.panel.hide()
        self.gear = QToolButton(self)
        self.gear.setObjectName("gear")
        self.gear.setText("⚙")
        self.gear.setToolTip("Camera controls (C)")
        self.gear.clicked.connect(self.toggle_panel)
        self.gear.hide()
        self._hide_gear = QTimer(self, singleShot=True, interval=1500, timeout=self._maybe_hide_gear)
        self.setMouseTracking(True)
        self.view.setMouseTracking(True)
        self._compact = False

    def sizeHint(self) -> QSize:
        return QSize(960, 540)

    def toggle_panel(self) -> None:
        self.set_panel(not self.panel.isVisible())

    def set_panel(self, on: bool) -> None:
        self.panel.setVisible(on and not self._compact)
        self._place()

    def set_compact(self, compact: bool) -> None:
        """Thumbnail mode: no controls, short status bar."""
        self._compact = compact
        if compact:
            self.panel.hide()
            self.gear.hide()
        self._place()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        elif event.type() in (QEvent.Type.MouseMove, QEvent.Type.Enter) and not self._compact:
            self.gear.show()
            self.gear.raise_()
            self._hide_gear.start()
        return False

    def _maybe_hide_gear(self) -> None:
        if not self.gear.underMouse():
            self.gear.hide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()

    def _place(self) -> None:
        w, h = self.width(), self.height()
        panel_w = self.panel.width() if self.panel.isVisible() else 0
        self.view.setGeometry(0, 0, w - panel_w, h)
        self.status.set_compact(self._compact or w - panel_w < 720)
        sh = self.status.sizeHint().height()
        self.status.setGeometry(0, h - sh, w - panel_w, sh)
        self.status.raise_()
        if self.panel.isVisible():
            self.panel.setGeometry(w - panel_w, 0, panel_w, h)
            self.panel.raise_()
        gw = self.gear.sizeHint().width()
        self.gear.move(w - panel_w - gw - 6, 6)
