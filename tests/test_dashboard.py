"""Dashboard cells DSH-01..10 and DSH-13. Run in the dashboard image: make test-dashboard.

The app runs in-process on 127.0.0.1 against a fixture SQLite file, fixture config files and a fake LiteLLM that
records every request it gets.
"""
import html
import http.cookiejar
import http.server
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = pathlib.Path(tempfile.mkdtemp())
PASSWORD = "correct-horse-battery-staple"
USERNAME = "charmer-owner"
SECRET = "s" * 40
METRICS_KEY = "sk-test-metrics-" + "9" * 30
CANARY = "CANARY-DASH-4471"
LITELLM_SEEN = []
LITELLM_UP = {"up": True}


class FakeLiteLLM(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        LITELLM_SEEN.append((self.path, self.headers.get("authorization")))
        if not LITELLM_UP["up"]:
            self.send_response(503)
            self.end_headers()
            return
        if self.path.startswith("/health/liveliness"):
            body = b'"I\'m alive!"'
        elif self.path.startswith("/metrics"):
            body = (b'litellm_deployment_state{model_id="fail-rl",model_group="g-fail"} 2.0\n'
                    b'litellm_deployment_state{model_id="two-a",model_group="g-two"} 0.0\n')
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        LITELLM_SEEN.append((self.path, "POST"))
        self.send_response(404)
        self.end_headers()


def build_fixtures():
    import yaml
    sys.path.insert(0, str(ROOT / "plugin"))
    import store as st
    shutil.copy(ROOT / "tests/config.test.yaml", FIX / "config.yaml")
    shutil.copy(ROOT / "tests/limits.test.yaml", FIX / "limits.yaml")
    cfg = yaml.safe_load((FIX / "config.yaml").read_text())
    rendered = {k: v for k, v in cfg.items() if not k.startswith("x-")}
    rendered["model_list"] = [d for d in cfg["model_list"] if d["model_info"]["account"] != "cloudflare" and d["model_info"]["id"] != "empty1"]  # cloudflare: no key
    (FIX / "rendered").mkdir()
    (FIX / "rendered/config.yaml").write_text(yaml.safe_dump(rendered))
    (FIX / "reports").mkdir()
    (FIX / "reports/discovery-latest.json").write_text(json.dumps(
        {"generated_at": "2026-10-28T05:00:00Z", "providers": {"stub": {"free_models": ["openai/ok-a@two-a", "openai/new-free-model"]}}}))
    s = st.Store(str(FIX / "charmer.sqlite"))
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    for m in range(0, 120, 7):
        ts = now - m * 60
        s.add("usage", "stub", "*", st._minute(ts), day, 3, 300, 30, 0, 0)
        s.add("usage", "stub", "openai/ok-a@two-a", st._minute(ts), day, 2, 200, 20, 0, 0)
        s.add("usage", "stub", "openai/ok-b@two-b", st._minute(ts), day, 1, 100, 10, 0, 0)
    s.add("usage", "stub", "openai/ok-a@d1", st._minute(now), day, 3, 30, 3, 0, 0)
    s.add("usage", "acct-x", "*", st._minute(now), day, 3, 10, 1, 0, 0)
    s.add("usage", "acct-x", "openai/ok-a@x1", st._minute(now), day, 3, 10, 1, 0, 0)
    s.add("key_usage", "smoke", "g-two", st._minute(now), 40, 4000, 2)
    s.add("provider_state", "h1", {"remaining_requests": 2, "remaining_tokens": None, "reset_at": now - 5, "seen": now - 65, "last_429": None}, now - 65)
    s.add("provider_state", "h2", {"remaining_requests": 7, "remaining_tokens": None, "reset_at": now + 50, "seen": now - 10, "last_429": None}, now - 10)
    for i in range(12):
        s.add("recent", now - i, "smoke", "g-two", "two-a" if i % 2 else "two-b", "abc123", 200, 340 + i, 330, i == 3)
    s.add("recent", now - 30, "app", "", "", None, 429, 5, 0, False)  # answered by the router itself: no group
    s.add_pull("rpm-b", "testing", "owner")
    s.add_pull("gm1", "Gemini 402: billing attached", "guard")
    s.add_alert("near_limit:acct-x", "acct-x has used 3 of 4 requests today", now - 30)
    s.flush()


def start():
    build_fixtures()
    fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    os.environ.update({
        "CHARMER_DB": str(FIX / "charmer.sqlite"), "CHARMER_CONFIG": str(FIX / "config.yaml"),
        "CHARMER_RENDERED": str(FIX / "rendered/config.yaml"), "CHARMER_LIMITS": str(FIX / "limits.yaml"),
        "CHARMER_REPORTS": str(FIX / "reports"),
        "LITELLM_URL": f"http://127.0.0.1:{fake.server_address[1]}", "DASHBOARD_PASSWORD": PASSWORD, "DASHBOARD_USERNAME": USERNAME,
        "DASHBOARD_SESSION_SECRET": SECRET, "CHARMER_METRICS_KEY": METRICS_KEY, "DASHBOARD_HEALTH_SECONDS": "0.3",
        "CHARMER_DASHBOARD_TZ": "Asia/Kolkata"})
    sys.path.insert(0, str(ROOT / "dashboard"))
    sys.path.insert(0, str(ROOT / "plugin"))
    import uvicorn
    import app as dash
    server = uvicorn.Server(uvicorn.Config(dash.app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", dash


BASE, DASH = start()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect()), jar


def req(opener, method, path, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with opener.open(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def login(opener=None, password=PASSWORD, headers=None, username=USERNAME):
    opener = opener or client()[0]
    status, h, _ = req(opener, "POST", "/login", {"username": username, "password": password}, headers)
    return opener, status, h


def csrf_of(opener):
    status, _, body = req(opener, "GET", "/pairs")
    m = re.search(r'name="csrf" value="([^"]+)"', body)
    return m.group(1) if m else None


class Data(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.o = login()[0]
        status, _, body = req(cls.o, "GET", "/api/state")
        assert status == 200, body
        cls.state = json.loads(body)

    def test_dsh01_every_deployment_account_and_key_with_used_limit_reset(self):
        """DSH-01: every deployment, account and key appears with used, limit and reset; unknown is null, never 0."""
        pairs = {p["id"]: p for p in self.state["pairs"]}
        for d in ("two-a", "two-b", "h1", "d1", "x1", "q2"):
            self.assertIn(d, pairs)
        x1 = pairs["x1"]
        self.assertEqual(x1["day"]["requests"]["used"], 3)
        self.assertEqual(x1["day"]["requests"]["limit"], 4)
        self.assertIsNotNone(x1["day"]["resets_at"])
        self.assertIsNone(pairs["two-a"]["day"]["requests"]["limit"], "no limit is null, not 0")
        self.assertEqual(pairs["two-a"]["day"]["requests"]["used"] * 3, pairs["two-b"]["day"]["requests"]["used"] * 6,
                         "a pair with no limit of its own shows its own count, never the account's total")
        self.assertEqual(x1["day"]["requests"]["scope"], "account")
        accounts = {a["name"]: a for a in self.state["providers"]}
        self.assertEqual(accounts["acct-x"]["day"]["requests"]["used"], 3)
        self.assertNotIn("keys", self.state, "one key for one user: no Keys page")
        self.assertIn("g-empty-declared", {g["name"] for g in self.state["groups"]}, "groups declared in limits.yaml show, empty")
        self.assertEqual(self.state["kpis"]["busy_today"], 2)
        self.assertGreater(self.state["kpis"]["requests_today"]["value"], 0)
        self.assertEqual(len(self.state["kpis"]["requests_today"]["series"]), 48)

    def test_calls_filters_default_to_all(self):
        """With no filter set, every Calls filter shows All, even when the router answered a call itself (no group)."""
        import re as _re
        o = login()[0]
        body = req(o, "GET", "/calls")[2]
        for name in ("key", "group", "deployment", "status"):
            select = _re.search(rf'<select name="{name}">(.*?)</select>', body, _re.S).group(1)
            self.assertNotIn("selected", select, name)

    def test_keys_page_is_gone(self):
        o = login()[0]
        self.assertEqual(req(o, "GET", "/keys")[0], 404)

    def test_dsh02_no_key_provider(self):
        """DSH-02: a provider whose deployments are all left out is "no key"; its deployments are absent from groups."""
        accounts = {a["name"]: a for a in self.state["providers"]}
        self.assertEqual(accounts["cloudflare"]["status"], "no key")
        grouped = {m["id"] for g in self.state["groups"] for m in g["members"]}
        self.assertNotIn("cf1", grouped)
        self.assertIn("openai/new-free-model", accounts["stub"]["available_models"])
        self.assertNotIn("openai/ok-a@two-a", accounts["stub"]["available_models"])

    def test_dsh03_expired_readings(self):
        """DSH-03: a header reading past its reset is shown as expired, not current."""
        pairs = {p["id"]: p for p in self.state["pairs"]}
        self.assertTrue(pairs["h1"]["provider"]["expired"])
        self.assertFalse(pairs["h2"]["provider"]["expired"])
        self.assertEqual(pairs["h2"]["provider"]["remaining_requests"], 7)

    def test_states_pulled_cooling_limit(self):
        """pulled, cooling (LiteLLM metrics) and day-limit states come through; a guard pull has no Unpull."""
        pairs = {p["id"]: p for p in self.state["pairs"]}
        self.assertEqual(pairs["rpm-b"]["state"], "pulled")
        self.assertTrue(pairs["rpm-b"]["can_unpull"])
        self.assertEqual(pairs["gm1"]["state"], "pulled")
        self.assertFalse(pairs["gm1"]["can_unpull"])
        self.assertEqual(pairs["fail-rl"]["state"], "cooling")
        self.assertEqual(pairs["d1"]["state"], "filtered")
        self.assertEqual((pairs["cf1"]["state"], pairs["cf1"]["label"]), ("nokey", "no key"))
        self.assertEqual(pairs["two-a"]["state"], "serving")


class Stale(unittest.TestCase):
    def test_rendered_deployment_missing_from_config_does_not_crash(self):
        """A rendered deployment the dashboard's config copy lacks crashes no page; Now shows it as "restart pending"."""
        import yaml
        path = FIX / "rendered/config.yaml"
        before = path.read_text()
        doc = yaml.safe_load(before)
        doc["model_list"].append({"model_name": "g-two", "model_info": {"id": "brand-new", "account": "stub"}, "litellm_params": {"model": "x"}})
        path.write_text(yaml.safe_dump(doc))
        try:
            o = login()[0]
            for page in ("/", "/calls", "/providers", "/pairs", "/alerts", "/brand", "/api/state"):
                status, _, body = req(o, "GET", page)
                self.assertEqual(status, 200, page)
            self.assertIn("restart pending", req(o, "GET", "/")[2], "the Now page names it in its group")
        finally:
            path.write_text(before)


class Health(unittest.TestCase):
    def test_dsh04_litellm_down(self):
        """DSH-04: with LiteLLM down, pages load from SQLite saying so; after 3 failed checks, one litellm_down alert."""
        o = login()[0]
        LITELLM_UP["up"] = False
        try:
            import sqlite3
            for _ in range(50):  # three failed checks at 0.3 s each, polled rather than slept
                with sqlite3.connect(os.environ["CHARMER_DB"]) as c:
                    if c.execute("select count(*) from alerts where kind='litellm_down'").fetchone()[0]:
                        break
                time.sleep(0.1)
            status, _, body = req(o, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("LiteLLM unreachable", body)
            import sqlite3
            with sqlite3.connect(os.environ["CHARMER_DB"]) as c:
                n = c.execute("select count(*) from alerts where kind='litellm_down'").fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            LITELLM_UP["up"] = True
            time.sleep(0.8)

    def test_dsh05_never_calls_a_provider_or_chat(self):
        """DSH-05: opening every page and the JSON makes no request but /metrics and /health/liveliness to LiteLLM."""
        o = login()[0]
        LITELLM_SEEN.clear()
        for path in ("/", "/calls", "/providers", "/pairs", "/alerts", "/api/state"):
            self.assertEqual(req(o, "GET", path)[0], 200, path)
        paths = {p.split("?")[0].rstrip("/") for p, _ in LITELLM_SEEN}
        self.assertTrue(paths <= {"/metrics", "/health/liveliness"}, paths)
        self.assertTrue(all(a in (f"Bearer {METRICS_KEY}", None) for p, a in LITELLM_SEEN if p.startswith("/metrics")))


class Auth(unittest.TestCase):
    def test_dsh06_everything_needs_a_session(self):
        """DSH-06: every route but /login, /healthz and static assets redirects or answers 401 without a session."""
        o = client()[0]
        for path in ("/", "/calls", "/providers", "/pairs", "/alerts", "/brand"):
            status, h, _ = req(o, "GET", path)
            self.assertIn(status, (302, 303), path)
            self.assertTrue(h.get("location", h.get("Location", "")).startswith("/login"))
        self.assertEqual(req(o, "GET", "/api/state")[0], 401)
        self.assertEqual(req(o, "POST", "/pairs/pull", {"id": "two-a"})[0], 401)
        self.assertEqual(req(o, "GET", "/login")[0], 200)
        self.assertEqual(req(o, "GET", "/healthz")[0], 200)

    def test_dsh07_wrong_password_and_throttle(self):
        """DSH-07: a wrong password is refused; the 6th try in a minute is refused even with the right one."""
        h = {"cf-connecting-ip": "198.51.100.7"}
        for _ in range(5):
            _, status, _ = login(password="wrong", headers=h)
            self.assertNotIn(status, (302, 303))
        _, status, _ = login(headers=h)
        self.assertEqual(status, 429)
        _, status, _ = login(headers={"cf-connecting-ip": "198.51.100.8"})
        self.assertIn(status, (302, 303))

    def test_dsh14_username_and_password_from_env(self):
        """DSH-14: sign-in takes DASHBOARD_USERNAME and DASHBOARD_PASSWORD from .env; either one wrong or missing is
        refused with one message that names neither as the wrong one; the form asks for both."""
        for ip, user, pw in (("198.51.100.20", "someone-else", PASSWORD), ("198.51.100.21", USERNAME, "wrong"),
                             ("198.51.100.22", "", PASSWORD)):
            o = client()[0]
            status, _, body = req(o, "POST", "/login", {"username": user, "password": pw}, {"cf-connecting-ip": ip})
            self.assertEqual(status, 401, user)
            self.assertIn("That username or password isn't right.", html.unescape(body))
        o = client()[0]
        status, _, _ = req(o, "POST", "/login", {"password": PASSWORD}, {"cf-connecting-ip": "198.51.100.23"})
        self.assertEqual(status, 401, "no username at all")
        _, status, _ = login(headers={"cf-connecting-ip": "198.51.100.24"})
        self.assertIn(status, (302, 303))
        _, _, page = req(client()[0], "GET", "/login")
        self.assertIn('name="username"', page)
        self.assertIn('autocomplete="username"', page)
        self.assertIn('name="password"', page)

    def test_dsh14_start_refuses_without_a_username(self):
        """DSH-14: the dashboard refuses to start without DASHBOARD_USERNAME, like a short password."""
        import subprocess
        env = {**os.environ, "DASHBOARD_USERNAME": ""}
        r = subprocess.run([sys.executable, "-c", "import auth"], cwd=str(ROOT / "dashboard"), env=env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DASHBOARD_USERNAME", r.stderr + r.stdout)

    def test_dsh08_cookie_flags_and_signature(self):
        """DSH-08: HttpOnly and SameSite=Lax; Secure only when https came from cloudflared; a forged cookie is refused."""
        _, _, h = login()
        cookie = h.get("set-cookie") or h.get("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("samesite=lax", cookie.lower())
        self.assertNotIn("Secure", cookie)
        _, _, h = login(headers={"x-forwarded-proto": "https", "cf-connecting-ip": "203.0.113.4"})
        self.assertIn("Secure", h.get("set-cookie") or h.get("Set-Cookie"))
        _, _, h = login(headers={"x-forwarded-proto": "https"})
        self.assertNotIn("Secure", h.get("set-cookie") or h.get("Set-Cookie"), "X-Forwarded-Proto alone is not trusted")
        value = cookie.split(";")[0].split("=", 1)[1]
        forged = value[:-4] + ("aaaa" if not value.endswith("aaaa") else "bbbb")
        o = client()[0]
        status, _, _ = req(o, "GET", "/api/state", headers={"cookie": f"charmer_session={forged}"})
        self.assertEqual(status, 401)

    def test_dsh09_csrf(self):
        """DSH-09: Pull, Unpull and Clear without the CSRF token answer 403 and change nothing."""
        o = login()[0]
        for path, data in (("/pairs/pull", {"id": "two-a"}), ("/pairs/unpull", {"id": "rpm-b"}), ("/alerts/clear", {"id": "1"})):
            self.assertEqual(req(o, "POST", path, data)[0], 403, path)
            self.assertEqual(req(o, "POST", path, {**data, "csrf": "wrong"})[0], 403, path)
        state = json.loads(req(o, "GET", "/api/state")[2])
        pairs = {p["id"]: p for p in state["pairs"]}
        self.assertEqual(pairs["two-a"]["state"], "serving")
        self.assertEqual(pairs["rpm-b"]["state"], "pulled")

    def test_pull_and_unpull_with_csrf(self):
        """Pull and Unpull write pulls rows (who: dashboard); a guard's pull cannot be lifted here."""
        o = login()[0]
        token = csrf_of(o)
        self.assertIsNotNone(token)
        self.assertIn(req(o, "POST", "/pairs/pull", {"id": "two-b", "csrf": token})[0], (302, 303))
        self.assertIn(req(o, "POST", "/pairs/unpull", {"id": "two-b", "csrf": token})[0], (302, 303))
        self.assertEqual(req(o, "POST", "/pairs/unpull", {"id": "gm1", "csrf": token})[0], 403)
        import sqlite3
        with sqlite3.connect(os.environ["CHARMER_DB"]) as c:
            rows = c.execute("select who, lifted from pulls where target='two-b'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "dashboard")
        self.assertIsNotNone(rows[0][1])


class Proxy(unittest.TestCase):
    def test_secure_cookie_behind_a_proxy(self):
        """Review: X-Forwarded-Proto is trusted with Cloudflare's header, or alone only when DASHBOARD_TRUST_PROXY=1."""
        sys.path.insert(0, str(ROOT / "dashboard"))
        import auth

        class Req:
            def __init__(self, **h):
                self.headers = h
        os.environ.pop("DASHBOARD_TRUST_PROXY", None)
        self.assertFalse(auth.secure_request(Req(**{"x-forwarded-proto": "https"})))
        self.assertTrue(auth.secure_request(Req(**{"x-forwarded-proto": "https", "cf-connecting-ip": "203.0.113.9"})))
        os.environ["DASHBOARD_TRUST_PROXY"] = "1"
        try:
            self.assertTrue(auth.secure_request(Req(**{"x-forwarded-proto": "https"})))
            self.assertFalse(auth.secure_request(Req(**{"x-forwarded-proto": "http"})))
        finally:
            os.environ.pop("DASHBOARD_TRUST_PROXY")


class Secrets(unittest.TestCase):
    def test_dsh10_no_secret_in_any_page(self):
        """DSH-10: no page or JSON carries a key, the metrics key, the password or prompt text."""
        o = login()[0]
        secrets = [PASSWORD, SECRET, METRICS_KEY, "sk-test-", "stub-provider-key", CANARY]
        for path in ("/", "/calls", "/providers", "/pairs", "/alerts", "/api/state", "/login", "/brand"):
            status, _, body = req(o, "GET", path)
            self.assertEqual(status, 200, path)
            for s in secrets:
                self.assertNotIn(s, body, f"{path} carries a secret")


class Tokens(unittest.TestCase):
    def test_dsh13_only_tokens_css_holds_literals(self):
        """DSH-13: brand files exist; no template, stylesheet or script but tokens.css has a literal colour, font,
        font size or shadow."""
        self.assertTrue((ROOT / "dashboard/brand/mark.svg").exists())
        self.assertTrue((ROOT / "dashboard/brand/README.md").exists())
        self.assertTrue((ROOT / "dashboard/static/tokens.css").exists())
        bad = []
        files = list((ROOT / "dashboard/templates").glob("*.html")) + [
            p for p in (ROOT / "dashboard/static").glob("*") if p.suffix in (".css", ".js") and p.name != "tokens.css"]
        rules = [r"#[0-9a-fA-F]{3,8}\b(?![-\w])", r"\brgba?\(", r"\bhsla?\(", r"font-family\s*:(?!\s*var\()",
                 r"font-size\s*:\s*[0-9.]", r"box-shadow\s*:(?!\s*(var\(|none))"]
        for f in files:
            text = f.read_text()
            text = re.sub(r"&#\d+;|&#x[0-9a-fA-F]+;", "", text)
            for rule in rules:
                for m in re.finditer(rule, text):
                    bad.append(f"{f.name}: {m.group(0)}")
        self.assertEqual(bad, [])
        self.assertTrue(files, "no templates or styles found")


if __name__ == "__main__":
    unittest.main()
