"""Regression tests for three auth gaps.

1. /auth/set-password replaced an existing password for any access token.
2. An access token outlived a password change (only refresh checked the version).
3. Rate limits and the sign-in code attempt cap read the counter and added to it
   in two steps, so requests sent together all passed on the same reading.
"""
import threading
import uuid
from unittest.mock import patch

import pytest

from .conftest import _make_user


class AtomicFakeRedis:
    """Counters with the atomicity real Redis gives a MULTI/EXEC, plus a pause
    after every plain read so a read-then-write sequence loses its race."""

    def __init__(self, parties: int = 1):
        self.data: dict[str, str] = {}
        self._lock = threading.Lock()
        # Every thread reads before any thread goes on, as a burst does.
        self._after_read = threading.Barrier(parties, timeout=5) if parties > 1 else None

    def get(self, key):
        with self._lock:
            value = self.data.get(key)
        if self._after_read is not None:
            try:
                self._after_read.wait()
            except threading.BrokenBarrierError:
                pass
        return value

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    def setex(self, key, ttl, value):
        with self._lock:
            self.data[key] = str(value)

    def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    def delete(self, *keys):
        with self._lock:
            for key in keys:
                self.data.pop(key, None)

    def ttl(self, key):
        return 30

    def pipeline(self, transaction=True):
        return _AtomicPipeline(self)


class _AtomicPipeline:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))
        return queue

    def execute(self):
        with self.r._lock:
            return [getattr(self.r, name)(*args, **kwargs) for name, args, kwargs in self.ops]


def _burst(fn, count):
    results = []
    lock = threading.Lock()

    def run():
        value = fn()
        with lock:
            results.append(value)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ── 3. Atomic counters ───────────────────────────────────────────────────────

def test_a_burst_cannot_pass_the_rate_limit_together():
    from apps.api.services import redis_service

    r = AtomicFakeRedis(parties=20)
    with patch.object(redis_service, "get_redis", return_value=r):
        results = _burst(lambda: redis_service.check_rate_limit("1.2.3.4", "login", 5, 600), 20)

    assert sum(1 for allowed, _ in results if allowed) == 5


def test_a_burst_gets_no_more_guesses_at_a_code_than_the_cap():
    from apps.api.services import redis_service

    r = AtomicFakeRedis(parties=20)
    with patch.object(redis_service, "get_redis", return_value=r):
        redis_service.store_magic_code("user-1", "123456")
        guesses = [f"{n:06d}" for n in range(20)]  # none is the code
        results = _burst(lambda: redis_service.verify_magic_code("user-1", guesses.pop()), 20)

    wrong_code = [message for ok, message in results if not ok and message == "Invalid code"]
    assert len(wrong_code) == redis_service.MAX_MAGIC_CODE_ATTEMPTS
    assert not any(ok for ok, _ in results)


def test_the_right_code_still_signs_in_and_is_used_up():
    from apps.api.services import redis_service

    r = AtomicFakeRedis()
    with patch.object(redis_service, "get_redis", return_value=r):
        redis_service.store_magic_code("user-1", "123456")
        assert redis_service.verify_magic_code("user-1", "000000") == (False, "Invalid code")
        assert redis_service.verify_magic_code("user-1", "123456") == (True, "")
        assert redis_service.verify_magic_code("user-1", "123456")[0] is False


def test_the_right_code_is_refused_once_the_attempts_are_spent():
    from apps.api.services import redis_service

    r = AtomicFakeRedis()
    with patch.object(redis_service, "get_redis", return_value=r):
        redis_service.store_magic_code("user-1", "123456")
        for _ in range(redis_service.MAX_MAGIC_CODE_ATTEMPTS):
            redis_service.verify_magic_code("user-1", "000000")
        assert redis_service.verify_magic_code("user-1", "123456")[0] is False


# ── 2. Access tokens follow token_version ────────────────────────────────────

@pytest.fixture
def signed_in(client, mock_db):
    """A user whose row every lookup returns, reached through the real
    get_current_user (no dependency override)."""
    user = _make_user(user_id=uuid.uuid4())
    user.token_version = 3
    mock_db.first.return_value = user
    return user


