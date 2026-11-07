#!/usr/bin/env bash
set -euo pipefail

# These credentials helpers are only used by the optional priority UID filter.
# BakaSU does not expose that option; keep its disabled build free of dead code.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
patch_file="$script_dir/../../patch/susfs-optional-helpers.patch"

if git apply --check "$patch_file" >/dev/null 2>&1; then
  git apply "$patch_file"
  echo 'SUSFS optional credentials helpers now follow the priority UID filter config.'
elif git apply --reverse --check "$patch_file" >/dev/null 2>&1; then
  echo 'SUSFS optional credentials helper guards are already present.'
else
  echo 'Cannot verify SUSFS optional credentials helpers; upstream layout changed.' >&2
  exit 1
fi
