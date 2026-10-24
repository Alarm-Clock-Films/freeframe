"""Per-address limit on sign-in code emails, and the portable rate-limit counter.

The per-IP limit on /auth/send-magic-code does not stop someone who knows an
address and keeps changing networks. reserve_magic_code_send makes the address
wait after each code, doubling the wait each time up to a maximum, so a flood
is bounded without a daily cap that would let anyone lock a person out of code
sign-in for a day.

Most tests drive the real limiter on an in-memory Redis with a fake clock; the
last ones use a real Redis server when one is reachable.
"""
import os
import uuid
from unittest.mock import patch

import pytest

from apps.api.config import settings
from apps.api.services import redis_service
from apps.api.tests.conftest import _make_user

ADDRESS = "peter@example.com"
DAY = 24 * 60 * 60


class FakeRedis:
    """The Redis calls the limiters make, with expiry on a fake clock.

    `redis7=False` rejects `EXPIRE ... NX` the way a server without that
    Redis 7 option does.
    """

    def __init__(self, redis7=True):
        self.now = 0.0
        self.data = {}
        self.expires = {}
        self.redis7 = redis7

    def _alive(self, key):
        if key in self.expires and self.expires[key] <= self.now:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def get(self, key):
        return self.data.get(key) if self._alive(key) else None

    def ttl(self, key):
        if not self._alive(key):
            return -2
        return int(self.expires[key] - self.now) if key in self.expires else -1

    def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return None
        self.data[key] = value
        self.expires.pop(key, None)
        if ex:
            self.expires[key] = self.now + ex
        return True

    def incr(self, key):
        self._alive(key)
        self.data[key] = int(self.data.get(key, 0)) + 1  # keeps any TTL, as Redis does
        return self.data[key]

    def expire(self, key, seconds, nx=False):
        if nx and not self.redis7:
            raise RuntimeError("ERR wrong number of arguments for 'expire' command")
        if not self._alive(key):
            return False
        if nx and key in self.expires:
            return False
        self.expires[key] = self.now + seconds
        return True

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)
            self.expires.pop(key, None)

    def pipeline(self, transaction=True):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append(lambda: getattr(self.r, name)(*args, **kwargs))
        return queue

    def execute(self):
        # MULTI/EXEC: a command Redis rejects fails the whole transaction and
        # applies none of it, as real Redis does for an unknown option.
        data, expires = dict(self.r.data), dict(self.r.expires)
        try:
            return [op() for op in self.ops]
        except Exception:
            self.r.data, self.r.expires = data, expires
            raise


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(redis_service, "get_redis", lambda: r)
    monkeypatch.setattr(settings, "magic_code_min_interval_seconds", 60)
    monkeypatch.setattr(settings, "magic_code_max_interval_seconds", 3600)
    return r


def _send_times(r, email, seconds, poll):
    """The times at which a request every `poll` seconds may send, over `seconds`."""
    times = []
    start = r.now
    while r.now - start < seconds:
        if redis_service.reserve_magic_code_send(email):
            times.append(r.now - start)
        r.now += poll
    return times


# ── the wait ──────────────────────────────────────────────────────────────────

def test_the_wait_doubles_from_a_minute_to_an_hour(fake_redis):
    times = _send_times(fake_redis, ADDRESS, 6 * 3600, poll=10)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[:7] == [60, 120, 240, 480, 960, 1920, 3600]
    assert set(gaps[6:]) == {3600}  # never longer than the maximum


def test_a_day_long_flood_sends_about_thirty(fake_redis):
    """Asking every few seconds all day, from anywhere."""
    sent = len(_send_times(fake_redis, ADDRESS, DAY, poll=5))
    assert 25 <= sent <= 30


def test_nobody_waits_longer_than_the_maximum(fake_redis):
    """No daily cap: after a flood, the owner gets a code within an hour."""
    _send_times(fake_redis, ADDRESS, 12 * 3600, poll=30)
    fake_redis.now += 3600
    assert redis_service.reserve_magic_code_send(ADDRESS)


def test_the_wait_shrinks_back_a_day_after_the_last_code(fake_redis):
    _send_times(fake_redis, ADDRESS, 6 * 3600, poll=30)
    fake_redis.now += DAY + 1
    times = _send_times(fake_redis, ADDRESS, 200, poll=10)
    assert times[1] - times[0] == 60


def test_signing_in_starts_the_address_over(fake_redis):
    _send_times(fake_redis, ADDRESS, 6 * 3600, poll=30)
    redis_service.clear_magic_code_sends(ADDRESS)
    times = _send_times(fake_redis, ADDRESS, 200, poll=10)
    assert times[:2] == [0, 60]


def test_the_address_is_matched_whatever_its_case(fake_redis):
    assert redis_service.reserve_magic_code_send("Peter@Example.com")
    assert not redis_service.reserve_magic_code_send("  peter@example.COM ")


def test_addresses_do_not_share_a_limit(fake_redis):
    assert redis_service.reserve_magic_code_send(ADDRESS)
    assert redis_service.reserve_magic_code_send("someone@example.com")


def test_a_minimum_of_zero_turns_the_limit_off(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "magic_code_min_interval_seconds", 0)
    assert len(_send_times(fake_redis, ADDRESS, 50, poll=1)) == 50
    assert fake_redis.data == {}


