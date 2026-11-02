"""Charmer's custom auth hook. Loaded by LiteLLM as `charmer_auth.user_api_key_auth`.

One user, one key: CHARMER_API_KEY in .env, the same value your app sends. Order of checks:
  1. LITELLM_MASTER_KEY -> PROXY_ADMIN, never through the tunnel (a `cf-connecting-ip` header).
  2. CHARMER_METRICS_KEY -> /metrics only, never through the tunnel.
  3. CHARMER_API_KEY -> every group, on inference paths only (anything else is 403). The plugin refuses unknown groups.
No match: "charmer: unknown key" (LiteLLM answers 401 "Authentication Error, charmer: unknown key").
A missing or weak key stops LiteLLM from starting.
"""
import hashlib
import hmac
import os
import sys

from fastapi import HTTPException, Request
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

INFERENCE_PATHS = frozenset({"/v1/chat/completions", "/chat/completions", "/v1/embeddings", "/embeddings",
                             "/v1/models", "/models"})
METRICS_PATHS = frozenset({"/metrics", "/metrics/"})
MIN_SECRET = 32


def _fail(msg):
    print(f"charmer_auth: {msg}", file=sys.stderr)
    raise SystemExit(f"charmer_auth: {msg}")


def _load():
    admin, metrics, key = (os.environ.get(n, "") for n in ("LITELLM_MASTER_KEY", "CHARMER_METRICS_KEY", "CHARMER_API_KEY"))
    if len(admin) < 48:
        # without it LiteLLM runs in no-auth dev mode and skips its own checks
        _fail("LITELLM_MASTER_KEY must be set in .env (48 or more characters: make key-new NAME=LITELLM_MASTER_KEY)")
    if len(key) < MIN_SECRET:
        _fail(f"CHARMER_API_KEY is missing or shorter than {MIN_SECRET} characters (make key-new)")
    if key in (admin, metrics):
        _fail("CHARMER_API_KEY must differ from the admin and metrics keys")
    return key.encode()


API_KEY = _load()
ADMIN = os.environ.get("LITELLM_MASTER_KEY", "").encode()
METRICS = os.environ.get("CHARMER_METRICS_KEY", "").encode()


def _same(a: bytes, b: bytes) -> bool:
    return bool(b) and hmac.compare_digest(a, b)


def _through_tunnel(request: Request) -> bool:
    return bool(request.headers.get("cf-connecting-ip"))


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    given = (api_key or "").encode()
    path = request.url.path
    hashed = hashlib.sha256(given).hexdigest()

    if _same(given, ADMIN):
        if _through_tunnel(request):
            raise Exception("charmer: unknown key")
        return UserAPIKeyAuth(api_key=hashed, user_role=LitellmUserRoles.PROXY_ADMIN, key_alias="admin")

    if _same(given, METRICS):
        if _through_tunnel(request):
            raise Exception("charmer: unknown key")
        if path not in METRICS_PATHS:
            raise HTTPException(status_code=403, detail="charmer: this key may only read /metrics")
        return UserAPIKeyAuth(api_key=hashed, key_alias="metrics", user_role=LitellmUserRoles.PROXY_ADMIN)  # /metrics only, above

    if _same(given, API_KEY):
        if path not in INFERENCE_PATHS:
            raise HTTPException(status_code=403, detail="charmer: this key may only call inference paths")
        return UserAPIKeyAuth(api_key=hashed, key_alias="app")

    raise Exception("charmer: unknown key")
