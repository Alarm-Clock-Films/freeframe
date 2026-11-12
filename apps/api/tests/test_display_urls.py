"""Links to images a page shows are signed once and handed out again while fresh.

A new signature is a new URL, so signing a thumbnail for every response made
the browser fetch it again on every reload and background refresh, cached or not.
"""
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from apps.api.services import s3_service
from apps.api.services.s3_service import (
    DISPLAY_URL_LIFETIME_SECONDS, DISPLAY_URL_REUSE_SECONDS, display_url, display_urls,
)


class FakeRedis:
    def __init__(self):
        self.data, self.ttl, self.mget_calls, self.executes = {}, {}, 0, 0

    def mget(self, keys):
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]

    def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    def pipeline(self, transaction=True):
        return self

    def execute(self):
        self.executes += 1


@pytest.fixture
def redis():
    r = FakeRedis()
    with patch("apps.api.services.redis_service.get_redis", return_value=r):
        yield r


def _another_process():
    """What a second API worker starts with: nothing held locally."""
    s3_service._display_url_local.clear()


def test_the_same_link_is_handed_out_again(redis):
    first = display_url("processed/p/a/v/thumbnail.jpg")
    with patch.object(s3_service, "generate_presigned_get_url", side_effect=AssertionError("signed again")):
        assert display_url("processed/p/a/v/thumbnail.jpg") == first
    # Held by the process: the second call did not go to Redis at all.
    assert redis.mget_calls == 1


def test_another_worker_hands_out_the_same_link(redis):
    first = display_url("processed/p/a/v/thumbnail.jpg")
    _another_process()

    with patch.object(s3_service, "generate_presigned_get_url", side_effect=AssertionError("signed again")):
        assert display_url("processed/p/a/v/thumbnail.jpg") == first
    assert redis.mget_calls == 2


def test_a_link_outlives_its_reuse_by_at_least_an_hour(redis):
    url = display_url("processed/p/a/v/thumbnail.jpg")

    assert parse_qs(urlparse(url).query)["X-Amz-Expires"] == [str(DISPLAY_URL_LIFETIME_SECONDS)]
    assert DISPLAY_URL_LIFETIME_SECONDS - DISPLAY_URL_REUSE_SECONDS >= 3600
    assert set(redis.ttl.values()) == {DISPLAY_URL_REUSE_SECONDS}


def test_a_link_past_its_reuse_is_signed_again(redis):
    key = "processed/p/a/v/thumbnail.jpg"
    display_url(key)
    (redis_key,) = redis.data
    redis.data[redis_key] = f"{time.time() - 1}|https://stale.example/old"
    _another_process()

    with patch.object(s3_service, "generate_presigned_get_url", return_value="https://s3.example/new") as sign:
        assert display_url(key) == "https://s3.example/new"
    sign.assert_called_once_with(key, expires_in=DISPLAY_URL_LIFETIME_SECONDS)


def test_a_grid_of_thumbnails_costs_one_read_and_one_write(redis):
    keys = [f"processed/p/a{n}/v/thumbnail.jpg" for n in range(200)]

    urls = display_urls(keys + keys[:5] + [None, ""])

    assert set(urls) == set(keys)
    assert len(set(urls.values())) == 200
    assert (redis.mget_calls, redis.executes) == (1, 1)

    assert display_urls(keys) == urls
    assert (redis.mget_calls, redis.executes) == (1, 1)


def test_links_are_still_signed_when_redis_is_down():
    with patch("apps.api.services.redis_service.get_redis", side_effect=ConnectionError("down")):
        url = display_url("processed/p/a/v/thumbnail.jpg")

    assert "thumbnail.jpg" in url and "X-Amz-Signature" in url


def test_a_link_signed_for_other_storage_settings_is_not_reused(redis, monkeypatch):
    key = "processed/p/a/v/thumbnail.jpg"
    first = display_url(key)
    _another_process()
    monkeypatch.setattr(s3_service.settings, "s3_bucket", "another-bucket")
    s3_service._display_url_namespace.cache_clear()
    try:
        second = display_url(key)
    finally:
        monkeypatch.undo()
        s3_service._display_url_namespace.cache_clear()

    assert second != first and "another-bucket" in second


def test_the_process_does_not_hold_links_without_bound(redis, monkeypatch):
    monkeypatch.setattr(s3_service, "_DISPLAY_URL_LOCAL_MAX", 10)

    display_urls(f"thumbs/{n}.jpg" for n in range(35))

    assert len(s3_service._display_url_local) <= 10


def test_stream_and_download_links_are_not_reused(redis):
    with patch.object(s3_service, "_get_presign_client") as client:
        client.return_value.generate_presigned_url.side_effect = ["https://s3/one", "https://s3/two"]
        assert s3_service.generate_presigned_get_url("raw/p/a/v/original.mov") == "https://s3/one"
        assert s3_service.generate_presigned_get_url("raw/p/a/v/original.mov") == "https://s3/two"
    assert redis.mget_calls == 0


def test_an_avatar_is_signed_once_for_a_page_of_comments(redis):
    import uuid
    from apps.api.services.avatar_keys import sign_avatar

    user_id = uuid.uuid4()
    key = f"avatars/{user_id}/face.png"

    links = {sign_avatar(user_id, key) for _ in range(50)}

    assert len(links) == 1
    assert redis.mget_calls == 1
    # Still only the user's own avatar.
    assert sign_avatar(uuid.uuid4(), key) is None
