"""Charmer's SQLite file. Standard library only; runs on Python 3.12 (dashboard) and 3.13 (LiteLLM).

The plugin never touches the file on the request path: it queues writes with `add()`, and one writer thread applies
them every 2 s (`start()`), or `flush()` does it on demand. Every connection waits up to 5 s for a lock.
Never prompt or completion text, never a key: callers pass counts, ids, names and hashes only.
"""
import json
import os
import sqlite3
import threading
import time
import urllib.request

SCHEMA = """
create table if not exists usage_minute (account text, model text, minute text, requests integer default 0,
  prompt integer default 0, completion integer default 0, neurons real default 0, primary key (account, model, minute));
create table if not exists usage_day (account text, model text, day text, requests integer default 0,
  prompt integer default 0, completion integer default 0, neurons real default 0, eval_requests integer default 0,
  primary key (account, model, day));
create table if not exists key_usage_minute (key text, grp text, minute text, requests integer default 0,
  tokens integer default 0, r429 integer default 0, primary key (key, grp, minute));
create table if not exists provider_state (deployment text primary key, state text, updated real);
create table if not exists openrouter_key (id integer primary key check (id = 1), reading text, read_at real);
create table if not exists recent_calls (id integer primary key autoincrement, ts real, key text, grp text,
  deployment text, session_hash text, status integer, latency_ms integer, tokens integer, diverted integer);
create table if not exists pulls (id integer primary key autoincrement, target text, reason text, who text,
  since real, lifted real);
create table if not exists alerts (id integer primary key autoincrement, ts real, kind text, message text,
  cleared real);
"""
KEEP_MINUTES_S = 2 * 86400
KEEP_DAYS_S = 90 * 86400
KEEP_CALLS = 1000


def connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=5000")
    if not readonly:
        conn.executescript(SCHEMA)
    return conn


