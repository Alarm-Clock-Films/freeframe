"""The raw object key's extension must never be taken from the client filename.

FFmpeg picks a demuxer from the URL's extension, and the transcoder streams the
raw object from a presigned URL. If a client could make that key end in a
playlist/manifest extension (.m3u8, .m3u, .xml, …) while declaring a plain
media mime type, the uploaded bytes would be read as an HLS/concat PLAYLIST and
the worker would fetch whatever "segment" URLs they name — file:// for local
disclosure, http:// for SSRF to cloud metadata or internal services.

So the key's extension is derived from the already-validated mime type, with
the client's own extension kept only when it is a plain media extension.
"""
import uuid

import pytest

from apps.api.schemas.upload import safe_raw_extension


# ---------------------------------------------------------------- unit helper

@pytest.mark.parametrize(
    "filename, mime, expected",
    [
        # Playlist / manifest / text extensions are never kept — the whole point.
        ("evil.m3u8", "video/mp4", ".mp4"),
        ("evil.m3u", "video/mp4", ".mp4"),
        ("evil.xml", "video/mp4", ".mp4"),
        ("evil.mpd", "video/mp4", ".mp4"),
        ("evil.txt", "audio/mpeg", ".mp3"),
        ("EVIL.M3U8", "video/quicktime", ".mov"),
        # No extension at all → canonical for the mime.
        ("noext", "audio/flac", ".flac"),
        # Plain media extensions are preserved verbatim.
        ("clip.mp4", "video/mp4", ".mp4"),
        ("clip.mov", "video/quicktime", ".mov"),
        ("clip.mkv", "video/x-matroska", ".mkv"),
        ("shot.jpeg", "image/jpeg", ".jpeg"),
        ("song.flac", "audio/flac", ".flac"),
    ],
)
def test_safe_raw_extension(filename, mime, expected):
    assert safe_raw_extension(filename, mime) == expected


def test_safe_raw_extension_never_returns_a_playlist_extension():
    dangerous = {".m3u8", ".m3u", ".xml", ".mpd", ".txt", ".concat"}
    for mime in ("video/mp4", "audio/mpeg", "image/png"):
        for name in ("x.m3u8", "x.m3u", "x.xml", "x.mpd", "x.txt", "x"):
            assert safe_raw_extension(name, mime) not in dangerous


# --------------------------------------------------------- end-to-end on the key

@pytest.mark.parametrize("path", ["new_asset", "new_version"])
def test_initiate_does_not_let_the_client_choose_the_key_extension(real_db, monkeypatch, path):
    """A `.m3u8` upload filename must not produce a `.m3u8` raw object key."""
    from apps.api.models.user import User
    from apps.api.models.project import Project, ProjectType
    from apps.api.models.asset import Asset, AssetType, MediaFile
    from apps.api.schemas.upload import InitiateUploadRequest
    import apps.api.routers.upload as upload_module
    import apps.api.routers.assets as assets_module

    owner = User(email=f"ext-{uuid.uuid4()}@t.local", name="t")
    real_db.add(owner); real_db.flush()
    project = Project(name="t", project_type=ProjectType.personal, created_by=owner.id)
    real_db.add(project); real_db.flush()

    for mod in (upload_module, assets_module):
        monkeypatch.setattr(mod, "create_multipart_upload", lambda k, m: "upload-id", raising=False)
        monkeypatch.setattr(mod, "upload_guard_error", lambda db, n: None, raising=False)
        monkeypatch.setattr(mod, "choose_part_size", lambda db, n: (10 * 1024 * 1024, None), raising=False)
        monkeypatch.setattr(mod, "require_project_role", lambda *a, **k: None, raising=False)

    req = dict(
        project_id=project.id, asset_name="clip",
        original_filename="evil.m3u8", mime_type="video/mp4", file_size_bytes=1024,
    )
    if path == "new_asset":
        result = upload_module.initiate_upload(
            InitiateUploadRequest(**req), db=real_db, current_user=owner,
        )
    else:
        asset = Asset(project_id=project.id, name="t", asset_type=AssetType.video,
                      created_by=owner.id)
        real_db.add(asset); real_db.flush()
        result = assets_module.initiate_new_version(
            asset.id, InitiateUploadRequest(**req), db=real_db, current_user=owner,
        )

    media_file = real_db.query(MediaFile).filter(
        MediaFile.version_id == result.version_id
    ).first()
    key = media_file.s3_key_raw
    assert not key.endswith(".m3u8"), key
    assert key.endswith("/original.mp4"), key
    # The uploader's real filename is still kept for display / download naming.
    assert media_file.original_filename == "evil.m3u8"
