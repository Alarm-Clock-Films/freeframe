"""Tests for HLS streaming proxy."""
import pytest
from unittest.mock import MagicMock, patch
import jwt


class TestCreateHlsToken:
    """Tests for HLS token generation."""

    def test_creates_valid_jwt(self):
        from apps.api.routers.hls_proxy import create_hls_token
        from apps.api.config import settings

        token = create_hls_token("hls/project-1/version-1")
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])

        assert payload["sub"] == "hls"
        assert payload["pfx"] == "hls/project-1/version-1"
        assert "exp" in payload


class TestVerifyHlsToken:
    """Tests for HLS token verification."""

    def test_valid_token(self):
        from apps.api.routers.hls_proxy import create_hls_token, _verify_hls_token

        token = create_hls_token("hls/proj/ver")
        prefix = _verify_hls_token(token)
        assert prefix == "hls/proj/ver"

    def test_invalid_token_raises(self):
        from apps.api.routers.hls_proxy import _verify_hls_token

        with pytest.raises(Exception) as exc_info:
            _verify_hls_token("garbage-token")
        assert exc_info.value.status_code == 401

    def test_wrong_sub_raises(self):
        from apps.api.routers.hls_proxy import _verify_hls_token
        from apps.api.config import settings

        token = jwt.encode(
            {"sub": "not-hls", "pfx": "some/path"},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(Exception) as exc_info:
            _verify_hls_token(token)
        assert exc_info.value.status_code == 403


class TestRewriteManifest:
    """Tests for m3u8 manifest URL rewriting."""

    @patch("apps.api.routers.hls_proxy.generate_presigned_get_url")
    def test_rewrites_ts_to_presigned(self, mock_presign):
        mock_presign.return_value = "https://s3.example.com/presigned-segment.ts"

        from apps.api.routers.hls_proxy import SEGMENT_URL_MAX_SECONDS, _rewrite_manifest

        content = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.000,\nsegment0.ts\n#EXT-X-ENDLIST"
        result = _rewrite_manifest(content, "hls/proj/ver", "720p/index.m3u8", "tok123")

        assert "https://s3.example.com/presigned-segment.ts" in result
        mock_presign.assert_called_once_with("hls/proj/ver/720p/segment0.ts", expires_in=SEGMENT_URL_MAX_SECONDS)

    @patch("apps.api.routers.hls_proxy.generate_presigned_get_url")
    def test_rewrites_m3u8_to_proxy_url(self, mock_presign):
        from apps.api.routers.hls_proxy import _rewrite_manifest

        content = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n720p/index.m3u8"
        result = _rewrite_manifest(content, "hls/proj/ver", "master.m3u8", "tok123")

        assert "720p/index.m3u8?token=tok123" in result
        mock_presign.assert_not_called()

    @patch("apps.api.routers.hls_proxy.generate_presigned_get_url")
    def test_preserves_comments_and_tags(self, mock_presign):
        from apps.api.routers.hls_proxy import _rewrite_manifest

        content = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-ENDLIST"
        result = _rewrite_manifest(content, "hls/proj/ver", "index.m3u8", "tok123")

        assert result == content


class TestHlsProxyEndpoint:
    """Tests for directory traversal prevention."""

    def test_rejects_non_m3u8(self):
        from apps.api.routers.hls_proxy import hls_proxy, create_hls_token

        token = create_hls_token("hls/proj/ver")
        with pytest.raises(Exception) as exc_info:
            hls_proxy("segment0.ts", token=token)
        assert exc_info.value.status_code == 400

    def test_rejects_directory_traversal(self):
        from apps.api.routers.hls_proxy import hls_proxy, create_hls_token

        token = create_hls_token("hls/proj/ver")
        with pytest.raises(Exception) as exc_info:
            hls_proxy("../../etc/passwd.m3u8", token=token)
        assert exc_info.value.status_code == 400

    def test_rejects_absolute_path(self):
        from apps.api.routers.hls_proxy import hls_proxy, create_hls_token

        token = create_hls_token("hls/proj/ver")
        with pytest.raises(Exception) as exc_info:
            hls_proxy("/etc/passwd.m3u8", token=token)
        assert exc_info.value.status_code == 400


class TestSegmentUrlLifetime:
    """A segment URL never outlives the token that the playlist was fetched with.

    The proxy checks only the token's signature and expiry, never the asset, so
    the token's lifetime is the bound on how long a player can keep fetching
    video after access to it ended. Segment URLs signed for a fixed 24 hours at
    each playlist fetch stretched that to about 48 hours from the token's issue.
    """

    def _token(self, seconds_left):
        from apps.api.config import settings
        from datetime import datetime, timedelta, timezone
        return jwt.encode(
            {"sub": "hls", "pfx": "hls/proj/ver",
             "exp": datetime.now(timezone.utc) + timedelta(seconds=seconds_left)},
            settings.jwt_secret, algorithm=settings.jwt_algorithm,
        )

    def _signed_expiries(self, token):
        """Fetch a variant playlist through the proxy with real URL signing, and
        return each segment URL's X-Amz-Expires."""
        from urllib.parse import parse_qs, urlparse
        from apps.api.routers.hls_proxy import hls_proxy

        body = b"#EXTM3U\n#EXTINF:2.000,\nseg0.ts\n#EXTINF:2.000,\nseg1.ts\n#EXT-X-ENDLIST"
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
        with patch("apps.api.routers.hls_proxy.get_s3_client", return_value=s3):
            response = hls_proxy("720p/index.m3u8", token=token)
        urls = [l for l in response.body.decode().split("\n") if l.startswith("http")]
        assert len(urls) == 2
        return [int(parse_qs(urlparse(u).query)["X-Amz-Expires"][0]) for u in urls]

    def test_a_token_near_its_end_signs_segments_only_for_what_is_left(self):
        for expires in self._signed_expiries(self._token(60)):
            assert 1 <= expires <= 60

    def test_a_fresh_token_signs_segments_for_its_full_lifetime(self):
        from apps.api.routers.hls_proxy import create_hls_token
        for expires in self._signed_expiries(create_hls_token("hls/proj/ver")):
            # 24 hours, rounded up to the hour the token ends on.
            assert 86400 - 60 <= expires <= 86400 + 3600

    def test_an_expired_token_signs_nothing(self):
        from apps.api.routers.hls_proxy import hls_proxy
        with pytest.raises(Exception) as exc_info:
            hls_proxy("720p/index.m3u8", token=self._token(-5))
        assert exc_info.value.status_code == 401


class FakeRedis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex


class TestSharedRewrittenPlaylists:
    """Tokens issued in the same hour end together and share one rewritten playlist.

    Rewriting signs a link per segment on every playlist request. Tokens used to
    end 24 hours after the second they were issued, so no two requests could
    share the work, and every page load got differently signed segment links
    that the browser's cache had never seen.
    """

    VARIANT = b"#EXTM3U\n#EXTINF:2.000,\nseg0.ts\n#EXTINF:2.000,\nseg1.ts\n#EXT-X-ENDLIST"
    MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n720p/index.m3u8\n"

    @pytest.fixture
    def redis(self):
        r = FakeRedis()
        with patch("apps.api.services.redis_service.get_redis", return_value=r):
            yield r

    def _fetch(self, path, token, body):
        from apps.api.routers.hls_proxy import hls_proxy

        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
        with patch("apps.api.routers.hls_proxy.get_s3_client", return_value=s3):
            response = hls_proxy(path, token=token)
        return response.body.decode(), s3.get_object.call_count

    def _token_at(self, when, prefix="hls/proj/ver"):
        from apps.api.routers import hls_proxy

        with patch.object(hls_proxy, "datetime") as clock:
            clock.now.return_value = when
            return hls_proxy.create_hls_token(prefix)

    def test_tokens_end_on_the_hour_at_least_a_day_out(self):
        from datetime import datetime, timezone
        from apps.api.config import settings

        issued = datetime(2026, 11, 12, 10, 17, 3, tzinfo=timezone.utc)
        exp = jwt.decode(
            self._token_at(issued), settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )["exp"]

        assert exp == datetime(2026, 11, 13, 11, 0, 0, tzinfo=timezone.utc).timestamp()
        # On the hour exactly, the token still gets more than a day.
        on_the_hour = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)
        exp = jwt.decode(
            self._token_at(on_the_hour), settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )["exp"]
        assert exp == datetime(2026, 11, 13, 11, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_a_second_viewer_in_the_same_hour_gets_the_same_segment_links_without_signing(self, redis):
        from apps.api.routers import hls_proxy

        first_token = hls_proxy.create_hls_token("hls/proj/ver")
        first, fetched = self._fetch("720p/index.m3u8", first_token, self.VARIANT)
        assert fetched == 1

        # A different token for the same video, as another page load gets.
        second_token = jwt.encode(
            {**jwt.decode(first_token, options={"verify_signature": False}), "n": 2},
            hls_proxy.settings.jwt_secret, algorithm=hls_proxy.settings.jwt_algorithm,
        )
        assert second_token != first_token
        with patch.object(hls_proxy, "generate_presigned_get_url", side_effect=AssertionError("signed again")):
            second, fetched = self._fetch("720p/index.m3u8", second_token, self.VARIANT)

        assert fetched == 0  # not read from storage again either
        assert second == first
        assert first.count("X-Amz-Signature") == 2

    def test_links_to_other_playlists_carry_the_callers_own_token(self, redis):
        from apps.api.routers import hls_proxy

        first_token = hls_proxy.create_hls_token("hls/proj/ver")
        second_token = jwt.encode(
            {**jwt.decode(first_token, options={"verify_signature": False}), "n": 2},
            hls_proxy.settings.jwt_secret, algorithm=hls_proxy.settings.jwt_algorithm,
        )

        first, _ = self._fetch("master.m3u8", first_token, self.MASTER)
        second, fetched = self._fetch("master.m3u8", second_token, self.MASTER)

        assert fetched == 0
        assert f"720p/index.m3u8?token={first_token}" in first
        assert f"720p/index.m3u8?token={second_token}" in second
        assert first_token not in second
        # What is kept holds no token at all.
        import base64, zlib
        (kept,) = redis.data.values()
        assert first_token not in zlib.decompress(base64.b64decode(kept)).decode()

    def test_another_video_or_another_expiry_shares_nothing(self, redis):
        from datetime import datetime, timedelta, timezone
        from apps.api.routers import hls_proxy

        now = datetime.now(timezone.utc)
        token = hls_proxy.create_hls_token("hls/proj/ver")
        other_video = hls_proxy.create_hls_token("hls/proj/other")
        later = self._token_at(now + timedelta(hours=3))

        self._fetch("720p/index.m3u8", token, self.VARIANT)
        _, fetched_other = self._fetch("720p/index.m3u8", other_video, self.VARIANT)
        _, fetched_later = self._fetch("720p/index.m3u8", later, self.VARIANT)

        assert (fetched_other, fetched_later) == (1, 1)
        assert len(redis.data) == 3

    def test_a_kept_playlist_is_dropped_within_the_hour_and_never_outlives_the_token(self, redis):
        from datetime import datetime, timedelta, timezone
        from apps.api.routers import hls_proxy

        self._fetch("720p/index.m3u8", hls_proxy.create_hls_token("hls/proj/ver"), self.VARIANT)
        short = jwt.encode(
            {"sub": "hls", "pfx": "hls/proj/ver", "exp": datetime.now(timezone.utc) + timedelta(seconds=40)},
            hls_proxy.settings.jwt_secret, algorithm=hls_proxy.settings.jwt_algorithm,
        )
        self._fetch("720p/index.m3u8", short, self.VARIANT)

        ttls = sorted(redis.ttl.values())
        assert ttls[1] == hls_proxy.HLS_MANIFEST_CACHE_SECONDS
        assert 1 <= ttls[0] <= 40

    def test_playlists_are_still_served_when_redis_is_down(self):
        from apps.api.routers import hls_proxy

        with patch("apps.api.services.redis_service.get_redis", side_effect=ConnectionError("down")):
            body, fetched = self._fetch("720p/index.m3u8", hls_proxy.create_hls_token("hls/proj/ver"), self.VARIANT)

        assert fetched == 1
        assert body.count("X-Amz-Signature") == 2

