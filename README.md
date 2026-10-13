# Ocelli

Linux monitor and remote for Blackmagic Pocket Cinema Camera 4K:
HDMI preview via Elgato Cam Link 4K, control and status over Bluetooth LE, and optional
recording of the HDMI feed straight to disk.

Built for **two cameras** (A and B), and tested with two. One camera works too: leave B
unassigned. Bluetooth is optional: with no camera paired you still get the preview and
stream recording. More than two cameras is not supported (and 1080p60 from three Cam Links on
one PC would need separate USB controllers for each).

Status: all milestones done (BLE, video, full GUI, time sync, stream recording).

## Setup (Ubuntu 24.04)

System packages:

```bash
sudo apt install bluez v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl \
  python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  python3-venv libxcb-cursor0
```

Recommended for long sessions: keep the Bluetooth dongle from autosuspending.

```bash
echo 'options btusb enable_autosuspend=0' | sudo tee /etc/modprobe.d/btusb-no-autosuspend.conf
sudo modprobe -r btusb && sudo modprobe btusb
```

Python environment. Use the **system** Python, so the apt-installed GStreamer bindings are
visible. A conda Python has no `gi`.

```bash
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q ble/tests
```

## Running the app

From the app menu: **Ocelli** (right-click → *Start fullscreen*). To (re)install that launcher:

```bash
./install-launcher.sh
```

Or from a terminal:

```bash
cd ~/ocelli
.venv/bin/python main.py              # add --fullscreen to start fullscreen
```

Only one Ocelli can run at a time (a second copy would find the Cam Links busy); starting it
again just says it's already running.

**Which camera is A?** A is shown on the left (side by side, and the left monitor in W mode).
If the physical left/right order doesn't match, use Settings → **Swap camera A ↔ B**. It swaps
the Bluetooth camera and Cam Link together and applies immediately.

The app connects to both cameras over Bluetooth (A first, then B: never at the same time on
the shared dongle) and starts both video feeds. If a link drops or a Cam Link is unplugged, it
keeps retrying and shows an alert banner. By default each camera's clock is set to this PC's time
whenever it connects (Settings → Options).

| Key | Action |
|---|---|
| 1 / 2 / 3 | main + thumbnail / side by side / single feed |
| ← / → | previous / next layout |
| Tab, or click the thumbnail | swap cameras |
| F, Esc, or double-click | fullscreen (video only; move the mouse to the top edge for the bar) |
| Z | 1:1 focus view: exact camera pixels, mouse wheel zooms ×1–×8 pixel-sharp, drag to pan |
| W | each camera in its own window (fullscreen on its own monitor if there are two) |
| C, or hover + ⚙ | camera control panel (record, ISO, shutter, white balance, lens) |
| D | debug overlay: actual capture size, fps, capture-to-app delay |
| H | hide/show the top bar |
| Ctrl+R | record both on/off |
| Ctrl+T | sync both camera clocks to this PC |
| Ctrl+, | settings: scan/pair cameras, assign BLE and Cam Links to A/B, options |

Each feed always shows a status bar: REC/STBY (yellow while waiting for the camera to
confirm), timecode, ISO/shutter/WB/format, active media and remaining time, battery, and
Bluetooth (BT) and video (VID) indicators (green = OK, yellow = connecting, red = problem).
Lens controls are greyed out when the camera reports no electronic lens.

**Preview quality.** The preview is the camera's HDMI output (1080p, YUYV 4:2:2), scaled with
smoothing to fit. For judging focus (e.g. with the camera's focus assist on), use **Z**: it shows
camera pixels 1:1 without any filtering, so single-pixel focus-assist dots stay exact. None of
this affects recordings (see below).

## Recording

Settings → **Recording → REC records** picks what the REC buttons (and Ctrl+R) do:

- **The camera's own media** (default): sends the record command over Bluetooth. The footage is
  the camera's full-quality file on its card or SSD.
- **The video stream, saved on this PC**: encodes the HDMI feed to a file; no Bluetooth needed.
  It is the 8-bit HDMI picture (1080p60 here) without audio, **including whatever the camera
  draws on HDMI** such as focus peaking, so turn those off on the camera or use *Clean feed*.
- **Both**.

Stream recordings go to the chosen folder (default `~/Videos/Ocelli`) as
`Ocelli_<camera>_<date>_<time>.mkv`. Codec H.264 or H.265, bitrate, and MKV or fragmented MP4
are in the same settings group; both survive a crash of the PC or the app (MKV is the safest).
It uses NVIDIA NVENC when GStreamer's `nvcodec` plugin is available (low CPU: two cameras
recording at once cost about 60% of one core on the test PC), otherwise x264/x265 on the CPU
(about 170% of one core per camera at 1080p60). The preview is not affected either way. If a
Cam Link drops out and returns, recording continues in a new `_part2` file. Recording stops
by itself when less than 1 GB of disk is left. The status bar shows `REC PC`, elapsed time and
file size.

If NVENC is missing although the NVIDIA driver is installed, GStreamer's plugin cache may be
stale: `rm ~/.cache/gstreamer-1.0/registry.x86_64.bin` and start Ocelli again.

The log is written to `logs/ocelli.log`, including every Bluetooth and video state change.

## Camera settings

- Setup > Bluetooth: **On**. A camera only advertises while nothing else is connected to it,
  so close the Blackmagic Camera app on any phone.
- HDMI output: Cam Link accepts up to 2160p30 or 1080p60. Turn on *Clean feed* if you don't
  want the camera's overlays in the preview.

## Bluetooth: CLI

Run from the project folder:

