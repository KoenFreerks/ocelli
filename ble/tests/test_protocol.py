"""Packet tests. Expected bytes come from the PDF 'Example Protocol Packets' (p. 105)."""

import pytest

from ble import protocol as p

# (description, command, expected bytes) straight from the PDF table
PDF_EXAMPLES = [
    (
        "trigger instantaneous auto focus on camera 4",
        p.Command(0, 1, p.DataType.VOID, p.Operation.ASSIGN, (), destination=4),
        [4, 4, 0, 0, 0, 1, 0, 0],
    ),
    (
        "turn on OIS on all cameras",
        p.Command(0, 6, p.DataType.VOID, p.Operation.ASSIGN, (1,), destination=255),
        [255, 5, 0, 0, 0, 6, 0, 0, 1, 0, 0, 0],
    ),
    (
        "set exposure to 10 ms on camera 4",
        p.Command(1, 5, p.DataType.INT32, p.Operation.ASSIGN, (10000,), destination=4),
        [4, 8, 0, 0, 1, 5, 3, 0, 0x10, 0x27, 0x00, 0x00],
    ),
    (
        "add 15% to zebra level",
        p.Command(4, 2, p.DataType.FIXED16, p.Operation.OFFSET, (0.15,), destination=4),
        [4, 6, 0, 0, 4, 2, 128, 1, 0x33, 0x01, 0, 0],
    ),
    (
        "select 1080p 23.98 mode on all cameras",
        p.Command(1, 0, p.DataType.INT8, p.Operation.ASSIGN, (24, 1, 3, 0, 0), destination=255),
        [255, 9, 0, 0, 1, 0, 1, 0, 24, 1, 3, 0, 0, 0, 0, 0],
    ),
    (
        "subtract 0.3 from gamma adjust for green & blue",
        p.Command(8, 1, p.DataType.FIXED16, p.Operation.OFFSET, (0.0, -0.3, -0.3, 0.0), destination=4),
        [4, 12, 0, 0, 8, 1, 128, 1, 0, 0, 0x9A, 0xFD, 0x9A, 0xFD, 0, 0],
    ),
]


@pytest.mark.parametrize("desc,cmd,expected", PDF_EXAMPLES, ids=[e[0] for e in PDF_EXAMPLES])
def test_encode_matches_pdf_examples(desc, cmd, expected):
    assert p.encode(cmd) == bytes(expected)


@pytest.mark.parametrize("desc,cmd,expected", PDF_EXAMPLES, ids=[e[0] for e in PDF_EXAMPLES])
def test_decode_pdf_examples(desc, cmd, expected):
    (decoded,) = p.decode(bytes(expected))
    assert decoded.key == cmd.key
    assert decoded.destination == cmd.destination
    assert decoded.data_type == cmd.data_type
    assert decoded.operation == cmd.operation
    assert decoded.values == pytest.approx(cmd.values, abs=1 / 2048)
    assert not decoded.malformed


def test_all_operations_combined_is_76_bytes_and_decodes():
    stream = b"".join(bytes(e[2]) for e in PDF_EXAMPLES)
    assert len(stream) == 76  # PDF: "all operations combined", length 76
    decoded = p.decode(stream)
    assert [d.key for d in decoded] == [e[1].key for e in PDF_EXAMPLES]


@pytest.mark.parametrize("n_values", range(0, 13))
def test_every_packet_is_padded_to_4_bytes(n_values):
    cmd = p.Command(1, 0, p.DataType.INT8, values=tuple(range(n_values)))
    pkt = p.encode(cmd)
    assert len(pkt) % 4 == 0
    assert pkt[1] == 4 + n_values  # length excludes header and padding
    assert pkt[8 + n_values :] == b"\x00" * (len(pkt) - 8 - n_values)


def test_packet_size_limit():
    p.encode(p.Command(7, 1, p.DataType.STRING, values=("x" * 56,)))  # 8 + 56 = 64
    with pytest.raises(p.ProtocolError):
        p.encode(p.Command(7, 1, p.DataType.STRING, values=("x" * 57,)))


