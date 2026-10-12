"""Command-line tool for testing one camera over BLE.

    python -m ble.cli scan
    python -m ble.cli assign A AA:BB:CC:DD:EE:01
    python -m ble.cli pair A
    python -m ble.cli monitor A
    python -m ble.cli record A start      (stop | toggle | status)
    python -m ble.cli record both start
    python -m ble.cli time both sync       (or: time both check)
    python -m ble.cli unpair A
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import config
from . import protocol as p
from .agent import PairingAgent, address_from_path, device_path
from .client import CameraLink, bluez_device_props, maintain, remove_bond, scan

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def resolve(target: str, cfg: config.AppConfig) -> tuple[str, str, str | None]:
    """Turn 'A', 'B' or a MAC address into (address, adapter, camera id)."""
    if target.upper() in config.CAMERA_IDS:
        cid = target.upper()
        cam = cfg.cameras[cid]
        if not cam.ble_address:
            sys.exit(f"Camera {cid} has no BLE address yet. Run: python -m ble.cli assign {cid} <MAC>")
        return cam.ble_address, cam.ble_adapter, cid
    if not MAC_RE.match(target):
        sys.exit(f"'{target}' is neither A, B nor a MAC address")
    cid = cfg.camera_for_address(target)
    adapter = cfg.cameras[cid].ble_adapter if cid else "hci0"
    return target.upper(), adapter, cid


# --- scan / assign ----------------------------------------------------------------


async def cmd_scan(args, cfg) -> None:
    print(f"Scanning {args.timeout:.0f}s on {args.adapter} for the Blackmagic camera service...")
    found = await scan(args.timeout, args.adapter)
    if not found:
        print("No cameras found. Is Bluetooth on in the camera (Setup > Bluetooth)?")
        print("A camera stops advertising while it is connected to another device.")
        return
    print(f"{'address':<19} {'name':<20} {'rssi':>5}  assigned  bonded")
    for addr, name, rssi in found:
        cid = cfg.camera_for_address(addr) or "-"
        props = await bluez_device_props(args.adapter, addr)
        bonded = "yes" if props.get("Paired") else "no"
        print(f"{addr:<19} {name:<20} {rssi:>5}  {cid:<8}  {bonded}")


async def cmd_assign(args, cfg) -> None:
    if not MAC_RE.match(args.address):
        sys.exit(f"'{args.address}' is not a MAC address")
    cid = args.camera.upper()
    other = cfg.camera_for_address(args.address)
    if other and other != cid:
        cfg.cameras[other].ble_address = None
        print(f"(removed {args.address} from camera {other})")
    cfg.cameras[cid].ble_address = args.address.upper()
    if args.adapter:
        cfg.cameras[cid].ble_adapter = args.adapter
    path = config.save(cfg)
    print(f"Camera {cid} -> {args.address.upper()} on {cfg.cameras[cid].ble_adapter}  ({path})")


# --- pair ---------------------------------------------------------------------------


async def ainput(prompt: str) -> str:
    """Cancellable line read from stdin."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)

    def ready() -> None:
        if not fut.done():
            fut.set_result(sys.stdin.readline())

    loop.add_reader(sys.stdin, ready)
    try:
        return await fut
    finally:
        loop.remove_reader(sys.stdin)


async def prompt_passkey(path: str) -> int | None:
    print()
    print("=" * 60)
    print(f" Pairing with {address_from_path(path)}")
    print(" The camera now shows a 6-digit PIN (Setup > Bluetooth).")
    print("=" * 60)
    for _ in range(3):
        text = (await ainput(" Enter PIN (empty to cancel): ")).strip()
        if not text:
            return None
        if text.isdigit() and len(text) == 6:
            return int(text)
        print(" The PIN must be exactly 6 digits.")
    return None


async def cmd_pair(args, cfg) -> None:
    address, adapter, cid = resolve(args.target, cfg)
    label = f"camera {cid} ({address})" if cid else address
    props = await bluez_device_props(adapter, address)
    if props.get("Paired") and not args.force:
        print(f"{label} is already bonded. Use 'monitor', or 'unpair' first to pair again.")
        return
    async with PairingAgent(prompt_passkey) as agent:
        link = CameraLink(address, adapter, cfg.ble.host_name)
        print(f"{ts()}  connecting to {label} on {adapter}...")
        await link.connect(cfg.ble.connect_timeout)
        print(f"{ts()}  connected: {link.info.manufacturer} {link.info.model}")
        try:
            print(f"{ts()}  requesting bond...")
            await link.pair()
            await agent.set_trusted(device_path(adapter, address))
            print(f"{ts()}  bonded and trusted.")
            await link.start_session()
            print(f"{ts()}  protocol version {link.info.protocol_version or '?'}; "
                  "encrypted characteristics are readable.")
        finally:
            await link.disconnect()
    print("Done. Next: python -m ble.cli monitor", cid or address)


