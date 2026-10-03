"""Opt-in integration with pinned AK3, official magiskboot and real BusyBox.

WALT_TEST_AK3: local checkout of the revision pinned by prepare_yaap_anykernel.py
WALT_TEST_MAGISKBOOT: official Magisk v31.0 x86_64 libmagiskboot.so
BUSYBOX (or WALT_TEST_BUSYBOX): runnable Linux BusyBox with ash and cpio

Only synthetic images in temporary directories are used. The simulated source
partitions are regular files under /dev/shm, never block devices. Nothing runs
the installer's top-level checks or write/restore loop.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

from test_walt_vendor_boot import make_cpio, make_dtb, make_image
from test_prepare_yaap_anykernel import arm64_tool_bytes


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/anykernel/anykernel.sh"
PARTITION_SIZE = 128 * 1024


def function_definitions(source):
    """Take complete existing top-level definitions, never the flashing loop."""
    wanted = {"walt_hash", "walt_same_image", "walt_metadata_hash", "walt_target",
              "walt_check_cpio_names", "walt_patch_roots", "walt_stage_image"}
    lines = source.splitlines(keepends=True)
    found = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(walt_[a-z_]+)\(\)\s+[({]", line)
        if not match or match.group(1) not in wanted:
            continue
        name = match.group(1)
        if line.rstrip().endswith(("}", ")")):
            found[name] = line
            continue
        for end in range(index + 1, len(lines)):
            if lines[end].strip() in ("}", ")") and not lines[end].startswith((" ", "\t")):
                found[name] = "".join(lines[index:end + 1])
                break
    if set(found) != wanted:
        raise AssertionError(f"installer function boundaries changed: {wanted - set(found)}")
    return "\n".join(found.values())


def fake_kernel(label):
    """Synthetic arm64 Image header; no executable kernel or device is needed."""
    header = bytearray(64)
    struct.pack_into("<QQ", header, 8, 0x80000, 64 + len(label))
    header[56:60] = b"ARM\x64"
    return bytes(header) + label


def boot_v4(kernel, ramdisk):
    header = bytearray(1584)
    header[:8] = b"ANDROID!"
    struct.pack_into("<4I", header, 8, len(kernel), len(ramdisk), 0, 1584)
    struct.pack_into("<I", header, 40, 4)
    image = bytearray()
    for part in (header, kernel, ramdisk):
        image += part
        image += bytes(-len(image) % 4096)
    return bytes(image)


class StageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ak3 = os.environ.get("WALT_TEST_AK3")
        cls.magiskboot = os.environ.get("WALT_TEST_MAGISKBOOT")
        cls.busybox = (os.environ.get("BUSYBOX") or
                       os.environ.get("WALT_TEST_BUSYBOX") or shutil.which("busybox"))
        if not all((cls.ak3, cls.magiskboot, cls.busybox)):
            raise unittest.SkipTest("set WALT_TEST_AK3, WALT_TEST_MAGISKBOOT and BUSYBOX")
        if not Path("/dev/shm").is_dir():
            raise unittest.SkipTest("Linux /dev/shm is required for regular-file AK3 target detection")
        spec = importlib.util.spec_from_file_location("prepare_yaap_anykernel", ROOT / "scripts/prepare_yaap_anykernel.py")
        cls.preparer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.preparer)
        revision = subprocess.check_output(["git", "-C", cls.ak3, "rev-parse", "HEAD"], text=True).strip()
        if revision != cls.preparer.AK3_REV:
            raise AssertionError(f"AK3 checkout must be {cls.preparer.AK3_REV}, got {revision}")

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="walt-stage-")
        self.addCleanup(self.work.cleanup)
        self.sources = tempfile.TemporaryDirectory(prefix="walt-source-", dir="/dev/shm")
        self.addCleanup(self.sources.cleanup)
        self.root = Path(self.work.name)
        self.package = self.root / "package"
        self.run_command(["git", "clone", "--quiet", "--shared", "--no-hardlinks", self.ak3, self.package])
        kernel = self.root / "Image"
        self.new_kernel = fake_kernel(b"new WALT test Image\n")
        kernel.write_bytes(self.new_kernel)
        # Only the package guard sees these synthetic AArch64 ELF headers. They
        # are never executed; staging still runs the real host-architecture tools.
        arm64_inputs = {}
        manifest = {}
        for name in ("busybox", "magiskboot"):
            path = self.root / f"arm64-header-{name}"
            path.write_bytes(arm64_tool_bytes(name.encode()))
            arm64_inputs[name] = path
            manifest[name] = {"source": f"fixture://{name}", "version": "test-header",
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        tool_info = self.root / "tool-info.json"
        tool_info.write_text(json.dumps(manifest))
        self.preparer.prepare(self.package, kernel, arm64_inputs["busybox"],
                              arm64_inputs["magiskboot"], tool_info)
        shutil.copy2(self.magiskboot, self.package / "tools/magiskboot")
        shutil.copy2(self.busybox, self.package / "tools/busybox")
        for name in ("magiskboot", "busybox"):
            (self.package / "tools" / name).chmod(0o755)
        self.applets = self.root / "applets"
        self.applets.mkdir()
        self.run_command([self.package / "tools/busybox", "--install", "-s", self.applets])
        self.backup = self.root / "backup"
        self.backup.mkdir()
        self.transaction = self.root / "transaction"
        (self.transaction / "ready").mkdir(parents=True)
        self.partition = {}
        self.initial = {}
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.package / 'tools'}:{self.applets}:{self.env['PATH']}"
        self.env["TMPDIR"] = str(self.root)

    def run_command(self, command, cwd=None, code=0, env=None):
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def magisk(self, *args, cwd=None, code=0):
        return self.run_command([self.magiskboot, *args], cwd=cwd or self.root, code=code)

    def compressed_cpio(self, name, files):
        source = self.root / f"{name}.cpio"
        target = self.root / f"{name}.lz4"
        source.write_bytes(make_cpio(files))
        self.magisk("compress=lz4_legacy", source, target)
        return target.read_bytes()

    def add_partition(self, name, image):
        self.assertLessEqual(len(image), PARTITION_SIZE)
        image = bytes(image) + bytes(PARTITION_SIZE - len(image))
        path = Path(self.sources.name) / f"{name}_a"
        path.write_bytes(image)
        self.assertTrue(path.is_file())
        self.assertFalse(path.is_block_device())
        self.partition[name] = path
        self.initial[name] = image
        return path

    def stage(self, name):
        functions = function_definitions(INSTALLER.read_text())
        harness = self.root / "stage.sh"
        harness.write_text("""#!/bin/sh
