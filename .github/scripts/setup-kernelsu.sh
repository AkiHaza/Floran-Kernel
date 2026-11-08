#!/usr/bin/env bash
set -euo pipefail

ksu_type=${1:?Missing KernelSU type}
branch=${2:-}
case "$ksu_type" in
  None) exit 0 ;;
  KernelSU-Official|KernelSU-Official-susfs)
    repository=https://github.com/tiann/KernelSU.git
    checkout=KernelSU
    branch=${branch:-main}
    ;;
  KernelSU-Next|KernelSU-Next-susfs)
    repository=https://github.com/pershoot/KernelSU-Next.git
    checkout=KernelSU-Next
    branch=${branch:-dev-susfs}
    ;;
  BakaSU|BakaSU-susfs)
    repository=https://github.com/Baka-SU/BakaSU.git
    checkout=KernelSU
    branch=${branch:-main}
    ;;
  *) echo "Unsupported KernelSU type: $ksu_type" >&2; exit 1 ;;
esac

if [[ $branch == -* ]] || ! git check-ref-format "refs/heads/$branch"; then
  echo "Invalid KernelSU branch: $branch" >&2
  exit 1
fi

# Clone the requested branch before setup: upstream scripts can swallow checkout
# failures. Never silently build their default branch instead of the user's.
# KernelSU version codes count commits; retain history for offline compilation.
git clone --branch "$branch" --single-branch "$repository" "$checkout"
if [[ $(git -C "$checkout" symbolic-ref --short HEAD) != "$branch" ]] ||
   ! git -C "$checkout" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
  echo "Requested KernelSU branch does not exist: $branch" >&2
  exit 1
fi
bash "$checkout/kernel/setup.sh" "$branch"
actual_branch=$(git -C "$checkout" symbolic-ref --short HEAD)
commit=$(git -C "$checkout" rev-parse HEAD)
expected=$(git -C "$checkout" rev-parse "refs/remotes/origin/$branch")
if [[ $actual_branch != "$branch" || $commit != "$expected" ||
      $(realpath drivers/kernelsu) != "$(realpath "$checkout/kernel")" ]]; then
  echo "KernelSU setup did not retain the requested branch: $branch" >&2
  exit 1
fi
echo "KernelSU source: $repository branch=$branch commit=$commit"
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  printf '\n### KernelSU source\n- Type: %s\n- Branch: %s\n- Commit: %s\n' \
    "$ksu_type" "$branch" "$commit" >> "$GITHUB_STEP_SUMMARY"
fi
