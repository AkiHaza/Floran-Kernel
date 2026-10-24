#!/usr/bin/env bash
set -euo pipefail

apply_ksu_patch() {
  local ksu_type susfs_dir ksu_dir ksu_root ksu_patch anchor function
  ksu_type=${1:?Missing KernelSU type}
  if [[ $ksu_type != KernelSU-Official-susfs ]]; then
    echo "Unsupported SUSFS KernelSU type: $ksu_type (official-susfs only)" >&2
    exit 1
  fi
  susfs_dir=$(realpath "${2:?Missing SUSFS source directory}")
  ksu_dir=$(realpath drivers/kernelsu)

  ksu_root=$(git -C "$ksu_dir" rev-parse --show-toplevel)
  ksu_patch="$susfs_dir/kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"
  # Check every hunk before changing KernelSU; never build a partial port.
  git -C "$ksu_root" apply --check "$ksu_patch"
  git -C "$ksu_root" apply "$ksu_patch"

  if ! grep -Eq '^[[:space:]]*config[[:space:]]+KSU_SUSFS[[:space:]]*$' "$ksu_dir/Kconfig"; then
    echo "KernelSU has no SUSFS Kconfig support: $ksu_type" >&2
    exit 1
  fi

  for anchor in 'susfs_init(' 'susfs_is_current_ksu_domain(' \
    'ksu_handle_post_execveat_sucompat(' 'CMD_SUSFS_SHOW_VERSION'; do
    if ! grep -rFq --include='*.c' "$anchor" "$ksu_dir"; then
      echo "KernelSU SUSFS integration is incomplete: missing $anchor" >&2
      exit 1
    fi
  done

  for function in ksu_setup_preinit_file ksu_preinit_handle_post_execveat; do
    if ! grep -rPzq --include='*.c' "(?m)^int[ \t]+${function}[ \t]*\([^;{}]*\)[ \t\r\n]*\{" "$ksu_dir"; then
      echo "KernelSU SUSFS preinit integration is incomplete: missing ${function}(" >&2
      exit 1
    fi
  done

  echo "KernelSU SUSFS integration verified: $ksu_type"
}

verify_build() {
  local mode out option artifact symbol
  mode=${1:?Missing verification mode}
  out=${2:?Missing kernel output directory}

  case "$mode" in
    config|built) ;;
    *) echo "Unknown SUSFS verification mode: $mode" >&2; exit 1 ;;
  esac

  for option in KSU KSU_SUSFS KSU_SUSFS_SUS_PATH KSU_SUSFS_SUS_MOUNT \
    KSU_SUSFS_SUS_KSTAT KSU_SUSFS_SPOOF_UNAME KSU_SUSFS_ENABLE_LOG \
    KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG \
    KSU_SUSFS_SUS_MAP; do
    if ! grep -Fxq "CONFIG_${option}=y" "$out/.config"; then
      echo "SUSFS verification failed: CONFIG_${option}=y missing from $out/.config" >&2
      exit 1
    fi
  done

  if [[ $mode == built ]]; then
    for artifact in fs/susfs.o vmlinux System.map arch/arm64/boot/Image; do
      if [[ ! -s "$out/$artifact" ]]; then
        echo "SUSFS verification failed: missing or empty $out/$artifact" >&2
        exit 1
      fi
    done
    # Static-key data must survive linking even when LTO inlines hook functions.
    for symbol in susfs_is_sdcard_android_data_not_decrypted ksu_is_input_hook_enabled \
      ksu_su_compat_enabled; do
      if ! awk -v symbol="$symbol" '$NF == symbol || $NF ~ ("^" symbol "[.]llvm[.][0-9]+$") { found = 1 } END { exit !found }' "$out/System.map"; then
        echo "SUSFS verification failed: $symbol missing from $out/System.map" >&2
        exit 1
      fi
    done
  fi

  echo "SUSFS $mode verification passed."
}

integrate_source() {
  local ksu_type script_dir
  ksu_type=${1:?Missing KernelSU type}
  if [[ $ksu_type != KernelSU-Official-susfs ]]; then
    echo "This integration helper only supports KernelSU-Official-susfs." >&2
    exit 1
  fi
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

  git clone --depth=1 -b gki-android14-6.1 https://gitlab.com/simonpunk/susfs4ksu.git susfs
  echo "SUSFS source commit: $(git -C susfs rev-parse HEAD)"
  echo "KernelSU source commit: $(git -C drivers/kernelsu rev-parse HEAD)"

  apply_ksu_patch "$ksu_type" susfs
  cp susfs/kernel_patches/fs/* fs/
  cp susfs/kernel_patches/include/linux/* include/linux/
  python3 "$script_dir/apply-susfs-kernel.py" \
    susfs/kernel_patches/50_add_susfs_in_gki-android14-6.1.patch

  grep '^#define SUSFS_VERSION' include/linux/susfs.h
  echo 'SUSFS source integration completed without unresolved patch hunks.'
}

main() {
  local mode=${1:?Usage: integrate-susfs.sh source KSU_TYPE | config|built OUTPUT_DIRECTORY}
  shift
  case "$mode" in
    source) integrate_source "$@" ;;
    config|built) verify_build "$mode" "$@" ;;
    *) echo "Unknown SUSFS verification mode: $mode" >&2; return 1 ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