def test_fixed16_range_and_clamp():
    assert p.to_fixed16(1.0) == 2048
    assert p.to_fixed16(-16.0) == -32768
    assert p.to_fixed16(15 + 2047 / 2048) == 32767
    assert p.to_fixed16(100.0) == 32767
    assert p.from_fixed16(0x0133) == pytest.approx(0.15, abs=1 / 2048)


def test_truncated_packet_raises():
    with pytest.raises(p.ProtocolError):
        p.decode(bytes([255, 8, 0, 0, 1, 14, 3, 0, 0x40]))


def test_malformed_payload_kept_raw():
    # INT32 type with 3 data bytes
    (cmd,) = p.decode(bytes([255, 7, 0, 0, 1, 14, 3, 2, 1, 2, 3, 0]))
    assert cmd.malformed
    assert cmd.values == (1, 2, 3)


def test_unknown_command_id_passthrough():
    (cmd,) = p.decode(bytes([255, 2, 9, 0, 0xAA, 0xBB, 0, 0]))
    assert isinstance(cmd, p.UnknownCommand)
    assert cmd.command_id == 9 and cmd.data == b"\xaa\xbb"


def test_status_update_operation_decodes():
    # camera -> host ISO 1600 report, operation 2 (code samples)
    (cmd,) = p.decode(p.encode(p.Command(1, 14, p.DataType.INT32, p.Operation.STATUS_UPDATE, (1600,))))
    assert cmd.operation == p.Operation.STATUS_UPDATE
    assert p.describe(cmd) == "ISO = 1600"


def test_builders():
    assert p.encode(p.set_iso(800, dest=1)) == bytes([1, 8, 0, 0, 1, 14, 3, 0, 0x20, 0x03, 0, 0])
    assert p.encode(p.set_white_balance(5600, 10)) == bytes(
        [255, 8, 0, 0, 1, 2, 2, 0, 0xE0, 0x15, 10, 0]
    )
    assert p.set_shutter_angle(172.8).values == (17280,)
    assert p.set_aperture_fstop(4.0).values[0] == pytest.approx(4.0)  # AV = 2*log2(4)
    assert p.aperture_fstop_from_av(4.0) == pytest.approx(4.0)


def test_transport_roundtrip_and_record():
    # mode preview, speed 0, flags disk1 active, slots CFast/SD/USB
    values = (0, 0, 0x20, 0, 1, 3)
    info = p.TransportInfo.from_values(values)
    assert not info.recording and info.slot_active(0) and not info.slot_active(1)
    rec = p.set_transport(info.with_mode(p.TransportMode.RECORD))
    pkt = p.encode(rec)
    assert pkt == bytes([255, 10, 0, 0, 10, 1, 1, 0, 2, 0, 0x20, 0, 1, 3, 0, 0])
    (back,) = p.decode(pkt)
    assert p.TransportInfo.from_values(back.values).recording


def test_transport_high_flag_bit_survives_signed_int8():
    info = p.TransportInfo(2, 0, 0x80 | 0x20, (2,))
    (back,) = p.decode(p.encode(p.set_transport(info)))
    assert p.TransportInfo.from_values(back.values) == info


def test_remaining_time():
    assert p.RemainingTime.from_raw(90).label() == "01:30"
    assert p.RemainingTime.from_raw(-125).label() == "02:05:00"
    assert p.RemainingTime.from_raw(-32768).label().endswith("+")
    assert p.RemainingTime.from_raw(0).label() == "FULL"


def test_timecode_full_packet_little_endian():
    word = 0x09125310  # PDF example 09:12:53:10
    data = bytes([255, 8, 0, 0, 0, 0, 0, 0]) + word.to_bytes(4, "little")
    assert str(p.decode_timecode(data)) == "09:12:53:10"


