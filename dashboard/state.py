"""Everything the dashboard shows, built from the SQLite file, the config files and LiteLLM's /metrics.

Never calls a provider or a chat endpoint. An unknown value is None, never 0.
"""
import collections
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

import yaml

import store as st

ENV = os.environ.get
DB = ENV("CHARMER_DB", "/data/charmer.sqlite")
LITELLM = ENV("LITELLM_URL", "http://litellm:4000").rstrip("/")
TZ = ZoneInfo(ENV("CHARMER_DASHBOARD_TZ", "UTC"))
STATUS_WORDS = {"serving": "serving", "near": "near limit", "filtered": "filtered", "cooling": "cooling",
                "pulled": "pulled", "nokey": "no key", "idle": "no traffic"}
GUARDS = {"guard", "budget", "plugin"}


def _yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def files():
    return {
        "config": _yaml(ENV("CHARMER_CONFIG", "/app/config.yaml")),
        "rendered": _yaml(ENV("CHARMER_RENDERED", "/app/rendered/config.yaml")),
        "limits": _yaml(ENV("CHARMER_LIMITS", "/app/limits.yaml")),
    }


def discovery():
    path = os.path.join(ENV("CHARMER_REPORTS", "/app/reports"), "discovery-latest.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---- LiteLLM health and metrics ------------------------------------------------------------------------------------
class Health:
    def __init__(self, store):
        self.store = store
        self.up = None
        self.checked_at = None
        self.failures = 0
        self.alerted = False
        self._metrics = (0.0, {})

    def check(self):
        try:
            with urllib.request.urlopen(f"{LITELLM}/health/liveliness", timeout=5) as r:
                ok = r.status == 200
        except Exception:  # noqa: BLE001
            ok = False
        self.checked_at = time.time()
        if ok:
            self.up, self.failures, self.alerted = True, 0, False
            return
        self.failures += 1
        self.up = False
        if self.failures >= 3 and not self.alerted:
            self.alerted = True
            self.store.alert_now("litellm_down", f"LiteLLM has not answered /health/liveliness {self.failures} times in a row")

    def loop(self, every):
        while True:
            self.check()
            time.sleep(every)

    def start(self):
        threading.Thread(target=self.loop, args=(float(ENV("DASHBOARD_HEALTH_SECONDS", "15")),), daemon=True).start()

    def deployment_states(self):
        """model_id -> 0 healthy, 1 partial, 2 down, from LiteLLM's Prometheus metrics; cached 5 s."""
        ts, cached = self._metrics
        if time.time() - ts < 5:
            return cached
        states = {}
        key = ENV("CHARMER_METRICS_KEY", "")
        try:
            req = urllib.request.Request(f"{LITELLM}/metrics/", headers={"authorization": f"Bearer {key}"} if key else {})
            with urllib.request.urlopen(req, timeout=5) as r:
                for line in r.read().decode().splitlines():
                    if line.startswith("litellm_deployment_state{"):
                        m = re.search(r'model_id="([^"]*)"', line)
                        if m:
                            states[m.group(1)] = float(line.rsplit(" ", 1)[1])
        except Exception:  # noqa: BLE001 - shown as unknown
            states = None
        self._metrics = (time.time(), states)
        return states


# ---- helpers --------------------------------------------------------------------------------------------------------
def _local_day(ts, tz):
    return dt.datetime.fromtimestamp(ts, tz).date()


def _next_midnight(now, tzname):
    tz = ZoneInfo(tzname or "UTC")
    today = dt.datetime.fromtimestamp(now, tz).date()
    return dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), tz).timestamp()


