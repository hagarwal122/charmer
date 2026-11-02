"""Plugin unit cells PLG-01..PLG-16. No proxy, no network. Runs in the LiteLLM image."""
import datetime as dt
import os
import time
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.setdefault("CHARMER_DB", os.path.join(TMP, "import.sqlite"))
os.environ.setdefault("CHARMER_NO_THREADS", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))

import charmer_plugin as cp  # noqa: E402
import store as st  # noqa: E402

LIMITS = {
    "accounts": {
        "stub": {"margin": 0, "day_tz": "UTC"},
        "hdr": {"margin": 3, "day_tz": "UTC"},
        "acct-x": {"day_requests": 4, "day_tz": "UTC"},
        "openrouter": {"day_requests": 1000, "eval_day_requests": 3, "margin": 20, "day_tz": "UTC"},
        "cloudflare": {"day_neurons": 10000, "chat_stop_at": 1000, "embed_stop_at": 9500, "neurons_per_1k_tokens": 27, "day_tz": "UTC"},
        "gemini": {"day_tz": "America/Los_Angeles", "day_requests": 500},
        "groq": {"minute_tokens": 8000, "day_tz": "UTC"},
    },
    "models": {
        "openai/ok-a@rpm-a": {"minute_requests": 2},
        "openai/ok-a@d1": {"day_requests": 3},
    },
}
T0 = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def dep(mid, model, account, order=1, mode=None):
    info = {"id": mid, "account": account}
    if mode:
        info["mode"] = mode
    return {"model_name": "g", "litellm_params": {"model": model, "order": order}, "model_info": info}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def plugin(path=None, clock=None, groups=None):
    store = st.Store(path or os.path.join(tempfile.mkdtemp(), "c.sqlite"))
    return cp.Charmer(limits=LIMITS, known_groups=groups or {"g": 2, "g-empty": 0}, store=store, clock=clock or Clock(), start=False)


def ids(deps):
    return [d["model_info"]["id"] for d in deps]


def run(p, deployment, call="c", group="g", tokens=(10, 5), headers=None, status=200, key="smoke", neurons=None):
    p.record_dispatch(call, deployment, group, est_tokens=tokens[0])
    p.record_result(call, deployment["model_info"]["id"], deployment["model_info"]["account"], deployment["litellm_params"]["model"],
                    group, prompt=tokens[0], completion=tokens[1], headers=headers or {}, status=status, key=key,
                    session=None, latency_ms=10, neurons=neurons)


class Counters(unittest.TestCase):
    def test_plg01_counts_by_account_model_minute_day_and_key(self):
        """PLG-01: events add requests and tokens to the right account, model, minute, day and key."""
        p = plugin()
        d = dep("x1", "openai/ok-a@x1", "acct-x")
        run(p, d)
        run(p, d, call="c2")
        self.assertEqual(p.day_count("acct-x", None, "requests"), 2)
        self.assertEqual(p.day_count("acct-x", "openai/ok-a@x1", "tokens"), 30)
        self.assertEqual(p.minute_count("acct-x", "openai/ok-a@x1", "requests"), 2)
        self.assertEqual(p.key_minute("smoke", "g")["requests"], 2)

    def test_plg01_router_local_failure_counts_nothing(self):
        """PLG-01: a router-local 429 (empty model_id) adds nothing to any provider's counts."""
        p = plugin()
        p.record_result("c", "", "", "", "g", prompt=0, completion=0, headers={}, status=429, key="smoke", session=None, latency_ms=1)
        self.assertEqual([k for k, v in p.day.items() if v["requests"] or v["tokens"]], [])
        self.assertEqual([k for k, q in p.events.items() if q], [])


class Headers(unittest.TestCase):
    def test_plg02_parses_remaining_and_reset_and_expires(self):
        """PLG-02: typed remaining fields, raw reset values ("60s", "1m30s", an epoch) and retry-after; a reading expires."""
        self.assertEqual(cp.parse_reset("60s", T0), T0 + 60)
        self.assertEqual(cp.parse_reset("1m30s", T0), T0 + 90)
        self.assertEqual(cp.parse_reset("1.5s", T0), T0 + 1.5)
        self.assertEqual(cp.parse_reset(str(int(T0 + 100)), T0), T0 + 100)
        self.assertIsNone(cp.parse_reset("garbage", T0))
        clock = Clock()
        p = plugin(clock=clock)
        h1 = dep("h1", "openai/hdr-a@h1", "hdr")
        run(p, h1, headers={"x_ratelimit_remaining_requests": 2, "llm_provider-x-ratelimit-reset-requests": "60s"})
        self.assertEqual(p.provider_state["h1"]["remaining_requests"], 2)
        self.assertEqual(ids(p.filter("g", [h1, dep("h2", "openai/hdr-b@h2", "hdr")], "n1", None, 10)), ["h2"])
        clock.t += 61
        self.assertEqual(len(p.filter("g", [h1, dep("h2", "openai/hdr-b@h2", "hdr")], "n2", None, 10)), 2)


