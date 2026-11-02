"""Dashboard access: a username and password from .env, a signed session cookie, a CSRF token, a login throttle.

Standard library only. The cookie is base64url(json payload) + "." + HMAC-SHA256 with DASHBOARD_SESSION_SECRET.
"""
import base64
import collections
import hashlib
import hmac
import json
import os
import secrets
import time

COOKIE = "charmer_session"
SESSION_S = 12 * 3600
TRIES_PER_MINUTE = 5

USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SECRET = os.environ.get("DASHBOARD_SESSION_SECRET", "").encode()
if not USERNAME or len(PASSWORD) < 16 or len(SECRET) < 32:
    raise SystemExit("dashboard: DASHBOARD_USERNAME, DASHBOARD_PASSWORD (16+ characters) and "
                     "DASHBOARD_SESSION_SECRET (32+) must be set")

_tries = collections.defaultdict(collections.deque)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())


def new_session() -> tuple[str, str]:
    """(cookie value, csrf token)."""
    csrf = secrets.token_urlsafe(24)
    payload = _b64(json.dumps({"exp": int(time.time()) + SESSION_S, "csrf": csrf}).encode())
    return f"{payload}.{_sign(payload)}", csrf


def read_session(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if data.get("exp", 0) > time.time() else None


def client_id(request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def throttled(client: str) -> bool:
    """True once a client has had 5 failed tries in the last minute; then even the right password waits."""
    now = time.monotonic()
    q = _tries[client]
    while q and q[0] < now - 60:
        q.popleft()
    if not q:
        del _tries[client]  # keep the map small
        return False
    return len(q) >= TRIES_PER_MINUTE


def failed_try(client: str) -> None:
    _tries[client].append(time.monotonic())


def credentials_ok(username: str, password: str) -> bool:
    """Both compared in constant time, both always, so the answer never tells which one was wrong."""
    user_ok = hmac.compare_digest((username or "").encode(), USERNAME.encode())
    pass_ok = hmac.compare_digest((password or "").encode(), PASSWORD.encode())
    return user_ok & pass_ok


def secure_request(request) -> bool:
    """HTTPS as seen through the proxy in front: X-Forwarded-Proto is trusted with Cloudflare's client header, or
    alone when DASHBOARD_TRUST_PROXY=1 (another HTTPS proxy you run)."""
    https = request.headers.get("x-forwarded-proto") == "https"
    return https and (bool(request.headers.get("cf-connecting-ip")) or os.environ.get("DASHBOARD_TRUST_PROXY") == "1")
