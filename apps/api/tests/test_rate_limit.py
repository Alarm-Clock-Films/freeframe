"""Tests for rate limiting functionality."""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestCheckRateLimit:
    """Tests for the Redis-based rate limit checker."""

    @patch("apps.api.services.redis_service.get_redis")
    def test_allows_request_under_limit(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [True, 1]
        mock_redis.pipeline.return_value = mock_pipe
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is True
        assert retry_after == 0
        # Counted and judged on that one answer: no separate read of the
        # counter that requests sent together could all pass on.
        mock_redis.get.assert_not_called()
        # The counter gets its window with SET NX EX, then INCR, in one
        # transaction: plain commands, so it works on any Redis (Upstash too).
        mock_pipe.set.assert_called_once_with("rl:test_action:127.0.0.1", 0, nx=True, ex=60)
        mock_pipe.incr.assert_called_once_with("rl:test_action:127.0.0.1")
        mock_pipe.expire.assert_not_called()  # EXPIRE ... NX needs Redis 7
        mock_pipe.execute.assert_called_once()

    @patch("apps.api.services.redis_service.get_redis")
    def test_blocks_request_at_limit(self, mock_get_redis):
        # The sixth request in a window of five: its own count is over the limit.
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 6]
        mock_redis.pipeline.return_value = mock_pipe
        mock_redis.ttl.return_value = 42
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is False
        assert retry_after == 42

    @patch("apps.api.services.redis_service.get_redis")
    def test_allows_request_below_limit(self, mock_get_redis):
        # The fifth request in a window of five is the last one allowed.
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 5]
        mock_redis.pipeline.return_value = mock_pipe
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is True
        assert retry_after == 0

    @patch("apps.api.services.redis_service.get_redis")
    def test_retry_after_minimum_is_one(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 11]
        mock_redis.pipeline.return_value = mock_pipe
        mock_redis.ttl.return_value = -1
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 10, 60)

        assert allowed is False
        assert retry_after == 1


class TestRateLimitDependency:
    """Tests for the FastAPI rate_limit dependency."""

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_allows_when_under_limit(self, mock_check):
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "10.0.0.1"

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)  # Should not raise

        mock_check.assert_called_once_with("10.0.0.1", "test", 5, 60)

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_raises_429_when_over_limit(self, mock_check):
        mock_check.return_value = (False, 30)
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "10.0.0.1"

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        with pytest.raises(Exception) as exc_info:
            dep(mock_request)
        assert exc_info.value.status_code == 429

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_falls_back_to_client_host(self, mock_check):
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        mock_request.headers.get.return_value = None
        mock_request.client.host = "192.168.1.1"

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)

        mock_check.assert_called_once_with("192.168.1.1", "test", 5, 60)


class TestGlobalRateLimitMiddleware:
    """Tests for the global rate limit middleware."""

    def test_exempt_paths_skipped(self):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware, EXEMPT_PATHS

        assert "/health" in EXEMPT_PATHS
        assert "/docs" in EXEMPT_PATHS
        assert "/redoc" in EXEMPT_PATHS
        assert "/openapi.json" in EXEMPT_PATHS

    @patch("apps.api.middleware.global_rate_limit.get_redis")
    def test_get_identity_extracts_user_from_jwt(self, mock_get_redis):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware
        from apps.api.config import settings

        middleware = GlobalRateLimitMiddleware(app=MagicMock())

        import jwt
        token = jwt.encode(
            {"sub": "user-123", "type": "access"},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        mock_request = MagicMock()
        mock_request.headers.get.return_value = f"Bearer {token}"

        identity = middleware._get_identity(mock_request)
        assert identity == "user:user-123"

    def test_get_identity_falls_back_to_ip(self):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        middleware = GlobalRateLimitMiddleware(app=MagicMock())
        mock_request = MagicMock()
        mock_request.headers.get.side_effect = lambda key, default="": {
            "authorization": "",
            "x-real-ip": "203.0.113.1",
        }.get(key, default)

        identity = middleware._get_identity(mock_request)
        assert identity == "ip:203.0.113.1"

    @staticmethod
    def _redis_answering(count, ttl=25):
        """An asyncio Redis whose one pipeline answers SET NX EX, INCR, TTL."""
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[None, count, ttl])
        redis = MagicMock()
        redis.pipeline.return_value = pipe
        return redis, pipe

    def test_check_allows_under_limit(self):
        from apps.api.middleware import global_rate_limit

        redis, pipe = self._redis_answering(600)
        middleware = global_rate_limit.GlobalRateLimitMiddleware(app=MagicMock())
        with patch.object(global_rate_limit, "get_redis", return_value=redis):
            allowed, retry_after = asyncio.run(middleware._check("user:123", "global_r", 600))

        assert allowed is True
        assert retry_after == 0
        # One round trip: window, count and time left queue on one pipeline.
        pipe.set.assert_called_once_with("grl:global_r:user:123", 0, nx=True, ex=60)
        pipe.incr.assert_called_once_with("grl:global_r:user:123")
        pipe.ttl.assert_called_once_with("grl:global_r:user:123")
        pipe.execute.assert_awaited_once()
        redis.get.assert_not_called()

    def test_check_blocks_over_limit(self):
        from apps.api.middleware import global_rate_limit

        redis, _ = self._redis_answering(601, ttl=25)
        middleware = global_rate_limit.GlobalRateLimitMiddleware(app=MagicMock())
        with patch.object(global_rate_limit, "get_redis", return_value=redis):
            allowed, retry_after = asyncio.run(middleware._check("user:123", "global_r", 600))

        assert allowed is False
        assert retry_after == 25

    def test_check_lets_the_request_through_when_redis_fails(self):
        from apps.api.middleware import global_rate_limit

        middleware = global_rate_limit.GlobalRateLimitMiddleware(app=MagicMock())
        with patch.object(global_rate_limit, "get_redis", side_effect=ConnectionError("down")):
            assert asyncio.run(middleware._check("user:123", "global_r", 600)) == (True, 0)

    def test_the_redis_client_has_a_timeout(self):
        """Without one a Redis that accepts connections and never answers held every request."""
        from apps.api.middleware import global_rate_limit

        async def client_kwargs():
            return global_rate_limit.get_redis().connection_pool.connection_kwargs

        kwargs = asyncio.run(client_kwargs())
        assert kwargs["socket_timeout"] == global_rate_limit.REDIS_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == global_rate_limit.REDIS_TIMEOUT_SECONDS

    def test_the_check_does_not_block_the_event_loop(self):
        """Other work on the loop runs while a request waits on Redis. The blocking
        client held the loop for the whole wait, so nothing else on the worker ran."""
        from apps.api.middleware import global_rate_limit

        ticks = []

        async def slow_execute():
            await asyncio.sleep(0.2)
            return [None, 1, 60]

        pipe = MagicMock()
        pipe.execute = slow_execute
        redis = MagicMock()
        redis.pipeline.return_value = pipe
        middleware = global_rate_limit.GlobalRateLimitMiddleware(app=MagicMock())

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks.append(1)

        async def both():
            with patch.object(global_rate_limit, "get_redis", return_value=redis):
                return await asyncio.gather(middleware._check("ip:1.2.3.4", "global_r", 600), ticker())

        result, _ = asyncio.run(both())
        assert result == (True, 0)
        assert len(ticks) == 5

    def test_it_is_plain_asgi_not_a_base_http_middleware(self):
        """BaseHTTPMiddleware wraps the response in a way that can hold back a
        streamed body, and the live event endpoint is a long-lived stream."""
        from starlette.middleware.base import BaseHTTPMiddleware
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        assert not issubclass(GlobalRateLimitMiddleware, BaseHTTPMiddleware)


