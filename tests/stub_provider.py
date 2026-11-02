"""A fake OpenAI-compatible provider for the smoke suite. Standard library only.

Behaviour is picked by the model name in the request (LiteLLM strips the `openai/` prefix; a `:free` suffix is
ignored). It also serves OpenRouter's GET /api/v1/key and Cloudflare's POST /graphql with bodies set by POST /_set.
Every request is logged; GET /_log returns the log, POST /_reset clears it. A second listener on port 9999 only
logs, so "nothing reached it" can be checked.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = []
LOCK = threading.Lock()
STATE = {"key": None, "neurons": None, "remaining": {"hdr-a": 10, "hdr-b": 10}, "once": {}}
DISCOVER = {  # one realistic body per provider list endpoint
    "openrouter": {"data": [
        {"id": "nvidia/nemotron-3.5-super-vl:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"], "context_length": 131072},
        {"id": "acme/new-model:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"], "context_length": 32768},
        {"id": "acme/paid-model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "supported_parameters": ["tools"]}]},
    "openrouter-endpoints": {"data": {"endpoints": [{"provider_name": "Nvidia"}]}},
    "gemini": {"models": [{"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                          {"name": "models/gemini-3.5-pro", "supportedGenerationMethods": ["generateContent"]}]},
    "mistral": {"data": [{"id": "ministral-14b-2512", "capabilities": {"function_calling": True}},
                         {"id": "ministral-8b-2512", "capabilities": {"function_calling": True}},
                         {"id": "mistral-small-2603", "capabilities": {"function_calling": True}},
                         {"id": "labs-leanstral-1-5", "capabilities": {"function_calling": True}}]},
    "groq": {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "acme/preview-model-preview"}]},
    "nvidia": {"data": [{"id": "mistralai/mistral-large-4-1t-a52b"}]},
    "cloudflare": {"result": [{"name": "@cf/openai/gpt-oss-120b", "properties": [{"property_id": "function_calling", "value": "true"}]},
                              {"name": "@cf/baai/bge-m3", "properties": []}]},
    "zai": {"data": [{"id": "glm-5.3-flash"}]},
}
LONG_ID = "call_1_" + "x" * 2000  # long and opaque; LiteLLM strips a "__thought__" part on non-Gemini routes


def log(entry):
    with LOCK:
        LOG.append(entry)


def completion(model, content=None, tool_calls=None, usage=(12, 5)):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": f"cmpl-{time.time_ns()}", "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


def tool_calls_for(body):
    tools = body.get("tools") or []
    if not tools:
        return None
    first = tools[0]["function"]["name"]
    calls = [{"id": "call_ok_1", "type": "function", "function": {"name": first, "arguments": "{}"}}]
    return calls


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _send(self, status, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, model, content, tool_calls, headers=None):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()

        def chunk(obj):
            line = f"data: {json.dumps(obj)}\n\n".encode() if obj != "[DONE]" else b"data: [DONE]\n\n"
            self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")

        base = {"id": f"cmpl-{time.time_ns()}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{**tc, "index": i}]}, "finish_reason": None}]})
            chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            for piece in (content[: len(content) // 2], content[len(content) // 2:]):
                chunk({**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        chunk({**base, "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}})
        chunk("[DONE]")
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/_log":
            with LOCK:
                return self._send(200, list(LOG))
        log({"port": port, "method": "GET", "path": self.path, "auth": bool(self.headers.get("authorization"))})
        if self.path.startswith("/api/v1/key"):
            if STATE["key"] is None:
                return self._send(503, {"error": "not set"})
            return self._send(200, {"data": STATE["key"]})
        if self.path.startswith("/discover/"):  # provider list endpoints for scripts/discover.py (DSC cells)
            name = self.path.split("/")[2].split("?")[0]
            override = (STATE.get("discover") or {}).get(name)
            if override == "fail":
                return self._send(500, {"error": "list failed"})
            return self._send(200, override if override is not None else DISCOVER.get(name, {}))
        if self.path.startswith("/v1/models"):
            return self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in ("ok-a", "ok-b")]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        port = self.server.server_address[1]
        body = self._body()
        if self.path == "/_reset":
            with LOCK:
                LOG.clear()
            STATE["once"].clear()
            STATE["remaining"] = {"hdr-a": 10, "hdr-b": 10}
            return self._send(200, {"ok": True})
        if self.path == "/_set":
            for k, v in body.items():
                if k == "reset_once":
                    STATE["once"].clear()
                elif k == "remaining":
                    STATE["remaining"].update(v)
                else:
                    STATE[k] = v
            return self._send(200, {"ok": True})
        model = str(body.get("model", "")).removesuffix(":free").split("@")[0]  # "ok-a@d1" is ok-a with its own counters
        msgs = body.get("messages") or []
        log({"port": port, "method": "POST", "path": self.path, "model": model, "stream": bool(body.get("stream")),
             "tool_call_ids": [tc.get("id") for m in msgs for tc in (m.get("tool_calls") or [])],
             "tool_result_ids": [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"],
             "text": json.dumps(msgs)[:4000], "keys": sorted(body)})
        if port == 9999:
            return self._send(200, completion(model, "port 9999"))
        if ":generateContent" in self.path:  # the Gemini 402 probe
            code = int(STATE.get("gemini_status") or 200)
            return self._send(code, {"candidates": []} if code == 200 else {"error": {"code": code, "message": "probe"}})
        if self.path == "/graphql":
            n = STATE["neurons"]
            if n == "empty":
                return self._send(200, {"data": {"viewer": {"accounts": [{"aiInferenceAdaptiveGroups": []}]}}})
            if n is None:
                return self._send(500, {"errors": [{"message": "no data"}]})
            return self._send(200, {"data": {"viewer": {"accounts": [{"aiInferenceAdaptiveGroups": [{"sum": {"totalNeurons": n}}]}]}}})
        if self.path.endswith("/embeddings"):
            inputs = body.get("input") or [""]
            inputs = inputs if isinstance(inputs, list) else [inputs]
            data = [{"object": "embedding", "index": i, "embedding": [((hash(t) + j) % 1000) / 1000 for j in range(1024)]}
                    for i, t in enumerate(inputs)]
            return self._send(200, {"object": "list", "data": data, "model": model,
                                    "usage": {"prompt_tokens": 4, "total_tokens": 4}})
        return self._chat(model, body)

    def _chat(self, model, body):
        stream = bool(body.get("stream"))
        if model == "rl-429":
            return self._send(429, {"error": {"message": "rate limited", "type": "rate_limit"}},
                              {"retry-after": os.environ.get("STUB_RETRY_AFTER_RL", "30"),
                               "x-ratelimit-remaining-requests": 0})
        if model == "rl-429-60":
            return self._send(429, {"error": {"message": "rate limited", "type": "rate_limit"}}, {"retry-after": 60})
        if model == "rl-429-once":
            now = time.time()
            first = STATE["once"].get(model)
            if first is None or now - first > 60:
                STATE["once"][model] = now
                return self._send(429, {"error": {"message": "rate limited once"}}, {"retry-after": 5})
        if model == "err-500":
            return self._send(500, {"error": {"message": "boom"}})
        if model == "auth-401":
            return self._send(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error",
                                              "code": "invalid_api_key"}})
        if model == "pay-402":
            return self._send(402, {"error": {"message": "Your prepayment credits are depleted.", "code": 402}})
        if model in ("strict", "strict-null"):  # like Groq/Mistral: unknown assistant fields are refused; strict-null also refuses null
            for m in body.get("messages") or []:
                if m.get("role") == "assistant":
                    extra = set(m) - {"role", "content", "tool_calls", "tool_call_id", "name"}
                    extra |= {f"tool_calls.{k}" for tc in m.get("tool_calls") or [] for k in set(tc) - {"id", "type", "function"}}
                    if extra:
                        return self._send(422, {"detail": [{"type": "extra_forbidden", "loc": sorted(extra)}]})
                    if model == "strict-null" and m.get("content") is None:
                        return self._send(400, {"errors": [{"message": "content: null not allowed"}]})
        if model == "slow":
            time.sleep(float(os.environ.get("STUB_SLOW_SECONDS", "30")))
        headers = {}
        if model in ("hdr-a", "hdr-b"):
            left = STATE["remaining"].get(model, 10)
            if left <= 0:
                return self._send(429, {"error": {"message": "no requests left"}},
                                  {"x-ratelimit-remaining-requests": 0, "x-ratelimit-limit-requests": 10,
                                   "x-ratelimit-reset-requests": "60s", "retry-after": 60})
            STATE["remaining"][model] = left - 1
            headers = {"x-ratelimit-limit-requests": 10, "x-ratelimit-remaining-requests": left - 1,
                       "x-ratelimit-reset-requests": "60s", "x-ratelimit-limit-tokens": 10000,
                       "x-ratelimit-remaining-tokens": 9000}
        if model == "cf-chat":
            headers = {"cf-ai-neurons": 200}
        tool_calls = None
        if model == "tools-long-id" and not any(m.get("role") == "tool" for m in body.get("messages") or []):
            tool_calls = [{"id": LONG_ID, "type": "function", "function": {"name": "find_orders", "arguments": "{\"payment\":[\"UNPAID\"]}"}},
                          {"id": "call_2", "type": "function", "function": {"name": "find_products", "arguments": "{\"query\":\"kurta\"}"}}]
        elif model != "tools-long-id" and body.get("tools") and not any(m.get("role") == "tool" for m in body.get("messages") or []):
            tool_calls = tool_calls_for(body)
        content = None if tool_calls else f"served by {model}"
        if stream:
            return self._stream(model, content, tool_calls, headers)
        return self._send(200, completion(model, content, tool_calls), headers)


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(9999,), daemon=True).start()
    serve(9000)
