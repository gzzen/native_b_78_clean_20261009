#!/usr/bin/env bash
# Path: scripts/install_codex.sh
# Purpose: Install the pinned Codex binaries from an offline bundle into tools/, with no network access.
# Usage: scripts/install_codex.sh path/to/codex-offline-<version>-<target>.tar   (from scripts/make_codex_bundle.sh)
#        scripts/install_codex.sh path/to/directory-with-the-release-archives
set -euo pipefail
source_path="${1:?Usage: scripts/install_codex.sh BUNDLE.tar|ARCHIVE_DIRECTORY}"
source_path="$(cd "$(dirname "$source_path")" && pwd)/$(basename "$source_path")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
lock=environment/codex_lock.json
sha256() { local out; out="$(sha256sum "$1" 2>/dev/null || shasum -a 256 "$1")"; echo "${out%% *}"; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if [[ -d "$source_path" ]]; then
  archives="$source_path"
else
  tar -xf "$source_path" -C "$work"
  archives="$work"
fi
version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$lock")"
destination="tools/codex/${version}"
mkdir -p "$destination"
python3 -c 'import json,sys
for name, entry in json.load(open(sys.argv[1]))["binaries"].items():
    print(name, entry["archive"], entry["archive_sha256"], entry["member"], entry["binary_sha256"])' "$lock" |
while read -r name archive archive_sha member binary_sha; do
  if [[ ! -f "${archives}/${archive}" ]]; then
    echo "missing ${archive} in ${source_path}" >&2
    exit 1
  fi
  if [[ "$(sha256 "${archives}/${archive}")" != "$archive_sha" ]]; then
    echo "sha256 mismatch for ${archive}; the bundle does not match ${lock}" >&2
    exit 1
  fi
  tar -xzf "${archives}/${archive}" -C "$work" "$member"
  if [[ "$(sha256 "${work}/${member}")" != "$binary_sha" ]]; then
    echo "sha256 mismatch for ${member} inside ${archive}" >&2
    exit 1
  fi
  install -m 0755 "${work}/${member}" "${destination}/${name}"
  echo "${destination}/${name}"
done
"${destination}/codex" --version