def _minute_ts(minute):
    return dt.datetime.strptime(minute, "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc).timestamp()


def _used(used, limit):
    return {"used": used, "limit": limit, "pct": (round(100 * used / limit, 1) if limit else None)}


def _pick(pair_used, pair_limit, account_used, account_limit):
    """The pair's own count and limit; the account's only when the binding limit is the account's (shared)."""
    if pair_limit:
        return {**_used(pair_used, pair_limit), "scope": "pair"}
    if account_limit:
        return {**_used(account_used, account_limit), "scope": "account"}
    return {**_used(pair_used, None), "scope": "pair"}


def _rows(store, sql, args=()):
    try:
        return store.query(sql, args)
    except Exception:  # noqa: BLE001 - a missing or locked file shows as empty, not as an error page
        return []


# ---- the state ----------------------------------------------------------------------------------------------------
def build(store, health):
    now = time.time()
    f = files()
    cfg, rendered, limits = f["config"], f["rendered"], f["limits"]
    accounts = limits.get("accounts") or {}
    mlimits = limits.get("models") or {}
    served = {d["model_info"]["id"] for d in rendered.get("model_list") or []}
    deployments = cfg.get("model_list") or []

    # day counts, per account in its own time zone
    since = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(days=2)).date().isoformat()
    day_rows = _rows(store, "select account, model, day, requests, prompt, completion, neurons, eval_requests"
                            " from usage_day where day >= ?", (since,))
    today_of = {a: _local_day(now, ZoneInfo((accounts.get(a) or {}).get("day_tz") or "UTC")).isoformat() for a in
                {r[0] for r in day_rows} | set(accounts)}
    day = collections.defaultdict(lambda: {"requests": 0, "tokens": 0, "neurons": 0.0, "eval_requests": 0})
    for a, m, d, req, p, c, n, ev in day_rows:
        if d == today_of.get(a):
            day[(a, m)] = {"requests": req, "tokens": p + c, "neurons": n, "eval_requests": ev}

    # minute rows for the last 48 hours (sparklines) and the last minute (rates)
    minute_rows = _rows(store, "select account, model, minute, requests, prompt, completion from usage_minute"
                               " where minute >= ?", (st._minute(now - 48 * 3600),))
    last_min = collections.defaultdict(lambda: {"requests": 0, "tokens": 0})
    # 48 hourly buckets on the hour in the dashboard's zone (India is +05:30), the last one holding now
    this_hour = now - (now + dt.datetime.fromtimestamp(now, TZ).utcoffset().total_seconds()) % 3600
    hour_start = [this_hour - (47 - i) * 3600 for i in range(48)]
    req_series, tok_series = [0] * 48, [0] * 48
    by_model = collections.defaultdict(lambda: [0] * 48)
    for a, m, minute, req, p, c in minute_rows:
        ts = _minute_ts(minute)
        idx = int((ts - hour_start[0]) // 3600)
        if m == "*":
            if 0 <= idx < 48:
                req_series[idx] += req
                tok_series[idx] += p + c
        else:
            if 0 <= idx < 48:
                by_model[m][idx] += req
            if ts >= now - 60:
                last_min[(a, m)]["requests"] += req
                last_min[(a, m)]["tokens"] += p + c
        if m == "*" and ts >= now - 60:
            last_min[(a, "*")]["requests"] += req
            last_min[(a, "*")]["tokens"] += p + c

    def window_total(series_rows, start_ts, end_ts):
        return sum(r for ts, r in series_rows if start_ts <= ts < end_ts)

    star = [(_minute_ts(mi), req, p + c) for a, m, mi, req, p, c in minute_rows if m == "*"]
    local_midnight = dt.datetime.combine(_local_day(now, TZ), dt.time(), TZ).timestamp()
    req_today = sum(r for ts, r, _ in star if ts >= local_midnight)
    tok_today = sum(t for ts, _, t in star if ts >= local_midnight)
    req_yday = sum(r for ts, r, _ in star if local_midnight - 86400 <= ts < now - 86400)
    tok_yday = sum(t for ts, _, t in star if local_midnight - 86400 <= ts < now - 86400)
    have_yday = any(ts < local_midnight for ts, _, _ in star)

    # busy answers (LiteLLM's 429s), counted per key by the plugin
    key_rows = _rows(store, "select key, grp, minute, requests, tokens, r429 from key_usage_minute where minute >= ?",
                     (st._minute(now - 48 * 3600),))
    busy_series = [0] * 48
    busy_today = 0
    for k, g, minute, req, tok, r429 in key_rows:
        ts = _minute_ts(minute)
        idx = int((ts - hour_start[0]) // 3600)
        if 0 <= idx < 48:
            busy_series[idx] += r429
        if ts >= local_midnight:
            busy_today += r429

    # pulls, provider readings, LiteLLM states
    pulls = {}
    for target, reason, who, since_ts in _rows(store, "select target, reason, who, since from pulls where lifted is null"):
        pulls[target] = {"reason": reason, "who": who, "since": since_ts}
    readings = {}
    for dep, state_json, updated in _rows(store, "select deployment, state, updated from provider_state"):
        try:
            readings[dep] = json.loads(state_json)
        except json.JSONDecodeError:
            pass
    lstates = health.deployment_states() or {}

    def pull_of(d):
        mid = d["model_info"]["id"]
        base = mid[5:] if mid.startswith("pair-") else mid
        for t in (mid, base, f"pair-{base}", f"{d['model_info'].get('account', '')}/"):
            if t in pulls:
                return {"target": t, **pulls[t]}
        return None

    pairs = []
    for d in deployments:
        info, params = d["model_info"], d.get("litellm_params") or {}
        mid, acct_name, model = info["id"], info.get("account", ""), params.get("model", "")
        acct = accounts.get(acct_name) or {}
        ml = mlimits.get(model) or {}
        margin = acct.get("margin", 0)
        a_day, m_day = day[(acct_name, "*")], day[(acct_name, model)]
        day_req = _pick(m_day["requests"], ml.get("day_requests"), a_day["requests"], acct.get("day_requests"))
        day_tok = _pick(m_day["tokens"], ml.get("day_tokens"), a_day["tokens"], acct.get("day_tokens"))
        stop = acct.get("embed_stop_at") if info.get("mode") == "embedding" else acct.get("chat_stop_at")
        neurons = _used(a_day["neurons"], stop or acct.get("day_neurons")) if (stop or acct.get("day_neurons")) else None
        m_min, a_min = last_min[(acct_name, model)], last_min[(acct_name, "*")]
        minute = {"requests": _pick(m_min["requests"], ml.get("minute_requests"), a_min["requests"], acct.get("minute_requests")),
                  "tokens": _pick(m_min["tokens"], ml.get("minute_tokens"), a_min["tokens"], acct.get("minute_tokens"))}
        reading = readings.get(mid)
        provider = None
        if reading:
            provider = {"remaining_requests": reading.get("remaining_requests"), "remaining_tokens": reading.get("remaining_tokens"),
                        "reset_at": reading.get("reset_at"), "age_s": int(now - (reading.get("seen") or now)),
                        "expired": bool(reading.get("reset_at") and now >= reading["reset_at"])}
        pulled = pull_of(d)
        lstate = lstates.get(mid)
        hard_hit = any(x and x["limit"] is not None and x["used"] >= x["limit"] - margin for x in (day_req, day_tok)) \
            or bool(neurons and neurons["limit"] is not None and neurons["used"] >= neurons["limit"])
        soft_hit = any(x["limit"] is not None and x["used"] >= x["limit"] for x in minute.values()) or bool(
            provider and not provider["expired"] and provider["remaining_requests"] is not None and provider["remaining_requests"] <= margin)
        near80 = any(x and x["pct"] is not None and x["pct"] >= 80 for x in (day_req, day_tok, neurons))
        if mid not in served:
            state = "nokey"
        elif pulled:
            state = "pulled"
        elif lstate is not None and lstate >= 2:
            state = "cooling"
        elif hard_hit:
            state = "filtered"
        elif soft_hit or near80:
            state = "near"
        else:
            state = "serving"
        tightest = max((x for x in (day_req, day_tok, neurons) if x and x["pct"] is not None), key=lambda x: x["pct"], default=None)
        pairs.append({
            "id": mid, "group": d["model_name"], "account": acct_name, "model": model, "mode": info.get("mode") or "chat",
            "state": state, "label": STATUS_WORDS[state], "weight": params.get("weight"), "order": params.get("order"),
            "minute": minute, "day": {"requests": day_req, "tokens": day_tok, "neurons": neurons,
                                      "resets_at": _next_midnight(now, acct.get("day_tz"))},
            "tightest": tightest, "provider": provider, "litellm_state": lstate,
            "pulled": pulled, "can_unpull": bool(pulled and pulled["who"] not in GUARDS),
        })
    by_id = {p["id"]: p for p in pairs}

    # groups (only what LiteLLM serves), plus declared groups with no member
    groups = collections.OrderedDict()
    for d in rendered.get("model_list") or []:
        member = by_id.get(d["model_info"]["id"])
        if member is None:  # rendered after a git pull this dashboard has not seen yet: make restart
            member = {"id": d["model_info"]["id"], "account": d["model_info"].get("account", ""), "model": "",
                      "state": "nokey", "label": "restart pending", "tightest": None, "day": {"resets_at": None}}
        groups.setdefault(d["model_name"], []).append(member)
    for g in limits.get("groups") or []:
        groups.setdefault(g, [])

    # providers (accounts)
    disc = (discovery().get("providers") or {})
    configured = {d.get("litellm_params", {}).get("model") for d in deployments}
    providers = []
    for name in sorted({p["account"] for p in pairs} | set(accounts)):
        acct = accounts.get(name) or {}
        mine = [p for p in pairs if p["account"] == name]
        status = "no key" if mine and all(p["state"] == "nokey" for p in mine) else (
            "pulled" if f"{name}/" in pulls else "ok")
        used = day[(name, "*")]
        providers.append({
            "name": name, "status": status, "pairs": [p["id"] for p in mine],
            "day": {"requests": _used(used["requests"], acct.get("day_requests")),
                    "tokens": _used(used["tokens"], acct.get("day_tokens")),
                    "neurons": _used(used["neurons"], acct.get("day_neurons")) if acct.get("day_neurons") else None,
                    "eval_requests": _used(used["eval_requests"], acct.get("eval_day_requests"))},
            "resets_at": _next_midnight(now, acct.get("day_tz")) if acct.get("day_tz") else None,
            "available_models": sorted(set((disc.get(name) or {}).get("free_models") or []) - configured),
            "source": acct.get("source"),
        })

    # pool left: the tightest account with a day limit
    pool = None
    for p in providers:
        r = p["day"]["requests"]
        if p["status"] == "ok" and r["limit"]:
            left = max(0.0, 100 - r["pct"])
            if pool is None or left < pool["value"]:
                pool = {"value": round(left, 1), "account": p["name"]}

    counts = collections.Counter(p["state"] for p in pairs if p["id"] in served)
    kpis = {
        "requests_today": {"value": req_today, "vs_yesterday": _pct_change(req_today, req_yday) if have_yday else None, "series": req_series},
        "tokens_today": {"value": tok_today, "vs_yesterday": _pct_change(tok_today, tok_yday) if have_yday else None, "series": tok_series},
        "pool_left": pool or {"value": None, "account": None},
        "pairs": {"serving": counts["serving"] + counts["near"], "total": len(served), "near": counts["near"],
                  "filtered": counts["filtered"], "cooling": counts["cooling"], "pulled": counts["pulled"]},
        "busy_today": busy_today, "busy_series": busy_series,
    }

    model_to_pair = {}
    for p in pairs:
        model_to_pair.setdefault(p["model"], p["id"][5:] if p["id"].startswith("pair-") else p["id"])
    stacked = collections.defaultdict(lambda: [0] * 48)
    for m, vals in by_model.items():
        name = model_to_pair.get(m, m)
        stacked[name] = [a + b for a, b in zip(stacked[name], vals)]


    recent = [{"ts": r[0], "key": r[1], "group": r[2], "deployment": r[3], "status": r[4], "latency_ms": r[5],
               "tokens": r[6], "diverted": bool(r[7])}
              for r in _rows(store, "select ts, key, grp, deployment, status, latency_ms, tokens, diverted"
                                    " from recent_calls order by id desc limit 1000")]
    alerts = [{"id": r[0], "ts": r[1], "kind": r[2], "message": r[3], "cleared": r[4]}
              for r in _rows(store, "select id, ts, kind, message, cleared from alerts order by id desc limit 200")]

    return {
        "now": now, "tz": str(TZ),
        "litellm": {"up": health.up, "checked_at": health.checked_at},
        "alerts_open": sum(1 for a in alerts if not a["cleared"]),
        "kpis": kpis,
        "by_pair": {"hours": hour_start, "today_from": local_midnight, "series": [{"pair": k, "values": v} for k, v in sorted(stacked.items()) if any(v)]},
        "groups": [{"name": g, "members": [{k: m[k] for k in ("id", "account", "model", "state", "label", "tightest", "day")} for m in ms]}
                   for g, ms in groups.items()],
        "recent": recent, "providers": providers, "pairs": pairs, "alerts": alerts,
    }


def _pct_change(now_value, before):
    if not before:
        return None
    return round(100 * (now_value - before) / before, 1)