def test_timecode_bare_value_and_drop_frame():
    assert str(p.decode_timecode((0x80000000 | 0x01020304).to_bytes(4, "little"))) == "01:02:03;04"


def test_timecode_invalid_bcd_returns_none():
    assert p.decode_timecode((0x0000000A).to_bytes(4, "little")) is None


def test_camera_status_flags():
    s = p.CameraStatus(0x3F)
    assert p.CameraStatus.PAIRED in s and p.CameraStatus.CAMERA_READY in s


@pytest.mark.parametrize("hexdata,label,ext,charging", [
    # captured from camera A, 2026-10-12
    ("ff0a0000090002026e2e64000b00", "100% 11.9V DC", True, False),   # on DC
    ("ff0a0000090002029c1e64000900", "100% 7.8V", False, False),      # DC unplugged
    ("ff0a000009000202ce2d64000f00", "100% 11.7V DC charging", True, True),
])
def test_battery_from_real_packets(hexdata, label, ext, charging):
    (cmd,) = p.decode(bytes.fromhex(hexdata))
    b = p.Battery.from_values(cmd.values)
    assert (b.label(), b.external_power, b.charging) == (label, ext, charging)
    assert p.describe(cmd) == f"Battery = {label}"


def test_real_initial_payload_samples():
    # captured from camera A, 2026-10-12
    cases = {
        "ff0a00000a010102000010000103": "Transport = PREVIEW speed 0 slots [CFAST, SD, USB*]",
        "ff0c0000090202020000000087fd0000": "Remaining time = ['FULL', 'FULL', '10:33:00', 'FULL']",
        "ff07000009010102000001": "Media status = ['NONE', 'NONE', 'READY']",
        "ff0600000a0001020203": "Codec = ProRes Proxy",
        "ff0800000002800200800000": "Aperture = no lens",
    }
    for hexdata, text in cases.items():
        (cmd,) = p.decode(bytes.fromhex(hexdata))
        assert p.describe(cmd) == text


def test_real_timecode_notification():
    # the Timecode characteristic carries a 9.4 packet; value at bytes 8-11
    assert str(p.decode_timecode(bytes.fromhex("ff0800ff0904030000000000"))) == "00:00:00:00"


def test_bcd_roundtrip():
    assert p.to_bcd(20261012, 8) == 0x20261012
    assert p.from_bcd(0x15104500, 8) == 15104500
    assert p.from_bcd(0x1A, 2) is None
    with pytest.raises(p.ProtocolError):
        p.to_bcd(123, 2)


def test_set_clock_packet():
    from datetime import datetime, timezone
    cmd = p.set_clock(datetime(2026, 10, 12, 13, 10, 45, tzinfo=timezone.utc))
    assert cmd.values == (0x13104500, 0x20261012)
    # int32 little-endian, header length 4 + 8
    assert p.encode(cmd) == bytes([255, 12, 0, 0, 7, 0, 3, 0,
                                   0x00, 0x45, 0x10, 0x13, 0x12, 0x10, 0x26, 0x20])
    (back,) = p.decode(p.encode(cmd))
    assert p.decode_clock(back.values) == datetime(2026, 10, 12, 13, 10, 45, tzinfo=timezone.utc)
    assert p.describe(back) == "Real time clock = 2026-10-12 13:10:45 UTC"


def test_set_clock_converts_local_time_to_utc():
    from datetime import datetime, timedelta, timezone
    cest = timezone(timedelta(hours=2))
    cmd = p.set_clock(datetime(2026, 10, 12, 0, 30, 0, tzinfo=cest))
    assert cmd.values == (0x22300000, 0x20261011)  # previous day in UTC


def test_timezone():
    cmd = p.set_timezone(120)
    assert p.encode(cmd) == bytes([255, 8, 0, 0, 7, 2, 3, 0, 120, 0, 0, 0])
    assert p.describe(cmd) == "Timezone = UTC+2h"
    assert p.describe(p.set_timezone(-330)) == "Timezone = UTC-5.5h"