class Store:
    def __init__(self, path):
        self.path = path
        self._ops = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.on_flush = None

    # ---- writes: queued, applied by the writer -------------------------------------------------------------------
    MAX_OPS = 200_000

    def add(self, op, *args):
        with self._lock:
            self._ops.append((op, args))
            if len(self._ops) > self.MAX_OPS:  # writes keep failing (disk full?): keep memory bounded, keep the newest
                del self._ops[: len(self._ops) // 2]
                print("charmer store: write queue overflowed, dropped the oldest half", flush=True)

    def add_pull(self, target, reason, who):
        self.add("pull", target, reason, who, time.time())

    def add_alert(self, kind, message, ts=None):
        self.add("alert", ts or time.time(), kind, message)

    def alert_now(self, kind, message, every=3600):
        """Write an alert at once, at most once per `every` seconds per kind, and POST it to ALERT_WEBHOOK_URL.
        For the cron scripts and the dashboard (the plugin keeps its own limit in memory). True if raised.
        Writes anything already queued first, so a pull queued just before its alert is never lost."""
        self.flush()
        now = time.time()
        conn = connect(self.path)
        try:
            with conn:
                if conn.execute("select 1 from alerts where kind = ? and ts > ?", (kind, now - every)).fetchone():
                    return False
                conn.execute("insert into alerts (ts, kind, message) values (?,?,?)", (now, kind, message))
        finally:
            conn.close()
        webhook(kind, message)
        return True

    def discard(self):
        with self._lock:
            self._ops = []

    def flush(self):
        with self._lock:
            ops, self._ops = self._ops, []
        if not ops:
            return
        conn = connect(self.path)
        try:
            with conn:
                for op, args in ops:
                    getattr(self, f"_w_{op}")(conn, *args)
                now = time.time()
                conn.execute("delete from usage_minute where minute < ?", (_minute(now - KEEP_MINUTES_S),))
                conn.execute("delete from key_usage_minute where minute < ?", (_minute(now - KEEP_MINUTES_S),))
                conn.execute("delete from recent_calls where id <= (select max(id) from recent_calls) - ?", (KEEP_CALLS,))
                conn.execute("delete from alerts where ts < ?", (now - KEEP_DAYS_S,))
                conn.execute("delete from usage_day where day < ?", (time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS_S)),))
                conn.execute("delete from pulls where lifted is not null and lifted < ?", (now - KEEP_DAYS_S,))
        except Exception:
            with self._lock:  # keep them for the next pass rather than lose them
                self._ops = ops + self._ops
            raise
        finally:
            conn.close()

    @staticmethod
    def _w_usage(conn, account, model, minute, day, requests, prompt, completion, neurons, eval_requests):
        conn.execute("""insert into usage_minute values (?,?,?,?,?,?,?) on conflict (account, model, minute) do update set
            requests = requests + excluded.requests, prompt = prompt + excluded.prompt,
            completion = completion + excluded.completion, neurons = neurons + excluded.neurons""",
                     (account, model, minute, requests, prompt, completion, neurons))
        conn.execute("""insert into usage_day values (?,?,?,?,?,?,?,?) on conflict (account, model, day) do update set
            requests = requests + excluded.requests, prompt = prompt + excluded.prompt,
            completion = completion + excluded.completion, neurons = neurons + excluded.neurons,
            eval_requests = eval_requests + excluded.eval_requests""",
                     (account, model, day, requests, prompt, completion, neurons, eval_requests))

    @staticmethod
    def _w_key_usage(conn, key, grp, minute, requests, tokens, r429):
        conn.execute("""insert into key_usage_minute values (?,?,?,?,?,?) on conflict (key, grp, minute) do update set
            requests = requests + excluded.requests, tokens = tokens + excluded.tokens, r429 = r429 + excluded.r429""",
                     (key, grp, minute, requests, tokens, r429))

    @staticmethod
    def _w_provider_state(conn, deployment, state, ts):
        conn.execute("insert or replace into provider_state values (?,?,?)", (deployment, json.dumps(state), ts))

    @staticmethod
    def _w_openrouter(conn, reading, ts):
        conn.execute("insert or replace into openrouter_key values (1,?,?)", (json.dumps(reading), ts))

    @staticmethod
    def _w_recent(conn, ts, key, grp, deployment, session_hash, status, latency_ms, tokens, diverted):
        conn.execute("insert into recent_calls (ts, key, grp, deployment, session_hash, status, latency_ms, tokens, diverted)"
                     " values (?,?,?,?,?,?,?,?,?)", (ts, key, grp, deployment, session_hash, status, latency_ms, tokens, int(diverted)))

    @staticmethod
    def _w_pull(conn, target, reason, who, ts):
        # one open row per (target, who): a guard's pull is never swallowed by an owner's
        if not conn.execute("select 1 from pulls where target = ? and who = ? and lifted is null", (target, who)).fetchone():
            conn.execute("insert into pulls (target, reason, who, since) values (?,?,?,?)", (target, reason, who, ts))

    @staticmethod
    def _w_unpull(conn, target, ts, whos=None):
        """Lift the open pulls on target; only those made by `whos` when given (the dashboard, a guard)."""
        if whos:
            marks = ",".join("?" * len(whos))
            conn.execute(f"update pulls set lifted = ? where target = ? and lifted is null and who in ({marks})", (ts, target, *whos))
        else:
            conn.execute("update pulls set lifted = ? where target = ? and lifted is null", (ts, target))

    @staticmethod
    def _w_clear_alert(conn, alert_id, ts):
        conn.execute("update alerts set cleared = ? where id = ? and cleared is null", (ts, alert_id))

    @staticmethod
    def _w_alert(conn, ts, kind, message):
        conn.execute("insert into alerts (ts, kind, message) values (?,?,?)", (ts, kind, message))

    # ---- reads -------------------------------------------------------------------------------------------------
    def query(self, sql, args=()):
        conn = connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def open_pulls(self):
        return {r[0] for r in self.query("select target from pulls where lifted is null")}

    def usage_days(self, since_day):
        return self.query("select account, model, day, requests, prompt, completion, neurons, eval_requests"
                          " from usage_day where day >= ?", (since_day,))

    def usage_minutes(self, since_minute):
        return self.query("select account, model, minute, requests, prompt, completion from usage_minute where minute >= ?",
                          (since_minute,))

    # ---- the writer thread -------------------------------------------------------------------------------------
    def start(self, interval=2.0):
        def loop():
            while not self._stop.wait(interval):
                self._pass()
        self._thread = threading.Thread(target=loop, name="charmer-store", daemon=True)
        self._thread.start()

    def _pass(self):
        try:
            self.flush()
        except Exception as e:  # noqa: BLE001 - logged, retried next pass
            print(f"charmer store: flush failed, will retry: {e}", flush=True)
        if self.on_flush:
            try:
                self.on_flush()
            except Exception as e:  # noqa: BLE001
                print(f"charmer store: after-flush hook failed: {e}", flush=True)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush()


def webhook(kind, message):
    """POST one alert to ALERT_WEBHOOK_URL, if set, off the caller's thread. The dashboard still lists it if this fails."""
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return

    def send():
        try:
            data = json.dumps({"source": "charmer", "kind": kind, "message": message}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"content-type": "application/json"}), timeout=10)
        except Exception as e:  # noqa: BLE001
            print(f"charmer: alert webhook failed: {e}", flush=True)
    threading.Thread(target=send, name="charmer-webhook", daemon=True).start()


def _minute(ts):
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))
