"""Stream recording on a synthetic 1080p60 source (no Cam Link or camera needed)."""

import shutil
import subprocess
import time

import pytest

pytest.importorskip("gi")
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import config  # noqa: E402
from video import recorder  # noqa: E402

Gst.init(None)


@pytest.fixture
def pipeline():
    p = Gst.parse_launch(
        "videotestsrc is-live=true ! video/x-raw,format=YUY2,width=1920,height=1080,"
        "framerate=60/1 ! tee name=t ! appsink max-buffers=1 drop=true sync=false")
    p.set_state(Gst.State.PLAYING)
    yield p
    p.set_state(Gst.State.NULL)


@pytest.mark.parametrize("codec,container", [("h264", "mkv"), ("h265", "mp4")])
def test_record_and_stop(pipeline, tmp_path, codec, container):
    if recorder.pick_encoder(codec) is None:
        pytest.skip(f"no {codec} encoder")
    rc = config.RecordConfig(codec=codec, container=container, bitrate_mbps=10,
                             folder=str(tmp_path))
    rec = recorder.StreamRecorder()
    path = str(tmp_path / f"clip.{container}")
    rec.start(pipeline, path, rc)
    assert rec.active
    time.sleep(3)
    assert rec.stop() is True
    assert not rec.active
    # the preview pipeline keeps running, and can record again
    assert pipeline.get_state(0)[1] == Gst.State.PLAYING
    rec.start(pipeline, str(tmp_path / f"again.{container}"), rc)
    time.sleep(1)
    assert rec.stop() is True

    if shutil.which("ffprobe"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
             "stream=nb_read_frames,width,height:format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, check=True).stdout.split()
        assert out[:2] == ["1920", "1080"]
        assert 2.5 < float(out[-1]) < 3.5  # starts at t=0 and lasts as long as it recorded
        assert 150 < int(out[2]) < 200  # about 60 fps, nothing dropped


def test_record_config_defaults_and_roundtrip():
    cfg = config.AppConfig.from_dict({"record": {"target": "both", "bitrate_mbps": 25}})
    assert cfg.record.target == "both" and cfg.record.bitrate_mbps == 25
    assert config.AppConfig.from_dict({}).record.target == "camera"  # old configs keep working
