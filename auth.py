"""Auth boundary (Faza 8A) — session cookie e nënshkruar + CSRF + rate-limit.

- local/test: autentikimi është hapur (dev-auth) — sjellja e vjetër e paprekur.
- production: çdo rrugë jo-publike kërkon session të vlefshëm; kredencialet
  nga env (AUTH_USERNAME + AUTH_PASSWORD ose AUTH_PASSWORD_SHA256).
- CSRF: double-submit cookie + header X-CSRF-Token për metoda state-changing.
- Rate-limit i thjeshtë in-memory për /login (mjafton për 8A; Redis më vonë).
"""
import os
import hashlib
import hmac
import secrets
import time

import config

COOKIE_SESSION = "oe_session"
COOKIE_CSRF = "oe_csrf"
PUBLIC_PREFIXES = ("/health", "/ready", "/login", "/logout", "/static", "/api/auth")
RATE = {"count": 0, "window_start": 0.0}
RATE_LIMIT = 5
RATE_WINDOW = 60


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def auth_required() -> bool:
    return os.environ.get("APP_ENV", "local") == "production"


def check_credentials(username: str, password: str) -> bool:
    ok_user = hmac.compare_digest(username, config.AUTH_USERNAME)
    expected = config.AUTH_PASSWORD_SHA256
    if not expected and config.AUTH_PASSWORD_PLAIN:
        expected = _hash_pw(config.AUTH_PASSWORD_PLAIN)
    ok_pass = bool(expected) and hmac.compare_digest(
        _hash_pw(password), expected if len(expected) == 64 else _hash_pw(expected))
    return ok_user and ok_pass


def make_session_token(username: str, ttl_hours: int | None = None) -> str:
    ttl = int(ttl_hours or config.SESSION_TTL_HOURS)
    exp = int(time.time()) + ttl * 3600
    payload = f"{username}|{exp}"
    sig = hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session_token(token: str) -> str | None:
    try:
        username, exp, sig = token.split("|")
        payload = f"{username}|{exp}"
        good = hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return username
    except (ValueError, TypeError):
        pass
    return None


def make_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_token: str, header_token: str) -> bool:
    return bool(cookie_token) and bool(header_token) and \
        hmac.compare_digest(cookie_token, header_token)


def rate_limit_login() -> bool:
    """Kthen True nëse lejohet; 5 provo / 60s (in-memory, 8A mjafton)."""
    now = time.time()
    if now - RATE["window_start"] > RATE_WINDOW:
        RATE["window_start"] = now
        RATE["count"] = 0
    RATE["count"] += 1
    return RATE["count"] <= RATE_LIMIT


def is_public(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)
