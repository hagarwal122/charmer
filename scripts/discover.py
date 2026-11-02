"""Model discovery: which free models each provider lists today, and which configured models have gone.

It only reports; it never changes the pool. List endpoints only: no chat call, no quota spent (DSC-03).
Writes reports/discovery-YYYY-MM-DD.md and reports/discovery-latest.json (the dashboard's Providers page).
Exit 1 if a configured model is missing from its provider's list, so cron mails the report. Never prints a key.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

ENV = os.environ.get
URLS = {
    "openrouter": ENV("DISCOVER_OPENROUTER_URL", "https://openrouter.ai/api/v1/models"),
    "openrouter-endpoints": ENV("DISCOVER_OPENROUTER_ENDPOINTS_URL", "https://openrouter.ai/api/v1/models"),
    "gemini": ENV("DISCOVER_GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"),
    "mistral": ENV("DISCOVER_MISTRAL_URL", "https://api.mistral.ai/v1/models"),
    "groq": ENV("DISCOVER_GROQ_URL", "https://api.groq.com/openai/v1/models"),
    "nvidia": ENV("DISCOVER_NVIDIA_URL", "https://integrate.api.nvidia.com/v1/models"),
    "cloudflare": ENV("DISCOVER_CLOUDFLARE_URL",
                      f"https://api.cloudflare.com/client/v4/accounts/{ENV('CF_ACCOUNT_ID', '')}/ai/models/search?per_page=1000"),
    "zai": ENV("DISCOVER_ZAI_URL", "https://api.z.ai/api/paas/v4/models"),
}
KEYS = {"gemini": ("x-goog-api-key", "GEMINI_API_KEY"), "mistral": ("authorization", "MISTRAL_API_KEY"),
        "groq": ("authorization", "GROQ_API_KEY"), "nvidia": ("authorization", "NVIDIA_NIM_API_KEY"),
        "cloudflare": ("authorization", "CF_API_TOKEN"), "zai": ("authorization", "ZAI_API_KEY")}
ROUTE = {"openrouter": "openrouter/", "gemini": "gemini/", "mistral": "mistral/", "groq": "groq/",
         "nvidia": "nvidia_nim/", "cloudflare": "cloudflare/", "zai": "zai/"}
NON_CHAT = ("tts", "image", "lyria", "transcribe", "robotics", "computer-use", "banana", "deep-research", "antigravity", "embedding")
ZAI_FREE = ["glm-4.7-flash"]  # Z.ai's list omits its free model (2026-10-28); glm-5.3-flash bills


def fetch(provider, url=None):
    headers = {"accept": "application/json", "user-agent": "charmer-discovery/1.0"}  # Groq's edge refuses Python-urllib
    if provider in KEYS:
        name, var = KEYS[provider]
        key = ENV(var, "")
        if not key:
            return None, "no key"
        headers[name] = key if name != "authorization" else f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url or URLS[provider], headers=headers), timeout=20) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"list failed: {e.code}"
    except Exception as e:  # noqa: BLE001 - reported, the others still run
        return None, f"list failed: {type(e).__name__}"


def listed(provider):
    """[(model id as the provider names it, free?, tools?, flags)] or an error string."""
    body, err = fetch(provider)
    if err:
        return err
    rows = []
    if provider == "openrouter":
        for m in body.get("data") or []:
            p = m.get("pricing") or {}
            free = str(p.get("prompt")) == "0" and str(p.get("completion")) == "0"
            tools = "tools" in (m.get("supported_parameters") or [])
            flags = []
            if free and tools:
                ep, _ = fetch("openrouter-endpoints", f"{URLS['openrouter-endpoints']}/{urllib.parse.quote(m['id'], safe='/:')}/endpoints")
                hosts = [e.get("provider_name", "") for e in ((ep or {}).get("data") or {}).get("endpoints") or []]
                if any("nvidia" in h.lower() for h in hosts):
                    flags.append("NVIDIA host: trial terms")
            rows.append((m["id"], free, tools, flags))
    elif provider == "gemini":
        for m in body.get("models") or []:
            mid = m["name"].removeprefix("models/")
            if any(word in mid for word in NON_CHAT):
                continue  # not a chat model
            flags = [f for f, hit in (("pro: not free", "pro" in mid), ("preview", "preview" in mid)) if hit] + ["check pricing page"]
            rows.append((mid, "pro" not in mid, "generateContent" in (m.get("supportedGenerationMethods") or []), flags))
    elif provider == "mistral":
        for m in body.get("data") or []:
            mid = m["id"]
            bad = mid.startswith("labs-") or "preview" in mid
            rows.append((mid, mid.startswith("ministral-") and not bad, bool((m.get("capabilities") or {}).get("function_calling")),
                         ["labs/preview: always trained on"] if bad else ([] if mid.startswith("ministral-") else ["not served on the Free plan"])))
    elif provider in ("groq", "nvidia"):
        for m in body.get("data") or []:
            flags = ["preview: not for production"] if "preview" in m["id"] else []
            if provider == "nvidia":
                flags.append("trial terms")
            rows.append((m["id"], True, None, flags))
    elif provider == "cloudflare":
        for m in body.get("result") or []:
            props = {p.get("property_id"): p.get("value") for p in m.get("properties") or []}
            rows.append((m["name"], True, str(props.get("function_calling")).lower() == "true", ["within 10,000 neurons a day"]))
    elif provider == "zai":
        ids = {m["id"] for m in body.get("data") or []}
        rows = [(mid, mid in ZAI_FREE, None, ["check docs.z.ai pricing"]) for mid in sorted(ids | set(ZAI_FREE))]
    return rows


def configured(config):
    """{account: {provider model id: (litellm model string, deployment ids, in chat?)}}"""
    out = {}
    for d in config.get("model_list") or []:
        account, model = d["model_info"]["account"], d["litellm_params"]["model"]
        provider_id = model.split("/", 1)[1] if model.startswith(("openrouter/", "gemini/", "mistral/", "groq/", "nvidia_nim/", "zai/")) \
            else model.removeprefix("cloudflare/").removeprefix("openai/")
        entry = out.setdefault(account, {}).setdefault(provider_id, [model, [], False])
        entry[1].append(d["model_info"]["id"])
        entry[2] = entry[2] or d["model_name"] == "chat"
    return out


def main():
    config = yaml.safe_load(open(ENV("CHARMER_CONFIG", "config.yaml")))
    conf = configured(config)
    today = dt.date.today().isoformat()
    gone, kept, new, errors, latest = [], [], [], [], {}
    for provider in ("openrouter", "gemini", "mistral", "groq", "nvidia", "cloudflare", "zai"):
        rows = listed(provider)
        mine = conf.get(provider, {})
        if isinstance(rows, str):
            errors.append((provider, rows))
            latest[provider] = {"free_models": [], "error": rows}
            continue
        by_id = {r[0]: r for r in rows}
        for pid, (model, ids, in_mate) in mine.items():
            if pid not in by_id:
                gone.append((", ".join(ids), provider, model, in_mate))
            else:
                _, free, tools, flags = by_id[pid]
                kept.append((", ".join(ids), provider, model, in_mate, free, tools, flags))
        free_tools = [r for r in rows if r[1] and r[2] is not False]
        latest[provider] = {"free_models": sorted(ROUTE[provider] + r[0] for r in free_tools)}
        new += [(provider, ROUTE[provider] + r[0], r[2], r[3]) for r in free_tools if r[0] not in mine]

    yes = lambda b: "?" if b is None else ("yes" if b else "no")  # noqa: E731
    lines = [f"# Charmer discovery {today}", "", "## Gone (configured, not listed by the provider)", "",
             "| Slug | Provider | Model | In chat? |", "| --- | --- | --- | --- |"]
    lines += [f"| {a} | {b} | `{c}` | {yes(d)} |" for a, b, c, d in gone] or ["| none | | | |"]
    lines += ["", "## Configured", "", "| Slug | Provider | Model | In chat? | Still free? | Tools? | Flags |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {a} | {b} | `{c}` | {yes(d)} | {yes(e)} | {yes(f)} | {', '.join(g)} |" for a, b, c, d, e, f, g in kept]
    lines += ["", "## New candidates (free, tools, not configured)", "", "| Provider | Model | Tools? | Flags |", "| --- | --- | --- | --- |"]
    lines += [f"| {a} | `{b}` | {yes(c)} | {', '.join(d)} |" for a, b, c, d in new] or ["| none | | | |"]
    lines += ["", "## Provider errors", "", "| Provider | Status |", "| --- | --- |"]
    lines += [f"| {a} | {b} |" for a, b in errors] or ["| none | |"]
    report = "\n".join(lines) + "\n"
    print(report)
    os.makedirs("reports", exist_ok=True)
    with open(f"reports/discovery-{today}.md", "w") as f:
        f.write(report)
    with open("reports/discovery-latest.json", "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "providers": latest}, f, indent=1)
    return 1 if gone else 0


if __name__ == "__main__":
    sys.exit(main())
