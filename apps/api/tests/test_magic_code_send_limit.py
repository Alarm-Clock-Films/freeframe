"""Per-address limit on sign-in code emails (redis_service.reserve_magic_code_send).

The per-IP limit on /auth/send-magic-code does not stop someone who knows an
address and keeps changing networks: before this limit, each new network got
five more emails sent to that address. These tests drive the real limiter on
an in-memory Redis with a controllable clock.
"""
from unittest.mock import patch

import pytest

from apps.api.config import settings
from apps.api.services import redis_service
from apps.api.tests.conftest import _make_user


class FakeRedis:
    """The few Redis calls the limiter makes, with expiry on a fake clock."""

    def __init__(self):
        self.now = 0.0
        self.data = {}
        self.expires = {}

    def _alive(self, key):
        if key in self.expires and self.expires[key] <= self.now:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return None
        self.data[key] = value
        if ex:
            self.expires[key] = self.now + ex
        return True

    def incr(self, key):
        self._alive(key)
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def expire(self, key, seconds, nx=False):
        if not self._alive(key):
            return False
        if nx and key in self.expires:
            return False
        self.expires[key] = self.now + seconds
        return True

    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, r):
        self.r, self.ops = r, []

    def incr(self, key):
        self.ops.append(lambda: self.r.incr(key))

    def expire(self, key, seconds, nx=False):
        self.ops.append(lambda: self.r.expire(key, seconds, nx=nx))

    def execute(self):
        return [op() for op in self.ops]


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(redis_service, "get_redis", lambda: r)
    monkeypatch.setattr(settings, "magic_code_min_interval_seconds", 60)
    monkeypatch.setattr(settings, "magic_code_max_per_hour", 5)
    monkeypatch.setattr(settings, "magic_code_max_per_day", 10)
    return r


def _sends(r, email, count, step):
    """How many of `count` requests, `step` seconds apart, may send."""
    allowed = 0
    for _ in range(count):
        allowed += redis_service.reserve_magic_code_send(email)
        r.now += step
    return allowed


# ── the limiter ───────────────────────────────────────────────────────────────

def test_one_email_per_minute(fake_redis):
    assert redis_service.reserve_magic_code_send("peter@example.com")
    fake_redis.now += 30
    assert not redis_service.reserve_magic_code_send("peter@example.com")
    fake_redis.now += 31
    assert redis_service.reserve_magic_code_send("peter@example.com")


def test_five_an_hour(fake_redis):
    assert _sends(fake_redis, "peter@example.com", 20, step=61) == 5


def test_ten_a_day(fake_redis):
    # Spread over 23 hours, the hourly cap resets but the daily one holds.
    assert _sends(fake_redis, "peter@example.com", 92, step=15 * 60) == 10


def test_limits_reset_when_the_day_is_over(fake_redis):
    _sends(fake_redis, "peter@example.com", 100, step=15 * 60)
    fake_redis.now += 86400
    assert redis_service.reserve_magic_code_send("peter@example.com")


def test_the_address_is_matched_whatever_its_case(fake_redis):
    assert redis_service.reserve_magic_code_send("Peter@Example.com")
    assert not redis_service.reserve_magic_code_send("  peter@example.COM ")


def test_addresses_do_not_share_a_limit(fake_redis):
    assert redis_service.reserve_magic_code_send("peter@example.com")
    assert redis_service.reserve_magic_code_send("someone@example.com")


def test_zero_turns_every_limit_off(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "magic_code_min_interval_seconds", 0)
    monkeypatch.setattr(settings, "magic_code_max_per_hour", 0)
    monkeypatch.setattr(settings, "magic_code_max_per_day", 0)
    assert _sends(fake_redis, "peter@example.com", 50, step=0) == 50


def test_fails_open_without_redis(monkeypatch):
    def broken():
        raise ConnectionError("redis down")
    monkeypatch.setattr(redis_service, "get_redis", broken)
    assert redis_service.reserve_magic_code_send("peter@example.com")


# ── the endpoint ──────────────────────────────────────────────────────────────

@pytest.fixture
def endpoint(client, mock_db, fake_redis, monkeypatch):
    """POST /auth/send-magic-code for a registered address, from any network."""
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    mock_db.first.return_value = _make_user(email="peter@example.com")
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.store_magic_code") as store, \
         patch("apps.api.routers.auth.send_task_safe") as send:
        def post():
            return client.post("/auth/send-magic-code", json={"email": "peter@example.com"})
        yield post, store, send


def test_a_flood_from_many_networks_sends_one_email(endpoint):
    """The regression: per-IP limits let each new network send five more."""
    post, store, send = endpoint
    for _ in range(11):
        post()
    assert send.call_count == 1
    assert store.call_count == 1


def test_a_refused_request_looks_like_any_other(endpoint):
    """Same answer, so the limit does not reveal which addresses have accounts."""
    post, _, _ = endpoint
    first, second = post(), post()
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_a_refused_request_keeps_the_code_already_sent(endpoint):
    """Asking again too soon must not replace the code in the user's inbox."""
    post, store, _ = endpoint
    post()
    post()
    assert store.call_count == 1


def test_unregistered_addresses_use_up_nothing(client, mock_db, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    mock_db.first.return_value = None
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)):
        client.post("/auth/send-magic-code", json={"email": "nobody@example.com"})
    assert fake_redis.data == {}
