"""The live checklist per pair: real providers through the local Charmer, with CHARMER_API_KEY.

  make live-check PAIR=pair-<slug>     LIVE-01..09, 11..16 for one pair; saves its tool history for LIVE-10
  make live-matrix                     LIVE-10: every saved history replayed to every other pair (finished and cut turns)

About 15 calls per pair, paced under the eval key's 10 a minute. Results go to reports/live/<pair>.json and .md;
Never prints a key. An agent-sized request is synthetic (this repo is open source).
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM", "http://litellm:4000")
KEY = os.environ["CHARMER_API_KEY"]
OUT = "reports/live"
GAP_S = float(os.environ.get("LIVE_GAP_SECONDS", "7"))
WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city.",
           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
CLOCK = {"type": "function", "function": {"name": "get_time", "description": "Current local time in an IANA time zone.",
         "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": ["timezone"]}}}
_last = [0.0]


def call(model, messages, tools=None, tool_choice=None, stream=False, extra=None, path="/v1/chat/completions"):
    wait = GAP_S - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    body = {"model": model, **({"messages": messages} if messages is not None else {}), **(extra or {})}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    if stream:
        body.update(stream=True, stream_options={"include_usage": True})
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {KEY}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, status, headers = r.read().decode(), r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read().decode(errors="replace"), e.code, {k.lower(): v for k, v in e.headers.items()}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": type(e).__name__, "ms": int((time.monotonic() - t0) * 1000)}
    out = {"status": status, "headers": headers, "ms": int((time.monotonic() - t0) * 1000)}
    if stream and status == 200:
        out.update(parse_stream(raw))
    else:
        try:
            out["json"] = json.loads(raw)
        except json.JSONDecodeError:
            out["text"] = raw[:500]
    return out


def parse_stream(raw):
    text, calls, chunks, done, usage = "", {}, 0, False, None
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        if line.strip() == "data: [DONE]":
            done = True
            continue
        chunk = json.loads(line[6:])
        chunks += 1
        usage = chunk.get("usage") or usage
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            text += d.get("content") or ""
            for pos, tc in enumerate(d.get("tool_calls") or []):
                key = tc.get("index", tc.get("id") or pos)
                cur = calls.setdefault(key, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                cur["id"] = tc.get("id") or cur["id"]
                fn = tc.get("function") or {}
                cur["function"]["name"] += fn.get("name") or ""
                cur["function"]["arguments"] += fn.get("arguments") or ""
                for extra_key in ("provider_specific_fields", "extra_content"):
                    if tc.get(extra_key):
                        cur[extra_key] = tc[extra_key]
    return {"stream": {"text": text, "tool_calls": list(calls.values()), "chunks": chunks, "done": done, "usage": usage}}


def message(r):
    if "stream" in r:
        return {"role": "assistant", "content": r["stream"]["text"] or None,
                **({"tool_calls": r["stream"]["tool_calls"]} if r["stream"]["tool_calls"] else {})}
    return (((r.get("json") or {}).get("choices") or [{}])[0]).get("message") or {}


def tool_calls(r):
    return message(r).get("tool_calls") or []


def args_ok(tc, required):
    try:
        a = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return False
    return isinstance(a, dict) and all(k in a for k in required)


def mate_sized():
    """~6K prompt tokens and 27 tools, Charmer-neutral, shaped like an agent's real request (LIVE-08)."""
    rules = " ".join(f"Rule {i}: answer only from tool results, name the record you used, keep replies short, and ask before any change that cannot be undone." for i in range(1, 140))
    tools = [{"type": "function", "function": {"name": f"tool_{i:02d}", "description": f"Look up record kind {i} by id, name or date range.",
              "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "from": {"type": "string", "format": "date"},
                                                              "to": {"type": "string", "format": "date"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}}}
             for i in range(25)] + [WEATHER, CLOCK]
    return [{"role": "system", "content": "You are an operations assistant. " + rules},
            {"role": "user", "content": "What is the weather in Paris right now?"}], tools


