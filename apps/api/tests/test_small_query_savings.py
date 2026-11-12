"""Three places that asked the database more often than they needed to.

  * the notification list: up to four lookups for each of fifty notifications;
  * "is this folder under that one": a query per level, per shared folder;
  * the connection pool: fixed at the driver defaults, with no way to size it.
"""
import uuid
from datetime import datetime, timezone

import pytest

from .test_share_links_trashed_content import _auth, _seed, client, db  # noqa: F401 - fixtures
from .test_share_listing_bulk import _QueryCounter


# ── Folder ancestry ──────────────────────────────────────────────────────────

def _folder(db, w, name, parent=None):
    from apps.api.models.folder import Folder

    f = Folder(project_id=w.project.id, name=name, created_by=w.owner.id,
               parent_id=parent.id if parent else None)
    db.add(f); db.flush()
    return f


def test_ancestry_follows_live_folders_only(db):
    from apps.api.services.permissions import _is_descendant_of, _is_under_any

    w = _seed(db)  # F holds S
    deep = _folder(db, w, "deep", w.S)
    other = _folder(db, w, "other")

    assert _is_descendant_of(db, deep.id, w.F.id) is True
    assert _is_descendant_of(db, w.S.id, w.F.id) is True
    assert _is_descendant_of(db, w.F.id, w.F.id) is True       # a folder counts as under itself
    assert _is_descendant_of(db, w.F.id, w.S.id) is False      # not the other way up
    assert _is_descendant_of(db, deep.id, other.id) is False
    assert _is_descendant_of(db, uuid.uuid4(), w.F.id) is False  # no such folder
    assert _is_under_any(db, deep.id, [other.id, w.F.id]) is True
    assert _is_under_any(db, deep.id, []) is False

    # A trashed folder on the way up, the ancestor included, breaks the chain.
    w.S.deleted_at = datetime.now(timezone.utc)
    db.flush()
    assert _is_descendant_of(db, deep.id, w.F.id) is False
    assert _is_descendant_of(db, deep.id, w.S.id) is False
    assert _is_descendant_of(db, deep.id, deep.id) is True


def test_a_trashed_folder_above_the_ancestor_does_not_matter(db):
    from apps.api.services.permissions import _is_descendant_of

    w = _seed(db)
    deep = _folder(db, w, "deep", w.S)
    w.F.deleted_at = datetime.now(timezone.utc)
    db.flush()

    assert _is_descendant_of(db, deep.id, w.S.id) is True


def test_ancestry_ends_on_a_parent_cycle(db):
    from apps.api.services.permissions import _is_descendant_of

    w = _seed(db)
    a = _folder(db, w, "a")
    b = _folder(db, w, "b", a)
    a.parent_id = b.id
    db.flush()

    assert _is_descendant_of(db, b.id, w.F.id) is False
    assert _is_descendant_of(db, b.id, a.id) is True


def test_ancestry_is_one_query_however_deep_and_however_many_ancestors(db):
    from apps.api.services.permissions import _is_under_any

    w = _seed(db)
    folder = w.S
    for n in range(8):
        folder = _folder(db, w, f"level-{n}", folder)
    others = [_folder(db, w, f"other-{n}").id for n in range(6)]

    with _QueryCounter(db) as counter:
        assert _is_under_any(db, folder.id, others + [w.F.id]) is True
    assert counter.count == 1


def test_a_multi_item_link_still_scopes_folders_and_assets(client, db):
    """The two callers that moved to the one-query walk."""
    w = _seed(db)  # the multi link lists asset A and folder F
    token = w.links["multi"].token
    outside = _folder(db, w, "outside")

    assert client.get(f"/share/{token}/assets?folder_id={w.S.id}").status_code == 200
    assert client.get(f"/share/{token}/assets?folder_id={outside.id}").status_code == 403
    assert client.get(f"/share/{token}/stream/{w.AS.id}").status_code == 200
    assert client.get(f"/share/{token}/stream/{w.A_creator.id}").status_code == 403


# ── Notifications ────────────────────────────────────────────────────────────

def _notify(db, w, asset, *, author=None, guest=None, body="Looks great, ship it"):
    from apps.api.models.activity import Notification, NotificationType
    from apps.api.models.comment import Comment

    comment = Comment(asset_id=asset.id, version_id=w.versions[asset.id].id, body=body,
                      author_id=author.id if author else None,
                      guest_author_id=guest.id if guest else None)
    db.add(comment); db.flush()
    n = Notification(user_id=w.owner.id, type=NotificationType.mention, asset_id=asset.id, comment_id=comment.id)
    db.add(n); db.flush()
    return n


def test_notifications_name_their_asset_actor_and_comment(client, db):
    from apps.api.models.user import GuestUser

    w = _seed(db)
    guest = GuestUser(email=f"guest-{uuid.uuid4().hex[:8]}@example.com", name="Visiting Client")
    db.add(guest); db.flush()
    _notify(db, w, w.A, author=w.editor, body="x" * 150)
    _notify(db, w, w.AF, guest=guest)

    resp = client.get("/me/notifications", headers=_auth(w.owner))

    assert resp.status_code == 200, resp.text
    by_asset = {n["asset_name"]: n for n in resp.json()}
    assert by_asset["A"]["actor_name"] == "editor" and by_asset["A"]["actor_is_guest"] is False
    assert by_asset["A"]["comment_preview"] == "x" * 100
    assert by_asset["A"]["project_id"] == str(w.project.id)
    assert by_asset["AF"]["actor_name"] == "Visiting Client" and by_asset["AF"]["actor_is_guest"] is True


def test_the_notification_list_costs_the_same_however_many_it_lists(client, db):
    w = _seed(db)
    headers = _auth(w.owner)

    def cost():
        with _QueryCounter(db) as counter:
            resp = client.get("/me/notifications", headers=headers)
        assert resp.status_code == 200
        return counter.count, len(resp.json())

    _notify(db, w, w.A, author=w.editor)
    queries_for_one, listed = cost()
    assert listed == 1

    for n in range(30):
        _notify(db, w, w.AF if n % 2 else w.A, author=w.viewer if n % 3 else w.editor)
    db.expire_all()

    queries_for_many, listed = cost()
    assert listed == 31
    assert queries_for_many == queries_for_one


# ── Connection pool ──────────────────────────────────────────────────────────

def test_the_pool_keeps_the_driver_defaults_unless_set(monkeypatch):
    from apps.api import database
    from apps.api.config import settings

    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(settings, "db_pool_size", None)
    monkeypatch.setattr(settings, "db_max_overflow", None)
    monkeypatch.setattr(settings, "db_pool_pre_ping", False)
    assert database._engine_options() == {}

    monkeypatch.setattr(settings, "db_pool_size", 12)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    monkeypatch.setattr(settings, "db_pool_pre_ping", True)
    assert database._engine_options() == {"pool_size": 12, "max_overflow": 0, "pool_pre_ping": True}


def test_vercel_keeps_its_small_pool_whatever_is_set(monkeypatch):
    from apps.api import database
    from apps.api.config import settings

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "db_pool_size", 50)

    assert database._engine_options() == {
        "pool_pre_ping": True, "pool_recycle": 300, "pool_size": 2, "max_overflow": 8,
    }


@pytest.mark.parametrize("name,value", [("DB_POOL_SIZE", "0"), ("DB_MAX_OVERFLOW", "-1")])
def test_a_pool_setting_that_cannot_work_is_refused_at_startup(monkeypatch, name, value):
    from pydantic import ValidationError
    from apps.api.config import Settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()
