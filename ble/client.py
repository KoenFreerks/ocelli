"""Async wrapper around one camera's BLE link.

The handshake mirrors Blackmagic's sample code (PeripheralInterface.swift):
subscribe to the notifying characteristics, read the protocol version, write
our name to Device Name, then write 0x01 to Camera Status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from dbus_fast import BusType, DBusError, Message, MessageType
from dbus_fast.aio import MessageBus

from . import protocol as p
from .agent import device_path

log = logging.getLogger(__name__)


class CameraNotFound(BleakError):
    pass


@dataclass
class LinkInfo:
    manufacturer: str = ""
    model: str = ""
    protocol_version: str = ""


def _text(data: bytes | bytearray) -> str:
    return bytes(data).rstrip(b"\x00").decode("utf-8", errors="replace")


class CameraLink:
    """One BLE connection to one camera. Callbacks run on the asyncio loop."""

    def __init__(self, address: str, adapter: str = "hci0", host_name: str = "Ocelli") -> None:
        self.address = address.upper()
        self.adapter = adapter
        self.host_name = host_name[: p.DEVICE_NAME_MAX]
        self.info = LinkInfo()
        self.on_command: Callable[[p.Command | p.UnknownCommand], None] | None = None
        self.on_timecode: Callable[[p.Timecode | None, bytes], None] | None = None
        self.on_status: Callable[[p.CameraStatus, bytes], None] | None = None
        # (characteristic name, raw bytes) for every notification, for logging
        self.on_raw: Callable[[str, bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.disconnected = asyncio.Event()
        # Latest value the camera reported for each (category, parameter).
        self.params: dict[tuple[int, int], p.Command] = {}
        self._waiters: list[tuple[asyncio.Future, tuple[int, int], Callable[[p.Command], bool]]] = []
        self._client: BleakClient | None = None
        self.last_rx = 0.0  # monotonic time of the last notification

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, timeout: float = 20.0) -> None:
        self.disconnected.clear()
        self.params.clear()
        # A bonded camera is already in BlueZ's device list, and it may already be
        # connected (BlueZ reconnects bonded devices itself). A connected camera
        # doesn't advertise, so use the known device instead of scanning for it.
        props = await bluez_device_info(self.adapter, self.address)
        if props:
            device = BLEDevice(self.address, props.get("Alias"),
                               {"path": device_path(self.adapter, self.address), "props": props})
        else:
            device = await BleakScanner.find_device_by_address(
                self.address, timeout=timeout, bluez={"adapter": self.adapter}
            )
        if device is None:
            raise CameraNotFound(f"{self.address} not found on {self.adapter} within {timeout:.0f}s")
        self._client = BleakClient(
            device,
            disconnected_callback=self._handle_disconnect,
            timeout=timeout,
            bluez={"adapter": self.adapter},
        )
        await self._client.connect()
        self.info.manufacturer = await self._read_text(p.MANUFACTURER_UUID)
        self.info.model = await self._read_text(p.MODEL_UUID)

    async def pair(self) -> None:
        """Bond with the camera; the BlueZ agent will ask for the PIN."""
        assert self._client is not None
        await self._client.pair()

    async def start_session(self, power_on: bool = True) -> None:
        assert self._client is not None
        c = self._client
        await c.start_notify(p.INCOMING_CONTROL_UUID, self._handle_incoming)
        await c.start_notify(p.TIMECODE_UUID, self._handle_timecode)
        await c.start_notify(p.CAMERA_STATUS_UUID, self._handle_status)
        self.info.protocol_version = await self._read_text(p.PROTOCOL_VERSION_UUID)
        await c.write_gatt_char(p.DEVICE_NAME_UUID, self.host_name.encode("utf-8"), response=True)
        if power_on:
            await c.write_gatt_char(p.CAMERA_STATUS_UUID, p.POWER_ON, response=True)

    async def send(self, cmd: p.Command) -> None:
        if not self.is_connected:
            raise BleakError(f"{self.address} is not connected")
        assert self._client is not None
        await self._client.write_gatt_char(p.OUTGOING_CONTROL_UUID, p.encode(cmd), response=True)

    async def wait_for_param(
        self,
        key: tuple[int, int],
        predicate: Callable[[p.Command], bool] = lambda _c: True,
        timeout: float = 5.0,
        accept_current: bool = True,
    ) -> p.Command:
        """Wait until the camera reports ``key`` with a value matching ``predicate``."""
        current = self.params.get(key)
        if accept_current and current is not None and predicate(current):
            return current
        fut: asyncio.Future[p.Command] = asyncio.get_running_loop().create_future()
        entry = (fut, key, predicate)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.remove(entry)

    async def transport(self, timeout: float = 5.0) -> p.TransportInfo:
        cmd = await self.wait_for_param((10, 1), timeout=timeout)
        return p.TransportInfo.from_values(cmd.values)

    async def set_recording(self, recording: bool, timeout: float = 5.0) -> p.TransportInfo:
        """Start or stop recording and wait for the camera to confirm.

        As in Blackmagic's sample, the camera's last 10.1 report is sent back
        with only the mode changed. Raises asyncio.TimeoutError if the camera
        does not switch (e.g. no media ready).
        """
        info = await self.transport(timeout)
        target = p.TransportMode.RECORD if recording else p.TransportMode.PREVIEW
        if info.mode == target:
            return info
        await self.send(p.set_transport(info.with_mode(target)))
        cmd = await self.wait_for_param(
            (10, 1),
            lambda c: p.TransportInfo.from_values(c.values).mode == target,
            timeout,
            accept_current=False,
        )
        return p.TransportInfo.from_values(cmd.values)

    async def sync_clock(self) -> tuple[datetime, int, float]:
        """Write the PC's timezone (7.2) and UTC time (7.0) to the camera.

        The clock value has whole seconds (FF = 00), so the write is timed to
        arrive on a second boundary, using half the measured write round trip
        as the one-way delay. Returns (UTC time sent, timezone minutes, rtt s).
        Accuracy is roughly the BLE latency jitter (~100 ms), not frame-accurate.
        """
        offset = datetime.now().astimezone().utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset else 0
        t0 = time.monotonic()
        await self.send(p.set_timezone(minutes))
        rtt = time.monotonic() - t0
        now = time.time()
        target = int(now) + 1
        if target - now < rtt / 2 + 0.05:
            target += 1
        await asyncio.sleep(max(0.0, target - time.time() - rtt / 2))
        sent = datetime.fromtimestamp(target, timezone.utc)
        await self.send(p.set_clock(sent))
        return sent, minutes, rtt

    def camera_clock(self) -> datetime | None:
        """Last 7.0 the camera reported, if it reports one."""
        cmd = self.params.get((7, 0))
        return p.decode_clock(cmd.values) if cmd else None

    def media_ready(self) -> bool | None:
        """True if any slot reports READY, None if the camera hasn't said yet."""
        cmd = self.params.get((9, 1))
        if cmd is None:
            return None
        return any(v == p.MediaStatus.READY for v in cmd.values)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except BleakError as e:
                log.debug("disconnect %s: %s", self.address, e)
        self._client = None

    async def _read_text(self, uuid: str) -> str:
        assert self._client is not None
        try:
            return _text(await self._client.read_gatt_char(uuid))
        except BleakError as e:
            log.debug("read %s failed: %s", uuid, e)
            return ""

    # --- notification handlers ---

    def _raw(self, name: str, data: bytearray) -> bytes:
        raw = bytes(data)
        self.last_rx = time.monotonic()
        if self.on_raw:
            self.on_raw(name, raw)
        return raw

    def _handle_incoming(self, _char, data: bytearray) -> None:
        raw = self._raw("incoming", data)
        try:
            commands = p.decode(raw)
        except p.ProtocolError as e:
            log.warning("%s: undecodable packet %s (%s)", self.address, raw.hex(" "), e)
            return
        for cmd in commands:
            if isinstance(cmd, p.Command) and not cmd.malformed:
                self.params[cmd.key] = cmd
                for fut, key, predicate in self._waiters:
                    if fut.done() or key != cmd.key:
                        continue
                    try:
                        matched = predicate(cmd)
                    except (p.ProtocolError, ValueError, IndexError):
                        matched = False
                    if matched:
                        fut.set_result(cmd)
            if self.on_command:
                self.on_command(cmd)

    def _handle_timecode(self, _char, data: bytearray) -> None:
        raw = self._raw("timecode", data)
        if self.on_timecode:
            self.on_timecode(p.decode_timecode(raw), raw)

    def _handle_status(self, _char, data: bytearray) -> None:
        raw = self._raw("status", data)
        if self.on_status and raw:
            self.on_status(p.CameraStatus(raw[0]), raw)

    def _handle_disconnect(self, _client: BleakClient) -> None:
        self.disconnected.set()
        if self.on_disconnect:
            self.on_disconnect()


