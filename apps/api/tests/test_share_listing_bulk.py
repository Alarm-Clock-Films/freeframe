"""GET /share/{token}/assets: bounded paging, and a fixed number of queries.

The listing needs no sign-in. It accepted any `page` and `per_page`, and ran
about six queries per asset and eleven per subfolder, so one anonymous request
could ask for a whole project and cost thousands of round trips. Against a
real Postgres through the real app.
"""
import uuid

import pytest
from sqlalchemy import event

from .test_share_links_trashed_content import _auth, _seed, client, db  # noqa: F401 - fixtures


def _asset(db, w, name, folder, *, ready_versions=1):
    from apps.api.models.asset import (
        Asset, AssetType, AssetVersion, FileType, MediaFile, ProcessingStatus,
    )

    a = Asset(project_id=w.project.id, name=name, asset_type=AssetType.video,
              created_by=w.owner.id, folder_id=folder.id if folder else None)
    db.add(a); db.flush()
    for number in range(1, ready_versions + 1):
        v = AssetVersion(asset_id=a.id, version_number=number,
                         processing_status=ProcessingStatus.ready, created_by=w.owner.id)
        db.add(v); db.flush()
        db.add(MediaFile(version_id=v.id, file_type=FileType.video, original_filename=f"{name}.mov",
                         mime_type="video/quicktime", file_size_bytes=1000 * number,
                         s3_key_raw=f"raw/{a.id}/{number}.mov",
                         s3_key_thumbnail=f"thumbs/{a.id}/v{number}.jpg", duration_seconds=float(number)))
        w.versions[a.id] = v
    db.flush()
    return a


def _comment(db, w, asset, *, parent=None, visibility="public", deleted=False):
    from datetime import datetime, timezone
    from apps.api.models.comment import Comment

    c = Comment(asset_id=asset.id, version_id=w.versions[asset.id].id, author_id=w.owner.id,
                parent_id=parent.id if parent else None, body="x", visibility=visibility,
                deleted_at=datetime.now(timezone.utc) if deleted else None)
    db.add(c); db.flush()
    return c


class _QueryCounter:
    def __init__(self, db):
        self.conn = db.connection()
        self.count = 0

    def _on_execute(self, conn, cursor, statement, *args):
        if not statement.lstrip().upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            self.count += 1

    def __enter__(self):
        event.listen(self.conn, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.conn, "before_cursor_execute", self._on_execute)


# ── Paging bounds ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "per_page=0", "per_page=-1", "per_page=501", "per_page=100000000", "page=0", "page=-3",
])
def test_the_public_listing_refuses_paging_outside_its_bounds(client, db, query):
    w = _seed(db)

    resp = client.get(f"/share/{w.links['folder'].token}/assets?{query}")

    assert resp.status_code == 422, resp.text


def test_the_public_listing_still_serves_the_largest_page_the_app_asks_for(client, db):
    w = _seed(db)

    resp = client.get(f"/share/{w.links['folder'].token}/assets?page=1&per_page=500")

    assert resp.status_code == 200, resp.text
    assert resp.json()["per_page"] == 500


@pytest.mark.parametrize("query", ["per_page=0", "per_page=501", "page=0"])
def test_the_activity_listing_refuses_paging_outside_its_bounds(client, db, query):
    w = _seed(db)

    resp = client.get(f"/share/{w.links['folder'].token}/activity?{query}", headers=_auth(w.owner))

    assert resp.status_code == 422, resp.text


# ── What the listing returns ─────────────────────────────────────────────────

