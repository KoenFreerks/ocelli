"""A BlueZ pairing agent that asks the user for the camera's 6-digit PIN.

The camera displays the PIN (passkey) and we type it in, so the agent registers
with the "KeyboardOnly" capability and only implements passkey entry; any other
request is rejected.
"""

# No `from __future__ import annotations`: dbus-fast reads D-Bus signatures
# from the method annotations below.

import asyncio
import logging
from collections.abc import Awaitable, Callable

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method

log = logging.getLogger(__name__)

AGENT_PATH = "/org/ocelli/pairing_agent"
CAPABILITY = "KeyboardOnly"
REJECTED = "org.bluez.Error.Rejected"
CANCELED = "org.bluez.Error.Canceled"

# Receives the BlueZ device path (e.g. /org/bluez/hci0/dev_AA_BB_...) and
# returns the passkey, or None to cancel.
PasskeyPrompt = Callable[[str], Awaitable["int | None"]]


def address_from_path(device_path: str) -> str:
    return device_path.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":")


class _Agent1(ServiceInterface):
    def __init__(self, prompt: PasskeyPrompt) -> None:
        super().__init__("org.bluez.Agent1")
        self._prompt = prompt
        self._pending: asyncio.Task | None = None

    @dbus_method()
    def Release(self) -> None:
        log.debug("agent released by BlueZ")

    @dbus_method()
    async def RequestPasskey(self, device: "o") -> "u":
        self._pending = asyncio.ensure_future(self._prompt(device))
        try:
            passkey = await self._pending
        except asyncio.CancelledError:
            raise DBusError(CANCELED, "pairing cancelled")
        finally:
            self._pending = None
        if passkey is None:
            raise DBusError(CANCELED, "pairing cancelled")
        return int(passkey)

    @dbus_method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        log.info("BlueZ asks to display passkey %06d for %s", passkey, device)

    @dbus_method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError(REJECTED, "legacy PIN codes are not supported")

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:
        raise DBusError(REJECTED, "legacy PIN codes are not supported")

    @dbus_method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        log.warning("unexpected numeric-comparison request from %s; rejecting", device)
        raise DBusError(REJECTED, "numeric comparison not supported")

    @dbus_method()
    def RequestAuthorization(self, device: "o") -> None:
        raise DBusError(REJECTED, "unsolicited pairing rejected")

    @dbus_method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        raise DBusError(REJECTED, "service authorization not supported")

    @dbus_method()
    def Cancel(self) -> None:
        log.info("BlueZ cancelled the pairing request")
        if self._pending is not None:
            self._pending.cancel()


class PairingAgent:
    """Registers the agent as BlueZ's default for as long as it is running.

    Use as ``async with PairingAgent(prompt): ...``.
    """

    def __init__(self, prompt: PasskeyPrompt) -> None:
        self._iface = _Agent1(prompt)
        self._bus: MessageBus | None = None
        self._manager = None

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus.export(AGENT_PATH, self._iface)
        introspection = await self._bus.introspect("org.bluez", "/org/bluez")
        obj = self._bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        self._manager = obj.get_interface("org.bluez.AgentManager1")
        await self._manager.call_register_agent(AGENT_PATH, CAPABILITY)
        await self._manager.call_request_default_agent(AGENT_PATH)
        log.debug("pairing agent registered at %s", AGENT_PATH)

    async def stop(self) -> None:
        if self._bus is None:
            return
        try:
            if self._manager is not None:
                await self._manager.call_unregister_agent(AGENT_PATH)
        except DBusError as e:
            log.debug("unregister agent: %s", e)
        self._bus.unexport(AGENT_PATH)
        self._bus.disconnect()
        self._bus = None

    async def set_trusted(self, device_path: str, trusted: bool = True) -> None:
        """Mark a device as trusted in BlueZ (as `bluetoothctl trust` does)."""
        from dbus_fast import Variant

        from .client import bluez_call

        assert self._bus is not None
        await bluez_call(self._bus, device_path, "org.freedesktop.DBus.Properties", "Set",
                         "ssv", ["org.bluez.Device1", "Trusted", Variant("b", trusted)])

    async def __aenter__(self) -> "PairingAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()


def device_path(adapter: str, address: str) -> str:
    return f"/org/bluez/{adapter}/dev_{address.upper().replace(':', '_')}"
