"""Image, audio and watermark processing guard the uploaded file FFmpeg reads.

They download the upload to a local file and run FFmpeg on it. FFmpeg picks a
demuxer from the file's contents (and its extension), whatever type the
uploader declared, and a demuxer such as concat opens the other files the
upload names. The video path has had a guard for this; these paths had none,
and no time limit either.
"""
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from packages.transcoder import image_processor
from packages.transcoder.image_processor import (
    AUDIO_DEMUXERS, AUDIO_TIMEOUT_SECONDS, IMAGE_DEMUXERS, IMAGE_TIMEOUT_SECONDS,
    local_input_args, process_audio, process_image,
)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")


def _value_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _guarded(cmd, demuxers):
    """The guard is there, complete, and ahead of the input it guards."""
    input_at = cmd.index("-i") if "-i" in cmd else len(cmd) - 1
    assert cmd.index("-protocol_whitelist") < input_at
    assert cmd.index("-format_whitelist") < input_at
    assert _value_after(cmd, "-protocol_whitelist") == "file"
    assert _value_after(cmd, "-format_whitelist") == ",".join(demuxers)


def _capture_runs(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "ffmpeg":
            with open(cmd[-1], "wb") as f:
                f.write(b"out")
        return MagicMock(returncode=0, stdout='{"format": {"duration": "1.5"}}')

    monkeypatch.setattr(image_processor.subprocess, "run", run)
    return calls


def _s3_writing(data: bytes):
    s3 = MagicMock()

    def download(_bucket, _key, path):
        with open(path, "wb") as f:
            f.write(data)

    s3.download_file.side_effect = download
    return s3


def test_no_playlist_or_script_demuxer_is_allowed():
    for name in ("hls", "dash", "concat", "applehttp", "image2pipe", "lavfi", "sdp", "rtsp"):
        assert name not in IMAGE_DEMUXERS
        assert name not in AUDIO_DEMUXERS
    assert local_input_args(("a", "b")) == ["-protocol_whitelist", "file", "-format_whitelist", "a,b"]


def test_every_image_ffmpeg_run_is_guarded_and_time_limited(monkeypatch):
    calls = _capture_runs(monkeypatch)

    process_image(_s3_writing(b"x"), "bucket", "raw/p/a/v/original.png", "processed/p/a/v")

    assert len(calls) == 2
    for cmd, kwargs in calls:
        _guarded(cmd, IMAGE_DEMUXERS)
        assert kwargs["timeout"] == IMAGE_TIMEOUT_SECONDS


def test_every_audio_ffmpeg_run_is_guarded_and_time_limited(monkeypatch):
    calls = _capture_runs(monkeypatch)

    result = process_audio(_s3_writing(b"x"), "bucket", "raw/p/a/v/original.mp3", "processed/p/a/v")

    assert [cmd[0] for cmd, _ in calls] == ["ffprobe", "ffmpeg"]
    for cmd, kwargs in calls:
        _guarded(cmd, AUDIO_DEMUXERS)
        assert kwargs["timeout"]
    assert calls[1][1]["timeout"] == AUDIO_TIMEOUT_SECONDS
    assert result["duration_seconds"] == 1.5


# ── Real FFmpeg ──────────────────────────────────────────────────────────────

def _make(path, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(path)], check=True)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["png", "jpg", "webp", "tiff", "gif"])
def test_real_images_still_process(tmp_path, ext):
    source = _make(tmp_path / f"a.{ext}", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1", "-frames:v", "1")
    s3 = _s3_writing(source.read_bytes())

    result = process_image(s3, "bucket", f"raw/p/a/v/original.{ext}", "processed/p/a/v")

    assert result == {
        "webp_key": "processed/p/a/v/processed.webp",
        "thumbnail_key": "processed/p/a/v/thumbnail.jpg",
    }


@needs_ffmpeg
def test_an_animated_gif_gets_a_thumbnail(tmp_path):
    source = _make(tmp_path / "a.gif", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5", "-frames:v", "3")

    result = process_image(_s3_writing(source.read_bytes()), "bucket", "raw/p/a/v/original.gif", "processed/p/a/v")

    assert result["thumbnail_key"] == "processed/p/a/v/thumbnail.jpg"


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["mp3", "aac", "ogg", "wav", "aiff", "flac", "m4a"])
def test_real_audio_still_processes(tmp_path, ext):
    source = _make(tmp_path / f"a.{ext}", "-f", "lavfi", "-i", "sine=d=1")
    s3 = _s3_writing(source.read_bytes())

    result = process_audio(s3, "bucket", f"raw/p/a/v/original.{ext}", "processed/p/a/v")

    assert result["mp3_key"] == "processed/p/a/v/processed.mp3"
    assert result["duration_seconds"] == pytest.approx(1.0, abs=0.2)


@needs_ffmpeg
def test_a_concat_script_uploaded_as_audio_is_refused(tmp_path, monkeypatch):
    """Unguarded, FFmpeg reads this as a concat script and opens the file it names."""
    monkeypatch.setattr(image_processor.tempfile, "tempdir", str(tmp_path))
    _make(tmp_path / "other.mp3", "-f", "lavfi", "-i", "sine=d=1")
    script = b"ffconcat version 1.0\nfile 'other.mp3'\n"

    unguarded = tmp_path / "script.audio"
    unguarded.write_bytes(script)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", str(unguarded)],
        capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "concat"  # what the guard is there to stop

    with pytest.raises(subprocess.CalledProcessError):
        process_audio(_s3_writing(script), "bucket", "raw/p/a/v/original.mp3", "processed/p/a/v")


# ── Watermark ────────────────────────────────────────────────────────────────

def _run_watermark(text, filename, key="raw/p/a/v/original.mp4"):
    from apps.api.models.asset import Asset, AssetVersion, MediaFile
    from apps.api.tasks.watermark_tasks import apply_watermark

    rows = {
        Asset: SimpleNamespace(id="00000000-0000-0000-0000-000000000001",
                               project_id="00000000-0000-0000-0000-000000000002"),
        AssetVersion: SimpleNamespace(id="00000000-0000-0000-0000-000000000003"),
        MediaFile: SimpleNamespace(original_filename=filename, s3_key_raw=key),
    }

    def query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.first.return_value = rows[model]
        return q

    db = MagicMock()
    db.query.side_effect = query
    seen = {}

    def run(cmd, **_kwargs):
        seen["cmd"] = cmd
        graph = _value_after(cmd, "-vf")
        text_path = graph.split("textfile='", 1)[1].split("'", 1)[0]
        with open(text_path, encoding="utf-8") as f:
            seen["text"] = f.read()
        with open(cmd[-1], "wb") as f:
            f.write(b"mp4")
        return MagicMock(returncode=0)

    with patch("apps.api.tasks.watermark_tasks.SessionLocal", return_value=db), \
         patch("apps.api.services.s3_service.get_s3_client", return_value=MagicMock()), \
         patch("apps.api.services.s3_service.put_object"), \
         patch("apps.api.tasks.watermark_tasks._publish_event"), \
         patch("apps.api.tasks.watermark_tasks.subprocess.run", side_effect=run), \
         patch.object(apply_watermark, "retry", side_effect=RuntimeError("retried")):
        apply_watermark.run("00000000-0000-0000-0000-000000000001", text, "center", 0.5, None)
    return seen


def test_watermark_local_file_is_not_named_after_the_uploaded_filename():
    seen = _run_watermark("CONFIDENTIAL", filename="playlist.m3u8")

    source = _value_after(seen["cmd"], "-i")
    assert source.endswith("source.mp4")
    assert seen["cmd"].index("-protocol_whitelist") < seen["cmd"].index("-i")
    assert _value_after(seen["cmd"], "-protocol_whitelist") == "file"


def test_watermark_text_never_reaches_the_filter_string():
    text = r"a'b\:fontfile=/etc/passwd %{localtime} x:y,scale=1:1"

    seen = _run_watermark(text, filename="clip.mp4")

    graph = _value_after(seen["cmd"], "-vf")
    assert seen["text"] == text
    assert "fontfile" not in graph and "localtime" not in graph and "scale=1:1" not in graph
    assert ":expansion=none:" in graph
