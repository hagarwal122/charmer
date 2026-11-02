"""Daily free-plan check (`make free-check`). Exit 1 means the owner gets mail.

1. Gemini: one 1-token call straight to Google (not through LiteLLM). 402 = billing got attached: pull gemini/.
2. OpenRouter: the plugin's last /key reading (it polls every 5 minutes) must show no money moving.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from _common import alert, store

GEMINI_URL = os.environ.get("GEMINI_PROBE_URL",
                            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent")


def gemini(s):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("gemini: no key, not in the pool, skipped")
        return 0
    body = json.dumps({"contents": [{"parts": [{"text": "ok"}]}], "generationConfig": {"maxOutputTokens": 1}}).encode()
    req = urllib.request.Request(GEMINI_URL, data=body, method="POST",
                                 headers={"content-type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001
        alert(s, "free_check", f"Gemini probe failed: {e}")
        return 1
    if code == 402:
        s.add_pull("gemini/", "Gemini probe answered 402: billing is attached. Unlink it, then make unpull ID=gemini/", "guard")
        alert(s, "gemini_402", "Gemini answered 402: billing got attached to the key's project. Pulled gemini/.")
        return 1
    if code == 429:
        print("gemini: 429, inconclusive (logged)")
        return 0
    print(f"gemini: {code}")
    return 0


def openrouter(s):
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("openrouter: no key, skipped")
        return 0
    rows = s.query("select reading, read_at from openrouter_key where id = 1")
    if not rows:
        alert(s, "free_check", "No OpenRouter key reading yet: is LiteLLM (and its plugin) running?")
        return 1
    reading, read_at = json.loads(rows[0][0]), rows[0][1]
    if time.time() - read_at > 3600:
        alert(s, "free_check", "The last OpenRouter key reading is over an hour old")
        return 1
    fmdr = reading.get("free_model_daily_requests") or {}
    bad = [why for why, hit in (
        ("usage above 0", (reading.get("usage") or 0) > 0), ("limit not 1", reading.get("limit") != 1),
        ("limit_reset set", reading.get("limit_reset") is not None), ("free daily limit below 1000", (fmdr.get("limit") or 0) < 1000),
        ("under 100 free requests left", (fmdr.get("remaining") if fmdr.get("remaining") is not None else 0) < 100)) if hit]
    if bad:
        alert(s, "free_check", "OpenRouter key: " + ", ".join(bad))
        return 1
    print("openrouter: ok")
    return 0


def main():
    s = store()
    return max(gemini(s), openrouter(s))


if __name__ == "__main__":
    sys.exit(main())