def test_a_maximum_at_the_minimum_keeps_the_wait_fixed(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "magic_code_max_interval_seconds", 0)
    times = _send_times(fake_redis, ADDRESS, 600, poll=10)
    assert {b - a for a, b in zip(times, times[1:])} == {60}


def test_fails_open_without_redis(monkeypatch):
    def broken():
        raise ConnectionError("redis down")
    monkeypatch.setattr(redis_service, "get_redis", broken)
    assert redis_service.reserve_magic_code_send(ADDRESS)
    redis_service.clear_magic_code_sends(ADDRESS)  # and does not raise


# ── the endpoints ─────────────────────────────────────────────────────────────

@pytest.fixture
def endpoint(client, mock_db, fake_redis, monkeypatch):
    """POST /auth/send-magic-code for a registered address, from any network."""
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    mock_db.first.return_value = _make_user(email=ADDRESS)
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.store_magic_code") as store, \
         patch("apps.api.routers.auth.send_task_safe") as send:
        def post():
            return client.post("/auth/send-magic-code", json={"email": ADDRESS})
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


def _signed_in_user():
    user = _make_user(email=ADDRESS)
    user.token_version = 1  # tokens are minted with it
    return user


def test_a_code_sign_in_resets_the_wait(client, mock_db, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    _send_times(fake_redis, ADDRESS, 3 * 3600, poll=30)
    assert fake_redis.data
    mock_db.first.return_value = _signed_in_user()
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.redis_verify_magic_code", return_value=(True, "")):
        resp = client.post("/auth/verify-magic-code", json={"email": ADDRESS, "code": "123456"})
    assert resp.status_code == 200
    assert not any(k.startswith(redis_service.MAGIC_CODE_SEND_PREFIX) for k in fake_redis.data)


def test_a_failed_code_does_not_reset_it(client, mock_db, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    redis_service.reserve_magic_code_send(ADDRESS)
    mock_db.first.return_value = _signed_in_user()
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)), \
         patch("apps.api.routers.auth.redis_verify_magic_code", return_value=(False, "nope")):
        resp = client.post("/auth/verify-magic-code", json={"email": ADDRESS, "code": "000000"})
    assert resp.status_code == 401
    assert not redis_service.reserve_magic_code_send(ADDRESS)


# ── rate-limit counters without Redis 7 ───────────────────────────────────────

def test_count_in_window_gives_the_counter_a_ttl_once(fake_redis):
    assert redis_service.count_in_window(fake_redis, "k", 600) == 1
    fake_redis.now += 100
    assert redis_service.count_in_window(fake_redis, "k", 600) == 2
    assert fake_redis.ttl("k") == 500  # not pushed back by the second count
    fake_redis.now += 500
    assert redis_service.count_in_window(fake_redis, "k", 600) == 1


def test_ip_rate_limit_works_without_expire_nx(monkeypatch):
    """Before: EXPIRE ... NX raised, the limiter failed open and allowed everything."""
    r = FakeRedis(redis7=False)
    monkeypatch.setattr(redis_service, "get_redis", lambda: r)
    results = [redis_service.check_rate_limit("1.2.3.4", "login", 3, 600)[0] for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert r.ttl(f"{redis_service.RATE_LIMIT_PREFIX}login:1.2.3.4") > 0


def test_global_rate_limit_works_without_expire_nx(monkeypatch):
    from apps.api.middleware import global_rate_limit

    r = FakeRedis(redis7=False)
    monkeypatch.setattr(global_rate_limit, "get_redis", lambda: r)
    mw = global_rate_limit.GlobalRateLimitMiddleware(app=None)
    results = [mw._check("ip:1.2.3.4", "read", 2)[0] for _ in range(4)]
    assert results == [True, True, False, False]


# ── against a real Redis, when one is reachable ───────────────────────────────

@pytest.fixture
def real_redis(monkeypatch):
    import redis

    client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    try:
        client.ping()
    except Exception:
        pytest.skip("no Redis server reachable")
    monkeypatch.setattr(redis_service, "get_redis", lambda: client)
    monkeypatch.setattr(settings, "magic_code_min_interval_seconds", 60)
    monkeypatch.setattr(settings, "magic_code_max_interval_seconds", 3600)
    address = f"real-{uuid.uuid4().hex}@example.com"
    yield client, address
    client.delete(*redis_service._send_keys(address))


def test_on_real_redis_the_wait_grows_and_every_key_expires(real_redis):
    client, address = real_redis
    wait_key, count_key = redis_service._send_keys(address)
    assert redis_service.reserve_magic_code_send(address)
    assert not redis_service.reserve_magic_code_send(address)
    assert 0 < client.ttl(wait_key) <= 60
    assert 0 < client.ttl(count_key) <= redis_service.MAGIC_CODE_SEND_RESET_SECONDS

    client.delete(wait_key)  # as if the first wait had passed
    assert redis_service.reserve_magic_code_send(address)
    assert 60 < client.ttl(wait_key) <= 120  # second wait is two minutes

    redis_service.clear_magic_code_sends(address)
    assert client.exists(wait_key, count_key) == 0


def test_on_real_redis_count_in_window_sets_one_ttl(real_redis):
    client, address = real_redis
    key = f"test_count:{address}"
    try:
        assert redis_service.count_in_window(client, key, 600) == 1
        assert redis_service.count_in_window(client, key, 600) == 2
        assert 0 < client.ttl(key) <= 600
    finally:
        client.delete(key)
