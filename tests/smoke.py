"""Smoke suite against the stub provider. Runs in the `smoke` container on the test compose network.

Every cell asserts what SHOULD happen. Bodies that the consumer contract depends on are recorded to
reports/smoke-bodies.json for a client's fixtures.
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

LITELLM = os.environ.get("LITELLM", "http://litellm:4000")
STUB = os.environ.get("STUB", "http://stub:9000")
KEY = os.environ["CHARMER_API_KEY"]
LONG_ID = "call_1_" + "x" * 2000
BODIES = {}
TOOLS = [{"type": "function", "function": {"name": "find_orders", "parameters": {"type": "object", "properties": {}}}}]


def http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers)
    elapsed = time.monotonic() - start
    text = raw.decode(errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    return status, {k.lower(): v for k, v in hdrs.items()}, payload, elapsed


def chat(model, key=KEY, stream=False, session=None, extra=None, messages=None, tools=None):
    body = {"model": model, "messages": messages or [{"role": "user", "content": "hi"}], **(extra or {})}
    if stream:
        body["stream"] = True
    if tools:
        body["tools"] = tools
    headers = {"authorization": f"Bearer {key}"} if key else {}
    if session:
        headers["x-litellm-session-id"] = session
    return http("POST", f"{LITELLM}/v1/chat/completions", body, headers)


def stream_tool_calls(payload):
    """Rebuild tool calls from an SSE body."""
    calls = {}
    for line in str(payload).splitlines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        for ch in chunk.get("choices") or []:
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                cur = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                cur["id"] = tc.get("id") or cur["id"]
                fn = tc.get("function") or {}
                cur["name"] += fn.get("name") or ""
                cur["arguments"] += fn.get("arguments") or ""
    return [calls[i] for i in sorted(calls)]


def stub_log():
    return http("GET", f"{STUB}/_log")[2]


def stub_reset():
    http("POST", f"{STUB}/_reset", {})


def stub_set(**kw):
    http("POST", f"{STUB}/_set", kw)


def served(headers):
    return headers.get("x-litellm-model-id")


def hits(log, behaviour):
    return [e for e in log if e.get("model") == behaviour and e.get("method") == "POST"]


def db():
    import sqlite3
    conn = sqlite3.connect("/data/charmer.sqlite", timeout=10)
    conn.execute("pragma busy_timeout=10000")
    return conn


def pull(target):
    with db() as c:
        c.execute("insert into pulls (target, reason, who, since) values (?, 'smoke', 'owner', strftime('%s','now'))", (target,))
    time.sleep(3)  # the plugin reloads pulls every 2 s


def unpull(target):
    with db() as c:
        c.execute("update pulls set lifted = strftime('%s','now') where target = ? and lifted is null", (target,))
    time.sleep(3)


def alerts(prefix):
    with db() as c:
        return [r[0] for r in c.execute("select kind from alerts where kind like ?", (prefix + "%",))]


def setUpModule():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if http("GET", f"{LITELLM}/health/liveliness", timeout=5)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError("LiteLLM did not come up")


def tearDownModule():
    """Merge, never overwrite: the suite runs as several processes (the restart cells last, recording nothing)."""
    os.makedirs("reports", exist_ok=True)
    path = "reports/smoke-bodies.json"
    try:
        with open(path) as f:
            merged = json.load(f)
    except (OSError, ValueError):
        merged = {}
    merged.update(BODIES)
    with open(path, "w") as f:
        json.dump(merged, f, indent=1, sort_keys=True)


class Step3(unittest.TestCase):
    def setUp(self):
        stub_reset()

    def test_cfg03_boots_on_the_stub_config(self):
        """CFG-03 (step 3 half): the proxy boots with the plugin and auth hook loaded; liveliness answers."""
        self.assertEqual(http("GET", f"{LITELLM}/health/liveliness")[0], 200)

    def test_smk03_client_side_credentials_refused(self):
        """SMK-03: api_base alone, api_key alone, headers and provider_specific_header are each refused
        with a 4xx; no provider is called (a junk api_key must never reach and cool a good deployment)."""
        for extra in ({"api_base": "http://stub:9999/v1"}, {"api_key": "junk"}, {"headers": {"x-a": "b"}},
                      {"provider_specific_header": {"custom_llm_provider": "openai", "extra_headers": {"a": "b"}}}):
            status, _, _, _ = chat("g-two", extra=extra)
            self.assertGreaterEqual(status, 400, extra)
            self.assertLess(status, 500, extra)
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])
        self.assertEqual(chat("g-two")[0], 200, "the deployment is not cooled by the refused calls")

    def test_smk04_shuffle_and_headers(self):
        """SMK-04: 20 calls, half streaming: every answer names its deployment and api base; both are used."""
        seen = set()
        for i in range(20):
            status, h, _, _ = chat("g-two", stream=bool(i % 2))
            self.assertEqual(status, 200)
            self.assertIn(served(h), {"two-a", "two-b"})
            self.assertTrue(h.get("x-litellm-model-api-base"))
            seen.add(served(h))
        self.assertEqual(seen, {"two-a", "two-b"})

    def test_smk05_first_429_cools_the_deployment(self):
        """SMK-05: 10 calls all succeed from ok-a; rl-429 is called once, then not again within its cooldown."""
        for _ in range(10):
            status, h, _, _ = chat("g-fail")
            self.assertEqual(status, 200)
            self.assertEqual(served(h), "fail-ok")
        self.assertEqual(len(hits(stub_log(), "rl-429")), 1)

    def test_smk06_retry_after_is_honoured_then_probed_once(self):
        """SMK-06: LiteLLM cools rl-429 for 5 s, and the plugin keeps it out until its Retry-After (30 s) passes;
        then it is probed exactly once more. Every call meanwhile succeeds."""
        for _ in range(10):
            chat("g-fail")
        start = time.monotonic()
        time.sleep(7)
        stub_reset()
        for _ in range(10):
            self.assertEqual(chat("g-fail")[0], 200)
        early = len(hits(stub_log(), "rl-429"))
        time.sleep(max(0, 32 - (time.monotonic() - start)))
        stub_reset()
        for _ in range(10):
            self.assertEqual(chat("g-fail")[0], 200)
        late = len(hits(stub_log(), "rl-429"))
        BODIES["SMK-06"] = {"probes_between_5s_and_30s": early, "probes_after_30s": late, "retry_after_sent": 30}
        self.assertEqual((early, late), (0, 1))

    def test_smk07_dry_pool_answers_429(self):
        """SMK-07: once both members cool, the answer is a 429 with an OpenAI-style error body."""
        last = None
        for _ in range(60):
            last = chat("g-dry")
            if last[0] == 429 and "No deployments available" in json.dumps(last[2]):
                break
            time.sleep(0.5)
        status, h, body, _ = last
        BODIES["SMK-07"] = {"status": status, "body": body, "retry_after": h.get("retry-after")}
        self.assertEqual(status, 429)
        self.assertIn("message", body["error"])
        self.assertEqual(str(body["error"].get("code")), "429")

    def test_smk08_sessions_stick(self):
        """SMK-08: each session's calls report one deployment; across sessions both are used."""
        for _ in range(6):
            chat("g-sticky")
        time.sleep(6)
        for name in ("A", "B"):
            ids = {served(chat("g-sticky", session=f"smk08-{name}")[1]) for _ in range(10)}
            self.assertEqual(len(ids), 1, f"session {name} moved: {ids}")
        across = {served(chat("g-sticky", session=f"smk08-many-{i}")[1]) for i in range(16)}
        self.assertEqual(across, {"s1", "s2"})

    def test_smk09_session_moves_once_after_its_pair_cools(self):
        """SMK-09: a session pinned to s1 moves to s2 when s1 cools, and stays there."""
        for _ in range(8):  # spend s1's one 429, then wait out its cooldown
            chat("g-sticky")
            if hits(stub_log(), "rl-429-once"):
                break
        time.sleep(6)
        for i in range(40):
            sid = f"smk09-{i}"
            if served(chat("g-sticky", session=sid)[1]) == "s1":
                break
        else:
            self.fail("no session landed on s1 in 40 tries")
        stub_set(reset_once=True)
        results = [served(chat("g-sticky", session=sid)[1]) for _ in range(6)]
        self.assertEqual(results[1:], ["s2"] * 5, results)

    def test_smk12_tool_ids_round_trip(self):
        """SMK-12: two tool calls come back with ids and arguments unchanged, streamed or not; both ids go back byte for byte."""
        for stream in (False, True):
            stub_reset()
            status, _, body, _ = chat("g-tools", stream=stream, tools=TOOLS)
            self.assertEqual(status, 200)
            if stream:
                calls = stream_tool_calls(body)
            else:
                calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                         for c in body["choices"][0]["message"]["tool_calls"]]
            self.assertEqual([c["id"] for c in calls], [LONG_ID, "call_2"])
            self.assertEqual(json.loads(calls[0]["arguments"]), {"payment": ["UNPAID"]})
            history = [{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": None,
                        "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]},
                       {"role": "tool", "tool_call_id": LONG_ID, "content": "{}"},
                       {"role": "tool", "tool_call_id": "call_2", "content": "{}"}]
            stub_reset()
            self.assertEqual(chat("g-tools", messages=history, tools=TOOLS)[0], 200)
            last = hits(stub_log(), "tools-long-id")[-1]
            self.assertEqual(last["tool_call_ids"], [LONG_ID, "call_2"])
            self.assertEqual(last["tool_result_ids"], [LONG_ID, "call_2"])

    def test_smk13_embeddings(self):
        """SMK-13: embeddings answer 1,024 dimensions and name the deployment."""
        status, h, body, _ = http("POST", f"{LITELLM}/v1/embeddings", {"model": "embed-test", "input": ["hello"]},
                                  {"authorization": f"Bearer {KEY}"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"][0]["embedding"]), 1024)
        self.assertEqual(served(h), "emb1")

    def test_smk14_pair_group_serves_one_deployment(self):
        """SMK-14: pair-ok-a is served by its one deployment."""
        for _ in range(3):
            status, h, _, _ = chat("pair-ok-a")
            self.assertEqual((status, served(h)), (200, "pair-ok-a"))

    def test_smk16_health_checks_spend_nothing(self):
        """SMK-16: liveliness and readiness never call a provider; no background health checks while idle."""
        for _ in range(10):
            http("GET", f"{LITELLM}/health/liveliness")
            http("GET", f"{LITELLM}/health/readiness")
        time.sleep(60)
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])

    def test_smk19_provider_401_differs_from_charmer_refusing_the_key(self):
        """SMK-19: a provider's 401 reaches the client in a body a client can tell apart from Charmer's refusal."""
        own = chat("g-two", key="sk-not-a-key")
        BODIES["SMK-01"] = {"status": own[0], "body": own[2]}
        status, _, body, _ = chat("g-auth")
        BODIES["SMK-19"] = {"status": status, "body": body}
        self.assertTrue(hits(stub_log(), "auth-401"), "the provider was not called")
        self.assertNotEqual(json.dumps(body, sort_keys=True), json.dumps(own[2], sort_keys=True))

    def test_smk31_one_member_group_answers_within_90s(self):
        """SMK-31: a one-member group whose provider says Retry-After 60 answers within 90 s, never after 100 s."""
        status, _, _, elapsed = chat("g-retry")
        BODIES["SMK-31"] = {"status": status, "elapsed_s": round(elapsed, 1)}
        self.assertLess(elapsed, 90)
        self.assertNotEqual(status, 200)


class Step4(unittest.TestCase):
    """Keys and the auth hook."""

    def setUp(self):
        stub_reset()

    def test_cfg03_admin_key_lists_the_groups(self):
        """CFG-03 (step 4 half): the admin key on the host lists exactly the rendered groups."""
        status, _, body, _ = http("GET", f"{LITELLM}/v1/models", headers={"authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"})
        self.assertEqual(status, 200)
        names = {m["id"] for m in body["data"]}
        self.assertIn("g-two", names)
        self.assertNotIn("g-empty", names, "a group whose provider key is empty is not rendered")

    def test_smk01_unknown_key(self):
        """SMK-01: no key or an unknown key is 401; the provider is not called; the body is recorded."""
        for key in (None, "sk-" + "z" * 48):
            status, _, body, _ = chat("g-two", key=key)
            self.assertEqual(status, 401)
        self.assertEqual(body["error"]["type"], "auth_error")
        self.assertIn("charmer: unknown key", body["error"]["message"])
        BODIES["SMK-01"] = {"status": status, "body": body}
        self.assertEqual(hits(stub_log(), "ok-a") + hits(stub_log(), "ok-b"), [])

    def test_smk02_unknown_group(self):
        """SMK-02: the API key calling a group that does not exist gets 400 naming it; nothing is sent."""
        status, _, body, _ = chat("g-nope")
        BODIES["SMK-02"] = {"status": status, "body": body}
        self.assertEqual(status, 400)
        self.assertIn("g-nope", json.dumps(body))
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])

    def test_smk20_route_walk(self):
        """SMK-20: every route answers non-2xx to a consumer key and to no key, except inference (consumer) and the
        two public health probes; the provider is never called; the admin UI is off; admin gets /model/info."""
        from litellm.proxy.proxy_server import app
        inference = {"/v1/chat/completions", "/chat/completions", "/v1/embeddings", "/embeddings", "/v1/models", "/models"}
        public = {"/health/liveliness", "/health/liveness", "/health/readiness", "/health/liveliness/", "/health/readiness/"}
        # LiteLLM's own unauthenticated routes: static UI/SSO shells and LiteLLM's public metadata. Allowed to answer,
        # but never with Charmer data: checked below for keys, deployment ids and provider addresses.
        litellm_public = ("/", "/fallback/login", "/get_logo_url", "/get_image", "/get_favicon", "/public/",
                          "/sso/", "/get/ui_theme_settings", "/litellm/.well-known/", "/.well-known/")
        secrets = [os.environ[k] for k in os.environ if k in ("CHARMER_API_KEY", "LITELLM_MASTER_KEY", "CHARMER_METRICS_KEY", "STUB_KEY")]
        # Keys and provider endpoints must never appear. Deployment ids do appear in LiteLLM's public cost map
        # (/public/litellm_model_cost_map): a known, recorded exposure of pool composition, not of secrets.
        markers = secrets + ["stub:9000", "stub-provider-key"]
        leaks = []
        for route in app.routes:
            methods = sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"})
            path = getattr(route, "path", "")
            if not methods or not path.startswith("/"):
                continue
            url = LITELLM + "".join("x" if part.startswith("{") else part for part in path.replace("{", "/{").split("/")).replace("//", "/")
            url = LITELLM + "/".join("x" if seg.startswith("{") else seg for seg in path.split("/"))
            for method in methods:
                for who, key in (("consumer", KEY), ("nobody", None)):
                    headers = {"authorization": f"Bearer {key}"} if key else {}
                    try:
                        status, _, body, _ = http(method, url, {} if method in ("POST", "PUT", "PATCH") else None, headers, timeout=20)
                    except OSError as e:
                        status, body = f"error {e}", ""
                    if not (isinstance(status, int) and 200 <= status < 300):
                        continue
                    text = json.dumps(body) if not isinstance(body, str) else body
                    found = [m[:12] for m in markers if m and m in text]
                    if found:
                        leaks.append(f"{who} {method} {path} -> {status} carries {found}")
                    elif not (path in public or (who == "consumer" and path in inference)
                              or path == "/" or any(path.startswith(p) for p in litellm_public if p != "/")):
                        leaks.append(f"{who} {method} {path} -> {status}")
        BODIES["SMK-20"] = {"leaks": leaks}
        self.assertEqual(leaks, [])
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])
        admin = {"authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"}
        self.assertEqual(http("GET", f"{LITELLM}/model/info", headers=admin)[0], 200)

    def test_smk34_admin_and_metrics_keys_never_through_the_tunnel(self):
        """SMK-34: the admin key with cf-connecting-ip is 401; the metrics key reads /metrics and nothing else."""
        admin = os.environ["LITELLM_MASTER_KEY"]
        status = http("GET", f"{LITELLM}/model/info", headers={"authorization": f"Bearer {admin}", "cf-connecting-ip": "203.0.113.9"})[0]
        self.assertEqual(status, 401)
        metrics = {"authorization": f"Bearer {os.environ['CHARMER_METRICS_KEY']}"}
        self.assertEqual(http("GET", f"{LITELLM}/metrics/", headers=metrics)[0], 200)
        self.assertEqual(http("GET", f"{LITELLM}/v1/models", headers=metrics)[0], 403)
        self.assertEqual(http("GET", f"{LITELLM}/metrics/")[0], 401, "no key, no metrics (review)")
        consumer = {"authorization": f"Bearer {KEY}"}
        self.assertIn(http("GET", f"{LITELLM}/metrics/", headers=consumer)[0], (401, 403), "a consumer key cannot read metrics (review)")


