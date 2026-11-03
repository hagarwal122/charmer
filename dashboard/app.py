"""Charmer's dashboard (05, C16). FastAPI, server-rendered pages, one JSON endpoint for live refresh.

Every route needs a session except /login, /healthz and /static. Writes: pulls (Pull/Unpull) and alerts (Clear) only.
"""
import datetime as dt
import hashlib
import os
import statistics
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

import auth
import state as S
import store as st

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = st.Store(S.DB)
HEALTH = S.Health(STORE)
HEALTH.start()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
PUBLIC = ("/login", "/healthz", "/static/")
PAGES = [("now", "/", "Now"), ("calls", "/calls", "Calls"), ("providers", "/providers", "Providers"),
         ("pairs", "/pairs", "Pairs"), ("alerts", "/alerts", "Alerts")]


# ---- formatting for templates ---------------------------------------------------------------------------------------
def _num(v):
    if v is None:
        return "—"
    v = float(v)
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e5, "k")):
        if abs(v) >= size:
            divisor = 1e3 if unit == "k" else size
            return f"{v / divisor:.1f}".removesuffix(".0") + unit
    return f"{v:,.0f}"


def _clock(ts):
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts, S.TZ).strftime("%H:%M")


def _when(ts):
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts, S.TZ)
    today = dt.datetime.now(S.TZ).date()
    return d.strftime("%H:%M") if d.date() == today else d.strftime("%d %b %H:%M")


def _stamp(ts):
    """A call's time to the second: two calls in one minute must read apart."""
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts, S.TZ)
    return d.strftime("%H:%M:%S") if d.date() == dt.datetime.now(S.TZ).date() else d.strftime("%d %b %H:%M:%S")


def _ago(ts):
    if not ts:
        return "—"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


templates.env.filters.update(num=_num, clock=_clock, when=_when, stamp=_stamp, ago=_ago)
def _theme_bg():
    """The page background per scheme, read from tokens.css for <meta name="theme-color"> (tokens stay the one source)."""
    import re
    css = open(os.path.join(HERE, "static", "tokens.css")).read()
    found = re.findall(r"--c-bg:\s*([^;]+);", css)
    return {"dark": found[0].strip(), "light": found[1].strip() if len(found) > 1 else found[0].strip()}


def _asset_version():
    """A hash of the static files: asset URLs change with their content, so no browser keeps an old app.css or app.js."""
    h = hashlib.sha256()
    folder = os.path.join(HERE, "static")
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), "rb") as f:
            h.update(name.encode() + f.read())
    return h.hexdigest()[:10]


templates.env.globals.update(pages=PAGES, tzname=str(S.TZ), theme_bg=_theme_bg(), asset_v=_asset_version())


# ---- sessions -------------------------------------------------------------------------------------------------------
@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC):
        return await call_next(request)
    session = auth.read_session(request.cookies.get(auth.COOKIE))
    if session is None:
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"error": "sign in first"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.session = session
    return await call_next(request)


async def form(request):
    return {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}


def page(request, name, data=None, status=200, **ctx):
    return templates.TemplateResponse(request, f"{name}.html", {
        "page": name, "s": data or S.build(STORE, HEALTH), "csrf": request.state.session["csrf"], **ctx}, status_code=status)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """A signed-in GET of an unknown page gets a page with the sidebar; everything else keeps the JSON answer."""
    if exc.status_code == 404 and request.method == "GET" and not request.url.path.startswith(("/api/", "/static/")) \
            and getattr(request.state, "session", None):
        return page(request, "404", status=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"page": "login", "error": None})


@app.post("/login")
async def login(request: Request):
    who = auth.client_id(request)
    if auth.throttled(who):
        return templates.TemplateResponse(request, "login.html", {"page": "login", "error": "Too many tries from here. Wait a minute, then try again."},
                                          status_code=429)
    sent = await form(request)
    if not auth.credentials_ok(sent.get("username", ""), sent.get("password", "")):
        auth.failed_try(who)
        return templates.TemplateResponse(request, "login.html", {"page": "login", "username": sent.get("username", ""),
                                          "error": "That username or password isn’t right."},
                                          status_code=401)
    value, _ = auth.new_session()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, value, max_age=auth.SESSION_S, httponly=True, samesite="lax",
                    secure=auth.secure_request(request), path="/")
    return resp


@app.post("/logout")
async def logout(request: Request):
    if (await form(request)).get("csrf") != request.state.session["csrf"]:
        return JSONResponse({"error": "bad csrf token"}, status_code=403)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/state")
async def api_state():
    return S.build(STORE, HEALTH)


@app.get("/", response_class=HTMLResponse)
async def now_page(request: Request):
    data = S.build(STORE, HEALTH)
    # every deployment is also its own pair-<id> group for evals: one card lists them, the consumers' groups get a card each
    return page(request, "now", data, groups=[g for g in data["groups"] if not g["name"].startswith("pair-")],
                evals=[g for g in data["groups"] if g["name"].startswith("pair-")])


