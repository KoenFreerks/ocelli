"""Settings: assign BLE cameras and capture devices to A/B, pair, options."""

from __future__ import annotations

import asyncio
import copy
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import config
from ble.agent import PairingAgent, address_from_path, device_path
from ble.client import CameraLink, bluez_device_props, scan
from video import devices

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    saved = Signal(object)  # the edited AppConfig; nothing changes unless Save is pressed

    def __init__(self, cfg: config.AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.cfg = copy.deepcopy(cfg)
        self.setWindowTitle("Ocelli settings")
        self.resize(720, 620)
        lay = QVBoxLayout(self)

        swap_row = QHBoxLayout()
        swap = QPushButton("Swap camera A ↔ B")
        swap.setToolTip("Exchange everything assigned to A and B (Bluetooth camera, Cam Link and "
                        "adapter), e.g. so A is the camera on the left")
        swap.clicked.connect(self._swap)
        swap_row.addWidget(swap)
        self.swap_info = QLabel("")
        swap_row.addWidget(self.swap_info, 1)
        lay.addLayout(swap_row)

        # Bluetooth
        box = QGroupBox("Bluetooth cameras")
        bl = QVBoxLayout(box)
        self.ble_table = self._table(["Address", "Name", "Signal", "Bonded", "Assigned"])
        bl.addWidget(self.ble_table)
        row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(lambda: asyncio.ensure_future(self._scan()))
        row.addWidget(self.scan_btn)
        for cid in config.CAMERA_IDS:
            b = QPushButton(f"Use as camera {cid}")
            b.clicked.connect(lambda _c, cid=cid: self._assign_ble(cid))
            row.addWidget(b)
        self.pair_btn = QPushButton("Pair…")
        self.pair_btn.clicked.connect(lambda: asyncio.ensure_future(self._pair()))
        row.addWidget(self.pair_btn)
        row.addStretch(1)
        bl.addLayout(row)
        self.ble_info = QLabel()
        self.ble_info.setWordWrap(True)
        bl.addWidget(self.ble_info)
        lay.addWidget(box)

        # Video
        box = QGroupBox("Capture devices (Cam Link)")
        vl = QVBoxLayout(box)
        self.vid_table = self._table(["Serial", "Node", "Assigned"])
        vl.addWidget(self.vid_table)
        row = QHBoxLayout()
        for cid in config.CAMERA_IDS:
            b = QPushButton(f"Use as camera {cid}")
            b.clicked.connect(lambda _c, cid=cid: self._assign_video(cid))
            row.addWidget(b)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._fill_video)
        row.addWidget(refresh)
        row.addStretch(1)
        vl.addLayout(row)
        lay.addWidget(box)

        # Options
        box = QGroupBox("Options")
        form = QFormLayout(box)
        self.auto_sync = QCheckBox("Set camera clock to this PC's time whenever a camera connects")
        self.auto_sync.setChecked(cfg.ble.auto_sync_time)
        form.addRow(self.auto_sync)
        self.stagger = QDoubleSpinBox(minimum=0, maximum=30, singleStep=0.5, suffix=" s")
        self.stagger.setValue(cfg.ble.stagger_seconds)
        form.addRow("Pause between Bluetooth connects", self.stagger)
        self.host_name = QLineEdit(cfg.ble.host_name)
        self.host_name.setMaxLength(32)
        form.addRow("Name shown on the cameras", self.host_name)
        lay.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._fill_ble([])
        self._fill_video()

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().hide()
        return t

    def _assigned(self, address: str) -> str:
        cid = self.cfg.camera_for_address(address)
        return f"Camera {cid}" if cid else ""

    # --- Bluetooth ----------------------------------------------------------------------

    def _fill_ble(self, rows: list[tuple[str, str, int | None, bool]], scanned: bool = False) -> None:
        known = {a for a, *_ in rows}
        for cid, cam in self.cfg.cameras.items():
            if cam.ble_address and cam.ble_address not in known:
                note = "(not advertising: connected or off)" if scanned else "(press Scan)"
                rows.append((cam.ble_address, note, None, True))
        self.ble_table.setRowCount(len(rows))
        for r, (addr, name, rssi, bonded) in enumerate(rows):
            for c, text in enumerate([addr, name, "" if rssi is None else f"{rssi} dBm",
                                      "yes" if bonded else "no", self._assigned(addr)]):
                self.ble_table.setItem(r, c, QTableWidgetItem(text))
        self.ble_table.resizeColumnsToContents()

    async def _scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.ble_info.setText("Scanning 8 s… (cameras that are connected don't advertise; "
                              "assigned cameras are listed anyway)")
        try:
            found = await scan(8, self.cfg.cameras["A"].ble_adapter)
            rows = []
            for addr, name, rssi in found:
                props = await bluez_device_props("hci0", addr)
                rows.append((addr, name, rssi, bool(props.get("Paired"))))
            self._fill_ble(rows, scanned=True)
            self.ble_info.setText(f"Found {len(found)} advertising camera(s).")
        except Exception as e:
            self.ble_info.setText(f"Scan failed: {e}")
        finally:
            self.scan_btn.setEnabled(True)

    def _selected_address(self) -> str | None:
        row = self.ble_table.currentRow()
        item = self.ble_table.item(row, 0) if row >= 0 else None
        return item.text() if item else None

    def _assign_ble(self, cid: str) -> None:
        addr = self._selected_address()
        if not addr:
            self.ble_info.setText("Select a camera in the list first.")
            return
        for other in self.cfg.cameras.values():
            if other.ble_address == addr:
                other.ble_address = None
        self.cfg.cameras[cid].ble_address = addr
        r = self.ble_table.currentRow()
        for row in range(self.ble_table.rowCount()):
            a = self.ble_table.item(row, 0).text()
            self.ble_table.setItem(row, 4, QTableWidgetItem(self._assigned(a)))
        self.ble_table.selectRow(r)

    async def _ask_pin(self, path: str) -> int | None:
        fut: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
        dlg = QInputDialog(self)
        dlg.setWindowTitle("Bluetooth pairing")
        dlg.setLabelText(f"Enter the 6-digit PIN shown on the camera\n({address_from_path(path)}):")
        dlg.setInputMode(QInputDialog.InputMode.TextInput)
        dlg.textValueSelected.connect(
            lambda t: fut.done() or fut.set_result(int(t) if t.strip().isdigit() else None))
        dlg.rejected.connect(lambda: fut.done() or fut.set_result(None))
        dlg.open()
        try:
            return await fut
        finally:
            dlg.deleteLater()

    async def _pair(self) -> None:
        addr = self._selected_address()
        if not addr:
            self.ble_info.setText("Select a camera in the list first.")
            return
        adapter = "hci0"
        self.pair_btn.setEnabled(False)
        try:
            props = await bluez_device_props(adapter, addr)
            if props.get("Paired"):
                self.ble_info.setText(f"{addr} is already bonded.")
                return
            self.ble_info.setText(f"Connecting to {addr}… the camera will show a PIN.")
            async with PairingAgent(self._ask_pin) as agent:
                link = CameraLink(addr, adapter, self.host_name.text() or "Ocelli")
                await link.connect(20)
                try:
                    await link.pair()
                    await agent.set_trusted(device_path(adapter, addr))
                    await link.start_session()
                finally:
                    await link.disconnect()
            self.ble_info.setText(f"Paired with {addr}. Assign it to A or B and Save.")
        except Exception as e:
            self.ble_info.setText(f"Pairing failed: {e}")
        finally:
            self.pair_btn.setEnabled(True)

    def _swap(self) -> None:
        cams = self.cfg.cameras
        cams["A"], cams["B"] = cams["B"], cams["A"]
        for row in range(self.ble_table.rowCount()):
            a = self.ble_table.item(row, 0).text()
            self.ble_table.setItem(row, 4, QTableWidgetItem(self._assigned(a)))
        self._fill_video()
        self.swap_info.setText("A and B swapped. Press Save to apply.")

    # --- Video --------------------------------------------------------------------------

    def _fill_video(self) -> None:
        devs = devices.list_devices()
        self.vid_table.setRowCount(len(devs))
        for r, d in enumerate(devs):
            cid = next((c for c, cam in self.cfg.cameras.items() if cam.video_device == d.by_id), "")
            for c, text in enumerate([d.serial, d.node, f"Camera {cid}" if cid else ""]):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, d.by_id)
                self.vid_table.setItem(r, c, item)
        self.vid_table.resizeColumnsToContents()

    def _assign_video(self, cid: str) -> None:
        row = self.vid_table.currentRow()
        if row < 0:
            return
        by_id = self.vid_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for cam in self.cfg.cameras.values():
            if cam.video_device == by_id:
                cam.video_device = None
        self.cfg.cameras[cid].video_device = by_id
        self._fill_video()
        self.vid_table.selectRow(row)

    # --- Save ---------------------------------------------------------------------------

    def _save(self) -> None:
        self.cfg.ble.auto_sync_time = self.auto_sync.isChecked()
        self.cfg.ble.stagger_seconds = self.stagger.value()
        self.cfg.ble.host_name = self.host_name.text().strip() or "Ocelli"
        self.saved.emit(self.cfg)
        self.accept()
