"""Limits on what a comment may carry, and three places that said too much.

Against a real Postgres through the real app, with the seeded project from
test_share_links_trashed_content.
"""
import time
import uuid
from unittest.mock import patch

import pytest

from .test_share_links_trashed_content import _auth, _seed, client, db  # noqa: F401 - fixtures


@pytest.fixture
def limits(monkeypatch):
    """Small limits, so the tests do not have to build megabytes."""
    from apps.api.config import settings

    monkeypatch.setattr(settings, "comment_max_chars", 50)
    monkeypatch.setattr(settings, "annotation_max_bytes", 200)
    monkeypatch.setattr(settings, "comment_attachment_max_bytes", 1000)
    return settings


def _guest(body="hello", **extra):
    return {"body": body, "guest_email": "guest@example.com", "guest_name": "Guest", **extra}


# ── Comment size ─────────────────────────────────────────────────────────────

def test_a_guest_comment_within_the_limits_is_accepted(client, db, limits):
    w = _seed(db)

    resp = client.post(f"/share/{w.links['asset'].token}/comment", json=_guest("x" * 50))

    assert resp.status_code == 201, resp.text


def test_a_guest_comment_longer_than_the_limit_is_refused(client, db, limits):
    w = _seed(db)

    resp = client.post(f"/share/{w.links['asset'].token}/comment", json=_guest("x" * 51))

    assert resp.status_code == 422, resp.text
    assert "at most 50 characters" in resp.text


def test_a_guest_annotation_larger_than_the_limit_is_refused(client, db, limits):
    w = _seed(db)
    token = w.links["asset"].token

    small = client.post(f"/share/{token}/comment", json=_guest(annotation={"drawing_data": {"objects": []}}))
    large = client.post(
        f"/share/{token}/comment",
        json=_guest(annotation={"drawing_data": {"objects": ["p" * 300]}}),
    )

    assert small.status_code == 201, small.text
    assert large.status_code == 422, large.text


@pytest.mark.parametrize("field", ["guest_email", "guest_name"])
def test_an_overlong_guest_identity_is_refused_not_a_server_error(client, db, field):
    w = _seed(db)

    resp = client.post(f"/share/{w.links['asset'].token}/comment", json=_guest(**{field: "n" * 256}))

    assert resp.status_code == 422, resp.text


def test_a_member_comment_and_its_edit_obey_the_same_limit(client, db, limits):
    w = _seed(db)
    version_id = str(w.versions[w.A.id].id)
    headers = _auth(w.editor)

    ok = client.post(f"/assets/{w.A.id}/comments", json={"version_id": version_id, "body": "fine"}, headers=headers)
    too_long = client.post(
        f"/assets/{w.A.id}/comments", json={"version_id": version_id, "body": "x" * 51}, headers=headers,
    )
    edit = client.patch(f"/comments/{ok.json()['id']}", json={"body": "x" * 51}, headers=headers)

    assert ok.status_code == 201, ok.text
    assert too_long.status_code == 422
    assert edit.status_code == 422


def test_a_comment_cannot_carry_an_unbounded_list_of_mentions(client, db):
    w = _seed(db)
    body = {
        "version_id": str(w.versions[w.A.id].id), "body": "hi",
        "mention_user_ids": [str(uuid.uuid4()) for _ in range(101)],
    }

    resp = client.post(f"/assets/{w.A.id}/comments", json=body, headers=_auth(w.editor))

    assert resp.status_code == 422


def test_an_attachment_larger_than_the_limit_is_refused(client, db, limits):
    w = _seed(db)
    headers = _auth(w.editor)
    comment = client.post(
        f"/assets/{w.A.id}/comments",
        json={"version_id": str(w.versions[w.A.id].id), "body": "see file"}, headers=headers,
    ).json()

    def attach(size):
        return client.post(
            f"/comments/{comment['id']}/attachments",
            json={"file_name": "notes.pdf", "file_size": size, "content_type": "application/pdf"},
            headers=headers,
        )

    assert attach(1000).status_code == 201
    assert attach(1001).status_code == 422
    assert attach(-1).status_code == 422


# ── Guest comment rate limit ─────────────────────────────────────────────────

def test_guest_comments_are_limited_per_address(client, db, monkeypatch):
    from apps.api.config import settings

    w = _seed(db)
    monkeypatch.setattr(settings, "guest_comments_per_minute", 7)
    seen = []

    def check(ip, action, max_requests, window_seconds):
        seen.append((ip, action, max_requests, window_seconds))
        return (False, 12)

    with patch("apps.api.middleware.rate_limit.check_rate_limit", side_effect=check):
        resp = client.post(f"/share/{w.links['asset'].token}/comment", json=_guest())

    assert resp.status_code == 429, resp.text
    assert resp.headers["retry-after"] == "12"
    assert seen == [(client.headers["x-real-ip"], "guest_comment", 7, 60)]