CALLS_PER_PAGE = 50


@app.get("/calls", response_class=HTMLResponse)
async def calls_page(request: Request):
    q = request.query_params
    filters = {k: q.get(k, "") for k in ("key", "group", "deployment", "status")}
    data = S.build(STORE, HEALTH)
    recent = data["recent"]
    rows = [c for c in recent if all(not filters[k] or c[k] == filters[k] for k in ("key", "group", "deployment"))]
    if filters["status"] in ("ok", "error"):
        rows = [c for c in rows if ((c["status"] or 0) < 400) == (filters["status"] == "ok")]
    pages = max(1, -(-len(rows) // CALLS_PER_PAGE))
    try:
        n = min(max(1, int(q.get("page", "1"))), pages)
    except ValueError:
        n = 1
    first = (n - 1) * CALLS_PER_PAGE
    ok = sum(1 for c in rows if (c["status"] or 0) < 400)
    latency = [c["latency_ms"] for c in rows if c["latency_ms"] is not None]
    kept = {k: v for k, v in filters.items() if v}
    return page(request, "calls", data, filters=filters, rows=rows[first:first + CALLS_PER_PAGE], total=len(rows),
                first=first, ok_pct=round(100 * ok / len(rows)) if rows else None,
                median=round(statistics.median(latency)) if latency else None,
                prev=f"/calls?{urllib.parse.urlencode({**kept, 'page': n - 1})}" if n > 1 else None,
                next=f"/calls?{urllib.parse.urlencode({**kept, 'page': n + 1})}" if n < pages else None,
                options={k: sorted({c[k] for c in recent if c[k]}) for k in ("key", "group", "deployment")})


@app.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    return page(request, "providers")


ATTENTION = {"near", "filtered", "cooling", "pulled"}


@app.get("/pairs", response_class=HTMLResponse)
async def pairs_page(request: Request):
    q = request.query_params
    group, want = q.get("group", ""), q.get("state", "")
    data = S.build(STORE, HEALTH)
    rows = [p for p in data["pairs"] if (not group or p["group"] == group) and (want != "attention" or p["state"] in ATTENTION)]
    changed = next((p for p in data["pairs"] if p["id"] == q.get("changed")), None)
    return page(request, "pairs", data, rows=rows, group=group, want=want, changed=changed,
                attention=sum(1 for p in data["pairs"] if p["state"] in ATTENTION))


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    return page(request, "alerts")


@app.get("/brand", response_class=HTMLResponse)
async def brand_page(request: Request):
    return page(request, "brand")


async def _checked(request):
    data = await form(request)
    if data.get("csrf") != request.state.session["csrf"]:
        return None
    return data


@app.post("/pairs/pull")
async def pull(request: Request):
    data = await _checked(request)
    if data is None:
        return JSONResponse({"error": "bad csrf token"}, status_code=403)
    target = data.get("id", "").strip()
    known = {p["id"] for p in S.build(STORE, HEALTH)["pairs"]}
    if target not in known:
        return JSONResponse({"error": "unknown deployment"}, status_code=404)
    STORE.add_pull(target, data.get("reason") or "pulled from the dashboard", "dashboard")
    return _save(_pairs_view(data, target))


@app.post("/pairs/unpull")
async def unpull(request: Request):
    data = await _checked(request)
    if data is None:
        return JSONResponse({"error": "bad csrf token"}, status_code=403)
    target = data.get("id", "").strip()
    rows = STORE.query("select who from pulls where target = ? and lifted is null", (target,))
    if not rows:
        return JSONResponse({"error": "not pulled"}, status_code=404)
    if any(who in S.GUARDS for (who,) in rows):
        return JSONResponse({"error": "a guard's pull is lifted by its guard"}, status_code=403)
    STORE.add("unpull", target, time.time(), ["owner", "dashboard"])
    return _save(_pairs_view(data, target))


def _pairs_view(data, changed):
    """Back to the Pairs view the button was pressed in (its group or state filter), naming the pair that changed."""
    kept = {k: data[k] for k in ("group", "state") if data.get(k)}
    return f"/pairs?{urllib.parse.urlencode({**kept, 'changed': changed})}"


@app.post("/alerts/clear")
async def clear_alert(request: Request):
    data = await _checked(request)
    if data is None:
        return JSONResponse({"error": "bad csrf token"}, status_code=403)
    try:
        STORE.add("clear_alert", int(data.get("id", "0")), time.time())
    except ValueError:
        return JSONResponse({"error": "bad alert id"}, status_code=400)
    return _save("/alerts")


def _save(back):
    """Write now; on failure drop the queued change (no later write applies it by surprise) and say so."""
    try:
        STORE.flush()
    except Exception:  # noqa: BLE001
        STORE.discard()
        return JSONResponse({"error": "could not save the change; the SQLite file may be locked or full"}, status_code=503)
    return RedirectResponse(back, status_code=303)
