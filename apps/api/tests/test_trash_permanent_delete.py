"""Permanently deleting from the trash: per asset, per folder, and "Empty trash".

Before these endpoints the trash could only be restored from; the only way to
free the storage was waiting for the retention GC or deleting from S3 by hand.
Runs against real Postgres (`real_db`) so the cascades are exercised for real.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import apps.api.tasks.cleanup_tasks as ct
from apps.api.models.user import User
from apps.api.models.project import Project, ProjectType, ProjectMember, ProjectRole
from apps.api.models.folder import Folder
from apps.api.models.asset import (
    Asset, AssetType, AssetVersion, MediaFile, FileType, ProcessingStatus,
)


@pytest.fixture
def s3_deleted(monkeypatch):
    deleted = []
    monkeypatch.setattr(ct, "delete_object", lambda k: deleted.append(k))
    monkeypatch.setattr(ct, "delete_prefix", lambda k: deleted.append(k))
    return deleted


@pytest.fixture
def seeded(real_db):
    user = User(email=f"trash-{uuid.uuid4()}@t.local", name="t")
    real_db.add(user); real_db.flush()
    project = Project(name="t", project_type=ProjectType.personal, created_by=user.id)
    real_db.add(project); real_db.flush()
    real_db.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.editor))
    real_db.flush()
    return user, project


@pytest.fixture
def api(real_db, seeded):
    """TestClient whose requests run on the real, rolled-back session as the seeded editor."""
    with patch("apps.api.services.s3_service.ensure_bucket_exists"), \
         patch("apps.api.services.s3_service.get_s3_client", return_value=MagicMock()):
        from fastapi.testclient import TestClient
        from apps.api.main import app
        from apps.api.database import get_db
        from apps.api.middleware.auth import get_current_user

        app.dependency_overrides[get_db] = lambda: real_db
        app.dependency_overrides[get_current_user] = lambda: seeded[0]
        try:
            yield TestClient(app, raise_server_exceptions=True)
        finally:
            app.dependency_overrides.clear()


def _asset(db, user, project, folder=None, deleted=True, key="raw/a.mp4"):
    """Seed an asset with one ready version and media file; returns the asset's id."""
    a = Asset(project_id=project.id, name="clip", asset_type=AssetType.video, created_by=user.id,
              folder_id=folder)
    db.add(a); db.flush()
    v = AssetVersion(asset_id=a.id, version_number=1, processing_status=ProcessingStatus.ready,
                     created_by=user.id)
    db.add(v); db.flush()
    db.add(MediaFile(version_id=v.id, file_type=FileType.video, original_filename="a.mp4",
                     mime_type="video/mp4", file_size_bytes=1, s3_key_raw=key,
                     s3_key_processed=f"processed/{a.id}/"))
    if deleted:
        a.deleted_at = datetime.now(timezone.utc)
    db.flush()
    return a.id


def _folder(db, user, project, parent=None, deleted=True):
    """Seed a folder; returns its id (the ORM object is unusable once its row is purged)."""
    f = Folder(project_id=project.id, name="f", created_by=user.id, parent_id=parent)
    db.add(f); db.flush()
    if deleted:
        f.deleted_at = datetime.now(timezone.utc)
        db.flush()
    return f.id


def _exists(db, model, obj_id) -> bool:
    db.expire_all()
    return db.query(model).filter(model.id == obj_id).first() is not None


def test_delete_asset_forever_removes_rows_and_storage(api, real_db, seeded, s3_deleted):
    user, project = seeded
    asset = _asset(real_db, user, project, key="raw/gone.mp4")

    resp = api.delete(f"/assets/{asset}/permanent")

    assert resp.status_code == 200
    assert resp.json()["assets"] == 1
    assert not _exists(real_db, Asset, asset)
    assert "raw/gone.mp4" in s3_deleted
    assert f"processed/{asset}/" in s3_deleted


def test_live_asset_cannot_be_deleted_forever(api, real_db, seeded, s3_deleted):
    user, project = seeded
    asset = _asset(real_db, user, project, deleted=False)

    resp = api.delete(f"/assets/{asset}/permanent")

    assert resp.status_code == 404
    assert _exists(real_db, Asset, asset)
    assert s3_deleted == []


def test_viewer_cannot_delete_forever(api, real_db, seeded, s3_deleted):
    user, project = seeded
    member = real_db.query(ProjectMember).filter(ProjectMember.user_id == user.id).first()
    member.role = ProjectRole.viewer
    real_db.flush()
    asset = _asset(real_db, user, project)

    resp = api.delete(f"/assets/{asset}/permanent")

    assert resp.status_code == 403
    assert _exists(real_db, Asset, asset)
    assert s3_deleted == []


def test_delete_folder_forever_takes_its_contents(api, real_db, seeded, s3_deleted):
    user, project = seeded
    parent = _folder(real_db, user, project)
    child = _folder(real_db, user, project, parent=parent)
    inner = _asset(real_db, user, project, folder=child, key="raw/inner.mp4")

    resp = api.delete(f"/folders/{parent}/permanent")

    assert resp.status_code == 200
    assert resp.json()["folders"] == 2
    assert not _exists(real_db, Folder, parent)
    assert not _exists(real_db, Folder, child)
    assert not _exists(real_db, Asset, inner)
    assert "raw/inner.mp4" in s3_deleted


def test_live_folder_cannot_be_deleted_forever(api, real_db, seeded, s3_deleted):
    user, project = seeded
    folder = _folder(real_db, user, project, deleted=False)

    resp = api.delete(f"/folders/{folder}/permanent")

    assert resp.status_code == 404
    assert _exists(real_db, Folder, folder)


def test_empty_trash_purges_only_trashed_items(api, real_db, seeded, s3_deleted):
    user, project = seeded
    trashed_folder = _folder(real_db, user, project)
    in_folder = _asset(real_db, user, project, folder=trashed_folder, key="raw/1.mp4")
    loose = _asset(real_db, user, project, key="raw/2.mp4")
    live_folder = _folder(real_db, user, project, deleted=False)
    live = _asset(real_db, user, project, folder=live_folder, deleted=False, key="raw/live.mp4")

    resp = api.delete(f"/projects/{project.id}/trash")

    assert resp.status_code == 200
    body = resp.json()
    assert body["folders"] == 1 and body["assets"] == 2
    assert not _exists(real_db, Folder, trashed_folder)
    assert not _exists(real_db, Asset, in_folder)
    assert not _exists(real_db, Asset, loose)
    assert _exists(real_db, Folder, live_folder)
    assert _exists(real_db, Asset, live)
    assert "raw/live.mp4" not in s3_deleted

    assert api.get(f"/projects/{project.id}/trash").json() == {"folders": [], "assets": []}