def test_a_folder_listing_describes_its_assets_and_subfolders(client, db):
    w = _seed(db)
    two = _asset(db, w, "two-versions", w.F, ready_versions=2)
    root = _comment(db, w, two)
    _comment(db, w, two, parent=root)
    _comment(db, w, two, visibility="internal")
    gone = _comment(db, w, two, deleted=True)
    _comment(db, w, two, parent=gone)  # under a deleted comment: not shown
    link = w.links["folder"]
    link.show_versions = True
    db.flush()

    body = client.get(f"/share/{link.token}/assets").json()

    assets = {a["name"]: a for a in body["assets"]}
    assert set(assets) == {"AF", "two-versions"}
    assert body["total"] == 2
    card = assets["two-versions"]
    # The newest ready version's file, and only what a link holder may see.
    assert card["file_size"] == 2000 and card["duration_seconds"] == 2.0
    assert f"thumbs/{two.id}/v2.jpg" in card["thumbnail_url"]
    assert card["comment_count"] == 2
    assert card["version_count"] == 2
    assert card["created_by_name"] == "owner"
    assert assets["AF"]["comment_count"] == 0 and assets["AF"]["version_count"] == 1

    assert [(s["name"], s["item_count"]) for s in body["subfolders"]] == [("S", 1)]
    assert len(body["subfolders"][0]["thumbnail_urls"]) == 1
    assert f"thumbs/{w.AS.id}/v1.jpg" in body["subfolders"][0]["thumbnail_urls"][0]


def test_version_count_stays_hidden_when_the_link_hides_versions(client, db):
    w = _seed(db)
    _asset(db, w, "two-versions", w.F, ready_versions=2)
    link = w.links["folder"]
    link.show_versions = False
    db.flush()

    body = client.get(f"/share/{link.token}/assets").json()

    assert {a["name"]: a["version_count"] for a in body["assets"]} == {"AF": 1, "two-versions": 1}


def test_a_subfolder_card_shows_at_most_four_thumbnails_and_counts_everything(client, db):
    from apps.api.models.folder import Folder

    w = _seed(db)
    for n in range(6):
        _asset(db, w, f"extra-{n}", w.S)
    db.add(Folder(project_id=w.project.id, name="deeper", parent_id=w.S.id, created_by=w.owner.id))
    db.flush()

    body = client.get(f"/share/{w.links['folder'].token}/assets").json()

    (card,) = body["subfolders"]
    assert card["item_count"] == 7 + 1  # AS and six more, plus one folder
    assert len(card["thumbnail_urls"]) == 4


def test_a_multi_item_link_lists_its_items(client, db):
    w = _seed(db)

    body = client.get(f"/share/{w.links['multi'].token}/assets").json()

    assert [a["name"] for a in body["assets"]] == ["A"]
    assert f"thumbs/{w.A.id}/v1.jpg" in body["assets"][0]["thumbnail_url"]
    assert [(s["name"], s["item_count"]) for s in body["subfolders"]] == [("F", 2)]
    assert len(body["subfolders"][0]["thumbnail_urls"]) == 1


# ── Cost ─────────────────────────────────────────────────────────────────────

def _queries_for(client, db, url):
    with _QueryCounter(db) as counter:
        resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return counter.count, resp.json()


@pytest.mark.parametrize("kind", ["folder", "project", "multi"])
def test_the_listing_costs_the_same_number_of_queries_however_much_it_lists(client, db, kind):
    from apps.api.models.folder import Folder
    from apps.api.models.share import ShareLinkItem

    w = _seed(db)
    link = w.links[kind]
    link.show_versions = True
    db.flush()
    url = f"/share/{link.token}/assets?per_page=500"
    before, small = _queries_for(client, db, url)

    for n in range(25):
        parent = w.F if kind == "folder" else None
        folder = Folder(project_id=w.project.id, name=f"sub-{n}", created_by=w.owner.id,
                        parent_id=parent.id if parent else None)
        db.add(folder); db.flush()
        _asset(db, w, f"in-sub-{n}", folder)
        extra = _asset(db, w, f"more-{n}", parent)
        _comment(db, w, extra)
        if kind == "multi":
            db.add(ShareLinkItem(share_link_id=link.id, asset_id=extra.id))
            db.add(ShareLinkItem(share_link_id=link.id, folder_id=folder.id))
    db.flush()
    db.expire_all()

    after, large = _queries_for(client, db, url)

    assert len(large["assets"]) == len(small["assets"]) + 25
    assert len(large["subfolders"]) == len(small["subfolders"]) + 25
    assert after == before