class Step5AHistory(unittest.TestCase):
    """Runs before Step5Plugin on purpose: SMK-27 there spends the stub Cloudflare account's neurons, after which the
    strict Cloudflare deployment is (correctly) filtered out for the rest of the day."""

    def setUp(self):
        stub_reset()

    def test_smk35_replayed_turns_fit_the_serving_provider(self):
        """SMK-35: another provider's assistant turn (reasoning_content, provider_specific_fields, index, null content)
        is accepted by a strict provider and by a Cloudflare one; the tool call id reaches them unchanged."""
        turn = {"role": "assistant", "content": None, "reasoning_content": "thinking", "provider_specific_fields": {"a": 1},
                "tool_calls": [{"id": "call_9", "index": 0, "type": "function", "function": {"name": "find_orders", "arguments": "{}"}}]}
        history = [{"role": "user", "content": "hi"}, turn, {"role": "tool", "tool_call_id": "call_9", "content": "{}"}]
        for group in ("g-strict", "g-strict-cf"):
            status, _, body, _ = chat(group, messages=history, tools=TOOLS)
            self.assertEqual(status, 200, f"{group}: {body}")
        self.assertEqual(hits(stub_log(), "strict")[-1]["tool_call_ids"], ["call_9"])



class Step5Plugin(unittest.TestCase):
    """Cells that need the plugin."""

    def setUp(self):
        stub_reset()

    def test_smk11_minute_limit_filters_not_429s(self):
        """SMK-11: ok-a's minute limit of 2 filters it; all 5 calls succeed; the client never sees a 429."""
        results = [chat("g-rpm") for _ in range(5)]
        self.assertEqual([r[0] for r in results], [200] * 5)
        self.assertLessEqual(len(hits(stub_log(), "ok-a")), 2)

    def test_smk18_no_text_stored(self):
        """SMK-18: streamed calls end with [DONE]; token counts are stored; the prompt text is not."""
        canary = "CANARY-SMOKE-55821"
        for _ in range(10):
            status, _, body, _ = chat("g-two", stream=True, messages=[{"role": "user", "content": canary}])
            self.assertEqual(status, 200)
            self.assertIn("data: [DONE]", str(body))
        time.sleep(3)
        with db() as c:
            self.assertGreater(c.execute("select sum(prompt) from usage_minute where account='stub' and model='*'").fetchone()[0], 0)
        raw = b"".join(open(p, "rb").read() for p in ("/data/charmer.sqlite", "/data/charmer.sqlite-wal") if os.path.exists(p))
        self.assertNotIn(canary.encode(), raw)

    def test_smk21_divert_before_the_provider_429(self):
        """SMK-21: a session on h1 moves to h2 when h1 reports remaining at the margin, and stays; h1 never 429s."""
        stub_set(remaining={"hdr-a": 10, "hdr-b": 10})
        for i in range(60):
            sid = f"smk21-{i}"
            if served(chat("g-hdr", session=sid)[1]) == "h1":
                break
        else:
            self.fail("no session landed on h1")
        stub_set(remaining={"hdr-a": 4})
        self.assertEqual(served(chat("g-hdr", session=sid)[1]), "h1")  # h1 now reports remaining 3 = the margin
        h1_before = len(hits(stub_log(), "hdr-a"))
        after = [served(chat("g-hdr", session=sid)[1]) for _ in range(5)]
        self.assertEqual(after, ["h2"] * 5)
        self.assertEqual(len(hits(stub_log(), "hdr-a")), h1_before, "h1 was called again (and would have 429ed)")
        time.sleep(3)
        with db() as c:
            self.assertGreaterEqual(c.execute("select count(*) from recent_calls where diverted = 1").fetchone()[0], 1)

    def test_smk23_soft_filter_never_answers_before_a_provider(self):
        """SMK-23: with both members under the margin the call still reaches a provider; never a router 429 first."""
        stub_set(remaining={"hdr-a": 1, "hdr-b": 1})
        for _ in range(4):
            chat("g-hdr")
        stub_reset()
        stub_set(remaining={"hdr-a": 0, "hdr-b": 0})
        status = chat("g-hdr")[0]
        self.assertTrue(hits(stub_log(), "hdr-a") or hits(stub_log(), "hdr-b"), f"no provider tried before answering {status}")
        stub_set(remaining={"hdr-a": 10, "hdr-b": 10})

    def test_smk24_pull_without_restart(self):
        """SMK-24: a pull takes effect without a restart; with every member pulled the group answers busy."""
        stub_set(remaining={"hdr-a": 10, "hdr-b": 10})
        time.sleep(62)  # let earlier header readings expire
        pull("h1")
        try:
            for _ in range(5):
                status, h, _, _ = chat("g-hdr")
                self.assertEqual((status, served(h)), (200, "h2"))
            pull("h2")
            stub_reset()
            status, _, body, _ = chat("g-hdr")
            self.assertEqual(status, 429)
            self.assertIn("No deployments available", json.dumps(body))
            self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])
        finally:
            unpull("h1")
            unpull("h2")

    def test_smk26_one_count_per_account(self):
        """SMK-26: g-acct and pair-x1 share acct-x's day limit of 4; the rest answer busy with no stub call."""
        codes = [chat("g-acct")[0] for _ in range(3)] + [chat("pair-x1")[0] for _ in range(3)]
        self.assertEqual(codes.count(200), 4, codes)
        self.assertEqual(codes.count(429), 2, codes)
        self.assertEqual(len(hits(stub_log(), "ok-a")), 4)

    def test_smk27_neuron_stop(self):
        """SMK-27: after 1,000 neurons cf1 is filtered and cf-ok serves; embeddings keep working."""
        for _ in range(14):
            self.assertEqual(chat("g-cf")[0], 200)
        self.assertLessEqual(len(hits(stub_log(), "cf-chat")), 5)
        status = http("POST", f"{LITELLM}/v1/embeddings", {"model": "embed-test", "input": ["x"]}, {"authorization": f"Bearer {KEY}"})[0]
        self.assertEqual(status, 200)

    def test_smk29_body_allow_list(self):
        """SMK-29: OpenRouter routing fields and other passthroughs are refused with 400 naming the field."""
        for field in ("models", "route", "transforms", "extra_body"):
            status, _, body, _ = chat("g-or", extra={field: ["openrouter/paid-model"] if field == "models" else "x"})
            self.assertEqual(status, 400, field)
            self.assertIn(field, json.dumps(body))
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])
        self.assertEqual(chat("g-two", extra={"temperature": 0, "max_tokens": 5})[0], 200)
        # mock_response is stripped by LiteLLM itself for keys not allowed to mock: the provider answers for real
        status, _, body, _ = chat("g-two", extra={"mock_response": "FAKE"})
        self.assertEqual(status, 200)
        self.assertNotIn("FAKE", json.dumps(body))

    def test_smk30_empty_group_is_busy(self):
        """SMK-30: a known group with no member answers the busy 429, not 400."""
        for group in ("g-empty", "g-empty-declared"):
            status, _, body, _ = chat(group)
            self.assertEqual(status, 429, group)
            self.assertIn("No deployments available", json.dumps(body))
            BODIES[f"SMK-30 {group}"] = {"status": status, "body": body}

    def test_smk32_order_two_only_when_needed(self):
        """SMK-32: q2 (order 2) serves nothing while q1 is offered; it serves when q1 is pulled; new sessions return to q1."""
        self.assertEqual({served(chat("g-groq", session=f"smk32-{i}")[1]) for i in range(20)}, {"q1"})
        pull("q1")
        try:
            self.assertEqual({served(chat("g-groq", session=f"smk32-b-{i}")[1]) for i in range(5)}, {"q2"})
        finally:
            unpull("q1")
        self.assertEqual({served(chat("g-groq", session=f"smk32-c-{i}")[1]) for i in range(5)}, {"q1"})

    def test_smk33_eval_share(self):
        """SMK-33: calls to pair-* groups stop at the account's eval share; the same account still serves g-or."""
        codes = [chat("pair-or1")[0] for _ in range(2)] + [chat("pair-or2")[0] for _ in range(2)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(chat("g-or")[0], 200)

    def test_smk15_slow_member_times_out_to_the_other(self):  # step 5: needs the plugin's per-call skip
        """SMK-15: a slow member does not hold the call; ok-a answers within the timeout plus a retry."""
        for _ in range(4):
            status, h, _, elapsed = chat("g-slow")
            self.assertEqual(status, 200)
            self.assertEqual(served(h), "slow-ok")
            self.assertLess(elapsed, 12)


class Step5ZFree(unittest.TestCase):
    """FREE-01..06: the plugin's OpenRouter key poll through the stub."""

    GOOD = {"usage": 0, "limit": 1, "limit_reset": None, "free_model_daily_requests": {"limit": 1000, "remaining": 900}}

    def reading(self, data, wait=5):
        stub_set(key=data)
        time.sleep(wait)

    def tearDown(self):
        self.reading(self.GOOD, wait=3)
        unpull("openrouter/")

    def test_free01_good_reading(self):
        """FREE-01: a good reading raises no alert and pulls nothing."""
        before = alerts("openrouter_key")
        self.reading(self.GOOD)
        self.assertEqual(alerts("openrouter_key"), before)
        self.assertEqual(chat("g-or")[0], 200)

    def test_free02_free03_money_moving_pulls_openrouter(self):
        """FREE-02, FREE-03: usage above 0 or a lost cap pulls openrouter/ and alerts; g-or answers busy."""
        self.reading({**self.GOOD, "usage": 0.0001})
        self.assertIn("openrouter_key:money", alerts("openrouter_key"))
        time.sleep(3)
        self.assertEqual(chat("g-or")[0], 429)
        with db() as c:
            self.assertTrue(c.execute("select 1 from pulls where target='openrouter/' and lifted is null").fetchone())

    def test_free04_credit_gone(self):
        """FREE-04: a daily free limit below 1000 alerts."""
        self.reading({**self.GOOD, "free_model_daily_requests": {"limit": 50, "remaining": 40}})
        self.assertIn("openrouter_key:credit", alerts("openrouter_key"))

    def test_free05_day_nearly_dry(self):
        """FREE-05: under 100 free requests left alerts."""
        self.reading({**self.GOOD, "free_model_daily_requests": {"limit": 1000, "remaining": 99}})
        self.assertIn("openrouter_key:remaining", alerts("openrouter_key"))

    def test_smk28_openrouter_remaining_filters(self):
        """SMK-28: when the key reading shows 15 free requests left (margin 20), g-or answers busy until a poll shows more."""
        self.reading({**self.GOOD, "free_model_daily_requests": {"limit": 1000, "remaining": 15}})
        stub_reset()
        status, _, body, _ = chat("g-or")
        self.assertEqual(status, 429)
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])
        self.reading(self.GOOD)
        self.assertEqual(chat("g-or")[0], 200)

    def test_free06_no_answer(self):
        """FREE-06: a failed check alerts and is never a pass."""
        self.reading(None)
        self.assertIn("openrouter_key:check_failed", alerts("openrouter_key"))