class Filter(unittest.TestCase):
    def test_plg03_hard_limits_may_empty_soft_never(self):
        """PLG-03: day limits and pulls are hard (may empty the list); minute limits are soft (never empty it)."""
        p = plugin()
        d1 = dep("d1", "openai/ok-a@d1", "stub")
        for i in range(3):
            run(p, d1, call=f"c{i}")
        self.assertEqual(p.filter("g", [d1], "n", None, 10), [], "a spent day limit is hard")
        a = dep("rpm-a", "openai/ok-a@rpm-a", "stub")
        for i in range(2):
            run(p, a, call=f"r{i}")
        self.assertEqual(ids(p.filter("g", [a], "n2", None, 10)), ["rpm-a"], "a spent minute limit alone never empties")
        b = dep("rpm-b", "openai/ok-b@rpm-b", "stub")
        self.assertEqual(ids(p.filter("g", [a, b], "n3", None, 10)), ["rpm-b"])
        p.pulls = {"rpm-b"}
        self.assertEqual(p.filter("g", [b], "n4", None, 10), [], "a pull is hard")

    def test_plg03_account_day_cap_and_eval_share(self):
        """PLG-03: an account's day cap counts both twins; pair-* calls stop at the eval share, prod continues."""
        p = plugin()
        x1, px1 = dep("x1", "openai/ok-a@x1", "acct-x"), dep("pair-x1", "openai/ok-a@x1", "acct-x")
        for i in range(2):
            run(p, x1, call=f"a{i}")
            run(p, px1, call=f"b{i}", group="pair-x1")
        self.assertEqual(p.filter("g", [x1], "n", None, 10), [])
        q = plugin()
        o1 = dep("pair-or1", "openai/ok-a@or1:free", "openrouter")
        for i in range(3):
            run(q, o1, call=f"e{i}", group="pair-or1")
        self.assertEqual(q.filter("pair-or1", [o1], "n", None, 10), [], "the eval share is spent")
        self.assertEqual(ids(q.filter("g-or", [dep("or1", "openai/ok-a@or1:free", "openrouter")], "n2", None, 10)), ["or1"])

    def test_plg03_neuron_stops(self):
        """PLG-03: Cloudflare chat stops at 7,000-style chat_stop_at; embeddings run until embed_stop_at."""
        p = plugin()
        cf = dep("cf1", "openai/cf-chat@cf1", "cloudflare")
        emb = dep("emb1", "openai/emb@emb1", "cloudflare", mode="embedding")
        for i in range(5):
            run(p, cf, call=f"c{i}", neurons=200)
        self.assertEqual(p.filter("g", [cf], "n", None, 10), [])
        self.assertEqual(ids(p.filter("embed", [emb], "n2", None, 10)), ["emb1"])

    def test_plg03_neurons_estimated_when_absent(self):
        """no neuron header means an estimate from tokens."""
        p = plugin()
        run(p, dep("cf1", "openai/cf-chat@cf1", "cloudflare"), tokens=(1000, 0))
        self.assertEqual(p.day_count("cloudflare", None, "neurons"), 27)

    def test_plg03_failed_this_call_is_skipped_softly(self):
        """a deployment that failed this call is skipped on its retry; never empties the list."""
        p = plugin()
        s1, ok = dep("slow1", "openai/slow@slow1", "stub"), dep("slow-ok", "openai/ok-a@slow-ok", "stub")
        p.record_dispatch("call-1", s1, "g", est_tokens=5)
        p.record_result("call-1", "slow1", "stub", "openai/slow@slow1", "g", prompt=0, completion=0, headers={}, status=408,
                        key="smoke", session=None, latency_ms=4000)
        self.assertEqual(ids(p.filter("g", [s1, ok], "call-1", None, 5)), ["slow-ok"])
        self.assertEqual(ids(p.filter("g", [s1], "call-1", None, 5)), ["slow1"])
        self.assertEqual(len(p.filter("g", [s1, ok], "other-call", None, 5)), 2)

    def test_plg13_in_flight_calls_count(self):
        """PLG-13: calls dispatched but not finished count against minute limits; completion corrects the estimate."""
        p = plugin()
        a, b = dep("rpm-a", "openai/ok-a@rpm-a", "stub"), dep("rpm-b", "openai/ok-b@rpm-b", "stub")
        p.record_dispatch("f1", a, "g", est_tokens=100)
        p.record_dispatch("f2", a, "g", est_tokens=100)
        self.assertEqual(ids(p.filter("g", [a, b], "f3", None, 100)), ["rpm-b"])
        p.record_result("f1", "rpm-a", "stub", "openai/ok-a@rpm-a", "g", prompt=7, completion=3, headers={}, status=200,
                        key="smoke", session=None, latency_ms=5)
        self.assertEqual(p.minute_count("stub", "openai/ok-a@rpm-a", "tokens"), 110)

    def test_plg13_rolling_minute(self):
        """PLG-13: the minute window rolls: 61 s later the spent minute limit is free again."""
        clock = Clock()
        p = plugin(clock=clock)
        a, b = dep("rpm-a", "openai/ok-a@rpm-a", "stub"), dep("rpm-b", "openai/ok-b@rpm-b", "stub")
        run(p, a, call="1")
        clock.t += 30
        run(p, a, call="2")
        self.assertEqual(ids(p.filter("g", [a, b], "n", None, 1)), ["rpm-b"])
        clock.t += 31
        self.assertEqual(len(p.filter("g", [a, b], "n2", None, 1)), 2)

    def test_plg14_hard_caps_survive_errors(self):
        """PLG-08, PLG-14: when a soft check throws, day caps still apply; when a hard check throws for a
        deployment, that deployment is dropped (fail closed); one alert, not one per request."""
        p = plugin()
        d1, ok = dep("d1", "openai/ok-a@d1", "stub"), dep("ok", "openai/ok-a@ok", "stub")
        for i in range(3):
            run(p, d1, call=f"c{i}")
        with mock.patch.object(p, "_soft_ok", side_effect=RuntimeError("boom")):
            self.assertEqual(ids(p.safe_filter("g", [d1, ok], "n1", None, 1)), ["ok"], "d1's spent day cap still applies")
        real = p._hard_ok
        with mock.patch.object(p, "_hard_ok", side_effect=lambda d, g, n: (_ for _ in ()).throw(TypeError("x")) if d["model_info"]["id"] == "ok" else real(d, g, n)):
            self.assertEqual(p.safe_filter("g", [d1, ok], "n2", None, 1), [], "a hard check that throws drops its deployment")
        self.assertEqual(len([a for a in p.pending_alerts if a["kind"] == "plugin_error"]), 1)

    def test_blank_margin_is_zero(self):
        """Audit 2: a blank margin in limits.yaml is 0, not a TypeError on every request."""
        limits = {"accounts": {"stub": {"margin": None, "day_requests": 3, "day_tz": "UTC"}}, "models": {}}
        p = cp.Charmer(limits=limits, known_groups={}, store=st.Store(os.path.join(tempfile.mkdtemp(), "c.sqlite")), clock=Clock(), start=False)
        d = dep("x", "openai/ok-a@x", "stub")
        self.assertEqual(ids(p.filter("g", [d], "n", None, 1)), ["x"])

    def test_plg14_empty_input_and_errors(self):
        """PLG-14: empty input returns empty; a filter error returns the input minus hard filters."""
        p = plugin()
        self.assertEqual(p.filter("g", [], "n", None, 1), [])
        p.pulls = {"bad"}
        good, bad = dep("good", "openai/ok-a@good", "stub"), dep("bad", "openai/ok-a@bad", "stub")
        with mock.patch.object(p, "_soft_ok", side_effect=RuntimeError("boom")):
            self.assertEqual(ids(p.safe_filter("g", [good, bad], "n2", None, 1)), ["good"])
            self.assertEqual(ids(p.safe_filter("g", [good, bad], "n3", None, 1)), ["good"])
        self.assertEqual(len([a for a in p.pending_alerts if a["kind"] == "plugin_error"]), 1, "one alert, not one per request")


