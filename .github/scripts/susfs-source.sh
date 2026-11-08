#!/usr/bin/env bash
# Source this helper from the kernel root. Do not execute build.config as code.

prepare_susfs_source() {
  local requested=${1:-} commit
  SUSFS_KMI=$(python3 - <<'PY'
from pathlib import Path
import re
import sys

for name in ('build.config.constants', 'build.config.common', 'build.config.gki', 'build.config'):
    path = Path(name)
    if not path.is_file():
        continue
    match = re.search(r'''(?m)^\s*(?:export\s+)?BRANCH\s*=\s*["']?(android\d+-\d+\.\d+)["']?\s*(?:#.*)?$''', path.read_text())
    if match:
        kmi = match.group(1)
        makefile = Path('Makefile').read_text()
        version = re.search(r'(?m)^VERSION\s*=\s*(\d+)\s*$', makefile)
        patchlevel = re.search(r'(?m)^PATCHLEVEL\s*=\s*(\d+)\s*$', makefile)
        if not version or not patchlevel or not kmi.endswith(f'-{version[1]}.{patchlevel[1]}'):
            sys.exit(f'Kernel KMI {kmi} does not match Makefile version')
        print(kmi)
        break
else:
    sys.exit('Cannot determine kernel KMI from build.config metadata')
PY
  ) || return 1

  case "$requested" in
    ''|stable) SUSFS_BRANCH="gki-${SUSFS_KMI}" ;;
    dev) SUSFS_BRANCH="gki-${SUSFS_KMI}-dev" ;;
    *) SUSFS_BRANCH=$requested ;;
  esac
  if ! git check-ref-format "refs/heads/$SUSFS_BRANCH"; then
    echo "Invalid SUSFS branch: $SUSFS_BRANCH" >&2
    return 1
  fi
  if [[ $SUSFS_BRANCH != "gki-${SUSFS_KMI}" && $SUSFS_BRANCH != "gki-${SUSFS_KMI}-"* ]]; then
    echo "SUSFS branch $SUSFS_BRANCH does not match kernel KMI $SUSFS_KMI" >&2
    return 1
  fi

  git clone --depth=1 --single-branch --branch "$SUSFS_BRANCH" \
    https://gitlab.com/simonpunk/susfs4ksu.git susfs || return 1
  if [[ $(git -C susfs symbolic-ref --short HEAD) != "$SUSFS_BRANCH" ]] ||
     ! git -C susfs show-ref --verify --quiet "refs/remotes/origin/$SUSFS_BRANCH"; then
    echo "Requested SUSFS branch does not exist: $SUSFS_BRANCH" >&2
    return 1
  fi
  # Dev branches keep the KMI patch filename; the branch's -dev suffix is not
  # part of the patch name. Fail before copying if the selected tree is invalid.
  SUSFS_PATCH="susfs/kernel_patches/50_add_susfs_in_gki-${SUSFS_KMI}.patch"
  if [[ ! -f $SUSFS_PATCH || ! -d susfs/kernel_patches/fs ||
        ! -d susfs/kernel_patches/include/linux ]]; then
    echo "SUSFS branch $SUSFS_BRANCH is missing patches for $SUSFS_KMI" >&2
    return 1
  fi
  commit=$(git -C susfs rev-parse HEAD) || return 1
  echo "SUSFS source: KMI=$SUSFS_KMI branch=$SUSFS_BRANCH commit=$commit"
  if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
    printf '\n### SUSFS source\n- KMI: %s\n- Branch: %s\n- Commit: %s\n' \
      "$SUSFS_KMI" "$SUSFS_BRANCH" "$commit" >> "$GITHUB_STEP_SUMMARY"
  fi
}