def save(pair, results):
    os.makedirs(OUT, exist_ok=True)
    json.dump(results, open(f"{OUT}/{pair}.json", "w"), indent=1, default=str)
    lines = [f"# Live checklist {pair} ({time.strftime('%Y-%m-%d %H:%M')})", "", "| Cell | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {c} | {v['result']} | {v['detail']} |" for c, v in results["cells"].items()]
    open(f"{OUT}/{pair}.md", "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


def cell(results, name, ok, detail, note=False):
    results["cells"][name] = {"result": "note" if note else ("pass" if ok else "FAIL"), "detail": str(detail).replace("|", "/")[:300]}


def run_pair(pair):
    results = {"pair": pair, "cells": {}}
    q = [{"role": "user", "content": "Reply with one short sentence: what colour is a clear daytime sky?"}]
    if pair.startswith("embed-"):
        a = call(pair, None, path="/v1/embeddings", extra={"input": ["charmer live check"]})
        b = call(pair, None, path="/v1/embeddings", extra={"input": ["charmer live check"]})
        va = ((a.get("json") or {}).get("data") or [{}])[0].get("embedding") or []
        vb = ((b.get("json") or {}).get("data") or [{}])[0].get("embedding") or []
        cell(results, "LIVE-13", a["status"] == 200 and len(va) == 1024 and va == vb, f"{a['status']}, {len(va)} dims, same vector: {va == vb}")
        cell(results, "LIVE-11", a.get("headers", {}).get("x-litellm-model-id") == pair, a.get("headers", {}).get("x-litellm-model-id"))
        save(pair, results)
        return results

    r1 = call(pair, q)
    text = (message(r1).get("content") or "").strip()
    cell(results, "LIVE-01", r1["status"] == 200 and len(text) > 0, f"{r1['status']} in {r1['ms']} ms: {text[:80]}")
    cell(results, "LIVE-11", r1.get("headers", {}).get("x-litellm-model-id") == pair, r1.get("headers", {}).get("x-litellm-model-id"))
    r2 = call(pair, q, stream=True)
    s = r2.get("stream") or {}
    cell(results, "LIVE-02", r2["status"] == 200 and s.get("done") and s.get("chunks", 0) > 1, f"{r2['status']}, {s.get('chunks')} chunks, [DONE]: {s.get('done')}")
    cell(results, "LIVE-03", True, "not run: needs your public hostname", note=True)
    weather_q = [{"role": "user", "content": "What is the weather in Paris?"}]
    forced = {"type": "function", "function": {"name": "get_weather"}}
    r4 = call(pair, weather_q, tools=[WEATHER, CLOCK], tool_choice=forced)
    tcs = tool_calls(r4)
    cell(results, "LIVE-04", r4["status"] == 200 and tcs and tcs[0]["function"]["name"] == "get_weather" and args_ok(tcs[0], ["city"]),
         f"{r4['status']}: {[(t['function']['name'], t['function']['arguments']) for t in tcs][:2]}")
    r5 = call(pair, [{"role": "user", "content": "Two separate questions: the weather in Paris, and the local time in Tokyo. Use both tools."}],
              tools=[WEATHER, CLOCK])
    n5 = len(tool_calls(r5))
    cell(results, "LIVE-05", r5["status"] == 200, f"{r5['status']}: {n5} tool call(s) in one turn" + (" (parallel)" if n5 > 1 else " (no parallel; record, not a failure)"), note=n5 < 2)
    r6 = call(pair, weather_q, tools=[WEATHER, CLOCK], tool_choice=forced, stream=True)
    s6 = (r6.get("stream") or {}).get("tool_calls") or []
    cell(results, "LIVE-06", r6["status"] == 200 and s6 and args_ok(s6[0], ["city"]),
         f"{r6['status']}: streamed {[(t['function']['name'], t['function']['arguments']) for t in s6][:2]}, finish by presence")
    ids = [t.get("id", "") for t in tcs]
    cell(results, "LIVE-09", bool(ids), f"id lengths {[len(i) for i in ids]}, sample {ids[0][:24] if ids else '—'}", note=True)
    history = None
    if tcs:
        assistant = message(r4)
        history = weather_q + [assistant, {"role": "tool", "tool_call_id": tcs[0]["id"], "content": json.dumps({"city": "Paris", "temp_c": 21, "sky": "clear"})}]
        r7 = call(pair, history, tools=[WEATHER, CLOCK])
        answer = message(r7).get("content") or ""
        cell(results, "LIVE-07", r7["status"] == 200 and "21" in answer, f"{r7['status']}: {answer[:100]}")
        results["history"] = history
        null_turn = weather_q + [{**assistant, "content": None}, history[-1]]
        r15 = call(pair, null_turn, tools=[WEATHER, CLOCK])
        cell(results, "LIVE-15", r15["status"] == 200, f"{r15['status']} with content: null on the assistant turn")
    else:
        cell(results, "LIVE-07", False, "no tool call to round-trip")
        cell(results, "LIVE-15", False, "no tool call to replay")
    if pair.startswith("pair-gemini"):
        ok = []
        for stream in (False, True):
            a = call(pair, weather_q, tools=[WEATHER], stream=stream)
            t = tool_calls(a)
            if not t:
                ok.append(f"stream={stream}: no tool call")
                continue
            b = call(pair, weather_q + [message(a), {"role": "tool", "tool_call_id": t[0]["id"], "content": '{"temp_c": 21}'}], tools=[WEATHER], stream=stream)
            ok.append(f"stream={stream}: call 2 {b['status']}")
        cell(results, "LIVE-14", all("call 2 200" in o for o in ok), "; ".join(ok))
    msgs, tools = mate_sized()
    r8 = call(pair, msgs, tools=tools)
    usage = (r8.get("json") or {}).get("usage") or {}
    cell(results, "LIVE-08", r8["status"] == 200, f"{r8['status']} in {r8['ms']} ms, {usage.get('prompt_tokens')} prompt tokens")
    cell(results, "LIVE-12", True, "limits from the console: owner's monthly check; headers below", note=True)
    headers_cell(pair, results)
    save(pair, results)
    return results


def headers_cell(pair, results):
    time.sleep(3)  # the plugin flushes every 2 s
    try:
        # immutable: a WAL reader otherwise writes -shm, which the non-root RUN_AS user cannot (CFG-31).
        conn = sqlite3.connect("file:/data/charmer.sqlite?mode=ro&immutable=1", uri=True, timeout=5)
        row = conn.execute("select state from provider_state where deployment = ?", (pair,)).fetchone()
        neurons = conn.execute("select max(neurons) from usage_day where account = 'cloudflare' and model = '*'").fetchone()[0]
        cell(results, "LIVE-16", True, (f"headers seen: {row[0]}" if row else "the provider sends no rate-limit headers (local counts only)")
             + (f"; Cloudflare neurons today {neurons:.0f}" if pair.startswith("pair-cf") and neurons else ""), note=True)
    except sqlite3.Error as e:
        cell(results, "LIVE-16", False, f"could not read the SQLite file: {e}")


def run_matrix():
    saved = {f[:-5]: json.load(open(f"{OUT}/{f}")) for f in sorted(os.listdir(OUT)) if f.endswith(".json") and not f.startswith("matrix")}
    histories = {p: r["history"] for p, r in saved.items() if r.get("history")}
    rows = []
    for target in histories:
        for source, h in histories.items():
            if source == target:
                continue
            finished = h + [{"role": "assistant", "content": "It is 21 °C and clear in Paris."}, {"role": "user", "content": "Thanks. And in Lyon?"}]
            for kind, msgs in (("finished turn", finished), ("cut after tool call", h)):
                r = call(target, msgs, tools=[WEATHER, CLOCK])
                rows.append((source, target, kind, r["status"], (str((r.get("json") or {}).get("error", "")) or "")[:120]))
    os.makedirs(OUT, exist_ok=True)
    lines = ["# LIVE-10 cross-pair matrix", "", "| History from | Replayed to | Case | Status | Error |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {a} | {b} | {c} | {d} | {e.replace('|', '/')} |" for a, b, c, d, e in rows]
    open(f"{OUT}/matrix.md", "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if all(r[3] == 200 for r in rows) else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--matrix"]:
        sys.exit(run_matrix())
    if sys.argv[1] == "--headers":
        res = json.load(open(f"{OUT}/{sys.argv[2]}.json"))
        headers_cell(sys.argv[2], res)
        save(sys.argv[2], res)
        sys.exit(0)
    res = run_pair(sys.argv[1])
    sys.exit(0 if all(c["result"] != "FAIL" for c in res["cells"].values()) else 1)