def test_the_guest_comment_limit_can_be_switched_off(client, db, monkeypatch):
    from apps.api.config import settings

    w = _seed(db)
    monkeypatch.setattr(settings, "guest_comments_per_minute", 0)

    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(False, 12)) as check:
        resp = client.post(f"/share/{w.links['asset'].token}/comment", json=_guest())

    assert resp.status_code == 201, resp.text
    check.assert_not_called()


# ── Share link password: shown to those who can set it ───────────────────────

def test_only_editors_and_owners_are_shown_a_share_links_password(client, db):
    from apps.api.services.crypto_service import encrypt_password

    w = _seed(db)
    link = w.links["password"]
    link.password_encrypted = encrypt_password("correct horse battery")
    db.flush()

    def details(user):
        resp = client.get(f"/share/{link.token}/details", headers=_auth(user))
        assert resp.status_code == 200, resp.text
        return resp.json()

    assert details(w.owner)["password_value"] == "correct horse battery"
    assert details(w.editor)["password_value"] == "correct horse battery"
    viewer = details(w.viewer)
    assert viewer["password_value"] is None
    assert viewer["has_password"] is True


# ── Direct shares: no address lookup for outsiders ───────────────────────────

def _share_user_paths(w):
    return [
        f"/projects/{w.project.id}/share/user",
        f"/folders/{w.F.id}/share/user",
        f"/assets/{w.A.id}/share/user",
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_someone_without_the_role_cannot_tell_which_addresses_have_accounts(client, db, index):
    w = _seed(db)
    path = _share_user_paths(w)[index]

    def ask(user, email):
        resp = client.post(path, json={"email": email, "permission": "view"}, headers=_auth(user))
        return resp.status_code, resp.json()

    for outsider in (w.viewer, w.creator):
        assert ask(outsider, w.owner.email) == ask(outsider, "nobody-here@t.local")
        assert ask(outsider, w.owner.email)[0] == 403
    # An editor still gets the useful answer.
    assert ask(w.editor, "nobody-here@t.local") == (404, {"detail": "User not found with that email"})


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("token", ["../../login?x=", "a b", "tok?next=//evil.example", ""])
def test_a_share_token_that_is_not_a_token_is_refused(client, db, index, token):
    w = _seed(db)

    with patch("apps.api.routers.share.send_task_safe") as send:
        resp = client.post(
            _share_user_paths(w)[index],
            json={"user_id": str(w.direct.id), "permission": "view", "share_token": token},
            headers=_auth(w.editor),
        )

    assert resp.status_code == 400, resp.text
    send.assert_not_called()


def test_a_real_share_token_still_goes_into_the_email_link(client, db):
    w = _seed(db)
    token = w.links["asset"].token

    with patch("apps.api.routers.share.send_task_safe") as send:
        resp = client.post(
            f"/projects/{w.project.id}/share/user",
            json={"user_id": str(w.direct.id), "permission": "view", "share_token": token},
            headers=_auth(w.editor),
        )

    assert resp.status_code == 201, resp.text
    assert send.call_args.kwargs["asset_link"].endswith(f"/share/{token}")


# ── Password login takes as long for an unknown address ──────────────────────

def test_login_checks_a_password_even_when_there_is_no_account(client, db):
    from apps.api.services import auth_service

    _seed(db)
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch.object(auth_service.bcrypt, "checkpw", wraps=auth_service.bcrypt.checkpw) as checkpw:
        resp = client.post("/auth/login", json={"email": "nobody-here@example.com", "password": "whatever-it-is"})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid credentials"}
    assert checkpw.call_count == 1


def test_login_checks_a_password_for_an_account_that_has_none(client, db):
    from apps.api.services import auth_service

    from apps.api.models.user import User

    address = f"codes-only-{uuid.uuid4().hex[:8]}@example.com"
    db.add(User(email=address, name="Codes Only"))  # signs in by emailed code: no password
    db.flush()
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch.object(auth_service.bcrypt, "checkpw", wraps=auth_service.bcrypt.checkpw) as checkpw:
        resp = client.post("/auth/login", json={"email": address, "password": "whatever-it-is"})

    assert resp.status_code == 401
    assert checkpw.call_count == 1


def test_the_decoy_check_never_accepts_a_password():
    from apps.api.services.auth_service import check_password, hash_password

    assert check_password("not-a-real-password", None) is False
    assert check_password("anything", "") is False
    assert check_password("right", hash_password("right")) is True
    assert check_password("wrong", hash_password("right")) is False
