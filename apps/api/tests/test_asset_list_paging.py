"""GET /projects/{id}/assets honours skip and limit, and filters before it pages.

It returned every asset whatever the caller asked for: `skip` and `limit` were
not parameters at all, so a folder card loaded (and had signed) its folder's
whole list to show three thumbnails. Against a real Postgres.
"""
from datetime import datetime, timedelta, timezone

import pytest

from .test_share_links_trashed_content import _auth, _seed, client, db  # noqa: F401 - fixtures
from .test_share_listing_bulk import _QueryCounter, _asset


def _fill(db, w, count, folder=None):
    """`count` more assets, each a second newer than the last."""
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    made = []
    for n in range(count):
        a = _asset(db, w, f"n{n:02d}", folder)
        a.created_at = base + timedelta(seconds=n)
        made.append(a)
    db.flush()
    return made


def _names(client, w, query=""):
    resp = client.get(f"/projects/{w.project.id}/assets?{query}", headers=_auth(w.viewer))
    assert resp.status_code == 200, resp.text
    return [a["name"] for a in resp.json()]


def test_without_a_limit_the_whole_folder_is_listed_newest_first(client, db):
    w = _seed(db)
    _fill(db, w, 5, w.F)

    names = _names(client, w, f"folder_id={w.F.id}")

    assert names == ["n04", "n03", "n02", "n01", "n00", "AF"]


def test_limit_and_skip_page_through_the_list_without_overlap(client, db):
    w = _seed(db)
    _fill(db, w, 7, w.F)
    everything = _names(client, w, f"folder_id={w.F.id}")

    pages = [_names(client, w, f"folder_id={w.F.id}&skip={skip}&limit=3") for skip in (0, 3, 6, 9)]

    assert [len(p) for p in pages] == [3, 3, 2, 0]
    assert [name for page in pages for name in page] == everything


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "skip=-1"])
def test_paging_outside_its_bounds_is_refused(client, db, query):
    w = _seed(db)

    resp = client.get(f"/projects/{w.project.id}/assets?{query}", headers=_auth(w.viewer))

    assert resp.status_code == 422


def test_a_page_is_cut_from_the_assets_that_are_listed(client, db):
    """Failed and still-uploading assets are left out before the page is taken,
    so they cannot use up its places."""
    from apps.api.models.asset import Asset, AssetType, AssetVersion, ProcessingStatus

    w = _seed(db)
    newest = datetime.now(timezone.utc) + timedelta(hours=1)
    for n, status in enumerate((ProcessingStatus.failed, ProcessingStatus.uploading)):
        a = _asset(db, w, f"hidden-{n}", w.F)
        a.created_at = newest + timedelta(seconds=n)
        db.query(AssetVersion).filter(AssetVersion.asset_id == a.id).update({"processing_status": status})
    empty = Asset(project_id=w.project.id, name="no-versions-yet", asset_type=AssetType.video,
                  created_by=w.owner.id, folder_id=w.F.id, created_at=newest + timedelta(seconds=5))
    db.add(empty)
    db.flush()

    assert _names(client, w, f"folder_id={w.F.id}&limit=2") == ["no-versions-yet", "AF"]
    assert set(_names(client, w, f"folder_id={w.F.id}&include_failed=true")) == {
        "no-versions-yet", "hidden-1", "hidden-0", "AF",
    }


def test_an_asset_with_a_usable_older_version_stays_listed(client, db):
    from apps.api.models.asset import AssetVersion, ProcessingStatus

    w = _seed(db)
    a = _asset(db, w, "re-uploading", w.F, ready_versions=2)
    db.query(AssetVersion).filter(
        AssetVersion.asset_id == a.id, AssetVersion.version_number == 2,
    ).update({"processing_status": ProcessingStatus.failed})
    db.flush()

    assert "re-uploading" in _names(client, w, f"folder_id={w.F.id}")


def test_a_small_page_costs_no_more_however_full_the_folder_is(client, db):
    w = _seed(db)
    url = f"/projects/{w.project.id}/assets?folder_id={w.F.id}&limit=3"
    headers = _auth(w.viewer)

    def cost():
        with _QueryCounter(db) as counter:
            resp = client.get(url, headers=headers)
        assert resp.status_code == 200
        return counter.count, len(resp.json())

    _fill(db, w, 3, w.F)
    before = cost()
    _fill(db, w, 60, w.F)
    db.expire_all()

    assert cost() == before