class TestGlobalRateLimitRequests:
    """Through the app: which requests are counted, and as whom."""

    @pytest.fixture
    def seen(self, monkeypatch):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        calls = []

        async def _check(self, identity, action, limit):
            calls.append((identity, action, limit))
            return (True, 0)

        monkeypatch.setattr(GlobalRateLimitMiddleware, "_check", _check)
        return calls

    def test_reads_and_writes_are_counted_separately_per_address(self, client, seen):
        client.get("/projects", headers={"x-real-ip": "203.0.113.9"})
        client.post("/auth/refresh", json={"refresh_token": "x"}, headers={"x-real-ip": "203.0.113.9"})

        assert seen == [("ip:203.0.113.9", "global_r", 600), ("ip:203.0.113.9", "global_w", 300)]

    def test_a_signed_in_request_is_counted_per_user(self, client, seen):
        from apps.api.services.auth_service import create_access_token

        token = create_access_token("5b0c0000-0000-0000-0000-000000000001")
        client.get("/projects", headers={"Authorization": f"Bearer {token}", "x-real-ip": "203.0.113.9"})

        assert seen == [("user:5b0c0000-0000-0000-0000-000000000001", "global_r", 600)]

    @pytest.mark.parametrize("path", ["/health", "/upload/presign-part", "/stream/hls/master.m3u8"])
    def test_exempt_paths_are_not_counted(self, client, seen, path):
        client.get(path)

        assert seen == []


class TestShortCircuitResponsesCarryCors:
    """A response a middleware returns on its own still reaches the browser readable (#525).

    Without Access-Control-Allow-Origin the browser hides the status, so a 429
    from the limiter (or the setup guard's 503) surfaced as a network error and
    the client could not read Retry-After to back off. `http://localhost:3000`
    is always in `settings.cors_origins`, and the any-origin regex echoes it too.
    """

    ORIGIN = "http://localhost:3000"

    @pytest.fixture
    def limited(self, monkeypatch):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        from .conftest import global_limit_answers
        monkeypatch.setattr(GlobalRateLimitMiddleware, "_check", global_limit_answers(False, 7))

    def test_rate_limit_429_has_cors_headers(self, client, limited):
        resp = client.get("/projects", headers={"Origin": self.ORIGIN})

        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "7"
        assert resp.headers.get("access-control-allow-origin") == self.ORIGIN
        exposed = resp.headers.get("access-control-expose-headers", "").lower()
        assert "retry-after" in exposed

    def test_preflight_is_answered_while_rate_limited(self, client, limited):
        # An authenticated GET is preflighted. If the preflight itself got the
        # bare 429, the browser would never send the request that carries one.
        resp = client.options(
            "/projects",
            headers={
                "Origin": self.ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == self.ORIGIN

    def test_setup_guard_503_has_cors_headers(self, client, monkeypatch):
        monkeypatch.setattr("apps.api.middleware.setup_guard._setup_complete", False)
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        monkeypatch.setattr("apps.api.database.SessionLocal", lambda: session)

        resp = client.get("/projects", headers={"Origin": self.ORIGIN})

        assert resp.status_code == 503
        assert resp.json()["needs_setup"] is True
        assert resp.headers.get("access-control-allow-origin") == self.ORIGIN

    def test_normal_response_still_has_cors_headers(self, client):
        resp = client.get("/health", headers={"Origin": self.ORIGIN})

        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == self.ORIGIN