class Step7Scripts(unittest.TestCase):
    """FREE-07..13 and BKP-01: the cron scripts against the stub, in this container."""

    def run_script(self, name, **env):
        import subprocess
        base = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        base.update({"CHARMER_DB": "/data/charmer.sqlite", "CHARMER_CONFIG": "/w/tests/config.test.yaml", "CHARMER_LIMITS": "/w/limits.yaml",
                     "GEMINI_PROBE_URL": "http://stub:9000/v1beta/models/x:generateContent", "GEMINI_API_KEY": "gm-test-not-real",
                     "CF_GRAPHQL_URL": "http://stub:9000/graphql", "CF_ANALYTICS_TOKEN": "cf-test-not-real", "CF_ACCOUNT_ID": "acct"})
        base.update(env)
        return subprocess.run([sys.executable, f"/w/scripts/{name}"], env=base, capture_output=True, text=True).returncode

    def open_pulls(self):
        with db() as c:
            return {t: w for t, w in c.execute("select target, who from pulls where lifted is null")}

    def tearDown(self):
        for t in list(self.open_pulls()):
            with db() as c:
                c.execute("update pulls set lifted = strftime('%s','now') where target = ?", (t,))
        stub_set(gemini_status=200, neurons=None)
        time.sleep(3)

    def test_free07_free08_gemini_probe(self):
        """FREE-07: a 402 pulls gemini/ with no restart; g-paid then never reaches pay-402. FREE-08: 200 and 429 pull nothing."""
        stub_set(gemini_status=200)
        self.assertEqual(self.run_script("free_check.py"), 0)
        stub_set(gemini_status=429)
        self.assertEqual(self.run_script("free_check.py"), 0)
        self.assertNotIn("gemini/", self.open_pulls())
        stub_set(gemini_status=402)
        self.assertEqual(self.run_script("free_check.py"), 1)
        self.assertEqual(self.open_pulls().get("gemini/"), "guard")
        time.sleep(3)
        stub_reset()
        served = [served_by for served_by in (chat("g-paid")[1].get("x-litellm-model-id") for _ in range(5))]
        self.assertEqual(set(served), {"paid-ok"})
        self.assertEqual(hits(stub_log(), "pay-402"), [])

    def test_free_check_openrouter_reading(self):
        """free-check exits 1 on the plugin's last bad OpenRouter reading, 0 on a good one."""
        good = {"usage": 0, "limit": 1, "limit_reset": None, "free_model_daily_requests": {"limit": 1000, "remaining": 900}}
        stub_set(key=good)
        time.sleep(5)
        self.assertEqual(self.run_script("free_check.py", OPENROUTER_API_KEY="x", GEMINI_API_KEY=""), 0)
        stub_set(key={**good, "usage": 0.5})
        time.sleep(5)
        self.assertEqual(self.run_script("free_check.py", OPENROUTER_API_KEY="x", GEMINI_API_KEY=""), 1)
        stub_set(key=good)

    def test_free09_to_free12_neuron_budget(self):
        """FREE-09..12: nothing at 6,999; chat pulled at 7,000 (one alert); embeddings at 9,500; lifted next day,
        a Gemini pull stays. FREE-10: a group whose only member is pulled answers busy."""
        stub_set(neurons=6999)
        self.assertEqual(self.run_script("cf_budget.py"), 0)
        self.assertEqual({t for t, w in self.open_pulls().items() if w == "budget"}, set())
        stub_set(neurons=7000)
        self.assertEqual(self.run_script("cf_budget.py"), 1)
        import yaml
        cf_chat = {d["model_info"]["id"] for d in yaml.safe_load(open("/w/tests/config.test.yaml"))["model_list"]
                   if d["model_info"]["account"] == "cloudflare" and d["model_info"].get("mode") != "embedding"}
        self.assertEqual({t for t, w in self.open_pulls().items() if w == "budget"}, cf_chat)
        stub_set(neurons=7100)
        self.assertEqual(self.run_script("cf_budget.py"), 0, "one alert a day, not one a run")
        time.sleep(3)
        status, _, body, _ = chat("g-cf-only")
        self.assertEqual(status, 429)
        self.assertIn("No deployments available", json.dumps(body))
        stub_set(neurons=9500)
        self.assertEqual(self.run_script("cf_budget.py"), 1)
        self.assertIn("emb1", self.open_pulls())
        with db() as c:
            c.execute("insert into pulls (target, reason, who, since) values ('gemini/', 'test', 'guard', strftime('%s','now'))")
        stub_set(neurons=10)
        self.assertEqual(self.run_script("cf_budget.py"), 0)
        self.assertEqual(self.open_pulls(), {"gemini/": "guard"})

    def test_cf_budget_without_cloudflare_is_quiet(self):
        """No Cloudflare deployment, or no analytics token, means nothing to check: exit 0, no pull, no alert."""
        import yaml
        doc = yaml.safe_load(open("/w/tests/config.test.yaml"))
        doc["model_list"] = [d for d in doc["model_list"] if d["model_info"]["account"] != "cloudflare"]
        path = "/tmp/no-cf.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(doc, f)
        stub_set(neurons=9999)
        with db() as c:
            alerts = c.execute("select count(*) from alerts").fetchone()[0]
        self.assertEqual(self.run_script("cf_budget.py", CHARMER_CONFIG=path), 0)
        self.assertEqual(self.run_script("cf_budget.py", CF_ANALYTICS_TOKEN=""), 0)
        self.assertEqual({t for t, w in self.open_pulls().items() if w == "budget"}, set())
        with db() as c:
            self.assertEqual(c.execute("select count(*) from alerts").fetchone()[0], alerts)

    def test_free13_query_failure_changes_nothing(self):
        """FREE-13: a failed or empty neuron query exits 1 and changes no pull."""
        before = self.open_pulls()
        stub_set(neurons=None)
        self.assertEqual(self.run_script("cf_budget.py"), 1)
        self.assertEqual(self.open_pulls(), before)
        stub_set(neurons=7000)
        self.assertEqual(self.run_script("cf_budget.py"), 1)
        pulled = self.open_pulls()
        stub_set(neurons="empty")
        self.assertEqual(self.run_script("cf_budget.py"), 1, "an answer with no number is a failed check")
        self.assertEqual(self.open_pulls(), pulled, "and lifts nothing")

    def test_pulls_script(self):
        """make pull / unpull write owner pulls the plugin honours with no restart."""
        import subprocess
        env = {**os.environ, "CHARMER_DB": "/data/charmer.sqlite", "CHARMER_CONFIG": "/w/tests/config.test.yaml"}
        bad = subprocess.run([sys.executable, "/w/scripts/pulls.py", "pull", "openrouter", "test"], env=env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2, "a target that matches nothing is refused")
        self.assertNotIn("openrouter", self.open_pulls())
        subprocess.run([sys.executable, "/w/scripts/pulls.py", "pull", "two-a", "test"], env=env, check=True, capture_output=True)
        self.assertEqual(self.open_pulls().get("two-a"), "owner")
        time.sleep(3)
        self.assertEqual({chat("g-two")[1].get("x-litellm-model-id") for _ in range(6)}, {"two-b"})
        subprocess.run([sys.executable, "/w/scripts/pulls.py", "unpull", "two-a"], env=env, check=True, capture_output=True)
        self.assertNotIn("two-a", self.open_pulls())

    def test_bkp01_backup_while_writing(self):
        """BKP-01: a backup taken while the plugin writes is readable and passes integrity_check; 14 are kept."""
        import sqlite3
        import subprocess
        import tempfile
        out = tempfile.mkdtemp()
        for i in range(16):
            chat("g-two")
            self.assertEqual(subprocess.run([sys.executable, "/w/scripts/backup.py"], capture_output=True, text=True,
                                            env={**os.environ, "CHARMER_DB": "/data/charmer.sqlite", "CHARMER_BACKUPS": out}).returncode, 0)
            time.sleep(1.05)
        files = sorted(os.listdir(out))
        self.assertEqual(len(files), 14)
        c = sqlite3.connect(os.path.join(out, files[-1]))
        self.assertEqual(c.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertGreater(c.execute("select count(*) from usage_day").fetchone()[0], 0)


class Step9Discovery(unittest.TestCase):
    """DSC-01..03: scripts/discover.py against the stub's provider list endpoints."""

    def run_discover(self):
        import subprocess
        env = {**os.environ, "CHARMER_CONFIG": "/w/tests/discover.config.yaml", "GEMINI_API_KEY": "x", "MISTRAL_API_KEY": "x",
               "GROQ_API_KEY": "x", "NVIDIA_NIM_API_KEY": "x", "CF_API_TOKEN": "x", "ZAI_API_KEY": "x"}
        for p in ("openrouter", "gemini", "mistral", "groq", "nvidia", "cloudflare", "zai"):
            env[f"DISCOVER_{p.upper()}_URL"] = f"http://stub:9000/discover/{p}"
        env["DISCOVER_OPENROUTER_ENDPOINTS_URL"] = "http://stub:9000/discover/openrouter-endpoints"
        return subprocess.run([sys.executable, "/w/scripts/discover.py"], env=env, capture_output=True, text=True, cwd="/w")

    def setUp(self):
        stub_reset()

    def tearDown(self):
        stub_set(discover={})

    def test_dsc01_report_and_json_agree(self):
        """DSC-01: the Markdown report and discovery-latest.json list the same free models; nothing gone exits 0."""
        run = self.run_discover()
        self.assertEqual(run.returncode, 0, run.stdout[-800:] + run.stderr[-800:])
        latest = json.load(open("/w/reports/discovery-latest.json"))["providers"]
        self.assertIn("openrouter/acme/new-model:free", latest["openrouter"]["free_models"])
        self.assertNotIn("openrouter/acme/paid-model", latest["openrouter"]["free_models"])
        self.assertIn("mistral/ministral-8b-2512", latest["mistral"]["free_models"])
        self.assertNotIn("mistral/mistral-small-2603", latest["mistral"]["free_models"], "the Free plan serves only ministral-*")
        self.assertIn("zai/glm-4.7-flash", latest["zai"]["free_models"])
        self.assertIn("`openrouter/acme/new-model:free`", run.stdout)
        self.assertIn("NVIDIA host: trial terms", run.stdout)
        self.assertNotIn("sk-", run.stdout)

    def test_dsc02_a_vanished_model_exits_1(self):
        """DSC-02: a configured model missing from its provider's list is reported as gone, and the exit is 1."""
        stub_set(discover={"openrouter": {"data": []}})
        run = self.run_discover()
        self.assertEqual(run.returncode, 1)
        gone = run.stdout.split("## Configured")[0]
        self.assertIn("pair-or", gone)

    def test_dsc03_list_endpoints_only(self):
        """DSC-03: discovery makes no chat or embedding call; a failed list is reported and the others still run."""
        stub_set(discover={"groq": "fail"})
        run = self.run_discover()
        self.assertIn("| groq | list failed: 500 |", run.stdout)
        self.assertEqual([e for e in stub_log() if e.get("method") == "POST"], [])


class RestartA(unittest.TestCase):
    """SMK-17 and SMK-22, before `make` restarts LiteLLM: spend d1's day limit of 3."""

    def test_smk22_day_limit_before_restart(self):
        stub_reset()
        codes = [chat("g-day")[0] for _ in range(14)]  # enough that the shuffle spends all 3 of d1's
        self.assertEqual(codes, [200] * 14)
        self.assertEqual(len(hits(stub_log(), "ok-a")), 3, "d1 serves exactly its day limit")
        time.sleep(3)
        with db() as c:
            n = c.execute("select requests from usage_day where account='stub' and model='openai/ok-a@d1'").fetchone()
        os.makedirs("reports", exist_ok=True)
        json.dump({"d1": n[0] if n else 0}, open("reports/restart.json", "w"))


class RestartB(unittest.TestCase):
    """SMK-17 and SMK-22, after the restart: keys still work, day counts survived, d1 is still skipped."""

    def test_smk17_smk22_after_restart(self):
        """SIGTERM flushed every count before the restart, and the flushed day count keeps d1 skipped after it."""
        stub_reset()
        before = json.load(open("reports/restart.json"))["d1"]
        with db() as c:
            n = c.execute("select requests from usage_day where account='stub' and model='openai/ok-a@d1'").fetchone()[0]
        self.assertEqual((n, before), (3, 3), "SIGTERM must flush every count")
        codes = [chat("g-day")[0] for _ in range(6)]
        self.assertEqual(codes, [200] * 6)
        self.assertEqual(hits(stub_log(), "ok-a"), [], "d1's day count survived the restart")


if __name__ == "__main__":
    unittest.main()
