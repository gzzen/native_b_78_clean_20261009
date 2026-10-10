#!/usr/bin/env bash
# Path: scripts/make_codex_bundle.sh
# Purpose: On a machine WITH internet, pack the pinned Codex release archives into one offline bundle.
# Copy the bundle to the generation server and install it there with scripts/install_codex.sh (no network).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
lock=environment/codex_lock.json
bundle="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bundle"])' "$lock")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 -c 'import json,sys
for entry in json.load(open(sys.argv[1]))["binaries"].values():
    print(entry["url"], entry["archive"], entry["archive_sha256"])' "$lock" |
while read -r url archive expected; do
  curl -fsSL --retry 3 -o "${work}/${archive}" "$url"
  actual="$(shasum -a 256 "${work}/${archive}" 2>/dev/null || sha256sum "${work}/${archive}")"
  if [[ "${actual%% *}" != "$expected" ]]; then
    echo "sha256 mismatch for ${archive}: expected ${expected}, got ${actual%% *}" >&2
    exit 1
  fi
done
cp "$lock" "${work}/codex_lock.json"
mkdir -p out
tar -cf "out/${bundle}" -C "$work" .
echo "out/${bundle}"
