"""Charmer's LiteLLM plugin. Loaded as `charmer_plugin.instance`.

It guards request bodies, records every call per provider account and model, filters pairs near a limit before
LiteLLM picks (hard: pulls, day caps, eval share; soft: minute limits, provider remaining, failed-this-call), and
keeps each session on one pair. Counters live in memory and are written to the SQLite file by store.py's writer.
"""
import atexit
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

import store as st

# Client body fields allowed through. LiteLLM itself adds metadata, litellm_session_id and litellm_trace_id.
ALLOWED_BODY = frozenset({
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls", "stream", "stream_options", "temperature",
    "top_p", "max_tokens", "max_completion_tokens", "stop", "response_format", "seed", "n", "user", "input",
    "encoding_format", "dimensions", "metadata", "litellm_session_id", "litellm_trace_id"})
SESSION_TTL_S = 1800
FAILED_TTL_S = 300
ALERT_EVERY_S = 3600
NEAR_LIMIT = 0.8
OPENROUTER_ALERT_REMAINING = 100


# Request fields that must never come from a consumer, wherever LiteLLM keeps them: a caller's own
# credentials or headers would reach the provider in place of the deployment's.
NEVER_FROM_CALLER = frozenset({"api_key", "api_base", "base_url", "api_version", "headers", "extra_headers",
                               "provider_specific_header", "organization", "extra_body"})


class BadBody(Exception):
    pass


class StartError(Exception):
    pass


def check_body(body):
    unknown = sorted(set(body or {}) - ALLOWED_BODY)
    if unknown:
        raise BadBody(f"charmer: field(s) not allowed: {', '.join(unknown)}")
    if (body or {}).get("n") not in (None, 1):
        raise BadBody("charmer: n must be 1")
    for tool in (body or {}).get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function" or set(tool) - {"type", "function"}:
            raise BadBody("charmer: only function tools are allowed (no provider server tools: they can bill)")


def check_request(body, data, credential_fields):
    """The whole request a consumer sent: the body copy, the dict LiteLLM routes on, and its credential fields."""
    present = sorted(NEVER_FROM_CALLER & (set(body or {}) | set(data or {})))
    if present or credential_fields:
        raise BadBody(f"charmer: field(s) not allowed: {', '.join(present or ['credentials'])}")
    check_body(body)


_RESET_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_reset(value, now):
    """Provider reset values: "60s", "1m30s", "20ms", plain seconds, or an epoch. None when unreadable."""
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        n = float(text)
        return n if n > 1e9 else now + n
    parts = _RESET_PART.findall(text)
    if not parts or "".join(a + b for a, b in parts) != text:
        return None
    unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return now + sum(float(a) * unit[b] for a, b in parts)


def day_key(tz, now):
    return dt.datetime.fromtimestamp(now, ZoneInfo(tz or "UTC")).date().isoformat()


def _id(d):
    return d["model_info"]["id"]


def _order(d):
    return d["litellm_params"].get("order") or 1


def rendezvous(session, deployments):
    return max(deployments, key=lambda d: hashlib.sha256(f"{session}|{_id(d)}".encode()).digest())


def check_start(callbacks, me, argv):
    """The plugin must run before LiteLLM's session affinity, and LiteLLM must run one worker (PLG-11)."""
    names = [type(c).__name__ for c in callbacks]
    if "DeploymentAffinityCheck" in names:
        mine = next((i for i, c in enumerate(callbacks) if c is me), None)
        if mine is None or mine > names.index("DeploymentAffinityCheck"):
            raise StartError("charmer: the plugin must come before DeploymentAffinityCheck in litellm.callbacks")
    if "--num_workers" in argv:
        if int(argv[argv.index("--num_workers") + 1]) != 1:
            raise StartError("charmer: LiteLLM must run exactly one worker")