class Sessions(unittest.TestCase):
    def test_plg04_deterministic_and_minimal_moves(self):
        """PLG-04: same session, same list, same pick; removing one deployment moves only its sessions; no move back."""
        deps = [dep(f"m{i}", f"openai/ok-a@m{i}", "stub") for i in range(4)]
        picks = {s: cp.rendezvous(s, deps)["model_info"]["id"] for s in (f"s{i}" for i in range(200))}
        for s, mid in picks.items():
            self.assertEqual(cp.rendezvous(s, list(reversed(deps)))["model_info"]["id"], mid)
        gone = [d for d in deps if d["model_info"]["id"] != "m0"]
        moved = [s for s, mid in picks.items() if cp.rendezvous(s, gone)["model_info"]["id"] != mid]
        self.assertTrue(all(picks[s] == "m0" for s in moved))
        p = plugin()
        first = ids(p.filter("g", deps, "c1", "sess", 1))
        self.assertEqual(len(first), 1)
        p.record_result("c1", first[0], "stub", "x", "g", prompt=1, completion=1, headers={}, status=200, key="k", session="sess", latency_ms=1)
        others = [d for d in deps if d["model_info"]["id"] != first[0]]
        second = ids(p.filter("g", others, "c2", "sess", 1))
        p.record_result("c2", second[0], "stub", "x", "g", prompt=1, completion=1, headers={}, status=200, key="k", session="sess", latency_ms=1)
        self.assertEqual(ids(p.filter("g", deps, "c3", "sess", 1)), second, "a session is not moved back")

    def test_plg04_lowest_order_first(self):
        """PLG-04: the session pick is among the lowest order present (Groq-style order 2 only when needed)."""
        q1, q2 = dep("q1", "openai/ok-a@q1", "stub"), dep("q2", "openai/ok-b@q2", "stub", order=2)
        for i in range(30):
            self.assertEqual(ids(plugin().filter("g", [q1, q2], "c", f"s{i}", 1)), ["q1"])
        self.assertEqual(ids(plugin().filter("g", [q2], "c", "s", 1)), ["q2"])


