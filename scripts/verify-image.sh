#!/bin/sh
# Verify the pinned LiteLLM image with the vendored cosign.pub and check the verified digest is the one in the
# compose file. Usage: scripts/verify-image.sh [compose-file]
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
compose=${1:-$root/docker-compose.yml}
cosign_image="gcr.io/projectsigstore/cosign:v2.5.3@sha256:f1946d0f30fc8e3777b02f2201e02efdba9fe38f4918162f937052fac98e083f"

ref=$(sed -n 's/^ *image: *\(ghcr\.io\/berriai\/litellm[^ ]*\).*/\1/p' "$compose" | head -1)
case "$ref" in
  *:*@sha256:*) ;;
  *) echo "verify: the LiteLLM image is not pinned as tag@sha256:digest: '$ref'" >&2; exit 1 ;;
esac
tagged=${ref%@*}
pinned=${ref#*@}

out=$(docker run --rm -v "$root/cosign.pub:/w/cosign.pub:ro" "$cosign_image" verify --key /w/cosign.pub "$tagged" 2>/dev/null) || {
  echo "verify: cosign could not verify $tagged with the vendored cosign.pub" >&2; exit 1; }
verified=$(printf '%s' "$out" | grep -o '"docker-manifest-digest":"sha256:[0-9a-f]*"' | head -1 | cut -d'"' -f4)

if [ "$verified" != "$pinned" ]; then
  echo "verify: cosign verified $verified but the compose file pins $pinned" >&2
  exit 1
fi
echo "verify: $tagged is signed and matches the pinned digest $pinned"
