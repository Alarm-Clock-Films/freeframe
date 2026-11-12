"""can_access_asset in one query, and for a list of assets in two.

Every request that opens, comments on or streams an asset runs this check; it
used to cost up to five round trips, and GET /assets/batch (what the uploads
panel polls) ran it once per asset. Against a real Postgres.
"""
import uuid
from datetime import datetime, timezone

import pytest

from .test_share_links_trashed_content import _auth, _seed, client, db  # noqa: F401 - fixtures
from .test_share_listing_bulk import _QueryCounter


def _outsider(db):
    from apps.api.models.user import User

    u = User(email=f"outsider-{uuid.uuid4().hex[:8]}@t.local", name="outsider")
    db.add(u); db.flush()
    return u


def _expected(w, outsider):
    """(user, asset) -> may access, on the seeded private project."""
    assets = {"A": w.A, "AF": w.AF, "AS": w.AS, "A_creator": w.A_creator}
    table = {}
    for name, asset in assets.items():
        for member in (w.owner, w.editor, w.viewer):
            table[(member.id, asset.id)] = True
        table[(w.direct.id, asset.id)] = name == "A"            # shared with them directly
        table[(w.creator.id, asset.id)] = name == "A_creator"   # they uploaded it
        table[(outsider.id, asset.id)] = False
    return assets, table


def _users(w, outsider):
    return [w.owner, w.editor, w.viewer, w.direct, w.creator, outsider]


def test_every_path_to_an_asset_answers_as_before(db):
    from apps.api.services.permissions import accessible_asset_ids, can_access_asset

    w = _seed(db)
    outsider = _outsider(db)
    assets, table = _expected(w, outsider)

    for user in _users(w, outsider):
        for asset in assets.values():
            assert can_access_asset(db, asset, user) is table[(user.id, asset.id)], (user.name, asset.name)
        allowed = accessible_asset_ids(db, list(assets.values()), user)
        assert allowed == {a.id for a in assets.values() if table[(user.id, a.id)]}, user.name


def test_a_public_project_opens_to_anyone_signed_in(db):
    from apps.api.services.permissions import accessible_asset_ids, can_access_asset

    w = _seed(db)
    outsider = _outsider(db)
    w.project.is_public = True
    db.flush()

    assert can_access_asset(db, w.AF, outsider) is True
    assert accessible_asset_ids(db, [w.A, w.AF, w.AS], outsider) == {w.A.id, w.AF.id, w.AS.id}


def test_nothing_reaches_an_asset_in_a_trashed_project(db):
    from apps.api.services.permissions import accessible_asset_ids, can_access_asset

    w = _seed(db)
    w.project.is_public = True
    w.project.deleted_at = datetime.now(timezone.utc)
    db.flush()

    for user in (w.owner, w.direct, w.creator):
        assert can_access_asset(db, w.A, user) is False
        assert can_access_asset(db, w.A_creator, user) is False
        assert accessible_asset_ids(db, [w.A, w.A_creator], user) == set()


def test_a_removed_member_and_a_withdrawn_share_lose_access(db):
    from apps.api.models.project import ProjectMember
    from apps.api.models.share import AssetShare
    from apps.api.services.permissions import accessible_asset_ids, can_access_asset

    w = _seed(db)
    now = datetime.now(timezone.utc)
    db.query(ProjectMember).filter(ProjectMember.user_id == w.viewer.id).update({"deleted_at": now})
    db.query(AssetShare).filter(AssetShare.shared_with_user_id == w.direct.id).update({"deleted_at": now})
    db.flush()

    for user in (w.viewer, w.direct):
        assert can_access_asset(db, w.A, user) is False
        assert accessible_asset_ids(db, [w.A, w.AF], user) == set()


def test_the_check_is_one_query(db):
    from apps.api.services.permissions import can_access_asset

    w = _seed(db)
    outsider = _outsider(db)
    db.flush()

    for user in (w.owner, w.direct, outsider):  # granted early, granted late, refused
        with _QueryCounter(db) as counter:
            can_access_asset(db, w.A, user)
        assert counter.count == 1


def test_a_list_is_two_queries_whatever_its_length(db):
    from apps.api.services.permissions import accessible_asset_ids

    w = _seed(db)
    assets = [w.A, w.AF, w.AS, w.A_creator]
    with _QueryCounter(db) as counter:
        accessible_asset_ids(db, assets, w.direct)
    assert counter.count == 2
    assert accessible_asset_ids(db, [], w.direct) == set()


def test_the_batch_endpoint_leaves_out_what_the_caller_may_not_open(client, db):
    w = _seed(db)
    ids = "&".join(f"ids={a.id}" for a in (w.A, w.AF, w.A_creator))

    def names(user):
        resp = client.get(f"/assets/batch?{ids}", headers=_auth(user))
        assert resp.status_code == 200, resp.text
        return {a["name"] for a in resp.json()}

    assert names(w.viewer) == {"A", "AF", "A-by-nonmember"}
    assert names(w.direct) == {"A"}
    assert names(w.creator) == {"A-by-nonmember"}
