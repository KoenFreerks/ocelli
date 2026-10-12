"""Find capture devices by their stable /dev/v4l/by-id path (never /dev/videoN)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

BY_ID = Path("/dev/v4l/by-id")
_SERIAL_RE = re.compile(r"usb-(?P<vendor>.+?)_(?P<serial>[^_]+)-video-index0$")


@dataclass(frozen=True)
class CaptureDevice:
    by_id: str  # /dev/v4l/by-id/usb-Elgato_Cam_Link_4K_<serial>-video-index0
    node: str  # current /dev/videoN; changes across replugs
    serial: str
    name: str


def list_devices() -> list[CaptureDevice]:
    """Capture nodes (video-index0) that are present right now."""
    out = []
    if not BY_ID.is_dir():
        return out
    for link in sorted(BY_ID.iterdir()):
        m = _SERIAL_RE.search(link.name)
        if not m or not link.exists():
            continue
        out.append(CaptureDevice(
            by_id=str(link),
            node=os.path.realpath(link),
            serial=m["serial"],
            name=m["vendor"].replace("_", " "),
        ))
    return out


def resolve(by_id: str) -> str | None:
    """Current /dev/videoN for a by-id path, or None if it isn't plugged in."""
    p = Path(by_id)
    return os.path.realpath(p) if p.exists() else None


def find(spec: str) -> CaptureDevice | None:
    """Match a full by-id path or a (partial) serial number."""
    for d in list_devices():
        if spec in (d.by_id, d.serial) or (len(spec) >= 4 and d.serial.endswith(spec)):
            return d
    return None
