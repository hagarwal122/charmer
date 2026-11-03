"""Live dashboard e2e (DSH-12) in a real Chromium, against the running stub stack. Docker only:
make test-e2e  (Playwright image on the test compose network).

Signs in, opens every page in dark and light at desktop and phone width, saves screenshots to reports/shots/,
and fails on a console error, a sideways page scroll, a phone table value slipping under its label, a missing live
refresh, a chart in the viewer's time zone instead of the dashboard's, or a Pull that does not stop traffic.
"""
import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DASHBOARD", "http://dashboard:8080")
LITELLM = os.environ.get("LITELLM", "http://litellm:4000")
USERNAME = os.environ["DASHBOARD_USERNAME"]
PASSWORD = os.environ["DASHBOARD_PASSWORD"]
KEY = os.environ["CHARMER_API_KEY"]
PAGES = ["/", "/calls", "/providers", "/pairs", "/alerts", "/brand"]
SIZES = {"desktop": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}
SHOTS = "reports/shots"
failures = []


def check(ok, message):
    print(("ok   " if ok else "FAIL ") + message, flush=True)
    if not ok:
        failures.append(message)


def call(group):
    req = urllib.request.Request(f"{LITELLM}/v1/chat/completions", method="POST",
                                 data=json.dumps({"model": group, "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                 headers={"content-type": "application/json", "authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("x-litellm-model-id")


def sign_in(page):
    page.goto(f"{BASE}/login")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE}/")


def main():
    os.makedirs(SHOTS, exist_ok=True)
    for i in range(36):  # warm-up traffic, so the screenshots show a working pool
        try:
            call(["g-two", "g-two", "g-hdr", "g-groq", "g-or", "g-cf"][i % 6])
        except Exception:  # noqa: BLE001 - a busy answer is fine here
            pass
    time.sleep(3)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("dark", "light"):
            for size, viewport in SIZES.items():
                ctx = browser.new_context(viewport=viewport, color_scheme=scheme, reduced_motion="reduce")
                page = ctx.new_page()
                errors = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"{BASE}/login")
                page.screenshot(path=f"{SHOTS}/{scheme}-{size}-login.png", full_page=True)
                sign_in(page)
                for path in PAGES:
                    page.goto(BASE + path)
                    page.wait_for_load_state("networkidle")
                    name = "now" if path == "/" else path.strip("/")
                    page.screenshot(path=f"{SHOTS}/{scheme}-{size}-{name}.png", full_page=True)
                    page.screenshot(path=f"{SHOTS}/{scheme}-{size}-{name}-top.png")
                    overflow = page.evaluate("document.scrollingElement.scrollWidth - window.innerWidth")
                    check(overflow <= 1, f"{scheme} {size} {path}: no sideways scroll (overflow {overflow}px)")
                    check(page.locator("h1").count() == 1, f"{scheme} {size} {path}: one h1")
                    if size == "phone" and path in ("/", "/pairs"):
                        slipped = page.evaluate("""[...document.querySelectorAll('.table:not(.table-calls) tbody td')].filter(td =>
                            [...td.children].some(c => c.getBoundingClientRect().left < td.getBoundingClientRect().left + 120)).length""")
                        check(slipped == 0, f"{scheme} phone {path}: every value sits right of its label ({slipped} slipped)")
                        wide = page.evaluate("Math.max(0, ...[...document.querySelectorAll('td .chip')].map(c => c.offsetWidth))")
                        check(wide < 160, f"{scheme} phone {path}: state chips hug their word ({wide}px)")
                    if size == "phone" and path in ("/", "/calls"):
                        tall = page.evaluate("Math.max(0, ...[...document.querySelectorAll('.table-calls tbody tr')].map(r => r.offsetHeight))")
                        check(0 < tall < 80, f"{scheme} phone {path}: a call is two short lines ({tall}px)")
                check(not errors, f"{scheme} {size}: no console errors {errors[:3]}")
                response = page.goto(f"{BASE}/no-such-page")  # after the error check: a 404 page logs its own status
                check(response.status == 404 and page.locator("h1").count() == 1, f"{scheme} {size}: an unknown page is a styled 404")
                page.screenshot(path=f"{SHOTS}/{scheme}-{size}-404.png")
                ctx.close()

        # nothing spills out of its card at any width between a tablet and a wide desktop (a 4-column table in a
        # narrow card used to run past the card's edge)
        ctx = browser.new_context(viewport=SIZES["desktop"], color_scheme="dark")
        page = ctx.new_page()
        sign_in(page)
        for width in (700, 768, 1024, 1280, 1601, 1920):
            page.set_viewport_size({"width": width, "height": 900})
            for path in ("/", "/pairs", "/providers", "/calls", "/alerts"):
                page.goto(BASE + path)
                spilled = page.evaluate("""[...document.querySelectorAll('.card')].flatMap(card => {
                    const c = card.getBoundingClientRect();
                    return [...card.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect();
                        return r.width && (r.right > c.right + 1 || r.left < c.left - 1); }).map(e => e.tagName);
                }).length""")
                check(spilled == 0, f"{width}px {path}: nothing spills out of its card ({spilled} elements)")
        ctx.close()

        # live refresh, keyboard, and the Pull/Unpull flow (DSH-12)
        ctx = browser.new_context(viewport=SIZES["desktop"], color_scheme="dark")
        page = ctx.new_page()
        sign_in(page)
        page.keyboard.press("Tab")
        check(page.evaluate("document.activeElement.className") == "skip", "the first Tab lands on Skip to content")
        before = page.locator('[data-bind="kpis.requests_today.value"]').inner_text()
        for _ in range(4):
            call("g-two")
        page.wait_for_timeout(6500)
        after = page.locator('[data-bind="kpis.requests_today.value"]').inner_text()
        check(after != before, f"Now refreshes live without a reload ({before} -> {after})")
        check(page.locator("[data-updated]").inner_text() == "live", "the refresh label says live")
        check(page.locator(".chart svg rect.bar").count() > 0, "the requests-by-pair chart draws bars")
        labels = page.locator(".chart svg text.axis").all_text_contents()
        hours = [t for t in labels if ":" in t]
        check(hours and hours[0] == "00:00" and all(t.endswith(":00") for t in hours),
              f"Today's axis starts at midnight in the dashboard's zone, on the hour ({hours})")
        check(0 < page.locator('[data-legend="by_pair"] li').count() <= 6, "the legend names at most five pairs and Other")
        box = page.locator(".chart svg").bounding_box()
        page.mouse.move(box["x"] + box["width"] - 3, box["y"] + box["height"] / 2)
        check(page.locator(".chart-tip").is_visible() and "requests" in page.locator(".chart-tip").inner_text(),
              "hovering the latest hour shows its requests per pair")
        page.route("**/api/state", lambda route: route.abort())
        page.wait_for_timeout(16500)
        first = page.locator("[data-updated]").inner_text()
        page.wait_for_timeout(2200)
        check(first.startswith("paused") and page.locator("[data-updated]").inner_text() == first,
              f"a stale page says paused once and does not re-announce every second ({first})")
        page.unroute("**/api/state")

        page.goto(f"{BASE}/pairs")
        page.click('form[action="/pairs/pull"]:has(input[value="two-a"]) button')
        page.wait_for_url(f"{BASE}/pairs?changed=two-a")
        row = page.locator("tr", has=page.locator("span.mono", has_text="two-a")).first
        check("pulled" in row.inner_text(), "after Pull, two-a shows pulled")
        check("takes no calls until you Unpull" in page.locator(".notice").inner_text(), "after Pull, a notice says what changed")
        time.sleep(3)
        served = [call("g-two") for _ in range(6)]
        check("two-a" not in served, f"after Pull, no call reaches two-a, no restart ({served})")
        page.screenshot(path=f"{SHOTS}/flow-pulled.png", full_page=True)
        row.locator('form[action="/pairs/unpull"] button').click()
        page.wait_for_url(f"{BASE}/pairs?changed=two-a")
        time.sleep(3)
        served = {call("g-two") for _ in range(16)}
        check("two-a" in served, f"after Unpull, two-a serves again ({sorted(served)})")
        ctx.close()
        browser.close()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