```bash
.venv/bin/python -m ble.cli scan                      # list cameras advertising the BMD service
.venv/bin/python -m ble.cli assign A AA:BB:CC:DD:EE:01
.venv/bin/python -m ble.cli assign B AA:BB:CC:DD:EE:02
.venv/bin/python -m ble.cli pair A                    # type the 6-digit PIN the camera shows
.venv/bin/python -m ble.cli monitor A                 # live status; Ctrl+C for a summary
.venv/bin/python -m ble.cli record A start            # stop | toggle | status
.venv/bin/python -m ble.cli record both start         # A, then B; prints confirmation timing
.venv/bin/python -m ble.cli time both sync           # set camera clocks to this PC's time + timezone
.venv/bin/python -m ble.cli unpair A                  # forget the bond in BlueZ
```

`record` connects (A first, then B after `ble.stagger_seconds`), sends the command, and waits
until the camera confirms it. If the camera doesn't confirm within `--timeout` seconds
(default 5), it reports an error; the usual cause is no media ready. After a start it shows
the timecode for `--watch` seconds. "both" sends to the two cameras at the same moment, but
Bluetooth timing varies by tens of milliseconds, so this is **not** frame-accurate sync.

`time sync` writes the PC's UTC time and timezone, timed to land on a whole second. Expect
roughly 100 ms accuracy: fine for matching clip timecodes to a log, **not** frame-accurate
multicam sync. The camera never reports its clock over Bluetooth, so check the result on the
camera's screen or in a clip's start timecode. Keep the PC on NTP (`timedatectl`).

Bonded cameras are often kept connected by BlueZ in the background. The tool connects through
BlueZ's existing device entry instead of scanning, so that's fine.

`monitor` prints every packet the camera sends, decoded where the protocol defines it, with
the raw bytes alongside. It also writes everything to `logs/ble-<cam>-<time>.jsonl`. It
reconnects automatically with backoff if the link drops.

Camera assignments are stored in `~/.config/ocelli/config.json`. Each camera has its own
`ble_adapter` (default `hci0`), so a second adapter can be added later.

### Pairing manually with bluetoothctl (alternative)

```text
$ bluetoothctl
[bluetooth]# agent KeyboardOnly
[bluetooth]# default-agent
[bluetooth]# scan le
        (wait for the camera's address, e.g. AA:BB:CC:DD:EE:01)
[bluetooth]# scan off
[bluetooth]# pair AA:BB:CC:DD:EE:01
        (camera shows a 6-digit PIN; type it at the "Enter passkey" prompt)
[bluetooth]# trust AA:BB:CC:DD:EE:01
[bluetooth]# disconnect AA:BB:CC:DD:EE:01
[bluetooth]# quit
```

Each camera is bonded once. If pairing gets stuck, `remove <address>` in bluetoothctl. Also
delete the PC from the camera's Bluetooth menu, then try again.

## Video preview

```bash
.venv/bin/python -m video.bench --list                     # Cam Links by stable by-id path  (--device takes A/B or the last 4+ chars of a serial)
.venv/bin/python -m video.bench --device A                 # camera A (from config), GStreamer, 1080p60 NV12
.venv/bin/python -m video.bench --device A --device B      # both feeds side by side
.venv/bin/python -m video.bench --device B --backend qt    # camera B via Qt Multimedia
.venv/bin/python -m video.bench --device A --clock         # glass-to-glass latency test
```

Keys: **D** debug overlay (actual capture size, fps, capture-to-app latency), **F** fullscreen,
**Esc** quit. `--seconds N` closes after N seconds and prints fps, CPU and latency stats.

Devices are always opened by `/dev/v4l/by-id/...`, so replugging a Cam Link (which may give it a
new `/dev/videoN`) doesn't matter. If the device disappears, the view shows *CAPTURE DEVICE
MISSING* and reopens it when it's back. If frames stop for 2 s, it shows *NO SIGNAL*.

Measured on this PC (1080p60):

| Setup | fps | CPU (share of 1 core) | capture to app |
|---|---|---|---|
| 1 camera, GStreamer, YUYV | 60.0 | ~50% | 9.8 ms |
| 1 camera, Qt Multimedia, YUYV | 59.9 | ~44% | not measurable |
| 2 cameras, GStreamer, YUYV, 2 min | 60.0 / 60.0 | 83–100% | 9.9 ms |
| **2 cameras, GStreamer, NV12 (default)** | 60.0 / 60.0 | ~80% | **7.4 ms** |

The two Cam Links sit on separate USB controllers (Intel chipset and ASMedia), so they don't
share bandwidth. Keep it that way if you move cables.

The video is drawn without OpenGL. On this Wayland + NVIDIA setup an OpenGL viewport renders
white, and under XWayland it crashes.

## Protocol

See [docs/PROTOCOL_NOTES.md](docs/PROTOCOL_NOTES.md). It's taken from the Blackmagic Camera
Control PDF (Aug 2025) and Blackmagic's Cameras Code Samples 1.2. Parameters that neither
source defines are shown raw and never interpreted.

## License

Ocelli is released under the [MIT License](LICENSE), © 2026 Koen Freerks.

It uses, but does not include, these third-party components (installed via pip or apt):
bleak and dbus-fast (MIT), qasync (BSD-2-Clause), PySide6/Qt (LGPL-3.0), PyGObject and
GStreamer (LGPL-2.1+). The camera protocol is implemented from Blackmagic Design's public
*Blackmagic Camera Control* developer documentation and Cameras Code Samples; neither is
redistributed here. Blackmagic Design, Pocket Cinema Camera, Elgato and Cam Link are
trademarks of their respective owners; this project is not affiliated with them.