OUTFD=1
WALT_PACKAGE=$1
WALT_TRANSACTION=$2
WALT_BACKUP=$3
WALT_BOOT=$4
WALT_VENDOR_BOOT=$5
WALT_RECOVERY=$6
part_to_test=$7
RAMDISK_COMPRESSION=auto
PATCH_VBMETA_FLAG=auto
NO_MAGISK_CHECK=1
NO_BLOCK_DISPLAY=1
AK3_STAGE_ONLY=1
blockdev() {
  [ "$#" = 2 ] && [ "$1" = --getsize64 ] && [ -f "$2" ] && [ ! -b "$2" ] || {
    echo 'TEST: refusing blockdev mutation or a real device' >&2
    return 99
  }
  wc -c < "$2"
}
getprop() { return 0; }
. "$WALT_PACKAGE/walt-ramdisk.sh"
. "$WALT_PACKAGE/walt-vendor-boot.sh"
""" + functions + "\nwalt_stage_image \"$part_to_test\"\n")
        paths = [self.partition.get(part, self.root / "absent") for part in ("boot", "vendor_boot", "recovery")]
        result = self.run_command([self.package / "tools/busybox", "ash", harness, self.package,
                                   self.transaction, self.backup, *paths, name], env=self.env)
        for part, original in self.initial.items():
            self.assertEqual(self.partition[part].read_bytes(), original, "staging wrote a source partition")
        self.assertEqual((self.backup / f"{name}.img").read_bytes(), self.initial[name])
        return result

    def unpack_ready(self, name, vendor=False):
        ready = self.transaction / "ready" / f"{name}.img"
        self.assertTrue(ready.is_file())
        self.assertEqual(ready.stat().st_size, PARTITION_SIZE)
        directory = self.root / f"check-{name}"
        directory.mkdir()
        self.magisk("unpack", ready, cwd=directory, code=3 if vendor else 0)
        return directory

    def cpio_member(self, archive, member):
        destination = self.root / ("extracted-" + hashlib.sha256(str(archive).encode() + member.encode()).hexdigest())
        self.magisk("cpio", archive, f"extract {member} {destination}")
        return destination.read_bytes()

    @staticmethod
    def module_files():
        return {
            "lib/modules/modules.load": b"qcom-cpufreq-hw.ko\nsched-walt.ko\nother.ko\n",
            "lib/modules/modules.dep": b"qcom-cpufreq-hw.ko:\nsched-walt.ko: qcom-cpufreq-hw.ko\nother.ko: qcom-cpufreq-hw.ko helper.ko\nhelper.ko:\n",
            "lib/modules/modules.alias": b"alias test-cpufreq qcom_cpufreq_hw\nalias test-other other\n",
            "lib/modules/other.ko": b"unchanged other module bytes\n",
        }

    def assert_modules_patched(self, archive):
        self.assertEqual(self.cpio_member(archive, "lib/modules/modules.load"), b"other.ko\n")
        self.assertEqual(self.cpio_member(archive, "lib/modules/modules.dep"), b"other.ko: helper.ko\nhelper.ko:\n")
        self.assertEqual(self.cpio_member(archive, "lib/modules/modules.alias"), b"alias test-other other\n")
        self.assertEqual(self.cpio_member(archive, "lib/modules/other.ko"), b"unchanged other module bytes\n")

    def test_vendor_boot_staging_with_real_ak3(self):
        platform = self.compressed_cpio("platform", {"init.fixture": b"unchanged platform data\n"})
        recovery = self.compressed_cpio("recovery", {"recovery.fixture": b"unchanged recovery data\n"})
        dlkm = self.compressed_cpio("dlkm", self.module_files())
        entries = [(b"", 1, platform, bytes(64)), (b"recovery", 2, recovery, bytes(64)),
                   (b"dlkm", 3, dlkm, struct.pack("<16I", *range(16)))]
        self.add_partition("vendor_boot", make_image(entries, dtb=make_dtb()))
        self.stage("vendor_boot")
        checked = self.unpack_ready("vendor_boot", vendor=True)
        self.assert_modules_patched(checked / "vendor_ramdisk/dlkm.cpio")
        self.assertEqual(self.cpio_member(checked / "vendor_ramdisk/ramdisk.cpio", "init.fixture"), b"unchanged platform data\n")
        self.assertEqual(self.cpio_member(checked / "vendor_ramdisk/recovery.cpio", "recovery.fixture"), b"unchanged recovery data\n")
        self.assertEqual((checked / "dtb").read_bytes(), make_dtb())
        self.assertEqual((checked / "bootconfig").read_bytes(), b"androidboot.test=1\n")

    def test_boot_staging_replaces_only_kernel(self):
        ramdisk = self.compressed_cpio("boot", {"init.fixture": b"generic boot ramdisk\n"})
        self.add_partition("boot", boot_v4(fake_kernel(b"old Image\n"), ramdisk))
        self.stage("boot")
        checked = self.unpack_ready("boot")
        self.assertEqual((checked / "kernel").read_bytes(), self.new_kernel)
        self.assertEqual(self.cpio_member(checked / "ramdisk.cpio", "init.fixture"), b"generic boot ramdisk\n")

    def test_shared_kernel_recovery_staging(self):
        ramdisk = self.compressed_cpio("recovery", self.module_files())
        self.add_partition("recovery", boot_v4(b"", ramdisk))
        self.stage("recovery")
        checked = self.unpack_ready("recovery")
        self.assert_modules_patched(checked / "ramdisk.cpio")
        self.assertFalse((checked / "kernel").exists())

    def assert_recovery_only_backed_up(self):
        self.assertEqual({path.name for path in self.backup.iterdir()}, {"recovery.img"})
        self.assertEqual((self.backup / "recovery.img").read_bytes(), self.initial["recovery"])
        self.assertEqual(self.partition["recovery"].read_bytes(), self.initial["recovery"])
        self.assertEqual(list((self.transaction / "ready").iterdir()), [])

    def test_shared_kernel_recovery_without_module_metadata_is_only_backed_up(self):
        ramdisk = self.compressed_cpio("recovery", {"init.fixture": b"recovery has no module metadata\n"})
        self.add_partition("recovery", boot_v4(b"", ramdisk))
        self.stage("recovery")
        self.assert_recovery_only_backed_up()

    def test_shared_kernel_recovery_with_clean_module_metadata_is_only_backed_up(self):
        files = {
            "lib/modules/modules.load": b"other.ko\nhelper.ko\n",
            "lib/modules/modules.dep": b"other.ko: helper.ko\nhelper.ko:\n",
            "lib/modules/modules.alias": b"alias test-other other\n",
            "lib/modules/other.ko": b"unchanged other module bytes\n",
            "lib/modules/helper.ko": b"unchanged helper module bytes\n",
        }
        ramdisk = self.compressed_cpio("recovery", files)
        self.add_partition("recovery", boot_v4(b"", ramdisk))
        self.stage("recovery")
        self.assert_recovery_only_backed_up()

    def test_recovery_with_own_kernel_is_not_staged(self):
        ramdisk = self.compressed_cpio("recovery", self.module_files())
        self.add_partition("recovery", boot_v4(fake_kernel(b"independent recovery Image\n"), ramdisk))
        self.stage("recovery")
        self.assertFalse((self.transaction / "ready/recovery.img").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
