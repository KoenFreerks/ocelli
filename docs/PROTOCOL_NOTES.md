# Blackmagic Camera Control: protocol notes

Source: *Blackmagic Camera Control, Developer Information*, **August 2025** revision
(developer page lists it as "Released: 07 Aug 2025"), 115 pages.
SDI Camera Control Protocol section is **Version 1.6.2** (pp. 96–105).
Bluetooth section pp. 109–110. Everything below is transcribed from that PDF; page
numbers refer to the printed page footers.

## Bluetooth LE (p. 109–110)

| Service / characteristic            | UUID                                   | Use |
|-------------------------------------|----------------------------------------|-----|
| Device Information Service          | `180A`                                 | |
| ├ Camera Manufacturer               | `2A29`                                 | read, "Blackmagic Design" |
| └ Camera Model                      | `2A24`                                 | read |
| **Blackmagic Camera Service**       | `291D567A-6D75-11E6-8B77-86F30CA893D3` | advertised; use for scan filtering |
| ├ Outgoing Camera Control (enc.)    | `5DD3465F-1AEE-4299-8493-D2ECA2F8E1BB` | write SDI-protocol packets |
| ├ Incoming Camera Control (enc.)    | `B864E140-76A0-416A-BF30-5876504537D9` | notify, SDI-protocol packets from camera |
| ├ Timecode (enc.)                   | `6D8F2110-86F1-41BF-9AFB-451D87E976C8` | notify, 32-bit BCD HH:MM:SS:FF (e.g. 09:12:53:10 = 0x09125310) |
| ├ Camera Status (enc.)              | `7FE8691D-95DC-4FC5-8ABD-CA74339B51B9` | notify, uint8 flags; write 0x00 = power off, 0x01 = power on |
| ├ Device Name                       | `FFAC0C52-C9FB-41A0-B063-CC76282EB89C` | write, max 32 chars, shown in camera's BT menu |
| └ Protocol Version                  | `8F1FD018-B508-456F-8F82-3D392BEE2706` | read, CCU protocol version |

Camera Status flags: `0x01` Power On, `0x02` Connected, `0x04` Paired, `0x08` Versions Verified,
`0x10` Initial Payload Received, `0x20` Camera Ready.

**Pairing:** encrypted characteristics need a bond. After connecting, any write to an encrypted
characteristic (e.g. writing `0x01` to Camera Status) starts bonding; the camera shows a 6-digit
PIN in its Bluetooth Setup menu, which is entered on the host (BlueZ passkey entry).

## Packet format (p. 96–97)

All little-endian (confirmed by examples: 10000 µs → `10 27 00 00`). Bit fields LSB first.
Messages and headers are 32-bit aligned.

```
byte 0  destination   uint8   0..254 device, 255 = broadcast
byte 1  length        uint8   length of command data ONLY (no header, no padding)
byte 2  command id    uint8   0 = change configuration
byte 3  reserved      uint8   0
byte 4.. command data (0..60 bytes), then 0x00 padding to a multiple of 4
```
Max packet size 64 bytes. (The text says "three byte header" but lists four fields; all worked
examples use a 4-byte header, which is what we implement.)

Command 0 "change configuration" data:
```
category  uint8
parameter uint8
data type uint8   0 void/bool, 1 int8, 2 int16, 3 int32, 4 int64, 5 UTF-8 string,
                  128 fixed16 (signed 5.11, value*2048, range -16.0..15.9995)
operation uint8   0 assign, 1 offset/toggle
data ...          N elements of data type
```
Example (p. 105): set exposure 10 ms on camera 4 →
`04 08 00 00 | 01 05 03 00 | 10 27 00 00`.
Example: turn on OIS on all cameras → `FF 05 00 00 | 00 06 00 00 | 01 00 00 00` (1 byte data + 3 pad).

## Parameters relevant to this app (p. 98–104)

| ID | Name | Type | Notes |
|----|------|------|-------|
| 0.0 | Focus | fixed16 | 0.0 near … 1.0 far |
| 0.1 | Instantaneous autofocus | void | |
| 0.2 | Aperture (f-stop) | fixed16 | AV, f = sqrt(2^AV), -1.0..16.0 |
| 0.3 | Aperture (normalised) | fixed16 | 0..1 |
| 0.5 | Instantaneous auto aperture | void | |
| 1.0 | Video mode | int8[5] | fps, M-rate, dims, interlaced, colour space |
| 1.2 | Manual white balance | int16[2] | [0] K 2500–10000, [1] tint -50..50 |
| 1.3 | Set auto WB | void | |
| 1.4 | Restore auto WB | void | |
| 1.5 | Exposure (µs) | int32 | 1..42000 |
| 1.9 | Recording format | int16[5] | file fps, sensor fps, width, height, flags |
| 1.10 | Auto exposure mode | int8 | 0 manual … 4 shutter+iris |
| 1.11 | Shutter angle | int32 | degrees × 100 (100..36000) |
| 1.12 | Shutter speed | int32 | 1/x s (e.g. 50) |
| 1.13 | Gain | int8 | dB |
| 1.14 | ISO | int32 | |
| 1.16 | ND filter stop | fixed16[2] | |
| 3.0 | Overlay enables | uint16[2] | bit 2 = clean feed; target bit 1 = HDMI |
| 4.7 | Timecode source | int8 | 0 clip, 1 timecode |
| 7.0 | Real time clock | int32[2] | [0] BCD HHMMSSFF (UTC), [1] BCD YYYYMMDD |
| 7.2 | Timezone | int32 | minutes offset from UTC |
| 10.0 | Codec | int8[2] | basic codec + variant |
| 10.1 | Transport mode | int8[5] | [0] 0 preview / 1 play / **2 record**, [1] speed, [2] flags, [3]/[4] slot media |