async def maintain(
    link: CameraLink,
    stop: asyncio.Event,
    *,
    timeout: float = 20.0,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    on_state: Callable[[str, str], None] | None = None,
    connect_lock: asyncio.Lock | None = None,
    settle: float = 0.0,
    stale_after: float = 15.0,
    after_connect: Callable[[CameraLink], Awaitable[None]] | None = None,
) -> None:
    """Keep ``link`` connected until ``stop`` is set, retrying with backoff.

    ``on_state(state, detail)`` gets "connecting", "connected", "lost",
    "stale", "failed" or "waiting".

    connect_lock: shared by all cameras on one adapter so connection attempts
        never overlap; held for ``settle`` extra seconds after a connect.
    stale_after: the camera sends battery status about once a second; if
        nothing arrives for this long the link is treated as dead.
    after_connect: e.g. an automatic clock sync; its errors are reported, not fatal.
    """

    def report(state: str, detail: str = "") -> None:
        if on_state:
            on_state(state, detail)

    delay = initial_delay
    while not stop.is_set():
        try:
            async with connect_lock or contextlib.nullcontext():
                if stop.is_set():
                    break
                report("connecting")
                await link.connect(timeout=timeout)
                await link.start_session()
                link.last_rx = time.monotonic()
                report("connected", link.info.model)
                if settle:
                    await asyncio.sleep(settle)
            delay = initial_delay
            if after_connect is not None:
                try:
                    await after_connect(link)
                except (BleakError, asyncio.TimeoutError, OSError) as e:
                    report("warning", f"after-connect step failed: {e}")
            reason = await _watch(link, stop, stale_after)
            if reason == "stop":
                break
            report(reason)
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            report("failed", str(e) or type(e).__name__)
        await link.disconnect()
        if stop.is_set():
            break
        wait = delay * random.uniform(0.8, 1.2)
        report("waiting", f"{wait:.1f}s")
        try:
            await asyncio.wait_for(stop.wait(), wait)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, max_delay)
    await link.disconnect()