class Days(unittest.TestCase):
    def test_plg05_day_boundaries_follow_day_tz(self):
        """PLG-05: UTC accounts roll at 00:00 UTC; Gemini at midnight Los Angeles, across daylight saving."""
        self.assertEqual(cp.day_key("UTC", dt.datetime(2026, 10, 28, 23, 59, tzinfo=dt.timezone.utc).timestamp()), "2026-10-28")
        self.assertEqual(cp.day_key("UTC", dt.datetime(2026, 10, 29, 0, 0, tzinfo=dt.timezone.utc).timestamp()), "2026-10-29")
        la = "America/Los_Angeles"
        # PDT (UTC-7) before 1 Nov 2026, PST (UTC-8) after
        self.assertEqual(cp.day_key(la, dt.datetime(2026, 10, 29, 6, 59, tzinfo=dt.timezone.utc).timestamp()), "2026-10-28")
        self.assertEqual(cp.day_key(la, dt.datetime(2026, 10, 29, 7, 0, tzinfo=dt.timezone.utc).timestamp()), "2026-10-29")
        self.assertEqual(cp.day_key(la, dt.datetime(2026, 11, 2, 7, 59, tzinfo=dt.timezone.utc).timestamp()), "2026-11-01")
        self.assertEqual(cp.day_key(la, dt.datetime(2026, 11, 2, 8, 0, tzinfo=dt.timezone.utc).timestamp()), "2026-11-02")


