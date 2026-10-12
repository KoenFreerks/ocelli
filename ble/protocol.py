"""Blackmagic Camera Control protocol: packet encoding and decoding.

Everything here is taken from docs/PROTOCOL_NOTES.md, which cites the
Blackmagic Camera Control PDF (Aug 2025, SDI protocol 1.6.2) and, where the
PDF is silent, Blackmagic's own Cameras Code Samples 1.2. Nothing is guessed:
parameters we cannot decode are passed through as raw values.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, IntFlag
from typing import Union

# --- Bluetooth LE (PDF p. 109-110) -------------------------------------------

SERVICE_UUID = "291d567a-6d75-11e6-8b77-86f30ca893d3"
OUTGOING_CONTROL_UUID = "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"
INCOMING_CONTROL_UUID = "b864e140-76a0-416a-bf30-5876504537d9"
TIMECODE_UUID = "6d8f2110-86f1-41bf-9afb-451d87e976c8"
CAMERA_STATUS_UUID = "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"
DEVICE_NAME_UUID = "ffac0c52-c9fb-41a0-b063-cc76282eb89c"
PROTOCOL_VERSION_UUID = "8f1fd018-b508-456f-8f82-3d392bee2706"
MANUFACTURER_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"

DEVICE_NAME_MAX = 32


class CameraStatus(IntFlag):
    """Flags of the Camera Status characteristic."""

    POWER_ON = 0x01
    CONNECTED = 0x02
    PAIRED = 0x04
    VERSIONS_VERIFIED = 0x08
    INITIAL_PAYLOAD_RECEIVED = 0x10
    CAMERA_READY = 0x20


POWER_ON = b"\x01"
POWER_OFF = b"\x00"

# --- Packet format (PDF p. 96-97) ---------------------------------------------

BROADCAST = 255
HEADER_SIZE = 4
MAX_PACKET_SIZE = 64
CMD_CHANGE_CONFIGURATION = 0


class DataType(IntEnum):
    VOID = 0  # also boolean
    INT8 = 1
    INT16 = 2
    INT32 = 3
    INT64 = 4
    STRING = 5
    FIXED16 = 128


class Operation(IntEnum):
    ASSIGN = 0
    OFFSET = 1
    STATUS_UPDATE = 2  # camera -> host; from the code samples, not the PDF


_STRUCT_CODE = {
    DataType.VOID: "B",
    DataType.INT8: "b",
    DataType.INT16: "h",
    DataType.INT32: "i",
    DataType.INT64: "q",
    DataType.FIXED16: "h",
}

FIXED16_SCALE = 2048  # signed 5.11 fixed point


def to_fixed16(value: float) -> int:
    raw = round(value * FIXED16_SCALE)
    return max(-32768, min(32767, raw))


def from_fixed16(raw: int) -> float:
    return raw / FIXED16_SCALE


def _pad_len(n: int) -> int:
    return (-n) % 4


class ProtocolError(ValueError):
    pass


Value = Union[int, float, str]


@dataclass(frozen=True)
class Command:
    """A 'change configuration' command (command id 0).

    ``values`` holds Python numbers: floats for FIXED16, ints for the integer
    and boolean types, or a single str for STRING.
    """

    category: int
    parameter: int
    data_type: DataType
    operation: Operation = Operation.ASSIGN
    values: tuple[Value, ...] = ()
    destination: int = BROADCAST
    raw: bytes = field(default=b"", compare=False, repr=False)
    malformed: bool = field(default=False, compare=False)

    @property
    def key(self) -> tuple[int, int]:
        return (self.category, self.parameter)

    @property
    def param_id(self) -> str:
        return f"{self.category}.{self.parameter}"


@dataclass(frozen=True)
class UnknownCommand:
    """A packet with a command id other than 0; kept as raw data."""

    destination: int
    command_id: int
    data: bytes
    raw: bytes = field(default=b"", compare=False, repr=False)


def _pack_values(data_type: DataType, values: tuple[Value, ...]) -> bytes:
    if data_type == DataType.STRING:
        if len(values) != 1 or not isinstance(values[0], str):
            raise ProtocolError("STRING commands take exactly one str value")
        return values[0].encode("utf-8")
    code = _STRUCT_CODE[DataType(data_type)]
    if data_type == DataType.FIXED16:
        ints = [to_fixed16(float(v)) for v in values]
    elif data_type == DataType.VOID:
        ints = [1 if v else 0 for v in values]
    else:
        ints = [int(v) for v in values]
    return struct.pack(f"<{len(ints)}{code}", *ints)


def _unpack_values(data_type: int, payload: bytes) -> tuple[tuple[Value, ...], bool]:
    """Return (values, malformed)."""
    if data_type == DataType.STRING:
        return (payload.decode("utf-8", errors="replace"),), False
    try:
        dt = DataType(data_type)
    except ValueError:
        return tuple(payload), True
    code = _STRUCT_CODE[dt]
    size = struct.calcsize(code)
    if len(payload) % size:
        return tuple(payload), True
    ints = struct.unpack(f"<{len(payload) // size}{code}", payload)
    if dt == DataType.FIXED16:
        return tuple(from_fixed16(i) for i in ints), False
    return tuple(ints), False


def encode(cmd: Command) -> bytes:
    """Encode a command into a packet padded to a 4-byte boundary."""
    payload = _pack_values(cmd.data_type, cmd.values)
    length = 4 + len(payload)  # category, parameter, type, operation + data
    body = bytes(
        [
            cmd.destination,
            length,
            CMD_CHANGE_CONFIGURATION,
            0,
            cmd.category,
            cmd.parameter,
            int(cmd.data_type),
            int(cmd.operation),
        ]
    ) + payload
    packet = body + b"\x00" * _pad_len(len(body))
    if len(packet) > MAX_PACKET_SIZE:
        raise ProtocolError(f"packet is {len(packet)} bytes, max {MAX_PACKET_SIZE}")
    return packet


def decode(data: bytes) -> list[Command | UnknownCommand]:
    """Decode one or more concatenated packets.

    Raises ProtocolError on a truncated packet; trailing zero padding is ignored.
    """
    data = bytes(data)
    out: list[Command | UnknownCommand] = []
    offset = 0
    while offset + HEADER_SIZE <= len(data):
        dest, length, command_id, _reserved = data[offset : offset + HEADER_SIZE]
        start = offset + HEADER_SIZE
        end = start + length
        if end > len(data):
            raise ProtocolError(
                f"truncated packet at offset {offset}: length {length}, "
                f"{len(data) - start} bytes left"
            )
        body = data[start:end]
        raw = data[offset:end]
        if command_id == CMD_CHANGE_CONFIGURATION and length >= 4:
            category, parameter, data_type, operation = body[:4]
            values, malformed = _unpack_values(data_type, body[4:])
            try:
                dt: DataType | int = DataType(data_type)
            except ValueError:
                dt = data_type
            try:
                op: Operation | int = Operation(operation)
            except ValueError:
                op = operation
            out.append(
                Command(category, parameter, dt, op, values, dest, raw, malformed)  # type: ignore[arg-type]
            )
        else:
            out.append(UnknownCommand(dest, command_id, body, raw))
        offset = end + _pad_len(length)
    rest = data[offset:]
    if rest.strip(b"\x00"):
        raise ProtocolError(f"{len(rest)} unparsed trailing bytes: {rest.hex(' ')}")
    return out


# --- Parameter registry ------------------------------------------------------
# source "pdf" = Camera Control PDF tables, "sample" = Blackmagic code samples 1.2.


@dataclass(frozen=True)
class Param:
    name: str
    data_type: DataType
    source: str = "pdf"


PARAMS: dict[tuple[int, int], Param] = {
    (0, 0): Param("Focus", DataType.FIXED16),
    (0, 1): Param("Instantaneous autofocus", DataType.VOID),
    (0, 2): Param("Aperture (f-stop)", DataType.FIXED16),
    (0, 3): Param("Aperture (normalised)", DataType.FIXED16),
    (0, 4): Param("Aperture (ordinal)", DataType.INT16),
    (0, 5): Param("Instantaneous auto aperture", DataType.VOID),
    (0, 6): Param("Optical image stabilisation", DataType.VOID),
    (0, 7): Param("Absolute zoom (mm)", DataType.INT16),
    (0, 8): Param("Absolute zoom (normalised)", DataType.FIXED16),
    (0, 9): Param("Continuous zoom (speed)", DataType.FIXED16),
    (1, 0): Param("Video mode", DataType.INT8),
    (1, 1): Param("Gain (up to Camera 4.9)", DataType.INT8),
    (1, 2): Param("Manual white balance", DataType.INT16),
    (1, 3): Param("Set auto WB", DataType.VOID),
    (1, 4): Param("Restore auto WB", DataType.VOID),
    (1, 5): Param("Exposure (us)", DataType.INT32),
    (1, 6): Param("Exposure (ordinal)", DataType.INT16),
    (1, 7): Param("Dynamic range mode", DataType.INT8),
    (1, 8): Param("Video sharpening level", DataType.INT8),
    (1, 9): Param("Recording format", DataType.INT16),
    (1, 10): Param("Auto exposure mode", DataType.INT8),
    (1, 11): Param("Shutter angle", DataType.INT32),
    (1, 12): Param("Shutter speed", DataType.INT32),
    (1, 13): Param("Gain (dB)", DataType.INT8),
    (1, 14): Param("ISO", DataType.INT32),
    (1, 15): Param("Display LUT", DataType.INT8),
    (1, 16): Param("ND filter stop", DataType.FIXED16),
    (2, 0): Param("Mic level", DataType.FIXED16),
    (2, 1): Param("Headphone level", DataType.FIXED16),
    (2, 2): Param("Headphone program mix", DataType.FIXED16),
    (2, 3): Param("Speaker level", DataType.FIXED16),
    (2, 4): Param("Input type", DataType.INT8),
    (2, 5): Param("Input levels", DataType.FIXED16),
    (2, 6): Param("Phantom power", DataType.VOID),
    (3, 0): Param("Overlay enables", DataType.INT16),
    (3, 3): Param("Overlays", DataType.INT8),
    (4, 0): Param("Brightness", DataType.FIXED16),
    (4, 1): Param("Exposure and focus tools", DataType.INT16),
    (4, 2): Param("Zebra level", DataType.FIXED16),
    (4, 3): Param("Peaking level", DataType.FIXED16),
    (4, 4): Param("Color bar enable", DataType.INT8),
    (4, 5): Param("Focus assist", DataType.INT8),
    (4, 6): Param("Program return feed enable", DataType.INT8),
    (4, 7): Param("Timecode source", DataType.INT8),
    (5, 0): Param("Tally brightness", DataType.FIXED16),
    (5, 1): Param("Front tally brightness", DataType.FIXED16),
    (5, 2): Param("Rear tally brightness", DataType.FIXED16),
    (6, 0): Param("Reference source", DataType.INT8),
    (6, 1): Param("Reference offset", DataType.INT32),
    (7, 0): Param("Real time clock", DataType.INT32),
    (7, 1): Param("System language", DataType.STRING),
    (7, 2): Param("Timezone", DataType.INT32),
    (7, 3): Param("Location", DataType.INT64),
    (8, 0): Param("Lift adjust", DataType.FIXED16),
    (8, 1): Param("Gamma adjust", DataType.FIXED16),
    (8, 2): Param("Gain adjust", DataType.FIXED16),
    (8, 3): Param("Offset adjust", DataType.FIXED16),
    (8, 4): Param("Contrast adjust", DataType.FIXED16),
    (8, 5): Param("Luma mix", DataType.FIXED16),
    (8, 6): Param("Color adjust", DataType.FIXED16),
    (8, 7): Param("Correction reset default", DataType.VOID),
    (9, 0): Param("Battery", DataType.INT16, "sample+observed"),
    (9, 1): Param("Media status", DataType.INT8, "sample"),
    (9, 2): Param("Remaining record time", DataType.INT16, "sample"),
    (9, 3): Param("Display thresholds (layout undocumented)", DataType.INT16, "sample"),
    (9, 4): Param("Display timecode", DataType.INT32, "sample"),
    (9, 5): Param("Camera spec", DataType.INT8, "sample"),
    (9, 6): Param("Switcher status (layout undocumented)", DataType.INT8, "sample"),
    (9, 7): Param("Display parameters (layout undocumented)", DataType.INT16, "sample"),
    (10, 0): Param("Codec", DataType.INT8),
    (10, 1): Param("Transport mode", DataType.INT8),
    (10, 2): Param("Playback control", DataType.INT8),
    (10, 5): Param("Stream", DataType.VOID),
    (10, 6): Param("Stream information", DataType.VOID),
    (10, 7): Param("Stream display 3D LUT", DataType.VOID),
}


def param_name(category: int, parameter: int) -> str:
    p = PARAMS.get((category, parameter))
    return p.name if p else "undocumented"


# --- Typed views of specific parameters --------------------------------------


class TransportMode(IntEnum):
    PREVIEW = 0
    PLAY = 1
    RECORD = 2


class StorageMedium(IntEnum):
    CFAST = 0
    SD = 1
    SSD = 2
    USB = 3  # code samples only


# flags byte of 10.1; slot-active masks in slot order (code samples)
TRANSPORT_LOOP = 0x01
TRANSPORT_PLAY_ALL = 0x02
TRANSPORT_TIMELAPSE = 0x80
SLOT_ACTIVE_MASKS = (0x20, 0x40, 0x10)


@dataclass(frozen=True)
class TransportInfo:
    """Parameter 10.1: [mode, speed, flags, medium slot 1, medium slot 2, ...]."""

    mode: int
    speed: int = 0
    flags: int = 0
    media: tuple[int, ...] = ()

    @classmethod
    def from_values(cls, values: tuple[Value, ...]) -> TransportInfo:
        if len(values) < 3:
            raise ProtocolError(f"transport mode needs >= 3 values, got {len(values)}")
        mode, speed, flags, *media = (int(v) for v in values)
        return cls(mode, speed, flags & 0xFF, tuple(m & 0xFF for m in media))

    def to_values(self) -> tuple[int, ...]:
        # INT8 is signed on the wire; flags/media are bit patterns, so wrap them.
        def s8(v: int) -> int:
            return v - 256 if v > 127 else v

        return (self.mode, self.speed, s8(self.flags), *(s8(m) for m in self.media))

    @property
    def recording(self) -> bool:
        return self.mode == TransportMode.RECORD

    def slot_active(self, index: int) -> bool:
        return index < len(SLOT_ACTIVE_MASKS) and bool(self.flags & SLOT_ACTIVE_MASKS[index])

    def with_mode(self, mode: int) -> TransportInfo:
        return TransportInfo(mode, self.speed, self.flags, self.media)


class BatteryFlag(IntFlag):
    """9.0 flags (code samples BatteryStatus enum)."""

    BATTERY_PRESENT = 0x01
    EXTERNAL_POWER = 0x02  # "ACPresent" in the samples
    CHARGING = 0x04
    PERCENT_ESTIMATED = 0x08
    PREFER_VOLTAGE = 0x10


@dataclass(frozen=True)
class Battery:
    """Parameter 9.0: int16 [millivolts, percent, flags].

    The flags come from the code samples; the element order was confirmed on a
    Pocket 4K (2026-10-12): unplugging DC dropped [0] from ~11.9 V to 7.8 V and
    cleared EXTERNAL_POWER. [0] is the voltage of whatever powers the camera.
    """

    millivolts: int
    percent: int
    flags: BatteryFlag

    @classmethod
    def from_values(cls, values: tuple[Value, ...]) -> Battery:
        if len(values) < 3:
            raise ProtocolError(f"battery needs 3 values, got {len(values)}")
        mv, pct, flags = (int(v) for v in values[:3])
        return cls(mv, pct, BatteryFlag(flags & 0x1F))

    @property
    def external_power(self) -> bool:
        return BatteryFlag.EXTERNAL_POWER in self.flags

    @property
    def charging(self) -> bool:
        return BatteryFlag.CHARGING in self.flags

    def label(self) -> str:
        parts = [f"{self.percent}%" if BatteryFlag.BATTERY_PRESENT in self.flags else "no battery",
                 f"{self.millivolts / 1000:.1f}V"]
        if self.external_power:
            parts.append("DC")
        if self.charging:
            parts.append("charging")
        return " ".join(parts)


class MediaStatus(IntEnum):
    NONE = 0
    READY = 1
    MOUNT_ERROR = -1
    RECORD_ERROR = -2


@dataclass(frozen=True)
class RemainingTime:
    """One slot of parameter 9.2."""

    seconds: int
    overflow: bool = False  # more time than the protocol can express

    @classmethod
    def from_raw(cls, raw: int) -> RemainingTime:
        if raw >= 0:
            return cls(raw)
        if raw == -32768:
            return cls(32767 * 60, overflow=True)
        return cls(-raw * 60)

    @property
    def full(self) -> bool:
        return self.seconds == 0 and not self.overflow

    def label(self) -> str:
        if self.full:
            return "FULL"
        h, rem = divmod(self.seconds, 3600)
        m, s = divmod(rem, 60)
        text = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        return text + ("+" if self.overflow else "")


BASIC_CODECS = {0: "CinemaDNG", 1: "DNxHD", 2: "ProRes", 3: "Blackmagic RAW"}
PRORES_VARIANTS = {0: "HQ", 1: "422", 2: "LT", 3: "Proxy", 4: "444", 5: "444XQ"}
BRAW_VARIANTS = {0: "Q0", 7: "Q1", 8: "Q3", 1: "Q5", 2: "3:1", 3: "5:1", 4: "8:1", 5: "12:1"}
CDNG_VARIANTS = {0: "uncompressed", 1: "lossy 3:1", 2: "lossy 4:1"}


def codec_label(values: tuple[Value, ...]) -> str:
    basic = int(values[0]) if values else -1
    variant = int(values[1]) if len(values) > 1 else None
    name = BASIC_CODECS.get(basic, f"codec {basic}")
    table = {0: CDNG_VARIANTS, 2: PRORES_VARIANTS, 3: BRAW_VARIANTS}.get(basic, {})
    if variant is None:
        return name
    return f"{name} {table.get(variant, variant)}"


def aperture_fstop_from_av(av: float) -> float:
    """PDF 0.2: f-number = sqrt(2^AV)."""
    return math.sqrt(2**av)


def av_from_fstop(fstop: float) -> float:
    return 2 * math.log2(fstop)


def describe(cmd: Command) -> str:
    """Human-readable 'name = value' for a decoded command."""
    v = cmd.values
    name = param_name(cmd.category, cmd.parameter)
    try:
        match cmd.key:
            case (1, 14):
                return f"ISO = {v[0]}"
            case (1, 2):
                return f"White balance = {v[0]}K tint {v[1]}"
            case (1, 11):
                return f"Shutter angle = {v[0] / 100:g}°"
            case (1, 12):
                return f"Shutter speed = 1/{v[0]}"
            case (1, 5):
                return f"Exposure = {v[0]} µs"
            case (1, 13):
                return f"Gain = {v[0]} dB"
            case (0, 0):
                return f"Focus = {v[0]:.3f}"
            case (0, 2):
                if v[0] == -16.0:  # 0x8000 = no lens (code samples kLensAperture_NoLens)
                    return "Aperture = no lens"
                return f"Aperture = f/{aperture_fstop_from_av(v[0]):.1f}"
            case (0, 3):
                return f"Aperture (normalised) = {v[0]:.3f}"
            case (1, 9):
                fps, sensor_fps, w, h, flags = v[:5]
                off = f" sensor {sensor_fps}fps" if flags & 0x04 else ""
                return f"Recording format = {w}x{h} @ {fps}fps{off} flags=0x{flags & 0xFFFF:02x}"
            case (10, 0):
                return f"Codec = {codec_label(v)}"
            case (10, 1):
                t = TransportInfo.from_values(v)
                mode = TransportMode(t.mode).name if t.mode in (0, 1, 2) else str(t.mode)
                slots = []
                for i, m in enumerate(t.media):
                    medium = StorageMedium(m).name if m in (0, 1, 2, 3) else str(m)
                    slots.append(f"{medium}{'*' if t.slot_active(i) else ''}")
                return f"Transport = {mode} speed {t.speed} slots [{', '.join(slots)}]"
            case (7, 0):
                clock = decode_clock(v)
                return f"Real time clock = {clock:%Y-%m-%d %H:%M:%S} UTC" if clock else f"Real time clock = {list(v)} (not BCD)"
            case (7, 2):
                return f"Timezone = UTC{int(v[0]) / 60:+g}h"
            case (9, 0):
                return f"Battery = {Battery.from_values(v).label()}"
            case (9, 1):
                states = [MediaStatus(x).name if x in (0, 1, -1, -2) else str(x) for x in v]
                return f"Media status = {states}"
            case (9, 2):
                return f"Remaining time = {[RemainingTime.from_raw(int(x)).label() for x in v]}"
    except (IndexError, ValueError, TypeError):
        pass
    return f"{name} = {list(v)}"


# --- Timecode characteristic ---------------------------------------------------

TIMECODE_DROP_FRAME = 0x80000000


@dataclass(frozen=True)
class Timecode:
    hours: int
    minutes: int
    seconds: int
    frames: int
    drop_frame: bool = False

    def __str__(self) -> str:
        sep = ";" if self.drop_frame else ":"
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{sep}{self.frames:02d}"


def _bcd_byte(b: int) -> int | None:
    hi, lo = b >> 4, b & 0x0F
    if hi > 9 or lo > 9:
        return None
    return hi * 10 + lo


def decode_timecode(data: bytes) -> Timecode | None:
    """Decode a Timecode notification.

    Per the code samples the notification is a full packet with the BCD value
    as a little-endian uint32 at bytes 8-11. A bare 4-byte value is accepted too.
    Returns None if the digits are not valid BCD.
    """
    data = bytes(data)
    if len(data) >= 12:
        word = struct.unpack_from("<I", data, 8)[0]
    elif len(data) == 4:
        word = struct.unpack("<I", data)[0]
    else:
        return None
    drop = bool(word & TIMECODE_DROP_FRAME)
    hh = _bcd_byte((word >> 24) & 0x3F)
    mm = _bcd_byte((word >> 16) & 0xFF)
    ss = _bcd_byte((word >> 8) & 0xFF)
    ff = _bcd_byte(word & 0xFF)
    if None in (hh, mm, ss, ff):
        return None
    return Timecode(hh, mm, ss, ff, drop)  # type: ignore[arg-type]


# --- Command builders ----------------------------------------------------------


def _cmd(cat: int, param: int, dt: DataType, *values: Value, dest: int = BROADCAST,
         op: Operation = Operation.ASSIGN) -> Command:
    return Command(cat, param, dt, op, tuple(values), dest)


def set_iso(iso: int, dest: int = BROADCAST) -> Command:
    return _cmd(1, 14, DataType.INT32, iso, dest=dest)


def set_white_balance(kelvin: int, tint: int = 0, dest: int = BROADCAST) -> Command:
    return _cmd(1, 2, DataType.INT16, kelvin, tint, dest=dest)


def auto_white_balance(dest: int = BROADCAST) -> Command:
    return _cmd(1, 3, DataType.VOID, dest=dest)


def set_shutter_angle(degrees: float, dest: int = BROADCAST) -> Command:
    return _cmd(1, 11, DataType.INT32, round(degrees * 100), dest=dest)


def set_shutter_speed(denominator: int, dest: int = BROADCAST) -> Command:
    return _cmd(1, 12, DataType.INT32, denominator, dest=dest)


def set_focus(normalised: float, dest: int = BROADCAST) -> Command:
    return _cmd(0, 0, DataType.FIXED16, normalised, dest=dest)


def autofocus(dest: int = BROADCAST) -> Command:
    return _cmd(0, 1, DataType.VOID, dest=dest)


def set_aperture_fstop(fstop: float, dest: int = BROADCAST) -> Command:
    return _cmd(0, 2, DataType.FIXED16, av_from_fstop(fstop), dest=dest)


def set_aperture_normalised(value: float, dest: int = BROADCAST) -> Command:
    return _cmd(0, 3, DataType.FIXED16, value, dest=dest)


def auto_aperture(dest: int = BROADCAST) -> Command:
    return _cmd(0, 5, DataType.VOID, dest=dest)


def to_bcd(value: int, digits: int) -> int:
    if value < 0 or value >= 10**digits:
        raise ProtocolError(f"{value} does not fit in {digits} BCD digits")
    out = 0
    for shift in range(digits):
        out |= (value // 10**shift % 10) << (4 * shift)
    return out


def from_bcd(value: int, digits: int) -> int | None:
    value &= (1 << (4 * digits)) - 1
    out = 0
    for shift in reversed(range(digits)):
        nibble = (value >> (4 * shift)) & 0xF
        if nibble > 9:
            return None
        out = out * 10 + nibble
    return out


def set_clock(utc: datetime, dest: int = BROADCAST) -> Command:
    """7.0 real time clock: [BCD HHMMSSFF (UTC), BCD YYYYMMDD].

    FF is always sent as 00; callers send it on a whole second.
    """
    if utc.tzinfo is not None:
        utc = utc.astimezone(timezone.utc).replace(tzinfo=None)
    t = to_bcd(utc.hour * 1_000_000 + utc.minute * 10_000 + utc.second * 100, 8)
    d = to_bcd(utc.year * 10_000 + utc.month * 100 + utc.day, 8)
    return _cmd(7, 0, DataType.INT32, t, d, dest=dest)


def set_timezone(minutes: int, dest: int = BROADCAST) -> Command:
    """7.2 timezone: minutes offset from UTC."""
    return _cmd(7, 2, DataType.INT32, minutes, dest=dest)


def decode_clock(values: tuple[Value, ...]) -> datetime | None:
    """Inverse of set_clock; returns a UTC datetime (frames dropped) or None."""
    if len(values) < 2:
        return None
    t = from_bcd(int(values[0]) & 0xFFFFFFFF, 8)
    d = from_bcd(int(values[1]) & 0xFFFFFFFF, 8)
    if t is None or d is None:
        return None
    try:
        return datetime(d // 10_000, d // 100 % 100, d % 100,
                        t // 1_000_000, t // 10_000 % 100, t // 100 % 100, tzinfo=timezone.utc)
    except ValueError:
        return None


def set_transport(info: TransportInfo, dest: int = BROADCAST) -> Command:
    """Send a full 10.1 payload; build it from the camera's last report."""
    return _cmd(10, 1, DataType.INT8, *info.to_values(), dest=dest)