async def cmd_unpair(args, cfg) -> None:
    address, adapter, _ = resolve(args.target, cfg)
    ok = await remove_bond(adapter, address)
    print(f"{address}: {'bond removed' if ok else 'not known to BlueZ'}")
    print("Also remove this PC from the camera's Bluetooth menu if it is listed there.")


# --- monitor ------------------------------------------------------------------------


class Monitor:
    def __init__(self, label: str, log_path: Path | None, show_timecode: bool) -> None:
        self.label = label
        self.show_timecode = show_timecode
        self.counts: Counter[str] = Counter()
        self.last_value: dict[str, str] = {}
        self.timecode = ""
        self.log = log_path.open("a") if log_path else None
        self.last_tc_print = 0.0

    def write(self, kind: str, raw: bytes, decoded: str = "") -> None:
        if self.log:
            self.log.write(json.dumps({
                "t": time.time(), "kind": kind, "hex": raw.hex(), "decoded": decoded,
            }) + "\n")
            self.log.flush()

    def on_raw(self, name: str, raw: bytes) -> None:
        self.write(name, raw)

    def on_command(self, cmd) -> None:
        if isinstance(cmd, p.UnknownCommand):
            print(f"{ts()}  CCU  cmd {cmd.command_id} (not change-config) {cmd.raw.hex(' ')}")
            return
        pid = cmd.param_id
        self.counts[pid] += 1
        known = cmd.key in p.PARAMS
        text = p.describe(cmd)
        tag = "" if known else "  <-- undocumented"
        if cmd.malformed:
            tag += "  <-- payload size does not fit type"
        dtype = cmd.data_type.name if isinstance(cmd.data_type, p.DataType) else cmd.data_type
        print(f"{ts()}  CCU  {pid:<6} {text:<52} [{dtype} {cmd.raw.hex(' ')}]{tag}")
        self.last_value[pid] = text
        self.write("decoded", cmd.raw, f"{pid} {text}")

    def on_timecode(self, tc, raw: bytes) -> None:
        self.timecode = str(tc) if tc else f"? {raw.hex(' ')}"
        now = time.monotonic()
        if self.show_timecode or now - self.last_tc_print >= 5:
            print(f"{ts()}  TC   {self.timecode}   [{raw.hex(' ')}]")
            self.last_tc_print = now

    def on_status(self, flags: p.CameraStatus, raw: bytes) -> None:
        names = "|".join(f.name.lower() for f in p.CameraStatus if f in flags) or "none"
        print(f"{ts()}  STAT 0x{raw[0]:02x} {names}")

    def on_state(self, state: str, detail: str) -> None:
        marker = "!!" if state in ("lost", "failed") else "--"
        print(f"{ts()}  {marker} {self.label}: {state} {detail}".rstrip())
        self.write("link", b"", f"{state} {detail}")

    def summary(self) -> None:
        print()
        print("Parameters received (id: count, last value):")
        for pid in sorted(self.counts, key=lambda s: tuple(map(int, s.split(".")))):
            print(f"  {pid:<6} x{self.counts[pid]:<4} {self.last_value[pid]}")
        if self.timecode:
            print(f"  timecode last {self.timecode}")
        if self.log:
            print(f"Raw log: {self.log.name}")
            self.log.close()