class StoreCells(unittest.TestCase):
    def test_plg06_no_file_io_on_the_calling_thread(self):
        """PLG-06: recording does no file I/O on the calling thread; a flush writes it; closing flushes."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        p = cp.Charmer(limits=LIMITS, known_groups={"g": 1}, store=s, clock=Clock(), start=False)
        caller = threading.get_ident()
        real = sqlite3.connect

        def guarded(*a, **k):
            if threading.get_ident() == caller:
                raise AssertionError("file I/O on the calling thread")
            return real(*a, **k)

        with mock.patch("store.sqlite3.connect", guarded):
            for i in range(200):
                run(p, dep("x1", "openai/ok-a@x1", "stub"), call=f"c{i}")
            t = threading.Thread(target=s.flush)
            t.start()
            t.join()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select sum(requests) from usage_day where account='stub' and model='*'").fetchone()[0], 200)

    def test_plg07_restart_reloads_today_and_pulls(self):
        """PLG-07: a new instance on the same file starts with today's counts and the open pulls."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        p = plugin(path)
        for i in range(3):
            run(p, dep("d1", "openai/ok-a@d1", "stub"), call=f"c{i}")
        p.store.add_pull("x9", "test", "owner")
        p.store.flush()
        p.store.close()
        q = plugin(path)
        self.assertEqual(q.day_count("stub", "openai/ok-a@d1", "requests"), 3)
        self.assertIn("x9", q.pulls)
        self.assertEqual(q.filter("g", [dep("d1", "openai/ok-a@d1", "stub")], "n", None, 1), [])

    def test_plg12_concurrent_writer(self):
        """PLG-12: another process holding a write lock delays, not loses, the flush; a failed reload keeps the last good pulls."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        p = plugin(path)
        p.store.add_pull("keep-me", "test", "owner")
        p.store.flush()
        p.reload_pulls()
        other = sqlite3.connect(path, timeout=10, check_same_thread=False)
        other.execute("begin immediate")
        t = threading.Timer(1.5, other.commit)
        t.start()
        run(p, dep("x1", "openai/ok-a@x1", "stub"))
        p.store.flush()
        t.join()
        other.close()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select sum(requests) from usage_day where model='*'").fetchone()[0], 1)
        with mock.patch.object(p.store, "open_pulls", side_effect=sqlite3.OperationalError("locked")):
            p.reload_pulls()
        self.assertIn("keep-me", p.pulls)

    def test_guard_pull_is_not_swallowed_by_an_owner_pull(self):
        """Audit 3: an owner pull and a guard pull on one target are two rows; lifting as the dashboard leaves the
        guard's; only the guard's own lift (or the owner's CLI) removes it."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        s.add_pull("gemini/", "owner test", "owner")
        s.add_pull("gemini/", "402", "guard")
        s.flush()
        self.assertEqual(sorted(w for (w,) in s.query("select who from pulls where target='gemini/' and lifted is null")), ["guard", "owner"])
        s.add("unpull", "gemini/", 1.0, ["owner", "dashboard"])
        s.flush()
        self.assertEqual(s.open_pulls(), {"gemini/"})
        s.add("unpull", "gemini/", 2.0, None)
        s.flush()
        self.assertEqual(s.open_pulls(), set())

    def test_plg10_no_text_and_no_key_in_the_store(self):
        """PLG-10: no prompt or completion text and no key reaches the file."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        p = plugin(path)
        canary = "CANARY-PROMPT-7731"
        p.record_dispatch("c", dep("x1", "openai/ok-a@x1", "stub"), "g", est_tokens=5)
        p.record_result("c", "x1", "stub", "openai/ok-a@x1", "g", prompt=5, completion=1, headers={"x": canary}, status=200,
                        key="app", session="sess-raw-id", latency_ms=1)
        p.store.flush()
        raw = open(path, "rb").read() + (open(path + "-wal", "rb").read() if os.path.exists(path + "-wal") else b"")
        self.assertNotIn(canary.encode(), raw)
        self.assertNotIn(b"sess-raw-id", raw)


class Startup(unittest.TestCase):
    def test_plg11_order_and_workers(self):
        """PLG-11: the start check refuses the plugin after DeploymentAffinityCheck, and more than one worker."""

        class DeploymentAffinityCheck:
            pass

        me = object()
        cp.check_start([me, DeploymentAffinityCheck()], me, ["litellm", "--num_workers", "1"])
        with self.assertRaises(cp.StartError):
            cp.check_start([DeploymentAffinityCheck(), me], me, ["litellm", "--num_workers", "1"])
        with self.assertRaises(cp.StartError):
            cp.check_start([me], me, ["litellm", "--num_workers", "2"])


class Body(unittest.TestCase):
    def test_body_allow_list(self):
        """only allow-listed body fields pass; OpenRouter routing fields and extra_body are refused."""
        cp.check_body({"model": "g", "messages": [], "stream": True, "metadata": {}, "litellm_session_id": "s"})
        for bad in ("models", "route", "transforms", "extra_body", "api_base", "api_key", "mock_response", "provider"):
            with self.subTest(field=bad):
                with self.assertRaises(cp.BadBody):
                    cp.check_body({"model": "g", "messages": [], bad: "x"})
        with self.assertRaises(cp.BadBody):
            cp.check_body({"model": "g", "messages": [], "n": 2})


class OpenRouterPoll(unittest.TestCase):
    def reading(self, **over):
        base = {"usage": 0, "limit": 1, "limit_reset": None,
                "free_model_daily_requests": {"limit": 1000, "remaining": 900}}
        base.update(over)
        return base

    def test_plg09_thresholds(self):
        """PLG-09: money moving pulls openrouter/ and alerts; the rest only alert; a good reading does neither."""
        cases = [({}, None), ({"usage": 0.0001}, "pull"), ({"limit": 5}, "pull"), ({"limit": None}, "pull"),
                 ({"limit_reset": "daily"}, "pull"), ({"free_model_daily_requests": {"limit": 50, "remaining": 40}}, "alert"),
                 ({"free_model_daily_requests": {"limit": 1000, "remaining": 99}}, "alert")]
        for over, want in cases:
            with self.subTest(over=over):
                p = plugin()
                p.apply_openrouter_reading(self.reading(**over))
                pulled = "openrouter/" in p.pulls
                alerted = any(a["kind"].startswith("openrouter_key") for a in p.pending_alerts)
                self.assertEqual(pulled, want == "pull")
                self.assertEqual(alerted, want is not None)
        p = plugin()
        p.apply_openrouter_reading(None)
        self.assertNotIn("openrouter/", p.pulls, "a failed poll (network) alerts but does not pull")
        self.assertTrue(any(a["kind"].startswith("openrouter_key") for a in p.pending_alerts))
        q = plugin()
        r = self.reading()
        del r["usage"]
        q.apply_openrouter_reading(r)
        self.assertIn("openrouter/", q.pulls, "a reading missing a money field is money unknown: fail closed")

    def test_openrouter_remaining_filters_at_margin(self):
        p = plugin()
        p.apply_openrouter_reading(self.reading(free_model_daily_requests={"limit": 1000, "remaining": 15}))
        self.assertEqual(p.filter("g-or", [dep("or1", "openai/ok-a@or1:free", "openrouter")], "n", None, 1), [])


class Hardening(unittest.TestCase):
    def test_body_refuses_credentials_and_non_function_tools(self):
        """Audit 1, 8: api_key, headers, provider_specific_header, extra_headers in the request, credential fields,
        and tools that are not functions (Gemini googleSearch, OpenRouter server tools) are refused."""
        for bad in ({"api_key": "junk"}, {"headers": {"x": "y"}}, {"provider_specific_header": {"a": 1}},
                    {"extra_headers": {"a": "b"}}, {"base_url": "http://x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(cp.BadBody):
                    cp.check_request({"model": "g", "messages": [], **bad}, {"model": "g", "messages": []}, [])
        with self.assertRaises(cp.BadBody):
            cp.check_request({"model": "g"}, {"model": "g"}, ["api_key"])
        for tools in ([{"googleSearch": {}}], [{"type": "web_search"}], [{"type": "function"}, {"type": "url_context"}]):
            with self.subTest(tools=tools):
                with self.assertRaises(cp.BadBody):
                    cp.check_body({"model": "g", "messages": [], "tools": tools})
        cp.check_body({"model": "g", "messages": [], "tools": [{"type": "function", "function": {"name": "f"}}]})
        cp.check_request({"model": "g", "messages": []}, {"model": "g", "messages": []}, [])

    def test_own_pull_survives_a_reload_before_its_flush(self):
        """Audit 6: a pull the plugin made is not dropped by a pulls reload that runs before it reaches the file."""
        p = plugin()
        p.pull("openrouter/", "money")
        p.reload_pulls()
        self.assertIn("openrouter/", p.pulls)
        p.store.flush()
        p.reload_pulls()
        self.assertIn("openrouter/", p.pulls)

    def test_odd_header_does_not_skip_bookkeeping(self):
        """Audit 12: a non-numeric remaining header is ignored; the call's failure, session and recent row are kept."""
        p = plugin()
        d = dep("h1", "openai/hdr-a@h1", "hdr")
        p.record_dispatch("c1", d, "g", est_tokens=5)
        p.record_result("c1", "h1", "hdr", "openai/hdr-a@h1", "g", prompt=1, completion=1,
                        headers={"x_ratelimit_remaining_requests": "abc"}, status=500, key="k", session="s", latency_ms=1)
        self.assertIn("h1", p.failed["c1"][1])

    def test_sessions_are_per_group(self):
        """Audit P3: a session id used for chat and embeddings keeps one pin per group."""
        p = plugin()
        chat = [dep(f"c{i}", f"openai/ok-a@c{i}", "stub") for i in range(3)]
        emb = [dep(f"e{i}", f"openai/emb@e{i}", "stub") for i in range(3)]
        a = ids(p.filter("g-chat", chat, "1", "sess", 1))
        p.record_result("1", a[0], "stub", "x", "g-chat", prompt=1, completion=1, headers={}, status=200, key="k", session="sess", latency_ms=1)
        b = ids(p.filter("g-emb", emb, "2", "sess", 1))
        p.record_result("2", b[0], "stub", "x", "g-emb", prompt=1, completion=1, headers={}, status=200, key="k", session="sess", latency_ms=1)
        self.assertEqual(ids(p.filter("g-chat", chat, "3", "sess", 1)), a)
        self.assertNotIn("3", p._diverted)

    def test_router_local_429_counted_once(self):
        """Audit P3: a router-local 429 is counted as busy once: by the post-call failure hook, not the log event."""
        import asyncio
        p = plugin()
        p.record_result("c", "", "", "", "g", prompt=0, completion=0, headers={}, status=429, key="k", session=None, latency_ms=1)
        asyncio.run(p.async_post_call_failure_hook({"model": "g"}, mock.Mock(status_code=429, message="No deployments available"), mock.Mock(key_alias="k")))
        self.assertEqual(p.key_minute("k", "g")["r429"], 1)

    def test_openrouter_bad_shapes_and_midnight(self):
        """Audit P3: a non-dict key reading is a failed check, not a dead thread; the remaining count is forgotten at
        the UTC day change."""
        p = plugin()
        p.apply_openrouter_reading("not a dict")
        self.assertTrue(any(a["kind"] == "openrouter_key:check_failed" for a in p.pending_alerts))
        clock = Clock()
        q = plugin(clock=clock)
        q.apply_openrouter_reading({"usage": 0, "limit": 1, "limit_reset": None, "free_model_daily_requests": {"limit": 1000, "remaining": 5}})
        self.assertEqual(q.filter("g-or", [dep("or1", "openai/ok-a@or1:free", "openrouter")], "n", None, 1), [])
        clock.t += 86400
        self.assertEqual(len(q.filter("g-or", [dep("or1", "openai/ok-a@or1:free", "openrouter")], "n2", None, 1)), 1)

    def test_store_close_flushes(self):
        """PLG-06: closing the store (SIGTERM/atexit) writes queued ops."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        s.add_alert("k", "m")
        s.close()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select count(*) from alerts").fetchone()[0], 1)


class Review(unittest.TestCase):
    def test_unknown_model_names_are_refused(self):
        """only group names reach routing; a deployment's own model name is refused."""
        p = plugin(groups={"g": 2, "g-empty": 0})
        self.assertTrue(p.is_known_group("g"))
        self.assertFalse(p.is_known_group("openai/ok-a@two-a"))
        self.assertFalse(p.is_known_group("gemini/gemini-3.1-flash-lite"))

    def test_a_single_deployment_route_is_filtered_too(self):
        """when LiteLLM routes to one deployment (a dict), a hard stop still refuses it."""
        import asyncio
        p = plugin()
        d = dep("d1", "openai/ok-a@d1", "stub")
        for i in range(3):
            run(p, d, call=f"c{i}")
        with self.assertRaises(Exception):
            asyncio.run(p.async_filter_deployments("g", d, [], {"litellm_call_id": "x"}))
        ok = dep("ok", "openai/ok-a@ok", "stub")
        self.assertEqual(asyncio.run(p.async_filter_deployments("g", ok, [], {"litellm_call_id": "y"})), ok)

    def test_pool_dry_alerts_ignore_key_throttling(self):
        """a key's own rate-limit 429 is busy for the key, not a dry pool."""
        import asyncio
        p = plugin()
        key = mock.Mock(key_alias="k")
        throttle = mock.Mock(status_code=429, message="Rate limit exceeded for api_key: abc. Limit type: requests")
        for _ in range(8):
            asyncio.run(p.async_post_call_failure_hook({"model": "g"}, throttle, key))
        self.assertFalse(any(a["kind"].startswith("pool_dry") for a in p.pending_alerts))
        self.assertEqual(p.key_minute("k", "g")["r429"], 8)
        dry = mock.Mock(status_code=429, message="No deployments available for selected model")
        for _ in range(6):
            asyncio.run(p.async_post_call_failure_hook({"model": "g"}, dry, key))
        self.assertTrue(any(a["kind"].startswith("pool_dry") for a in p.pending_alerts))

    def test_retry_after_longer_than_reset_wins(self):
        """on a 429 the later of the reset and retry-after is used."""
        p = plugin()
        d = dep("h1", "openai/hdr-a@h1", "hdr")
        run(p, d, status=429, headers={"x_ratelimit_remaining_requests": 0, "llm_provider-x-ratelimit-reset-requests": "10s", "retry-after": "120"})
        self.assertEqual(p.provider_state["h1"]["reset_at"], T0 + 120)

    def test_starting_registers_the_shutdown_flush(self):
        """PLG-06: a started plugin registers store.close with atexit, so a graceful stop flushes."""
        with mock.patch("charmer_plugin.atexit.register") as reg, mock.patch.object(st.Store, "start"):
            s = st.Store(os.path.join(tempfile.mkdtemp(), "c.sqlite"))
            cp.Charmer(limits=LIMITS, known_groups={}, store=s, clock=Clock(), start=True)
        reg.assert_called_with(s.close)

    def test_store_alert_is_rate_limited_and_webhooked(self):
        """alerts written by scripts and the dashboard go through the store: once an hour per kind,
        each one POSTed to ALERT_WEBHOOK_URL."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        with mock.patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "http://hook.test/x"}), mock.patch("store.urllib.request.urlopen") as post:
            self.assertTrue(s.alert_now("cf_budget", "one"))
            self.assertFalse(s.alert_now("cf_budget", "two"))
            self.assertTrue(s.alert_now("gemini_402", "three"))
            for t in __import__("threading").enumerate():
                if t.name == "charmer-webhook":
                    t.join(2)
        self.assertEqual(post.call_count, 2)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select count(*) from alerts").fetchone()[0], 2)

    def test_alert_now_writes_the_queued_pull(self):
        """A cron script queues a pull, then raises its alert: both reach the file (FREE-07 relies on it)."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        s.add_pull("gemini/", "402", "guard")
        with mock.patch("store.webhook"):
            s.alert_now("gemini_402", "pulled")
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select who from pulls where target='gemini/' and lifted is null").fetchone(), ("guard",))

    def test_retention(self):
        """day rows and lifted pulls older than 90 days are pruned."""
        path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
        s = st.Store(path)
        old = time.time() - 100 * 86400
        s.add("usage", "a", "*", st._minute(old), time.strftime("%Y-%m-%d", time.gmtime(old)), 1, 0, 0, 0, 0)
        s.add("pull", "x", "r", "owner", old)
        s.add("unpull", "x", old + 1, None)
        s.flush()
        s.add_alert("k", "m")
        s.flush()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("select count(*) from usage_day").fetchone()[0], 0)
            self.assertEqual(db.execute("select count(*) from pulls").fetchone()[0], 0)


