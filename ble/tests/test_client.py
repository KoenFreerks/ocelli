"""CameraLink record logic against a fake camera (no Bluetooth needed)."""

import asyncio

import pytest

from ble import protocol as p
from ble.client import CameraLink

# camera A's real initial 10.1: preview, USB slot active, media CFast/SD/USB
INITIAL_TRANSPORT = bytes.fromhex("ff0a00000a010102000010000103")


class FakeCameraLink(CameraLink):
    """Echoes every transport command back as a status update, like the camera."""

    def __init__(self, accept: bool = True):
        super().__init__("00:00:00:00:00:01")
        self.accept = accept
        self.sent: list[bytes] = []

    @property
    def is_connected(self) -> bool:
        return True

    async def send(self, cmd: p.Command) -> None:
        pkt = p.encode(cmd)
        self.sent.append(pkt)
        if self.accept and cmd.key == (10, 1):
            reply = p.Command(10, 1, p.DataType.INT8, p.Operation.STATUS_UPDATE, cmd.values)
            loop = asyncio.get_running_loop()
            loop.call_later(0.01, self._handle_incoming, None, bytearray(p.encode(reply)))


def run(coro):
    return asyncio.run(coro)


def test_start_recording_echoes_full_transport_with_record_mode():
    async def go():
        link = FakeCameraLink()
        link._handle_incoming(None, bytearray(INITIAL_TRANSPORT))
        info = await link.set_recording(True, timeout=1)
        return link, info

    link, info = run(go())
    assert info.recording
    # same payload as the camera's report, only mode 0 -> 2
    assert link.sent == [bytes.fromhex("ff0a00000a010100020010000103" + "0000")]


def test_already_recording_sends_nothing():
    async def go():
        link = FakeCameraLink()
        rec = bytes.fromhex("ff0a00000a010102020010000103")
        link._handle_incoming(None, bytearray(rec))
        await link.set_recording(True, timeout=1)
        return link

    assert run(go()).sent == []


def test_camera_refusing_times_out():
    async def go():
        link = FakeCameraLink(accept=False)
        link._handle_incoming(None, bytearray(INITIAL_TRANSPORT))
        await link.set_recording(True, timeout=0.2)

    with pytest.raises(asyncio.TimeoutError):
        run(go())


def test_media_ready_from_status():
    link = FakeCameraLink()
    assert link.media_ready() is None
    link._handle_incoming(None, bytearray(bytes.fromhex("ff07000009010102000001")))
    assert link.media_ready() is True
    link._handle_incoming(None, bytearray(bytes.fromhex("ff07000009010102000000")))
    assert link.media_ready() is False


class FakeConnectLink(CameraLink):
    """Records connect timing; never actually talks to Bluetooth."""

    log: list = []

    def __init__(self, name: str, connect_time: float = 0.05):
        super().__init__("00:00:00:00:00:02")
        self.name = name
        self.connect_time = connect_time
        self.connects = 0

    async def connect(self, timeout: float = 20.0) -> None:
        self.disconnected.clear()
        self.log.append((self.name, "start", asyncio.get_running_loop().time()))
        await asyncio.sleep(self.connect_time)
        self.log.append((self.name, "end", asyncio.get_running_loop().time()))
        self.connects += 1

    async def start_session(self, power_on: bool = True) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def test_shared_lock_serialises_connects():
    from ble.client import maintain

    async def go():
        FakeConnectLink.log = []
        lock, stop = asyncio.Lock(), asyncio.Event()
        a, b = FakeConnectLink("A"), FakeConnectLink("B")
        tasks = [asyncio.ensure_future(maintain(l, stop, connect_lock=lock, settle=0.05,
                                                stale_after=0)) for l in (a, b)]
        await asyncio.sleep(0.4)
        stop.set()
        await asyncio.gather(*tasks)
        return FakeConnectLink.log

    log = run(go())
    # each connect finishes before the next one starts
    events = [(n, e) for n, e, _ in log]
    assert events == [("A", "start"), ("A", "end"), ("B", "start"), ("B", "end")]
    a_end = next(t for n, e, t in log if (n, e) == ("A", "end"))
    b_start = next(t for n, e, t in log if (n, e) == ("B", "start"))
    assert b_start - a_end >= 0.05  # settle time honoured


def test_silent_link_is_reconnected():
    from ble.client import maintain

    async def go():
        stop = asyncio.Event()
        link = FakeConnectLink("A", connect_time=0.01)
        states = []
        task = asyncio.ensure_future(maintain(
            link, stop, initial_delay=0.05, stale_after=1.5,
            on_state=lambda s, d: states.append(s)))
        await asyncio.sleep(3.0)  # never receives anything -> stale -> reconnect
        stop.set()
        await task
        return link.connects, states

    connects, states = run(go())
    assert "stale" in states
    assert connects >= 2