async def cmd_monitor(args, cfg) -> None:
    address, adapter, cid = resolve(args.target, cfg)
    label = f"camera {cid}" if cid else address
    props = await bluez_device_props(adapter, address)
    if not props.get("Paired"):
        print(f"Warning: {address} is not bonded; encrypted characteristics will fail. "
              f"Run: python -m ble.cli pair {args.target}")
    log_path = None
    if not args.no_log:
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"ble-{cid or address.replace(':', '')}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    mon = Monitor(label, log_path, args.timecode)
    link = CameraLink(address, adapter, cfg.ble.host_name)
    link.on_raw = mon.on_raw
    link.on_command = mon.on_command
    link.on_timecode = mon.on_timecode
    link.on_status = mon.on_status

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    if args.seconds:
        loop.call_later(args.seconds, stop.set)
    print(f"Monitoring {label} ({address}) on {adapter}. Ctrl+C to stop.")
    try:
        await maintain(
            link, stop,
            timeout=cfg.ble.connect_timeout,
            initial_delay=cfg.ble.retry_initial,
            max_delay=cfg.ble.retry_max,
            on_state=mon.on_state,
        )
    finally:
        mon.summary()


# --- record -------------------------------------------------------------------------


async def open_link(target: str, cfg: config.AppConfig) -> tuple[str, CameraLink, bool]:
    """Connect and start a session. Returns (label, link, was_already_connected)."""
    address, adapter, cid = resolve(target, cfg)
    label = cid or address
    props = await bluez_device_props(adapter, address)
    if not props.get("Paired"):
        sys.exit(f"{label} is not bonded. Run: python -m ble.cli pair {target}")
    link = CameraLink(address, adapter, cfg.ble.host_name)
    print(f"{ts()}  {label}: connecting...")
    await link.connect(cfg.ble.connect_timeout)
    await link.start_session()
    await link.transport(timeout=10)  # wait for the initial status payload
    try:
        await link.wait_for_param((9, 1), timeout=3)  # media status follows shortly
    except asyncio.TimeoutError:
        pass
    print(f"{ts()}  {label}: connected")
    return label, link, bool(props.get("Connected"))


async def send_recording(links: list[tuple[str, CameraLink, bool]], recording: bool,
                         timeout: float) -> bool:
    """Send start/stop to all links at once; True if every camera confirmed."""
    verb = "start" if recording else "stop"
    print(f"{ts()}  sending {verb} to {', '.join(l for l, _, _ in links)}")
    t0 = time.monotonic()

    async def one(label: str, link: CameraLink) -> float | None:
        try:
            await link.set_recording(recording, timeout=timeout)
        except asyncio.TimeoutError:
            print(f"{ts()}  {label}: !! camera did not confirm {verb} within {timeout:.0f}s")
            return None
        dt = time.monotonic() - t0
        print(f"{ts()}  {label}: confirmed {'RECORDING' if recording else 'stopped'} "
              f"after {dt * 1000:.0f} ms")
        return dt

    results = await asyncio.gather(*(one(label, link) for label, link, _ in links))
    ok = [r for r in results if r is not None]
    if len(ok) == 2:
        print(f"{ts()}  confirmations {abs(ok[0] - ok[1]) * 1000:.0f} ms apart "
              "(BLE timing only, not frame-accurate sync)")
    return len(ok) == len(links)


async def watch_timecode(links: list[tuple[str, CameraLink, bool]], seconds: float) -> None:
    print(f"{ts()}  watching timecode for {seconds:.0f}s...")
    last: dict[str, str] = {}
    for label, link, _ in links:
        link.on_timecode = lambda tc, raw, label=label: last.__setitem__(label, str(tc))
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        await asyncio.sleep(min(1.0, remaining))
        print(f"{ts()}  " + "   ".join(f"{l}: {last.get(l, '--')}" for l, _, _ in links))


async def cmd_record(args, cfg) -> None:
    targets = ["A", "B"] if args.target.lower() == "both" else [args.target]
    links: list[tuple[str, CameraLink, bool]] = []
    try:
        # Shared adapter: connect one after the other, never simultaneously.
        for i, t in enumerate(targets):
            if i:
                await asyncio.sleep(cfg.ble.stagger_seconds)
            links.append(await open_link(t, cfg))

        for label, link, _ in links:
            info = await link.transport()
            ready = link.media_ready()
            state = "RECORDING" if info.recording else "not recording"
            print(f"{ts()}  {label}: {state}; media ready: {ready}")

        if args.action == "status":
            return
        if args.action == "toggle":
            recording = not all([(await l.transport()).recording for _, l, _ in links])
        else:
            recording = args.action == "start"

        if recording:
            for label, link, _ in links:
                if link.media_ready() is False:
                    print(f"{ts()}  {label}: warning, no slot reports media READY; "
                          "the camera will probably refuse")

        t_start = time.monotonic()
        ok = await send_recording(links, recording, args.timeout)
        if not recording or not ok:
            return
        if args.duration:
            # Count from the moment start was sent: confirmations can lag by seconds.
            await watch_timecode(links, args.duration - (time.monotonic() - t_start))
            await send_recording(links, False, args.timeout)
        elif args.watch:
            await watch_timecode(links, args.watch)
    finally:
        for label, link, was_connected in links:
            # Leave the link up if another program (e.g. monitor) had it open.
            if not was_connected:
                await link.disconnect()