class History(unittest.TestCase):
    def test_plg15_fit_history(self):
        """PLG-15: replayed assistant turns keep only standard fields (plus Gemini's on Gemini), ids untouched,
        and null content becomes "" for Cloudflare only."""
        turn = {"role": "assistant", "content": None, "reasoning_content": "r", "provider_specific_fields": {"x": 1},
                "thinking_blocks": [{"t": 1}], "images": [], "reasoning": "r",
                "tool_calls": [{"id": "call_1__thought__sig", "type": "function", "index": 0, "provider_specific_fields": {"s": 1},
                                "function": {"name": "f", "arguments": "{}"}}]}
        msgs = [{"role": "user", "content": "hi"}, turn, {"role": "tool", "tool_call_id": "call_1__thought__sig", "content": "{}"}]
        groq = cp.fit_history(msgs, "groq", "groq/openai/gpt-oss-120b")
        self.assertEqual(set(groq[1]), {"role", "content", "tool_calls"})
        self.assertEqual(set(groq[1]["tool_calls"][0]), {"id", "type", "function"})
        self.assertEqual(groq[1]["tool_calls"][0]["id"], "call_1__thought__sig")
        self.assertIsNone(groq[1]["content"])
        self.assertEqual(groq[0], msgs[0])
        self.assertEqual(groq[2], msgs[2])
        cf = cp.fit_history(msgs, "cloudflare", "cloudflare/@cf/openai/gpt-oss-120b")
        self.assertEqual(cf[1]["content"], "")
        gm = cp.fit_history(msgs, "gemini", "gemini/gemini-3.1-flash-lite")
        self.assertIn("provider_specific_fields", gm[1])
        self.assertIn("provider_specific_fields", gm[1]["tool_calls"][0])
        self.assertIsNone(gm[1]["content"])
        self.assertIn("reasoning_content", msgs[1], "the caller's list is not changed in place")

    def test_mistral_header_names(self):
        """PLG-02: Mistral's x-ratelimit-remaining-req-minute is read like the standard names."""
        p = plugin()
        run(p, dep("m1", "mistral/ministral-14b-2512", "stub"), headers={"llm_provider-x-ratelimit-remaining-req-minute": "29"})
        self.assertEqual(p.provider_state["m1"]["remaining_requests"], 29)


