import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Demuxers FFmpeg may pick for an uploaded image or audio file. FFmpeg chooses
# one from the file's contents, not from the type the uploader declared, and
# some demuxers treat the file as a list of other files to open: an upload
# declared as a PNG but holding a concat script had FFmpeg open whatever the
# script named. These lists hold only the single-file formats behind
# ALLOWED_MIME_TYPES (apps/api/schemas/upload.py); `mov` covers HEIC and M4A.
# A name is what `ffprobe -show_entries format=format_name` prints.
IMAGE_DEMUXERS = ("image2", "jpeg_pipe", "mjpeg", "png_pipe", "apng", "webp_pipe", "tiff_pipe", "gif", "mov")
AUDIO_DEMUXERS = ("mp3", "aac", "ogg", "wav", "w64", "aiff", "flac", "mov")

# Without a limit, a file built to decode forever held a worker slot for good.
# An image is one frame; audio is normalized far faster than it plays, so the
# audio limit only ends a run that is going nowhere.
IMAGE_TIMEOUT_SECONDS = 300
AUDIO_TIMEOUT_SECONDS = 2 * 60 * 60


def local_input_args(demuxers: tuple[str, ...]) -> list[str]:
    """Options to put before `-i <local file>` (or an ffprobe input) when the
    file is an untrusted upload: only these demuxers, and no protocol but
    `file`, so nothing the file names can be fetched over the network."""
    return ["-protocol_whitelist", "file", "-format_whitelist", ",".join(demuxers)]


def _extra_args(content_type: str, storage_class: str | None) -> dict:
    """ExtraArgs for one output upload, with the storage class when set."""
    extra = {"ContentType": content_type, "CacheControl": "max-age=86400"}
    if storage_class:
        extra["StorageClass"] = storage_class
    return extra


def process_image(
    s3_client, bucket: str, input_s3_key: str, output_prefix: str, storage_class: str | None = None,
) -> dict:
    """Convert image to WebP + generate thumbnail. Returns dict of S3 keys."""
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
        tmp_input = f.name
    work_dir = tempfile.mkdtemp()
    result = {}
    try:
        s3_client.download_file(bucket, input_s3_key, tmp_input)

        # Convert to WebP
        webp_path = os.path.join(work_dir, "processed.webp")
        subprocess.run(
            [
                "ffmpeg", "-y", *local_input_args(IMAGE_DEMUXERS), "-i", tmp_input,
                "-c:v", "libwebp", "-quality", "85", webp_path,
            ],
            check=True, capture_output=True, timeout=IMAGE_TIMEOUT_SECONDS,
        )
        webp_key = f"{output_prefix}/processed.webp"
        s3_client.upload_file(webp_path, bucket, webp_key, ExtraArgs=_extra_args("image/webp", storage_class))
        result["webp_key"] = webp_key

        # Thumbnail (max 400x400)
        thumb_path = os.path.join(work_dir, "thumbnail.jpg")
        subprocess.run(
            [
                "ffmpeg", "-y", *local_input_args(IMAGE_DEMUXERS), "-i", tmp_input,
                "-vf", "scale='min(400,iw)':'min(400,ih)':force_original_aspect_ratio=decrease",
                # One frame: a GIF has many, and FFmpeg refuses to write more
                # than one to a single JPEG, which failed every GIF upload.
                "-frames:v", "1", thumb_path,
            ],
            check=True, capture_output=True, timeout=IMAGE_TIMEOUT_SECONDS,
        )
        thumb_key = f"{output_prefix}/thumbnail.jpg"
        s3_client.upload_file(thumb_path, bucket, thumb_key, ExtraArgs=_extra_args("image/jpeg", storage_class))
        result["thumbnail_key"] = thumb_key

    finally:
        os.unlink(tmp_input)
        shutil.rmtree(work_dir, ignore_errors=True)
    return result


def process_audio(
    s3_client, bucket: str, input_s3_key: str, output_prefix: str, storage_class: str | None = None,
) -> dict:
    """Normalize audio to MP3 + generate waveform JSON.

    Returns dict of S3 keys plus a `duration_seconds` key (probed from the
    input via ffprobe; None if the probe fails)."""
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        tmp_input = f.name
    work_dir = tempfile.mkdtemp()
    result = {}
    try:
        s3_client.download_file(bucket, input_s3_key, tmp_input)

        # Probe duration before processing (#124)
        duration_seconds = None
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", *local_input_args(AUDIO_DEMUXERS),
                    "-print_format", "json", "-show_format", tmp_input,
                ],
                check=True, capture_output=True, text=True, timeout=120,
            )
            duration_seconds = float(json.loads(probe.stdout).get("format", {}).get("duration") or 0) or None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError, OSError, AttributeError) as exc:
            log.warning("audio duration probe failed for %s: %s", input_s3_key, exc)  # a failed probe must not fail audio processing
        result["duration_seconds"] = duration_seconds

        # Normalize and convert to MP3
        mp3_path = os.path.join(work_dir, "processed.mp3")
        subprocess.run(
            [
                "ffmpeg", "-y", *local_input_args(AUDIO_DEMUXERS), "-i", tmp_input,
                "-af", "loudnorm=I=-23:TP=-2:LRA=7",
                "-codec:a", "libmp3lame", "-qscale:a", "2",
                mp3_path,
            ],
            check=True, capture_output=True, timeout=AUDIO_TIMEOUT_SECONDS,
        )
        mp3_key = f"{output_prefix}/processed.mp3"
        s3_client.upload_file(mp3_path, bucket, mp3_key, ExtraArgs=_extra_args("audio/mpeg", storage_class))
        result["mp3_key"] = mp3_key

        # Waveform as JSON (simplified peak data)
        waveform_data = {"peaks": [], "duration": 0, "sample_rate": 44100}
        waveform_path = os.path.join(work_dir, "waveform.json")
        with open(waveform_path, "w") as wf:
            json.dump(waveform_data, wf)
        waveform_key = f"{output_prefix}/waveform.json"
        s3_client.upload_file(waveform_path, bucket, waveform_key, ExtraArgs=_extra_args("application/json", storage_class))
        result["waveform_key"] = waveform_key

    finally:
        os.unlink(tmp_input)
        shutil.rmtree(work_dir, ignore_errors=True)
    return result