def _bearer(user, version, auth_method=None):
    from apps.api.services.auth_service import create_access_token

    token = create_access_token(str(user.id), token_version=version, auth_method=auth_method)
    return {"Authorization": f"Bearer {token}"}


def test_an_access_token_from_before_a_password_change_is_refused(client, signed_in):
    assert client.get("/auth/me", headers=_bearer(signed_in, 2)).status_code == 401
    assert client.get("/auth/me", headers=_bearer(signed_in, 3)).status_code == 200


def test_optional_auth_ignores_a_stale_access_token(mock_db, signed_in):
    from fastapi.security import HTTPAuthorizationCredentials
    from apps.api.middleware.auth import get_optional_user

    def resolve(version):
        token = _bearer(signed_in, version)["Authorization"].split(" ", 1)[1]
        return get_optional_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), mock_db)

    assert resolve(2) is None
    assert resolve(3) is signed_in


def test_deactivating_a_user_ends_their_sessions(client, mock_db):
    from apps.api.main import app
    from apps.api.middleware.auth import get_current_user
    from apps.api.models.user import UserStatus

    admin = _make_user()
    admin.is_superadmin = True
    target = _make_user(user_id=uuid.uuid4(), email="gone@example.com")
    target.token_version = 4
    mock_db.first.return_value = target
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        resp = client.patch(f"/admin/users/{target.id}/deactivate")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, resp.text
    assert target.status == UserStatus.deactivated
    assert target.token_version == 5


# ── 1. /auth/set-password ────────────────────────────────────────────────────

NEW_PASSWORD = {"password": "a-brand-new-password"}


def test_set_password_refuses_to_replace_a_password_with_an_ordinary_token(client, signed_in):
    before = signed_in.password_hash

    resp = client.post("/auth/set-password", json=NEW_PASSWORD, headers=_bearer(signed_in, 3))

    assert resp.status_code == 403, resp.text
    assert signed_in.password_hash == before
    assert signed_in.token_version == 3


def test_set_password_sets_the_first_password_with_any_token(client, signed_in):
    signed_in.password_hash = None

    resp = client.post("/auth/set-password", json=NEW_PASSWORD, headers=_bearer(signed_in, 3))

    assert resp.status_code == 200, resp.text
    assert signed_in.password_hash
    # Nothing to revoke: there was no password for anyone else to have used.
    assert signed_in.token_version == 3


def test_a_code_sign_in_can_reset_a_forgotten_password_and_ends_other_sessions(client, signed_in):
    from apps.api.services.auth_service import AUTH_METHOD_CODE

    before = signed_in.password_hash

    resp = client.post(
        "/auth/set-password", json=NEW_PASSWORD,
        headers=_bearer(signed_in, 3, auth_method=AUTH_METHOD_CODE),
    )

    assert resp.status_code == 200, resp.text
    assert signed_in.password_hash != before
    assert signed_in.token_version == 4


def test_verify_magic_code_marks_its_access_token_as_a_code_sign_in(client, mock_db, signed_in):
    from apps.api.services.auth_service import AUTH_METHOD_CODE, decode_token

    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.redis_verify_magic_code", return_value=(True, "")), \
         patch("apps.api.routers.auth.clear_magic_code_sends"):
        resp = client.post("/auth/verify-magic-code", json={"email": signed_in.email, "code": "123456"})

    assert resp.status_code == 200, resp.text
    assert decode_token(resp.json()["access_token"])["amr"] == AUTH_METHOD_CODE
    # A refresh token never carries it: the proof is only as fresh as the access token.
    assert "amr" not in decode_token(resp.json()["refresh_token"])


def test_password_login_does_not_count_as_a_code_sign_in(client, mock_db, signed_in):
    from apps.api.services.auth_service import decode_token

    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.verify_password", return_value=True), \
         patch("apps.api.routers.auth.clear_magic_code_sends"):
        resp = client.post("/auth/login", json={"email": signed_in.email, "password": "whatever-it-is"})

    assert resp.status_code == 200, resp.text
    assert "amr" not in decode_token(resp.json()["access_token"])
