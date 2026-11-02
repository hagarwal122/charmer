"""Nightly backup (`make backup`): SQLite's online backup into backups/, keep the newest 14."""
import os
import sqlite3
import sys
import time

DB = os.environ.get("CHARMER_DB", "/data/charmer.sqlite")
OUT = os.environ.get("CHARMER_BACKUPS", "/backups")
KEEP = 14


def main():
    os.makedirs(OUT, exist_ok=True)
    target = os.path.join(OUT, f"charmer-{time.strftime('%Y%m%d-%H%M%S')}.sqlite")
    src = sqlite3.connect(DB, timeout=10)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    ok = dst.execute("pragma integrity_check").fetchone()[0]
    dst.close()
    src.close()
    if ok != "ok":
        print(f"backup: integrity check failed: {ok}")
        return 1
    files = sorted(f for f in os.listdir(OUT) if f.startswith("charmer-") and f.endswith(".sqlite"))
    for old in files[:-KEEP]:
        os.remove(os.path.join(OUT, old))
    print(f"backup: {target} ok, {min(len(files), KEEP)} kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