STANDARD_MESSAGE = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})
STANDARD_TOOL_CALL = frozenset({"id", "type", "function"})
GEMINI_MESSAGE = frozenset({"provider_specific_fields", "thinking_blocks"})
GEMINI_TOOL_CALL = frozenset({"provider_specific_fields"})


def fit_history(messages, account, model):
    """Make replayed assistant turns acceptable to the deployment about to serve the call (LIVE-10, 2026-10-28).

    A turn made by one provider carries its own extras (reasoning_content, reasoning, provider_specific_fields,
    thinking_blocks, images, index); Groq answers 400 and Mistral 422 to them, and Cloudflare 400s on content null.
    So: standard OpenAI fields only, plus Gemini's own fields when Gemini serves, and "" for null content on
    Cloudflare. Tool call ids are never touched."""
    gemini = model.startswith("gemini/")
    keep_m = STANDARD_MESSAGE | (GEMINI_MESSAGE if gemini else frozenset())
    keep_t = STANDARD_TOOL_CALL | (GEMINI_TOOL_CALL if gemini else frozenset())
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            continue
        n = {k: v for k, v in m.items() if k in keep_m}
        if n.get("tool_calls"):
            n["tool_calls"] = [{k: v for k, v in tc.items() if k in keep_t} for tc in n["tool_calls"]]
        if n.get("content") is None and account == "cloudflare":
            n["content"] = ""
        out.append(n)
    return out


