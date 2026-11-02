"""Shared by the cron scripts: the SQLite store and alerts. Runs in the LiteLLM image."""
import os
import sys

for path in ("/app/plugin", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin")):
    if os.path.exists(os.path.join(path, "store.py")):
        sys.path.insert(0, path)
        break

import store as st  # noqa: E402


def store():
    return st.Store(os.environ.get("CHARMER_DB", "/data/charmer.sqlite"))


def alert(s, kind, message):
    """Print, record (at most once an hour per kind) and webhook an alert. Cron also mails the exit code."""
    print(f"ALERT {kind}: {message}", flush=True)
    s.alert_now(kind, message)
