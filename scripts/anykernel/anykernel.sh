### YAAP-17 WALT installer, using the pinned AnyKernel3 tools.
properties() { '
kernel.string=YAAP-17 WALT by AkiHaza
do.devicecheck=0
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; }

BLOCK=boot
IS_SLOT_DEVICE=1
SLOT_SELECT=active
RAMDISK_COMPRESSION=auto
PATCH_VBMETA_FLAG=auto
NO_MAGISK_CHECK=1
AK3_STAGE_ONLY=1
. tools/ak3-core.sh
. "$AKHOME/walt-ramdisk.sh"
. "$AKHOME/walt-vendor-boot.sh"
. "$AKHOME/walt-external-modules.sh"

WALT_PACKAGE=$AKHOME
WALT_TRANSACTION=$AKHOME/walt-transaction
WALT_ATTEMPTED=
case "$SLOT" in _a|_b) ;; *) abort "Cannot determine the active A/B slot.";; esac
[ "$(getprop ro.board.platform)" = pineapple ] || abort "This WALT package requires SM8650/pineapple."
case "$BLOCK" in */boot"$SLOT") ;; *) abort "Boot partition does not match the active slot.";; esac
WALT_BOOT=$BLOCK
WALT_VENDOR_BOOT=${BLOCK%/*}/vendor_boot$SLOT
WALT_RECOVERY=${BLOCK%/*}/recovery$SLOT
[ -b "$WALT_VENDOR_BOOT" ] || abort "Missing vendor_boot partition."
mkdir -p "$WALT_TRANSACTION/ready" || abort "Cannot create staging directory."
TMPDIR=$WALT_TRANSACTION/tmp
mkdir -p "$TMPDIR" || abort "Cannot create temporary directory."
export TMPDIR

walt_hash() ( set -o pipefail; sha256sum "$1" | awk '{print $1}'; )
walt_same_image() {
  local first second
  first=$(walt_hash "$1") || return 1
  second=$(walt_hash "$2") || return 1
  [ -n "$first" ] && [ "$first" = "$second" ]
}
walt_metadata_hash() (
  set -o pipefail
  find "$1" -type f -name 'modules.*' -print0 |
    while IFS= read -r -d '' file; do
      sha256sum "$file" || exit 1
    done | LC_ALL=C sort | sha256sum
)
walt_target() {
  case "$1" in
    boot) printf '%s\n' "$WALT_BOOT";;
    vendor_boot) printf '%s\n' "$WALT_VENDOR_BOOT";;
    recovery) printf '%s\n' "$WALT_RECOVERY";;
    *) return 1;;
  esac
}

# Inspect actual late-load requests, including versioned system module trees.
walt_check_external_modules || abort "Vendor/system module loading verification failed; see the diagnostics above."

# Some Android BusyBox builds print UNKNOWN for FUSE with %T. The numeric
# filesystem magic is stable across BusyBox versions.
case "$(stat -f -c %t /sdcard 2>/dev/null)" in
  ef53|f2f52010|65735546|5dca2df5|4d44|2011bab0) ;;
  *) abort "Mount persistent internal storage at /sdcard before installing.";;
esac
backup_bytes=0
for target in "$WALT_BOOT" "$WALT_VENDOR_BOOT" "$WALT_RECOVERY"; do
  [ -b "$target" ] || continue
  bytes=$(blockdev --getsize64 "$target") || abort "Cannot read partition size."
  case "$bytes" in ''|*[!0-9]*) abort "Invalid partition size.";; esac
  backup_bytes=$((backup_bytes + bytes))
done
backup_free_kb=$(df -Pk /sdcard | awk 'END {print $4}')
case "$backup_free_kb" in ''|*[!0-9]*) abort "Cannot check backup storage space.";; esac
[ "$backup_free_kb" -ge $(((backup_bytes + 1023) / 1024 + 65536)) ] || abort "Insufficient internal storage for original images."
WALT_BACKUP=/sdcard/YAAP-WALT-backups/$(date +%Y%m%d-%H%M%S)$SLOT-$$
mkdir -p "$WALT_BACKUP" || abort "Mount internal storage to save the original boot images."
chmod 700 "$WALT_BACKUP"
ui_print "Preparing WALT boot/vendor_boot images for slot $SLOT."
ui_print "Original images: $WALT_BACKUP"

walt_check_cpio_names() (
  set -o pipefail
  local layout=$1 directory=$2
  awk '$1 == "cpio" { print $2 }' "$layout" | LC_ALL=C sort > "$directory/expected-cpio.txt"
  (cd "$directory/vendor_ramdisk" && find . -maxdepth 1 -type f -name '*.cpio' | sed 's|^./||' | LC_ALL=C sort) > "$directory/actual-cpio.txt" || return 1
  cmp -s "$directory/expected-cpio.txt" "$directory/actual-cpio.txt"
)

walt_patch_roots() {
  local root patched=0 before after
  WALT_METADATA_CHANGED=0
  for root in "$RAMDISK" "$VENDORRD"/*; do
    [ -d "$root" ] || continue
    before=$(walt_metadata_hash "$root") || return 1
    walt_patch_ramdisk "$root" || return 1
    after=$(walt_metadata_hash "$root") || return 1
    if [ "$before" != "$after" ]; then
      [ "$1" != verify ] || return 1
      WALT_METADATA_CHANGED=1
    fi
    patched=1
  done
  [ "$patched" = 1 ]
}

# Each phase gets isolated AK3 paths; no partition is written during staging.
walt_stage_image() (
  part=$1
  target=$(walt_target "$part") || exit 1
  stage=$WALT_TRANSACTION/$part
  original=$WALT_BACKUP/$part.img
  mkdir -p "$stage" || exit 1
  size=$(blockdev --getsize64 "$target") || exit 1
  case "$size" in ''|*[!0-9]*) exit 1;; esac
  [ "$size" -gt 0 ] && [ $((size % 4096)) -eq 0 ] || exit 1
  dd if="$target" of="$original" bs=1048576 || exit 1
  chmod 600 "$original"
  [ "$(wc -c < "$original")" -eq "$size" ] || exit 1
  walt_same_image "$original" "$target" || exit 1
  if [ "$part" = vendor_boot ]; then
    walt_vendor_layout "$original" > "$stage/original-layout.txt" || exit 1
  fi

  AKHOME=$stage
  cd "$stage" || exit 1
  ln -s "$WALT_PACKAGE/tools" tools || exit 1
  BLOCK=$target
  IS_SLOT_DEVICE=0
  unset SLOT SLOT_SELECT
  . "$WALT_PACKAGE/tools/ak3-core.sh"
  # Split the validated snapshot, not a second live read of the partition.
  BLOCK=$original
  CUSTOMDD=bs=1048576
  split_boot
  if [ "$part" = recovery ] && [ -s "$SPLITIMG/kernel" ]; then
    ui_print "Recovery has its own kernel; retaining its module metadata."
    exit 0
  fi
  if [ "$part" = boot ]; then
    cp "$WALT_PACKAGE/boot-files/Image" "$AKHOME/Image" || exit 1
  else
    if [ "$part" = vendor_boot ]; then
      walt_check_cpio_names "$stage/original-layout.txt" "$SPLITIMG" || exit 1
    fi
    unpack_ramdisk
    walt_patch_roots patch || exit 1
    if [ "$WALT_METADATA_CHANGED" = 0 ]; then
      ui_print "$part module metadata needs no changes; retaining original image."
      exit 0
    fi
    repack_ramdisk
  fi
  flash_boot
  [ -s "$AKHOME/boot-new.img" ] || exit 1

  if [ "$part" = vendor_boot ]; then
    walt_vendor_layout "$AKHOME/boot-new.img" > "$stage/new-layout.txt" || exit 1
    cmp -s "$stage/original-layout.txt" "$stage/new-layout.txt" || exit 1
  fi
  mkdir "$stage/verify" || exit 1
  cd "$stage/verify" || exit 1
  magiskboot unpack -h "$AKHOME/boot-new.img" > infotmp 2>&1
  unpack_status=$?
  case "$part:$unpack_status" in
    vendor_boot:3|boot:0|recovery:0) ;;
    *) exit 1;;
  esac
  if [ "$part" = boot ]; then
    cmp -s kernel "$WALT_PACKAGE/boot-files/Image" || exit 1
  else
    SPLITIMG=$stage/verify
    RAMDISK=$stage/verified-ramdisk
    VENDORRD=$stage/verified-vendor-ramdisk
    [ "$part" != vendor_boot ] || walt_check_cpio_names "$stage/new-layout.txt" "$SPLITIMG" || exit 1
    unpack_ramdisk
    walt_patch_roots verify || exit 1
  fi
  # Full-size staged images let read-back hashes include every partition byte.
  ready=$WALT_TRANSACTION/ready/$part.img
  dd if=/dev/zero of="$ready" bs=4096 count=$((size / 4096)) || exit 1
  dd if="$AKHOME/boot-new.img" of="$ready" bs=1048576 conv=notrunc || exit 1
  [ "$(wc -c < "$ready")" -eq "$size" ] || exit 1
)

walt_stage_image vendor_boot || abort "vendor_boot preparation failed; no partitions were written."
if [ -b "$WALT_RECOVERY" ]; then
  walt_stage_image recovery || abort "Recovery preparation failed; no partitions were written."
fi
walt_stage_image boot || abort "Boot preparation failed; no partitions were written."
sync
(cd "$WALT_BACKUP" && sha256sum ./*.img > SHA256SUMS) || abort "Cannot record backup checksums."

walt_restore() {
  local part target failed=0
  trap - EXIT HUP INT TERM
  for part in $WALT_ATTEMPTED; do
    target=$(walt_target "$part")
    ui_print "Restoring original $part..."
    blockdev --setrw "$target" 2>/dev/null
    if ! dd if="$WALT_BACKUP/$part.img" of="$target" bs=1048576; then
      failed=1
    fi
    sync
    walt_same_image "$target" "$WALT_BACKUP/$part.img" || failed=1
  done
  if [ "$failed" = 1 ]; then
    ui_print "Restore failed. Recover the saved images in $WALT_BACKUP before rebooting."
  else
    ui_print "Original partition contents restored."
  fi
  exit 1
}
trap walt_restore EXIT HUP INT TERM
ui_print "All required images verified. Updating prepared partitions."
for part in vendor_boot recovery boot; do
  [ -f "$WALT_TRANSACTION/ready/$part.img" ] || continue
  target=$(walt_target "$part") || abort "Invalid partition in write plan."
  # Include the current partition before its first write, including partial failures.
  WALT_ATTEMPTED="$part $WALT_ATTEMPTED"
  blockdev --setrw "$target" 2>/dev/null || abort "Cannot make $part writable."
  dd if="$WALT_TRANSACTION/ready/$part.img" of="$target" bs=1048576 || abort "Writing $part failed."
  sync
  walt_same_image "$target" "$WALT_TRANSACTION/ready/$part.img" || abort "$part read-back verification failed."
done
trap - EXIT HUP INT TERM
ui_print "WALT images installed. Keep the original image set at $WALT_BACKUP for rollback."
