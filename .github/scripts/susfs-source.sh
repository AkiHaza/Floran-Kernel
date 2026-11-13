#!/usr/bin/env bash
# Source this helper from the kernel root.

prepare_susfs_source() {
  local dev=${1:-false} commit
  SUSFS_KMI=android14-6.1
  SUSFS_BRANCH="gki-${SUSFS_KMI}"
  if [[ $dev == true ]]; then
    SUSFS_BRANCH="${SUSFS_BRANCH}-dev"
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
