#!/usr/bin/env bash
# Path: scripts/install_codex.sh
# Purpose: Download the pinned Linux Codex binaries from environment/codex_lock.json into tools/, verified by sha256.
# The attempt image fetches the same pinned archives itself; tools/ is for the real-CLI tests.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
lock=environment/codex_lock.json
version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$lock")"
destination="tools/codex/${version}"
mkdir -p "$destination"
python3 -c 'import json,sys
for name, entry in json.load(open(sys.argv[1]))["binaries"].items():
    print(name, entry["url"], entry["archive_sha256"], entry["member"])' "$lock" |
while read -r name url expected member; do
  if [[ -x "${destination}/${name}" && "$(cat "${destination}/${name}.archive.sha256" 2>/dev/null)" == "$expected" ]]; then
    echo "${destination}/${name}"
    continue
  fi
  work="$(mktemp -d)"
  curl -fsSL --retry 3 -o "${work}/archive.tar.gz" "$url"
  actual="$(shasum -a 256 "${work}/archive.tar.gz" 2>/dev/null || sha256sum "${work}/archive.tar.gz")"
  if [[ "${actual%% *}" != "$expected" ]]; then
    echo "sha256 mismatch for ${url}: expected ${expected}, got ${actual%% *}" >&2
    rm -rf "$work"
    exit 1
  fi
  tar -xzf "${work}/archive.tar.gz" -C "$work" "$member"
  install -m 0755 "${work}/${member}" "${destination}/${name}"
  printf '%s\n' "$expected" > "${destination}/${name}.archive.sha256"
  rm -rf "$work"
  echo "${destination}/${name}"
done