async def _watch(link: CameraLink, stop: asyncio.Event, stale_after: float) -> str:
    """Return "stop", "lost" (BLE disconnect) or "stale" (connected but silent)."""
    while True:
        try:
            await asyncio.wait_for(_wait_any(stop, link.disconnected), 1.0)
        except asyncio.TimeoutError:
            if stale_after and time.monotonic() - link.last_rx > stale_after:
                return "stale"
            continue
        return "stop" if stop.is_set() else "lost"


async def _wait_any(*events: asyncio.Event) -> None:
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def scan(timeout: float = 10.0, adapter: str = "hci0") -> list[tuple[str, str, int]]:
    """Return (address, name, rssi) for devices advertising the camera service."""
    found = await BleakScanner.discover(
        timeout=timeout,
        return_adv=True,
        service_uuids=[p.SERVICE_UUID],
        bluez={"adapter": adapter},
    )
    out = [(d.address, d.name or adv.local_name or "", adv.rssi) for d, adv in found.values()]
    return sorted(out, key=lambda t: -t[2])


async def bluez_call(bus: MessageBus, path: str, interface: str, member: str,
                     signature: str = "", body: list | None = None) -> Message:
    """Plain D-Bus method call to BlueZ; raises DBusError on an error reply."""
    reply = await bus.call(Message(
        destination="org.bluez", path=path, interface=interface, member=member,
        signature=signature, body=body or [],
    ))
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name or "error", str(reply.body[0]) if reply.body else "")
    return reply


async def bluez_device_info(adapter: str, address: str) -> dict[str, object]:
    """All org.bluez.Device1 properties, or {} if BlueZ doesn't know the device.

    BlueZ drops unpaired devices from its object tree shortly after a scan ends,
    so look the device up in the managed-object list instead of introspecting it.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bluez_call(bus, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        props = reply.body[0].get(device_path(adapter, address), {}).get("org.bluez.Device1", {})
        return {k: v.value for k, v in props.items()}
    finally:
        bus.disconnect()


async def bluez_device_props(adapter: str, address: str) -> dict[str, object]:
    """Paired/Bonded/Trusted/Connected flags from BlueZ, or {} if unknown."""
    info = await bluez_device_info(adapter, address)
    return {k: v for k, v in info.items() if k in ("Paired", "Bonded", "Trusted", "Connected")}


async def remove_bond(adapter: str, address: str) -> bool:
    """Forget a device in BlueZ (like `bluetoothctl remove`). Returns False if unknown."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        await bluez_call(bus, f"/org/bluez/{adapter}", "org.bluez.Adapter1", "RemoveDevice",
                         "o", [device_path(adapter, address)])
        return True
    except DBusError as e:
        log.debug("remove %s: %s", address, e)
        return False
    finally:
        bus.disconnect()
