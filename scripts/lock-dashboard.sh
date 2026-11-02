#!/bin/sh
# Regenerate dashboard/requirements.txt with hashes for linux/arm64 and linux/amd64. Runs in a container.
set -eu
IMG="python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159"
docker run --rm -v "$(cd "$(dirname "$0")/.." && pwd)/dashboard:/out" "$IMG" sh -c '
set -e
pip download -q --no-cache-dir -d /tmp/arm "fastapi==0.120.4" "uvicorn==0.38.0" "jinja2==3.1.6" "pyyaml==6.0.3" >/dev/null
pip download -q --no-cache-dir -d /tmp/amd --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.12 --only-binary=:all: $(ls /tmp/arm | sed -E "s/^([A-Za-z0-9_.]+)-([0-9][^-]*)-.*/\1==\2/" | tr "\n" " ") >/dev/null
python - <<PY
import hashlib, os, re, collections
h = collections.defaultdict(set)
for d in ("/tmp/arm", "/tmp/amd"):
    for f in os.listdir(d):
        m = re.match(r"^([A-Za-z0-9_.]+)-([0-9][^-]*)-", f)
        h[(m.group(1).replace("_", "-").lower(), m.group(2))].add(hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest())
out = ["# Dashboard dependencies, pinned with hashes for linux/arm64 and linux/amd64. Regenerate: make dashboard-lock"]
for (n, v), hs in sorted(h.items()):
    hs = sorted(hs)
    out.append(n + "==" + v + " \\\\")
    out += ["    --hash=sha256:" + x + (" \\\\" if i < len(hs) - 1 else "") for i, x in enumerate(hs)]
open("/out/requirements.txt", "w").write("\n".join(out) + "\n")
print(len(h), "packages locked")
PY'
