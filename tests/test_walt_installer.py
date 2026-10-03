#!/usr/bin/env python3
"""Run the real installer transaction in BusyBox ash with file-backed fixtures.

No device nodes are opened. Every dd and blockdev call is replaced by a wrapper
that accepts only paths inside the test's temporary directory. The only source
adaptation changes block-device existence tests to regular-file fixture tests.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/anykernel/anykernel.sh"
BUSYBOX = os.environ.get("BUSYBOX") or shutil.which("busybox")


def section(text, start, end=None):
    if text.count(start) != 1:
        raise AssertionError(f"Installer boundary changed: {start}")
    result = text[text.index(start):]
    if end is not None:
        if result.count(end) != 1:
            raise AssertionError(f"Installer boundary changed: {end}")
        result = result[:result.index(end)]
    return result


@unittest.skipUnless(BUSYBOX, "BusyBox is required for the Android ash runtime")
class WaltInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="walt-installer-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = INSTALLER.read_text()
        self.applets = self.base / "applets"
        self.applets.mkdir()
        for name in ("awk", "cat", "chmod", "cmp", "cp", "find", "grep", "ln",
                     "mkdir", "mktemp", "rm", "sed", "sha256sum", "sort", "stat", "wc"):
            (self.applets / name).symlink_to(Path(BUSYBOX).resolve())
        for name in ("partitions", "backup", "transaction/ready", "new", "package/tools"):
            (self.base / name).mkdir(parents=True, exist_ok=True)
        self.originals = {}
        for part in ("vendor_boot", "recovery", "boot"):
            original = (f"ORIGINAL {part}\n".encode() * 512)[:4096]
            replacement = (f"REPLACEMENT {part}\n".encode() * 512)[:4096]
            self.originals[part] = original
            (self.base / "partitions" / f"{part}_a").write_bytes(original)
            (self.base / "new" / f"{part}.img").write_bytes(replacement)
        self.helpers = section(self.source, "walt_hash()", "# Inspect actual late-load requests")
        self.stage = section(self.source, "walt_stage_image() (", "walt_stage_image vendor_boot ||")
        self.transaction = section(self.source, "walt_stage_image vendor_boot ||")
        # A regular fixture file stands in for a detected recovery block node.
        probe = '[ -b "$WALT_RECOVERY" ]'
        self.assertEqual(self.transaction.count(probe), 1)
        self.transaction = self.transaction.replace(probe, '[ -f "$WALT_RECOVERY" ]')

    def run_ash(self, body, **settings):
        env = {
            **os.environ,
            "PATH": str(self.applets),
            "SANDBOX": str(self.base),
            "BUSYBOX_TEST": str(Path(BUSYBOX).resolve()),
            **{key: str(value) for key, value in settings.items()},
        }
        return subprocess.run(
            [str(BUSYBOX), "ash", "-c", self.harness() + "\n" + body],
            env=env, text=True, capture_output=True, timeout=20,
        )

    def harness(self):
        return r'''
WALT_PACKAGE=$SANDBOX/package
WALT_TRANSACTION=$SANDBOX/transaction
WALT_BACKUP=$SANDBOX/backup
WALT_BOOT=$SANDBOX/partitions/boot_a
WALT_VENDOR_BOOT=$SANDBOX/partitions/vendor_boot_a
WALT_RECOVERY=$SANDBOX/partitions/recovery_a
WALT_ATTEMPTED=
SLOT=_a
EVENTS=$SANDBOX/events
: > "$EVENTS"
ui_print() { printf '%s\n' "$*"; }
abort() { ui_print "$*"; exit 1; }
sync() { :; }
getprop() { printf '%s\n' "${TEST_PLATFORM:-pineapple}"; }
blockdev() {
  case "$2" in "$SANDBOX"/partitions/*) ;; *) echo 'UNSAFE blockdev' >&2; exit 90;; esac
  case "$1" in
    --getsize64) wc -c < "$2";;
    --setrw) printf 'setrw %s\n' "${2##*/}" >> "$EVENTS";;
    *) echo 'UNEXPECTED blockdev operation' >&2; exit 90;;
  esac
}
dd() {
  local input= output= arg
  for arg in "$@"; do
    case "$arg" in if=*) input=${arg#if=};; of=*) output=${arg#of=};; esac
  done
  case "$input" in "$SANDBOX"/*|/dev/zero) ;; *) echo 'UNSAFE dd input' >&2; exit 91;; esac
  case "$output" in "$SANDBOX"/*) ;; *) echo 'UNSAFE dd output' >&2; exit 91;; esac
  case "$output" in
    "$SANDBOX"/partitions/*)
      printf 'write %s %s\n' "${input#"$SANDBOX"/}" "${output##*/}" >> "$EVENTS"
      case "$input" in
        "$WALT_TRANSACTION"/ready/*)
          if [ "${output##*/}" = "${FAIL_PART:-none}_a" ] && [ ! -f "$SANDBOX/failed-once" ]; then
            : > "$SANDBOX/failed-once"
            "$BUSYBOX_TEST" dd if="$input" of="$output" bs=16 count=1 conv=notrunc 2>/dev/null || return 1
            return 1
          fi
          ;;
      esac
      ;;
  esac
  "$BUSYBOX_TEST" dd "$@" 2>/dev/null || return 1
  case "$input" in
    "$WALT_TRANSACTION"/ready/*)
      if [ "${output##*/}" = "${CORRUPT_PART:-none}_a" ]; then
        printf '!' | "$BUSYBOX_TEST" dd of="$output" bs=1 count=1 conv=notrunc 2>/dev/null || return 1
      fi
      ;;
  esac
}
'''

    def fixture_stage(self):
        return r'''
walt_stage_image() {
  local part=$1 target
  printf 'stage %s\n' "$part" >> "$EVENTS"
  [ "$part" != "${FAIL_STAGE:-none}" ] || return 1
  target=$(walt_target "$part") || return 1
  cp "$target" "$WALT_BACKUP/$part.img" || return 1
  cp "$SANDBOX/new/$part.img" "$WALT_TRANSACTION/ready/$part.img"
}
'''

    def events(self):
        return (self.base / "events").read_text().splitlines()

    def assert_originals(self):
        for part, original in self.originals.items():
            self.assertEqual((self.base / "partitions" / f"{part}_a").read_bytes(), original)

    def test_all_preparation_completes_before_first_partition_write(self):
        result = self.run_ash(self.helpers + self.fixture_stage() + self.transaction)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertEqual(events[:3], ["stage vendor_boot", "stage recovery", "stage boot"])
        self.assertEqual(sum(line.startswith("write ") for line in events), 3)
        self.assertNotIn("Restoring original", result.stdout)
        for part in self.originals:
            self.assertEqual((self.base / "partitions" / f"{part}_a").read_bytes(),
                             (self.base / "new" / f"{part}.img").read_bytes())
        self.assertTrue((self.base / "backup/SHA256SUMS").is_file())

    def test_preparation_failure_never_writes_any_partition(self):
        for failing in ("vendor_boot", "recovery", "boot"):
            with self.subTest(part=failing):
                result = self.run_ash(self.helpers + self.fixture_stage() + self.transaction,
                                      FAIL_STAGE=failing)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(line.startswith(("write ", "setrw ")) for line in self.events()))
                self.assert_originals()

    def test_partial_write_restores_current_and_prior_partitions_in_reverse_order(self):
        result = self.run_ash(self.helpers + self.fixture_stage() + self.transaction, FAIL_PART="boot")
        self.assertNotEqual(result.returncode, 0)
        writes = [line for line in self.events() if line.startswith("write ")]
        self.assertEqual(writes, [
            "write transaction/ready/vendor_boot.img vendor_boot_a",
            "write transaction/ready/recovery.img recovery_a",
            "write transaction/ready/boot.img boot_a",
            "write backup/boot.img boot_a",
            "write backup/recovery.img recovery_a",
            "write backup/vendor_boot.img vendor_boot_a",
        ])
        self.assert_originals()
        self.assertIn("Original partition contents restored.", result.stdout)

    def test_first_partial_write_is_also_rolled_back(self):
        result = self.run_ash(self.helpers + self.fixture_stage() + self.transaction,
                              FAIL_PART="vendor_boot")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([x for x in self.events() if x.startswith("write ")], [
            "write transaction/ready/vendor_boot.img vendor_boot_a",
            "write backup/vendor_boot.img vendor_boot_a",
        ])
        self.assert_originals()

    def test_successful_write_with_bad_readback_rolls_back(self):
        result = self.run_ash(self.helpers + self.fixture_stage() + self.transaction,
                              CORRUPT_PART="recovery")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recovery read-back verification failed.", result.stdout)
        self.assertEqual([x for x in self.events() if x.startswith("write ")], [
            "write transaction/ready/vendor_boot.img vendor_boot_a",
            "write transaction/ready/recovery.img recovery_a",
            "write backup/recovery.img recovery_a",
            "write backup/vendor_boot.img vendor_boot_a",
        ])
        self.assert_originals()

    def test_failed_hashes_cannot_compare_as_equal_empty_strings(self):
        result = self.run_ash(self.helpers + r'''
sha256sum() { return 1; }
if walt_same_image "$WALT_BOOT" "$WALT_BOOT"; then exit 12; fi
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_originals()

    def test_restore_hash_failure_is_reported(self):
        body = self.helpers + self.fixture_stage() + r'''
sha256sum() {
  if [ -f "$SANDBOX/failed-once" ]; then return 1; fi
  "$BUSYBOX_TEST" sha256sum "$@"
}
''' + self.transaction
        result = self.run_ash(body, FAIL_PART="boot")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore failed.", result.stdout)
        self.assertNotIn("Original partition contents restored.", result.stdout)
        self.assert_originals()

    def test_independent_recovery_uses_real_stage_skip_and_is_never_written(self):
        # Only the AK3 codec boundary is stubbed. The installer itself decides
        # whether the independent recovery kernel requires skipping this image.
        (self.base / "package/tools/ak3-core.sh").write_text(r'''
BOOTIMG=$AKHOME/boot.img
SPLITIMG=$AKHOME/split_img
split_boot() {
  mkdir -p "$SPLITIMG" || return 1
  printf 'independent recovery kernel\n' > "$SPLITIMG/kernel"
}
unpack_ramdisk() { echo 'unexpected recovery unpack' >&2; exit 92; }
flash_boot() { echo 'unexpected recovery repack' >&2; exit 92; }
''')
        # Rename only the function definition to call the real implementation
        # for recovery while providing prepared fixtures for the other images.
        stage = self.stage.replace("walt_stage_image() (", "real_stage_image() (", 1)
        dispatch = self.fixture_stage().replace("walt_stage_image() {", "fixture_stage_image() {", 1)
        dispatch += r'''
walt_stage_image() {
  if [ "$1" = recovery ]; then real_stage_image recovery; else fixture_stage_image "$1"; fi
}
'''
        result = self.run_ash(self.helpers + stage + dispatch + self.transaction)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Recovery has its own kernel", result.stdout)
        self.assertFalse((self.base / "transaction/ready/recovery.img").exists())
        self.assertFalse(any(line.startswith("write ") and line.endswith("recovery_a")
                             for line in self.events()))
        self.assertEqual((self.base / "partitions/recovery_a").read_bytes(), self.originals["recovery"])

    def test_slot_and_platform_checks_reject_mismatched_targets(self):
        checks = section(self.source, 'case "$SLOT" in _a|_b)', "WALT_BOOT=$BLOCK")
        for slot, block, platform, success in (
            ("_a", "boot_a", "pineapple", True),
            ("_b", "boot_b", "pineapple", True),
            ("", "boot_a", "pineapple", False),
            ("_c", "boot_c", "pineapple", False),
            ("_a", "boot_b", "pineapple", False),
            ("_a", "boot_a", "other", False),
        ):
            with self.subTest(slot=slot, block=block, platform=platform):
                result = self.run_ash('SLOT=$TEST_SLOT\nBLOCK=$SANDBOX/$TEST_BLOCK\n' + checks,
                                      TEST_SLOT=slot, TEST_BLOCK=block, TEST_PLATFORM=platform)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def external_check(self):
        code = '. "$EXTERNAL_HELPER"\nwalt_check_external_modules "$SANDBOX/vendor/lib/modules" "$SANDBOX/system/lib/modules"\n'
        return self.run_ash(code, EXTERNAL_HELPER=INSTALLER.parent / "walt-external-modules.sh",
                            TMPDIR=self.base)

    def make_module_tree(self, family):
        directory = self.base / family / "lib/modules"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "modules.dep").write_text("safe.ko:\n")
        (directory / "modules.load").write_text("safe.ko\n")
        return directory

    def test_external_check_requires_both_vendor_and_system(self):
        self.make_module_tree("system")
        self.assertNotEqual(self.external_check().returncode, 0)
        self.make_module_tree("vendor")
        self.assertEqual(self.external_check().returncode, 0)

    def test_nested_system_modules_are_discovered(self):
        directory = self.base / "system/lib/modules/android17-6.1"
        directory.mkdir(parents=True)
        (directory / "modules.load").write_text("safe.ko\n")
        self.make_module_tree("vendor")
        self.assertNotEqual(self.external_check().returncode, 0)
        (directory / "modules.dep").write_text("safe.ko:\n")
        self.assertEqual(self.external_check().returncode, 0)

    def test_external_references_and_binary_indexes_fail_but_debug_does_not(self):
        self.make_module_tree("system")
        vendor = self.make_module_tree("vendor")
        load = vendor / "modules.load"
        load.write_text("qcom-cpufreq-hw-debug.ko\nsched_walt_debug.ko\n")
        self.assertEqual(self.external_check().returncode, 0)
        for reference in ("qcom-cpufreq-hw.ko", "sched_walt", "/lib/modules/qcom_cpufreq_hw.ko.zst"):
            with self.subTest(reference=reference):
                load.write_text(reference + "\n")
                self.assertNotEqual(self.external_check().returncode, 0)
        load.write_text("safe.ko\n")
        (vendor / "modules.dep.bin").write_bytes(b"\0binary\0")
        self.assertNotEqual(self.external_check().returncode, 0)

    def test_backup_storage_rejects_temporary_filesystems(self):
        check = section(self.source, 'case "$(stat -f -c %t /sdcard', "backup_bytes=0")
        for filesystem, success in (("1021994", False), ("858458f6", False), ("UNKNOWN", False),
                                    ("ef53", True), ("f2f52010", True), ("65735546", True),
                                    ("5dca2df5", True), ("4d44", True), ("2011bab0", True)):
            with self.subTest(filesystem=filesystem):
                result = self.run_ash('stat() { printf "%s\\n" "$TEST_FILESYSTEM"; }\n' + check,
                                      TEST_FILESYSTEM=filesystem)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
