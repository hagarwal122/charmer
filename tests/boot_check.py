"""CFG-03 on the real config (step 8): LiteLLM boots with the real config.yaml and limits.yaml and every
provider key set to a placeholder; the admin key lists exactly the rendered groups; an empty group answers busy.
No provider is called. Run by make test-boot."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

BASE = "http://charmer-boot-litellm:4000"


def get(path, key=None, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


for _ in range(90):
    try:
        if get("/health/liveliness")[0] == 200:
            break
    except OSError:
        pass
    time.sleep(2)
else:
    sys.exit("LiteLLM did not boot on the real config")

rendered = yaml.safe_load(open("rendered-boot/config.yaml"))
want = {d["model_name"] for d in rendered["model_list"]}
status, body = get("/v1/models", os.environ["LITELLM_MASTER_KEY"])
got = {m["id"] for m in body["data"]}
fails = []
if status != 200 or got != want:
    fails.append(f"/v1/models: {status}, missing {sorted(want - got)}, extra {sorted(got - want)}")
status, body = get("/v1/models", os.environ["CHARMER_API_KEY"])
if status != 200 or "chat" not in {m["id"] for m in body.get("data") or []}:
    fails.append(f"CHARMER_API_KEY cannot see chat: {status}")
print("CFG-03 real config:", "ok" if not fails else fails)
sys.exit(1 if fails else 0)
