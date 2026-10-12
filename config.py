"""App configuration: camera A/B assignments, stored as JSON in ~/.config/ocelli/."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

CAMERA_IDS = ("A", "B")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "ocelli" / "config.json"


@dataclass
class CameraConfig:
    ble_address: str | None = None
    # One adapter per camera is allowed; both default to the single UB500.
    ble_adapter: str = "hci0"
    # Always a /dev/v4l/by-id/... path, never /dev/videoN.
    video_device: str | None = None


@dataclass
class PreviewConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 60
    # YUYV (4:2:2): full vertical colour resolution. NV12 (4:2:0) is ~2.5 ms fresher and
    # 25% less USB data, but colour detail is visibly blockier on fine microscope detail.
    pixel_format: str = "YUYV"
    backend: str = "gst"


@dataclass
class BleConfig:
    # Delay between connecting camera A and camera B on a shared adapter.
    stagger_seconds: float = 2.0
    connect_timeout: float = 20.0
    retry_initial: float = 2.0
    retry_max: float = 30.0
    host_name: str = "Ocelli"
    # Write the PC's time and timezone to each camera whenever it connects.
    auto_sync_time: bool = True
    # A link with no notifications for this long is treated as dead.
    stale_seconds: float = 15.0


@dataclass
class UiConfig:
    layout: str = "pip"  # "pip" (main + thumbnail), "side", "single"
    main_camera: str = "A"
    show_debug: bool = False


@dataclass
class AppConfig:
    cameras: dict[str, CameraConfig] = field(
        default_factory=lambda: {cid: CameraConfig() for cid in CAMERA_IDS}
    )
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    ble: BleConfig = field(default_factory=BleConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        cfg = cls()
        for cid, cam in data.get("cameras", {}).items():
            if cid in CAMERA_IDS:
                cfg.cameras[cid] = CameraConfig(**cam)
        if "preview" in data:
            cfg.preview = PreviewConfig(**data["preview"])
        if "ble" in data:
            cfg.ble = BleConfig(**data["ble"])
        if "ui" in data:
            cfg.ui = UiConfig(**data["ui"])
        return cfg

    def camera_for_address(self, address: str) -> str | None:
        for cid, cam in self.cameras.items():
            if cam.ble_address and cam.ble_address.upper() == address.upper():
                return cid
        return None


def load(path: Path | None = None) -> AppConfig:
    path = path or default_path()
    if not path.exists():
        return AppConfig()
    return AppConfig.from_dict(json.loads(path.read_text()))


def save(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path