class EmptyGroups(unittest.TestCase):
    def test_known_empty_group_is_busy(self):
        """a group declared in limits.yaml but with no member answers busy, not 400."""
        p = plugin(groups={"g": 2, "g-empty": 0})
        self.assertTrue(p.is_empty_group("g-empty"))
        self.assertFalse(p.is_empty_group("g"))
        self.assertFalse(p.is_empty_group("never-heard-of"))

    def test_declared_groups_come_from_limits(self):
        """One key, no keys.yaml: groups that start empty (chat) are declared under `groups` in limits.yaml."""
        import yaml
        d = tempfile.mkdtemp()
        files = {"limits.yaml": {**LIMITS, "groups": ["chat"]},
                 "config.yaml": {"model_list": [{"model_name": "pair-a", "model_info": {"id": "pair-a"}, "litellm_params": {"model": "x"}}]},
                 "rendered.yaml": {"model_list": [{"model_name": "pair-a", "model_info": {"id": "pair-a"}, "litellm_params": {"model": "x"}}]}}
        for name, doc in files.items():
            with open(os.path.join(d, name), "w") as f:
                yaml.safe_dump(doc, f)
        env = {"CHARMER_LIMITS": os.path.join(d, "limits.yaml"), "CHARMER_CONFIG": os.path.join(d, "config.yaml"),
               "CHARMER_RENDERED": os.path.join(d, "rendered.yaml"), "CHARMER_DB": os.path.join(d, "c.sqlite"),
               "CHARMER_KEYS": os.path.join(d, "missing.yaml")}
        with mock.patch.dict(os.environ, env):
            p = cp._from_env()
        self.assertEqual(p.known_groups, {"pair-a": 1, "chat": 0})


if __name__ == "__main__":
    unittest.main()
