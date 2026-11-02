#!/bin/sh
# CFG-01: make verify passes on the real compose file and fails on a tampered digest or a bare tag.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0

"$here/scripts/verify-image.sh" "$here/docker-compose.yml" >"$tmp/ok.log" 2>&1 || { echo "FAIL: verify on the real compose file"; tail -5 "$tmp/ok.log"; fail=1; }

sed -E 's/(litellm:v1\.106\.4@sha256:)[0-9a-f]{4}/\10000/' "$here/docker-compose.yml" >"$tmp/bad-digest.yml"
if "$here/scripts/verify-image.sh" "$tmp/bad-digest.yml" >"$tmp/bad.log" 2>&1; then echo "FAIL: a tampered digest passed"; fail=1; fi

sed -E 's/(litellm:v1\.106\.4)@sha256:[0-9a-f]{64}/\1/' "$here/docker-compose.yml" >"$tmp/bare.yml"
if "$here/scripts/verify-image.sh" "$tmp/bare.yml" >"$tmp/bare.log" 2>&1; then echo "FAIL: a bare tag passed"; fail=1; fi

[ "$fail" = 0 ] && echo "CFG-01 ok"
exit "$fail"