async def cmd_time(args, cfg) -> None:
    targets = ["A", "B"] if args.target.lower() == "both" else [args.target]
    links: list[tuple[str, CameraLink, bool]] = []
    try:
        for i, t in enumerate(targets):
            if i:
                await asyncio.sleep(cfg.ble.stagger_seconds)
            links.append(await open_link(t, cfg))
        for label, link, _ in links:
            if args.action == "sync":
                sent, minutes, rtt = await link.sync_clock()
                local = sent.astimezone()
                print(f"{ts()}  {label}: set clock to {local:%Y-%m-%d %H:%M:%S} local "
                      f"({sent:%H:%M:%S} UTC, timezone UTC{minutes / 60:+g}h), "
                      f"write round trip {rtt * 1000:.0f} ms")
            # Does the camera report its clock back? Wait briefly for 7.0.
            try:
                await link.wait_for_param((7, 0), timeout=args.wait,
                                          accept_current=args.action == "check")
            except asyncio.TimeoutError:
                pass
            clock = link.camera_clock()
            if clock is None:
                print(f"{ts()}  {label}: camera did not report its clock (7.0) over Bluetooth; "
                      "verify on the camera's screen or in a clip's timecode")
                continue
            drift = (clock - datetime.now(clock.tzinfo)).total_seconds()
            flag = "  <-- more than 1 s off" if abs(drift) > 1 else ""
            print(f"{ts()}  {label}: camera clock {clock.astimezone():%Y-%m-%d %H:%M:%S}, "
                  f"drift {drift:+.1f} s{flag}")
    finally:
        for label, link, was_connected in links:
            if not was_connected:
                await link.disconnect()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ble.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find cameras advertising the Blackmagic service")
    s.add_argument("--timeout", type=float, default=10)
    s.add_argument("--adapter", default="hci0")

    s = sub.add_parser("assign", help="assign a BLE address to camera A or B")
    s.add_argument("camera", choices=["A", "B", "a", "b"])
    s.add_argument("address")
    s.add_argument("--adapter", help="Bluetooth adapter for this camera (default hci0)")

    s = sub.add_parser("pair", help="bond with a camera (enter the PIN it shows)")
    s.add_argument("target", help="A, B or a MAC address")
    s.add_argument("--force", action="store_true", help="try even if BlueZ says it is bonded")

    s = sub.add_parser("unpair", help="forget a camera's bond in BlueZ")
    s.add_argument("target")

    s = sub.add_parser("monitor", help="connect and print everything the camera sends")
    s.add_argument("target")
    s.add_argument("--seconds", type=float, help="stop after N seconds")
    s.add_argument("--timecode", action="store_true", help="print every timecode update")
    s.add_argument("--no-log", action="store_true", help="don't write logs/*.jsonl")

    s = sub.add_parser("record", help="start/stop recording on A, B or both")
    s.add_argument("target", help="A, B, both, or a MAC address")
    s.add_argument("action", choices=["start", "stop", "toggle", "status"])
    s.add_argument("--timeout", type=float, default=5, help="seconds to wait for confirmation")
    s.add_argument("--watch", type=float, default=5,
                   help="after starting, print timecode for N seconds (0 = off)")
    s.add_argument("--duration", type=float,
                   help="with 'start': record for N seconds, then stop (one connection)")

    s = sub.add_parser("time", help="sync the camera clock to this PC, or check it")
    s.add_argument("target", help="A, B, both, or a MAC address")
    s.add_argument("action", choices=["sync", "check"])
    s.add_argument("--wait", type=float, default=4,
                   help="seconds to wait for the camera to report its clock")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.load()
    handler = {"scan": cmd_scan, "assign": cmd_assign, "pair": cmd_pair,
               "unpair": cmd_unpair, "monitor": cmd_monitor, "record": cmd_record, "time": cmd_time}[args.cmd]
    try:
        asyncio.run(handler(args, cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