### Not present in this revision
No battery, remaining-record-time or media free-space parameter is defined in the
August 2025 tables (groups 9 and 12 do not appear). Recording state comes from 10.1[0];
timecode comes from the Timecode characteristic.

## Additions from Blackmagic Cameras Code Samples 1.2 (Mac, © 2023 Blackmagic Design)

Official sample source, used where the PDF is silent. Files cited are under
`Libraries/` in the zip. Anything still undefined is marked as such.

**Operation type 2 = StatusUpdate** (`CCUPacketTypes.swift` `OperationType`): the value
used in packets coming *from* the camera. The PDF only lists 0 and 1.

**Group 9 "Status"** (`StatusParameter`): 9.0 Battery, 9.1 MediaStatus,
9.2 RemainingRecordTime, 9.3 DisplayThresholds, 9.4 DisplayTimecode, 9.5 CameraSpec,
9.6 SwitcherStatus, 9.7 DisplayParameters.

| ID | Payload (from `CCUDecodingFunctions.swift`) |
|----|------|
| 9.1 Media status | int8 per slot: 0 none, 1 ready, -1 mount error, -2 record error |
| 9.2 Remaining record time | int16 per slot: >0 seconds, <0 minutes (abs), -32768 = more than fits ("+"), 0 = full |
| 9.5 Camera spec | 4 × uint8, [1] = camera model |
| 9.0 Battery | **int16 [millivolts, percent, flags]**. The layout isn't in the samples; it was **confirmed on camera A 2026-10-12**: unplugging DC dropped [0] from ~11.9 V to 7.8 V and cleared flag 0x02; replugging restored it, and 0x04 appeared while charging. [0] is the voltage of the active supply (the DC input when plugged in). Flags from the samples: 0x01 battery present, 0x02 AC/DC present, 0x04 charging, 0x08 percentage estimated, 0x10 prefer voltage display. |

**10.1 Transport mode, real layout** (`Data/TransportInfo.swift`): `[mode, speed, flags,
medium slot1, medium slot2, …]`. The number of slots is variable (= payload length − 3),
not fixed at 5. Flag bits: 0x01 loop, 0x02 play all, 0x20 disk1 active,
0x40 disk2 active, **0x10 disk3 active**, 0x80 time-lapse.
Storage medium: 0 CFast, 1 SD, 2 SSD, **3 USB** (PDF lists only 0–2).
To record, the sample sends the whole TransportInfo back with mode changed
(`CreateTransportInfoCommand`: dest 255, int8, assign). We do the same: echo the
last received 10.1 with `mode = 2` (record) or `0` (preview).

**Timecode characteristic** (confirmed on camera: the notification is a 9.4 'display timecode' packet, `ff 08 00 ff 09 04 03 00` + LE uint32) (`BasicCameraControlViewController.onTimecodePacketReceived`):
the notification is a full CCU-style packet; the timecode is the **little-endian uint32 at
bytes 8–11**, BCD HHMMSSFF. Bit 31 = drop-frame flag (so the top hours digit uses only
2 bits).

**Bond/handshake order** (`Camera/PeripheralInterface.swift`): discover → enable notify on
every characteristic → read Protocol Version → write host name to Device Name →
write `0x01` to Camera Status (this starts bonding / PIN on camera).

**Group 12 Metadata** exists in the samples (reel, scene, take, lens info, …) but the
source itself says: "All metadata CCU commands are undocumented and are subject to
change". We don't use it.

## Time sync (7.0 / 7.2), observed on camera 2026-10-12

- Neither camera includes group 7 in its initial status payload, and neither echoes 7.0 or 7.2
  after a write (waited 4 s). There is no request/read command in the protocol, so **the
  camera clock cannot be read back over Bluetooth**. Verification has to come from the camera
  screen or from a recorded clip's start timecode, which follows the camera clock.
- `CameraLink.sync_clock()` sends 7.2 (PC UTC offset in minutes), then 7.0 with FF = 00, timed
  so it arrives on a whole second (half the measured write round trip, ~50–60 ms, as the
  one-way delay).
- **Confirmed on both cameras (2026-10-12):** after `time both sync` the camera shows the PC's
  local time, so 7.0 is taken as UTC and displayed using 7.2, as the PDF says.