def _number(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _estimate(x):
    return len(json.dumps(x, default=str)) // 4 if x else 0


def _hash(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:16]


class Charmer(CustomLogger):
    def __init__(self, limits, known_groups, store, clock=time.time, start=True):
        super().__init__()
        self.accounts = (limits or {}).get("accounts") or {}
        self.models = (limits or {}).get("models") or {}
        self.known_groups = known_groups or {}
        self.store = store
        self.clock = clock
        self.lock = threading.RLock()
        self.events = collections.defaultdict(collections.deque)  # (account, model|'*') -> deque of [ts, req, tok]
        self.inflight = {}  # (call_id, deployment_id) -> dict
        self.day = collections.defaultdict(lambda: {"requests": 0, "tokens": 0, "neurons": 0.0, "eval_requests": 0})
        self.key_counts = collections.defaultdict(lambda: {"requests": 0, "tokens": 0, "r429": 0})
        self.provider_state = {}
        self.failed = {}  # call_id -> (ts, set of deployment ids)
        self.sessions = {}  # session hash -> (deployment id, ts)
        self.pulls = set()
        self.openrouter_remaining = None
        self.pending_alerts = []
        self._diverted = {}  # call id -> ts
        self._own_pulls = set()  # pulls this plugin made that may not have reached the file yet
        self.openrouter_day = None
        self._last_alert = {}
        self._busy = collections.defaultdict(collections.deque)
        self._checked = False
        self._broken = None
        self._load()
        if start:
            store.on_flush = self.reload_pulls
            store.start()
            atexit.register(store.close)
            if os.environ.get("OPENROUTER_API_KEY"):
                threading.Thread(target=self._poll_openrouter, name="charmer-openrouter", daemon=True).start()

    # ---- state from the file -----------------------------------------------------------------------------------
    def _load(self):
        now = self.clock()
        try:
            since = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(days=2)).date().isoformat()
            for account, model, day, requests, prompt, completion, neurons, eval_requests in self.store.usage_days(since):
                if day == self._day(account, now):
                    self.day[(account, model, day)] = {"requests": requests, "tokens": prompt + completion,
                                                       "neurons": neurons, "eval_requests": eval_requests}
            for account, model, minute, requests, prompt, completion in self.store.usage_minutes(st._minute(now - 60)):
                ts = dt.datetime.strptime(minute, "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc).timestamp()
                self.events[(account, model)].append([ts, requests, prompt + completion])
            self.pulls = self.store.open_pulls()
        except Exception as e:  # noqa: BLE001 - a lost file is a nuisance, not a disaster
            print(f"charmer: could not load state, starting from zero: {e}", flush=True)

    def reload_pulls(self):
        try:
            from_file = self.store.open_pulls()
            self._own_pulls -= from_file
            self.pulls = from_file | self._own_pulls
        except Exception as e:  # noqa: BLE001 - keep the last good set (fail closed)
            self.alert("plugin_error", f"could not reload pulls, keeping the last good set: {e}")

    # ---- helpers -----------------------------------------------------------------------------------------------
    def _acct(self, account):
        return self.accounts.get(account) or {}

    def _day(self, account, now):
        return day_key(self._acct(account).get("day_tz"), now)

    def _window(self, key, now):
        q = self.events[key]
        while q and q[0][0] < now - 60:
            q.popleft()
        return q

    def minute_count(self, account, model, kind):
        q = self._window((account, model or "*"), self.clock())
        return sum(e[1] if kind == "requests" else e[2] for e in q)

    def day_count(self, account, model, kind):
        return self.day[(account, model or "*", self._day(account, self.clock()))][kind]

    def key_minute(self, key, group):
        return self.key_counts[(key, group, st._minute(self.clock()))]

    def is_known_group(self, group):
        return group in self.known_groups

    def is_empty_group(self, group):
        return group in self.known_groups and self.known_groups[group] == 0

    def alert(self, kind, message):
        now = self.clock()
        if now - self._last_alert.get(kind, -1e18) < ALERT_EVERY_S:
            return
        self._last_alert[kind] = now
        self.pending_alerts.append({"ts": now, "kind": kind, "message": message})
        self.store.add_alert(kind, message, now)
        st.webhook(kind, message)

    def pull(self, target, reason):
        self.pulls.add(target)
        self._own_pulls.add(target)
        self.store.add_pull(target, reason, "plugin")

    # ---- recording ---------------------------------------------------------------------------------------------
    def record_dispatch(self, call_id, deployment, group, est_tokens, session=None, text=None):
        now = self.clock()
        mid, info = _id(deployment), deployment["model_info"]
        account, model = info.get("account", ""), deployment["litellm_params"].get("model", "")
        is_eval = str(group or "").startswith("pair-")
        with self.lock:
            evs = [[now, 1, est_tokens], [now, 1, est_tokens]]
            self.events[(account, "*")].append(evs[0])
            self.events[(account, model)].append(evs[1])
            self.inflight[(call_id, mid)] = {"events": evs, "account": account, "model": model, "session": session, "ts": now}
            today = self._day(account, now)
            for m in ("*", model):
                self.day[(account, m, today)]["requests"] += 1
                if is_eval:
                    self.day[(account, m, today)]["eval_requests"] += 1
                self.store.add("usage", account, m, st._minute(now), today, 1, 0, 0, 0, int(is_eval))
            self._near_limit(account, today)

    def record_result(self, call_id, model_id, account, model, group, prompt, completion, headers, status, key,
                      session, latency_ms, neurons=None):
        now = self.clock()
        headers = headers or {}
        minute = st._minute(now)
        with self.lock:
            if not model_id:  # router-local answer: no provider was involved; busy is counted by the failure hook
                self.store.add("recent", now, key, group, "", None, status, latency_ms, 0, False)
                return
            fly = self.inflight.pop((call_id, model_id), None)
            if fly is None:
                self.record_dispatch(call_id, {"model_info": {"id": model_id, "account": account or ""},
                                               "litellm_params": {"model": model or ""}}, group, 0, session)
                fly = self.inflight.pop((call_id, model_id))
            account, model = account or fly["account"], model or fly["model"]
            session = session or fly["session"]
            tokens = int(prompt or 0) + int(completion or 0)
            for ev in fly["events"]:
                ev[2] = tokens
            n = self._neurons(account, headers, neurons, tokens)
            today = self._day(account, now)
            for m in ("*", model):
                self.day[(account, m, today)]["tokens"] += tokens
                self.day[(account, m, today)]["neurons"] += n
                self.store.add("usage", account, m, minute, today, 0, int(prompt or 0), int(completion or 0), n, 0)
            self._provider_headers(model_id, headers, status, now)
            if status >= 400:
                ts, ids = self.failed.get(call_id, (now, set()))
                ids.add(model_id)
                self.failed[call_id] = (ts, ids)
                if status in (401, 403):
                    self.alert("provider_auth", f"{model_id} ({account}) answered {status}: check that provider's key")
            diverted = self._diverted.pop(call_id, None) is not None
            if session and status < 400:
                self.sessions[_hash(f"{group}|{session}")] = (model_id, now)
            counts = self.key_counts[(key, group, minute)]
            counts["requests"] += 1
            counts["tokens"] += tokens
            self.store.add("key_usage", key or "", group or "", minute, 1, tokens, 0)
            self.store.add("recent", now, key, group, model_id, _hash(session) if session else None, status,
                           latency_ms, tokens, diverted)
            self._near_limit(account, today)
            self._gc(now)

    def _neurons(self, account, headers, given, tokens):
        raw = headers.get("llm_provider-cf-ai-neurons") or headers.get("cf-ai-neurons") or given
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        rate = self._acct(account).get("neurons_per_1k_tokens")
        return tokens / 1000 * rate if rate else 0.0

    def _provider_headers(self, model_id, headers, status, now):
        def first(*names):
            for n in names:
                if headers.get(n) is not None:
                    return headers[n]
            return None
        rem_req = first("x_ratelimit_remaining_requests", "x-ratelimit-remaining-requests",
                        "llm_provider-x-ratelimit-remaining-requests", "llm_provider-x-ratelimit-remaining-req-minute",
                        "x-ratelimit-remaining-req-minute")  # the last two: Mistral's names
        rem_tok = first("x_ratelimit_remaining_tokens", "x-ratelimit-remaining-tokens",
                        "llm_provider-x-ratelimit-remaining-tokens", "llm_provider-x-ratelimit-remaining-tokens-minute",
                        "x-ratelimit-remaining-tokens-minute")
        reset = parse_reset(first("llm_provider-x-ratelimit-reset-requests", "x_ratelimit_reset_requests",
                                  "x-ratelimit-reset-requests"), now)
        retry_after = parse_reset(first("retry-after", "llm_provider-retry-after"), now)
        if reset and retry_after:
            reset = max(reset, retry_after)  # on a 429 the provider's longer wait wins
        rem_req, rem_tok = _number(rem_req), _number(rem_tok)  # an odd header is ignored, never raised
        if rem_req is None and rem_tok is None and status != 429:
            return
        state = {"remaining_requests": rem_req if rem_req is not None else (0 if status == 429 else None),
                 "remaining_tokens": rem_tok,
                 "reset_at": reset or retry_after or now + 60, "seen": now, "last_429": now if status == 429 else None}
        self.provider_state[model_id] = state
        self.store.add("provider_state", model_id, state, now)

    def _near_limit(self, account, today):
        acct = self._acct(account)
        used = self.day[(account, "*", today)]
        for kind, limit in (("requests", acct.get("day_requests")), ("tokens", acct.get("day_tokens")),
                            ("neurons", acct.get("day_neurons"))):
            if limit and used[kind] >= NEAR_LIMIT * limit:
                self.alert(f"near_limit:{account}", f"{account} has used {used[kind]:.0f} of {limit} {kind} today")

    def _gc(self, now):
        for k in [k for k, (ts, _) in self.failed.items() if ts < now - FAILED_TTL_S]:
            del self.failed[k]
        for k in [k for k, (_, ts) in self.sessions.items() if ts < now - SESSION_TTL_S]:
            del self.sessions[k]
        for k in [k for k, v in self.inflight.items() if v["ts"] < now - FAILED_TTL_S]:
            del self.inflight[k]  # a call with no result event
        for k in [k for k, ts in self._diverted.items() if ts < now - FAILED_TTL_S]:
            del self._diverted[k]
        old = st._minute(now - 180)
        for k in [k for k in self.key_counts if k[2] < old]:
            del self.key_counts[k]

    # ---- filtering ---------------------------------------------------------------------------------------------
    def _pulled(self, d):
        mid = _id(d)
        base = mid[5:] if mid.startswith("pair-") else mid
        return bool({mid, base, f"pair-{base}", f"{d['model_info'].get('account', '')}/"} & self.pulls)

    def _hard_ok(self, d, group, now):
        if self._pulled(d):
            return False
        info, model = d["model_info"], d["litellm_params"].get("model", "")
        account = info.get("account", "")
        acct = self._acct(account)
        margin = acct.get("margin") or 0
        today = self._day(account, now)
        used = self.day[(account, "*", today)]
        used_model = self.day[(account, model, today)]
        mlim = self.models.get(model) or {}
        if acct.get("day_requests") and used["requests"] >= acct["day_requests"] - margin:
            return False
        if acct.get("day_tokens") and used["tokens"] >= acct["day_tokens"] - margin:
            return False
        stop = acct.get("embed_stop_at") if info.get("mode") == "embedding" else acct.get("chat_stop_at")
        if stop is not None and used["neurons"] >= stop:
            return False
        if acct.get("day_neurons") and used["neurons"] >= acct["day_neurons"]:
            return False
        if mlim.get("day_requests") and used_model["requests"] >= mlim["day_requests"] - margin:
            return False
        if mlim.get("day_tokens") and used_model["tokens"] >= mlim["day_tokens"] - margin:
            return False
        if str(group or "").startswith("pair-") and acct.get("eval_day_requests") is not None \
                and used["eval_requests"] >= acct["eval_day_requests"]:
            return False
        if account == "openrouter" and self.openrouter_remaining is not None and self.openrouter_remaining <= margin \
                and self.openrouter_day == day_key("UTC", now):  # a reading from before midnight UTC no longer binds
            return False
        return True

    def _soft_ok(self, d, call_id, est_tokens, now):
        mid = _id(d)
        if mid in (self.failed.get(call_id) or (0, set()))[1]:
            return False
        model = d["litellm_params"].get("model", "")
        account = d["model_info"].get("account", "")
        for key, lim in (((account, "*"), self._acct(account)), ((account, model), self.models.get(model) or {})):
            q = self._window(key, now)
            if lim.get("minute_requests") and sum(e[1] for e in q) >= lim["minute_requests"]:
                return False
            if lim.get("minute_tokens") and sum(e[2] for e in q) + est_tokens > lim["minute_tokens"]:
                return False
        state = self.provider_state.get(mid)
        if state and now < state["reset_at"]:
            margin = self._acct(account).get("margin") or 0
            if state["remaining_requests"] is not None and state["remaining_requests"] <= margin:
                return False
            if state["remaining_tokens"] is not None and state["remaining_tokens"] < est_tokens:
                return False
        return True

    def filter(self, group, deployments, call_id, session, est_tokens):
        if not deployments:
            return []
        now = self.clock()
        with self.lock:
            hard = [d for d in deployments if self._hard_ok(d, group, now)]
            out = [d for d in hard if self._soft_ok(d, call_id, est_tokens, now)] or hard
            if not session or not out:
                return out
            sh = _hash(f"{group}|{session}")
            last = self.sessions.get(sh)
            if last and any(_id(d) == last[0] for d in out):
                return [d for d in out if _id(d) == last[0]]
            lowest = min(_order(d) for d in out)
            pick = rendezvous(sh, [d for d in out if _order(d) == lowest])
            if last:
                self._diverted[call_id] = now
            self.sessions[sh] = (_id(pick), now)
            return [pick]

    def safe_filter(self, group, deployments, call_id, session, est_tokens):
        try:
            return self.filter(group, deployments, call_id, session, est_tokens)
        except Exception as e:  # noqa: BLE001 - hard filters still apply; a hard check that fails drops its pair
            self.alert("plugin_error", f"filter failed, serving with hard filters only: {e}")
            now, kept = self.clock(), []
            for d in deployments:
                try:
                    if self._hard_ok(d, group, now):
                        kept.append(d)
                except Exception:  # noqa: BLE001 - fail closed for this deployment
                    pass
            return kept

    # ---- OpenRouter key check ------------------------------------------------------------------------
    def apply_openrouter_reading(self, reading):
        if not isinstance(reading, dict):
            self.alert("openrouter_key:check_failed", "OpenRouter key check failed (no answer)")
            return
        self.store.add("openrouter", reading, self.clock())  # free-check reads the latest, good or bad
        fmdr = reading.get("free_model_daily_requests")
        money = ("usage" not in reading or (reading.get("usage") or 0) > 0 or reading.get("limit") != 1
                 or reading.get("limit_reset") is not None or not isinstance(fmdr, dict))
        if money:
            self.pull("openrouter/", "OpenRouter key: money may be moving (usage, cap or reset)")
            self.alert("openrouter_key:money", f"OpenRouter key check failed: usage={reading.get('usage')} "
                                         f"limit={reading.get('limit')} limit_reset={reading.get('limit_reset')}; pulled openrouter/")
            return
        self.openrouter_remaining = _number(fmdr.get("remaining"))
        self.openrouter_day = day_key("UTC", self.clock())
        if (fmdr.get("limit") or 0) < 1000:
            self.alert("openrouter_key:credit", f"OpenRouter free daily limit is {fmdr.get('limit')}: the credit no longer lifts it")
        elif self.openrouter_remaining is not None and self.openrouter_remaining < OPENROUTER_ALERT_REMAINING:
            self.alert("openrouter_key:remaining", f"OpenRouter has {self.openrouter_remaining} free requests left today")

    def _poll_openrouter(self):
        url = os.environ.get("OPENROUTER_KEY_URL", "https://openrouter.ai/api/v1/key")
        every = float(os.environ.get("CHARMER_OR_POLL_SECONDS", "300"))
        while True:
            try:
                req = urllib.request.Request(url, headers={"authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    reading = (json.loads(r.read()) or {}).get("data")
            except Exception:  # noqa: BLE001 - a failed poll is an alert, not a pull
                reading = None
            try:
                with self.lock:
                    self.apply_openrouter_reading(reading)
            except Exception as e:  # noqa: BLE001 - the poll must never die
                print(f"charmer: OpenRouter check failed to apply: {e}", flush=True)
            time.sleep(every)

    # ---- LiteLLM hooks -----------------------------------------------------------------------------------------
    def _start_check(self):
        if self._checked:
            return
        self._checked = True
        import litellm
        try:
            check_start(litellm.callbacks, self, sys.argv)
        except StartError as e:
            self._broken = str(e)
            self.alert("plugin_error", self._broken)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        self._start_check()
        if self._broken:
            raise HTTPException(status_code=503, detail=self._broken)
        psr = data.get("proxy_server_request") or {}
        body = psr.get("body")
        try:
            check_request(body if isinstance(body, dict) else data, data, psr.get("credential_fields"))
        except BadBody as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if str(call_type).split(".")[-1] in ("completion", "acompletion", "embeddings", "aembedding") \
                and not self.is_known_group(data.get("model")):
            # only group names route; a deployment's own model name would skip the plugin's filter
            raise HTTPException(status_code=400, detail=f"charmer: {data.get('model')!r} is not a group this proxy serves")
        if self.is_empty_group(data.get("model")):
            raise HTTPException(status_code=429, detail=f"No deployments available for selected model, "
                                                         f"{data.get('model')} has no member yet (charmer)")
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        info = kwargs.get("model_info") or {}
        if kwargs.get("messages"):
            kwargs["messages"] = fit_history(kwargs["messages"], info.get("account", ""), kwargs.get("model", ""))
        if info.get("id"):
            md = kwargs.get("metadata") or {}
            self.record_dispatch(kwargs.get("litellm_call_id"),
                                 {"model_info": info, "litellm_params": {"model": kwargs.get("model", "")}},
                                 md.get("model_group"), _estimate(kwargs.get("messages") or kwargs.get("input")),
                                 session=md.get("session_id") or kwargs.get("litellm_session_id"))
        return kwargs

    async def async_filter_deployments(self, model, healthy_deployments, messages, request_kwargs=None, parent_otel_span=None):
        if isinstance(healthy_deployments, dict):  # one named deployment: the hard stops still apply
            if not self._hard_ok(healthy_deployments, model, self.clock()):
                import litellm
                raise litellm.RateLimitError(message=f"No deployments available for selected model, {model} is stopped (charmer)",
                                             llm_provider="", model=model)
            return healthy_deployments
        if not isinstance(healthy_deployments, list):
            return healthy_deployments
        rk = request_kwargs or {}
        md = rk.get("metadata") or rk.get("litellm_metadata") or {}
        session = md.get("session_id") or rk.get("litellm_session_id")
        return self.safe_filter(model, healthy_deployments, rk.get("litellm_call_id"), session,
                                _estimate(messages or rk.get("input")))

    def _result(self, kwargs, start, end, status):
        slo = kwargs.get("standard_logging_object") or {}
        md = slo.get("metadata") or {}
        try:
            latency = int((end - start).total_seconds() * 1000)
        except Exception:  # noqa: BLE001
            latency = 0
        self.record_result(kwargs.get("litellm_call_id"), slo.get("model_id") or "", None, None, slo.get("model_group"),
                           slo.get("prompt_tokens") or 0, slo.get("completion_tokens") or 0,
                           (slo.get("hidden_params") or {}).get("additional_headers") or {}, status,
                           md.get("user_api_key_alias"), None, latency)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._result(kwargs, start_time, end_time, 200)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        status = getattr(kwargs.get("exception"), "status_code", None) or 500
        self._result(kwargs, start_time, end_time, int(status))

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        status = getattr(original_exception, "status_code", None) or getattr(original_exception, "code", None)
        if str(status) != "429":
            return
        text = str(getattr(original_exception, "message", "") or original_exception)
        dry = "Rate limit exceeded for api_key" not in text  # a key's own throttle is not a dry pool
        group = request_data.get("model")
        now = self.clock()
        with self.lock:
            q = self._busy[group]
            if dry:
                q.append(now)
            while q and q[0] < now - 300:
                q.popleft()
            if dry and len(q) > 5:
                self.alert(f"pool_dry:{group}", f"{group} answered busy {len(q)} times in 5 minutes")
            key = getattr(user_api_key_dict, "key_alias", None) or ""
            self.key_counts[(key, group, st._minute(now))]["r429"] += 1
            self.store.add("key_usage", key, group or "", st._minute(now), 0, 0, 1)


def _yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def _from_env():
    limits = _yaml(os.environ.get("CHARMER_LIMITS", "/app/conf/limits.yaml"))
    config = _yaml(os.environ.get("CHARMER_CONFIG", "/app/conf/config.yaml"))
    rendered = _yaml(os.environ.get("CHARMER_RENDERED", "/app/rendered/config.yaml"))
    counts = collections.Counter(d["model_name"] for d in rendered.get("model_list") or [])
    groups = {d["model_name"] for d in config.get("model_list") or []} | set(limits.get("groups") or [])  # groups that exist before they have a member
    known = {g: counts.get(g, 0) for g in groups}
    return Charmer(limits, known, st.Store(os.environ.get("CHARMER_DB", "/data/charmer.sqlite")),
                   start=os.environ.get("CHARMER_NO_THREADS") != "1")


instance = _from_env()
