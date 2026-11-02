"""Cloudflare neuron cross-check (`make cf-budget`, every 15 minutes). Exit 1 means the owner gets mail.

Reads today's Workers AI neurons from Cloudflare's GraphQL analytics (neurons spent outside Charmer count too).
At 7,000 it pulls the Cloudflare chat deployments, at 9,500 the embedding one too; once the day resets and the count
is under 7,000 again, it lifts only the pulls it made. Field names confirmed at step 11 (U).
"""
import datetime as dt
import json
import os
import sys
import urllib.request

import yaml

from _common import alert, store

URL = os.environ.get("CF_GRAPHQL_URL", "https://api.cloudflare.com/client/v4/graphql")


def stops():
    """The same stops the plugin uses, from limits.yaml's cloudflare account (one place for the numbers)."""
    acct = ((yaml.safe_load(open(os.environ.get("CHARMER_LIMITS", "limits.yaml"))) or {}).get("accounts") or {}).get("cloudflare") or {}
    return int(acct.get("chat_stop_at", 7000)), int(acct.get("embed_stop_at", 9500))
QUERY = """query($acct: String!, $day: Date!) { viewer { accounts(filter: {accountTag: $acct}) {
  aiInferenceAdaptiveGroups(limit: 1, filter: {date_geq: $day}) { sum { totalNeurons } } } } }"""


def neurons_today():
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    body = json.dumps({"query": QUERY, "variables": {"acct": os.environ.get("CF_ACCOUNT_ID", ""), "day": day}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "content-type": "application/json", "authorization": f"Bearer {os.environ.get('CF_ANALYTICS_TOKEN', '')}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    groups = data["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
    if not groups:  # no number is a failed check, never zero (FREE-13)
        raise ValueError("the analytics answer holds no neuron count")
    return float(groups[0]["sum"]["totalNeurons"])


def cloudflare_deployments(config_path):
    cfg = yaml.safe_load(open(config_path)) or {}
    chat, embed = [], []
    for d in cfg.get("model_list") or []:
        info = d.get("model_info") or {}
        if info.get("account") == "cloudflare":
            (embed if info.get("mode") == "embedding" else chat).append(info["id"])
    return chat, embed


def main():
    s = store()
    chat, embed = cloudflare_deployments(os.environ.get("CHARMER_CONFIG", "config.yaml"))
    if not (chat or embed) or not os.environ.get("CF_ACCOUNT_ID") or not os.environ.get("CF_ANALYTICS_TOKEN"):
        print("cf-budget: no Cloudflare deployment or no CF_ACCOUNT_ID / CF_ANALYTICS_TOKEN: nothing to check")
        return 0
    try:
        n = neurons_today()
    except Exception as e:  # noqa: BLE001 - never a pass, never a change
        alert(s, "cf_budget", f"Cloudflare neuron query failed: {e}")
        return 1
    CHAT_AT, EMBED_AT = stops()
    open_pulls = {t: who for t, who in s.query("select target, who from pulls where lifted is null and who = 'budget'")}
    new = []
    targets = (chat if n >= CHAT_AT else []) + (embed if n >= EMBED_AT else [])
    for t in targets:
        if t not in open_pulls:
            s.add_pull(t, f"Cloudflare neurons today {n:.0f}", "budget")
            new.append(t)
    if n < CHAT_AT:
        for t, who in open_pulls.items():
            if who == "budget":
                s.add("unpull", t, __import__("time").time(), ["budget"])
                print(f"lifted {t}")
    s.flush()
    print(f"neurons today {n:.0f}; pulled now: {new or 'nothing new'}")
    if new:
        alert(s, "cf_budget", f"Cloudflare neurons today {n:.0f}: pulled {', '.join(new)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
