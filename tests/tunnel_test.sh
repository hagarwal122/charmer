#!/bin/sh
# CFG-28: the tunnel's ingress (cloudflared/config.yml) sends only the inference paths and liveliness to LiteLLM;
# everything else, admin and metrics included, is a 404 at the edge, and the dashboard is never published.
# Run by the pinned cloudflared image's own `ingress rule`, offline: no credentials and no Cloudflare call.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
image=$(sed -n 's/^ *image: *\(cloudflare\/cloudflared[^ ]*\).*/\1/p' "$here/docker-compose.yml" | head -1)
cfd() { docker run --rm -v "$here/cloudflared/config.yml:/c/config.yml:ro" "$image" tunnel --config /c/config.yml "$@" 2>&1; }
fail=0

cfd ingress validate | grep -q '^OK$' || { echo "FAIL: ingress does not validate"; fail=1; }

expect() {  # expect <path> <service>
  got=$(cfd ingress rule "https://charmer.example.com$1" | sed -n 's/^[[:space:]]*service: //p')
  [ "$got" = "$2" ] || { echo "FAIL: $1 went to '$got', want '$2'"; fail=1; }
}
for p in /v1/chat/completions /v1/embeddings /v1/models /health/liveliness; do expect "$p" http://litellm:4000; done
for p in / /health /health/readiness /metrics /model/new /model/delete /key/generate /ui /v1/models/chat \
         /v1/chat/completions/x /login /dashboard; do expect "$p" http_status:404; done
grep -Eq '^[[:space:]]*service:.*dashboard' "$here/cloudflared/config.yml" && { echo "FAIL: the dashboard is routed"; fail=1; }

# The dashboard's own tunnel (optional): everything to the dashboard, never LiteLLM.
dash() { docker run --rm -v "$here/cloudflared/dashboard.yml:/c/config.yml:ro" "$image" tunnel --config /c/config.yml "$@" 2>&1; }
dash ingress validate | grep -q '^OK$' || { echo "FAIL: dashboard ingress does not validate"; fail=1; }
for p in / /login /static/app.css; do
  got=$(dash ingress rule "https://charmer-dashboard.example.com$p" | sed -n 's/^[[:space:]]*service: //p')
  [ "$got" = http://dashboard:8080 ] || { echo "FAIL: dashboard $p went to '$got'"; fail=1; }
done
grep -Eq '^[[:space:]]*service:.*litellm' "$here/cloudflared/dashboard.yml" && { echo "FAIL: the dashboard tunnel reaches LiteLLM"; fail=1; }

[ "$fail" = 0 ] && echo "CFG-28 ok"
exit "$fail"
