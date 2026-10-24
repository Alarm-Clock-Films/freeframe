"""BotID verification cookie (services/botid.py) and API security headers."""
import time
from unittest.mock import patch

import jwt
import pytest

from apps.api.config import Settings, settings
from apps.api.services import botid

SECRET = "ab" * 32
OTHER_SECRET = "cd" * 32


def _token(secret=SECRET, **overrides):
    now = int(time.time())
    claims = {
        "purpose": botid.PURPOSE,
        "iss": botid.ISSUER,
        "aud": botid.AUDIENCE,
        "iat": now,
        "exp": now + botid.MAX_AGE_SECONDS,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, bytes.fromhex(secret), algorithm="HS256")


@pytest.fixture
def botid_on(monkeypatch):
    monkeypatch.setattr(settings, "botid_cookie_secret", SECRET)


def _send_code(client, cookie=None):
    headers = {"Cookie": f"{botid.COOKIE_NAME}={cookie}"} if cookie else {}
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)):
        return client.post("/auth/send-magic-code", json={"email": "someone@example.com"}, headers=headers)


# ── the cookie ────────────────────────────────────────────────────────────────

def test_a_fresh_cookie_from_this_deployment_is_accepted(botid_on):
    assert botid.is_verified(_token())


@pytest.mark.parametrize("token", [
    None,
    "",
    "not-a-jwt",
    _token(secret=OTHER_SECRET),                       # signed with another key
    _token(purpose="something-else"),                  # signed by us for another use
    _token(aud="someone-else"),
    _token(iss="someone-else"),
    _token(exp=int(time.time()) - 120),                # expired
    _token(iat=int(time.time()) - 3600,                # minted long ago with a long exp
           exp=int(time.time()) + 3600),
    _token(iat=None),                                  # no iat
    jwt.encode({"purpose": botid.PURPOSE, "iss": botid.ISSUER, "aud": botid.AUDIENCE,
                "iat": int(time.time()), "exp": int(time.time()) + 60},
               key=None, algorithm="none"),            # unsigned
])
def test_anything_else_is_refused(botid_on, token):
    assert not botid.is_verified(token)


# ── the endpoints ─────────────────────────────────────────────────────────────

def test_sign_in_requires_the_cookie_when_botid_is_on(client, mock_db, botid_on):
    mock_db.first.return_value = None
    resp = _send_code(client)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "botid_required"


def test_sign_in_goes_through_with_a_valid_cookie(client, mock_db, botid_on):
    mock_db.first.return_value = None
    resp = _send_code(client, cookie=_token())
    assert resp.status_code == 200


def test_every_protected_endpoint_checks_the_cookie(client, mock_db, botid_on):
    mock_db.first.return_value = None
    with patch("apps.api.middleware.rate_limit.check_rate_limit", return_value=(True, 0)):
        for path, body in [
            ("/auth/send-magic-code", {"email": "a@example.com"}),
            ("/auth/verify-magic-code", {"email": "a@example.com", "code": "123456"}),
            ("/auth/login", {"email": "a@example.com", "password": "x"}),
            ("/share/some-token/verify", {"password": "x"}),
        ]:
            resp = client.post(path, json=body)
            assert resp.status_code == 403, path
            assert resp.json()["detail"]["code"] == "botid_required", path


def test_botid_off_leaves_sign_in_alone(client, mock_db, monkeypatch):
    """Docker and local development set no BOTID_COOKIE_SECRET."""
    monkeypatch.setattr(settings, "botid_cookie_secret", None)
    mock_db.first.return_value = None
    assert _send_code(client).status_code == 200


# ── the setting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_secret_turns_botid_off(value):
    s = Settings(jwt_secret="x", database_url="postgresql://u@h/d", redis_url="redis://h", botid_cookie_secret=value)
    assert s.botid_cookie_secret is None


@pytest.mark.parametrize("value", ["short", "zz" * 32, "ab" * 31])
def test_a_malformed_secret_stops_startup(value):
    with pytest.raises(ValueError, match="BOTID_COOKIE_SECRET"):
        Settings(jwt_secret="x", database_url="postgresql://u@h/d", redis_url="redis://h", botid_cookie_secret=value)


# ── security headers ──────────────────────────────────────────────────────────

def test_api_responses_carry_security_headers(client):
    resp = client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_error_responses_carry_them_too(client, mock_db, botid_on):
    mock_db.first.return_value = None
    resp = _send_code(client)
    assert resp.status_code == 403
    assert resp.headers["x-content-type-options"] == "nosniff"
