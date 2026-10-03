#!/usr/bin/env python3
"""Execute the ramdisk patcher in BusyBox ash against unpacked-ramdisk fixtures.

Run: BUSYBOX=/path/to/busybox python3 -m unittest discover -s tests -v
No boot image, device, or kernel build is needed.
"""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


PATCHER = Path(__file__).resolve().parents[1] / "scripts/anykernel/walt-ramdisk.sh"
BUSYBOX = os.environ.get("BUSYBOX") or shutil.which("busybox")


@unittest.skipUnless(BUSYBOX, "BusyBox is required to test the Android ash runtime")
class WaltRamdiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="walt-ramdisk-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "ramdisk fragments"
        self.root.mkdir()
        # Exercise BusyBox implementations as well as ash, rather than silently
        # relying on GNU awk/find/cp features available on the development host.
        self.applets = self.base / "busybox-applets"
        self.applets.mkdir()
        for applet in ("awk", "find", "cp", "cmp", "mv", "mktemp", "rm", "stat", "chmod", "tr", "readlink"):
            (self.applets / applet).symlink_to(Path(BUSYBOX).resolve())

    def write(self, relative, text, mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def patch(self, success=True, root=None):
        result = subprocess.run(
            [str(BUSYBOX), "ash", "-c", '. "$1"; walt_patch_ramdisk "$2"',
             "test", str(PATCHER), str(root or self.root)],
            text=True, capture_output=True,
            env={**os.environ, "PATH": str(self.applets)},
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    def test_all_fragments_canonical_names_and_permissions(self):
        for fragment in ("first", "second fragment", "third\nfragment"):
            prefix = f"{fragment}/lib/modules"
            load = self.write(
                f"{prefix}/modules.load.recovery",
                "# sched-walt.ko stays in comments\nother.ko\n"
                "/vendor/lib/modules/qcom_cpufreq_hw.ko.zst\n"
                "kernel/sched/walt/sched-walt.ko.gz # built in\n"
                "qcom-cpufreq-hw-debug.ko\nsched-walt-debug.ko\n",
                0o640,
            )
            self.write(f"{prefix}/modules.dep",
                       "qcom-cpufreq-hw.ko: clock.ko\n"
                       "kernel/sched_walt.ko.xz: qcom_cpufreq_hw.ko\n"
                       "consumer.ko: keep.ko /lib/modules/qcom_cpufreq_hw.ko sched-walt.ko.lz4 final.ko\n"
                       "only.ko: sched_walt.ko\n"
                       "qcom-cpufreq-hw-debug.ko: other.ko\n")
            self.write(f"{prefix}/modules.alias",
                       "alias of:qcom qcom_cpufreq_hw\nalias walt sched-walt\n"
                       "alias debug qcom-cpufreq-hw-debug\n")
            self.write(f"{prefix}/modules.options",
                       "options qcom_cpufreq_hw option=1\nsched_walt foo=bar\nother flag=1\n")
            self.write(f"{prefix}/modules.blocklist",
                       "blacklist qcom-cpufreq-hw\nblocklist sched_walt\nother\n")
            self.write(f"{prefix}/modules.builtin", "kernel/existing.ko\n")
            self.write(f"{prefix}/qcom-cpufreq-hw.ko", "original module bytes\n", 0o600)
            self.write(f"{prefix}/sched_walt.ko", "original WALT bytes\n")
            self.assertEqual(stat.S_IMODE(load.stat().st_mode), 0o640)
        self.patch()
        for fragment in ("first", "second fragment", "third\nfragment"):
            directory = self.root / fragment / "lib/modules"
            self.assertEqual((directory / "modules.load.recovery").read_text(),
                             "# sched-walt.ko stays in comments\nother.ko\n"
                             "qcom-cpufreq-hw-debug.ko\nsched-walt-debug.ko\n")
            self.assertEqual(stat.S_IMODE((directory / "modules.load.recovery").stat().st_mode), 0o640)
            self.assertEqual((directory / "modules.dep").read_text(),
                             "consumer.ko: keep.ko final.ko\nonly.ko:\nqcom-cpufreq-hw-debug.ko: other.ko\n")
            self.assertEqual((directory / "modules.alias").read_text(), "alias debug qcom-cpufreq-hw-debug\n")
            self.assertEqual((directory / "modules.options").read_text(), "other flag=1\n")
            self.assertEqual((directory / "modules.blocklist").read_text(), "other\n")
            self.assertEqual((directory / "qcom-cpufreq-hw.ko").read_text(), "original module bytes\n")
            self.assertEqual((directory / "sched_walt.ko").read_text(), "original WALT bytes\n")
        before = self.snapshot()
        self.patch()
        self.assertEqual(self.snapshot(), before)

    def test_softdeps_preserve_other_dependencies_and_remove_empty_sections(self):
        path = self.write("lib/modules/modules.softdep",
                          "softdep qcom_cpufreq_hw pre: clock\n"
                          "softdep sched-walt post: other\n"
                          "softdep consumer pre: clock sched_walt post: qcom-cpufreq-hw last # keep\n"
                          "softdep pre_empty pre: sched_walt post: final\n"
                          "softdep post_empty pre: first post: qcom_cpufreq_hw\n"
                          "softdep all_empty pre: sched_walt post: qcom-cpufreq-hw\n"
                          "softdep debug pre: qcom-cpufreq-hw-debug\n")
        self.patch()
        self.assertEqual(path.read_text(),
                         "softdep consumer pre: clock post: last # keep\n"
                         "softdep pre_empty post: final\n"
                         "softdep post_empty pre: first\n"
                         "softdep debug pre: qcom-cpufreq-hw-debug\n")

    def test_unrelated_metadata_and_scripts_unchanged(self):
        self.write("lib/modules/modules.custom", "other-module.ko\n")
        self.write("init.rc", "# insmod sched-walt.ko\ninsmod /lib/modules/other.ko\n")
        self.write("setup.sh", "# qcom-cpufreq-hw.ko\ninsmod /lib/modules/other.ko\n")
        (self.root / "harmless").symlink_to("absent-file")
        before = self.snapshot()
        self.patch()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root / "harmless").is_symlink())

    def assert_preflight_failure(self, relative, contents):
        load = self.write("first/lib/modules/modules.load", "qcom-cpufreq-hw.ko\nother.ko\n")
        self.write(relative, contents)
        before = self.snapshot()
        self.patch(success=False)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("qcom-cpufreq-hw.ko", load.read_text())

    def test_rejects_unknown_target_metadata(self):
        self.assert_preflight_failure("last/lib/modules/modules.unsupported", "kernel/sched_walt.ko\n")

    def test_rejects_binary_index_even_without_readable_names(self):
        self.assert_preflight_failure("last/lib/modules/modules.dep.bin", "\x00\x01\x02")

    def test_rejects_binary_data_in_text_index(self):
        self.assert_preflight_failure("last/lib/modules/modules.dep", "other.ko: \x00sched-walt.ko\n")

    def test_rejects_binary_data_in_unknown_index(self):
        self.assert_preflight_failure("last/lib/modules/modules.custom", "\x00sched-walt.ko\n")

    def test_rejects_binary_data_in_startup_script(self):
        self.assert_preflight_failure("last/setup.sh", "insmod sched-\x00walt.ko\n")

    def test_rejects_direct_init_load(self):
        self.assert_preflight_failure("last/init.vendor.rc", "on boot\n    insmod /vendor/lib/modules/qcom_cpufreq_hw.ko\n")

    def test_rejects_indirect_shell_load(self):
        self.assert_preflight_failure("last/setup.sh", 'module="/lib/modules/sched-walt.ko"\ninsmod "$module"\n')

    def test_hash_in_quoted_script_path_cannot_hide_load(self):
        self.assert_preflight_failure("last/setup.sh", 'insmod "/lib/#modules/qcom-cpufreq-hw.ko"\n')

    def test_rejects_malformed_relevant_metadata(self):
        self.assert_preflight_failure("last/lib/modules/modules.softdep", "softdep other sched_walt\n")

    def test_rejects_multiple_modules_on_one_load_line(self):
        self.assert_preflight_failure("last/lib/modules/modules.load", "sched-walt.ko other.ko\n")

    def test_rejects_metadata_symlink_without_touching_target(self):
        outside = self.base / "outside.modules"
        outside.write_text("sched-walt.ko\n")
        directory = self.root / "lib/modules"
        directory.mkdir(parents=True)
        (directory / "modules.load").symlink_to(outside)
        self.patch(success=False)
        self.assertEqual(outside.read_text(), "sched-walt.ko\n")

    def test_rejects_directory_symlink_without_touching_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "modules.load").write_text("qcom-cpufreq-hw.ko\n")
        (self.root / "linked-fragment").symlink_to(outside, target_is_directory=True)
        self.patch(success=False)
        self.assertEqual((outside / "modules.load").read_text(), "qcom-cpufreq-hw.ko\n")

    def test_rejects_dangling_module_search_path_symlinks(self):
        self.write("first/lib/modules/modules.load", "qcom-cpufreq-hw.ko\nother.ko\n")
        paths = ("lib/modules", "lib", "lib64", "usr", "system", "system_root", "vendor",
                 "odm", "system_dlkm", "vendor_dlkm", "odm_dlkm", "first_stage_ramdisk")
        for index, relative in enumerate(paths):
            with self.subTest(path=relative):
                link = self.root / f"fragment-{index}" / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                outside = self.base / f"absent-target-{index}"
                link.symlink_to(outside, target_is_directory=True)
                before = self.snapshot()
                self.patch(success=False)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(link.is_symlink())
                self.assertFalse(outside.exists())
                link.unlink()

    def test_rejects_dangling_link_inside_module_tree(self):
        directory = self.root / "lib/modules"
        directory.mkdir(parents=True)
        (directory / "subdirectory").symlink_to(self.base / "absent-module-subtree",
                                                target_is_directory=True)
        self.patch(success=False)

    def test_actual_android_partition_aliases_are_preserved(self):
        aliases = {
            "bin": "/system/bin", "etc": "/system/etc", "product": "/system/product",
            "system_ext": "/system/system_ext", "d": "/sys/kernel/debug",
            "bugreports": "/data/user_de/0/com.android.shell/files/bugreports",
            **{f"odm/{name}": f"/vendor/odm/{name}" for name in
               ("app", "bin", "etc", "firmware", "framework", "lib", "lib64", "overlay", "priv-app", "usr")},
            "odm_dlkm/etc": "/odm/odm_dlkm/etc", "vendor_dlkm/etc": "/vendor/vendor_dlkm/etc",
        }
        for relative, target in aliases.items():
            link = self.root / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
        self.write("init.rc", "on boot\n    setprop test.property 1\n")
        # This recovery layout has no module metadata, as in the device image.
        before = self.snapshot()
        self.patch()
        self.assertEqual(self.snapshot(), before)
        for relative, target in aliases.items():
            self.assertTrue((self.root / relative).is_symlink())
            self.assertEqual(os.readlink(self.root / relative), target)
        # The aliases do not prevent real, internal module metadata edits.
        load = self.write("lib/modules/modules.load", "qcom-cpufreq-hw\nother.ko\n")
        self.patch()
        self.assertEqual(load.read_text(), "other.ko\n")

    def test_android_alias_allowlist_requires_exact_path_and_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        external = outside / "modules.load"
        external.write_text("qcom-cpufreq-hw.ko\n")
        for relative, target in (("odm/lib", str(outside)),
                                 ("fragment/odm/lib", "/vendor/odm/lib"),
                                 ("lib/modules", "/vendor/odm/lib")):
            with self.subTest(relative=relative, target=target):
                link = self.root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                self.patch(success=False)
                self.assertEqual(external.read_text(), "qcom-cpufreq-hw.ko\n")
                link.unlink()

    def test_android_aliases_do_not_skip_script_preflight(self):
        (self.root / "odm").mkdir()
        (self.root / "odm/lib").symlink_to("/vendor/odm/lib")
        self.assert_preflight_failure("init.rc", "on boot\n    insmod /lib/modules/qcom-cpufreq-hw.ko\n")

    def test_rejects_root_symlink(self):
        link = self.base / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        self.patch(success=False, root=link)
        self.patch(success=False, root=str(link) + "/")

    def test_external_hardlink_is_not_modified(self):
        outside = self.base / "outside.modules"
        outside.write_text("sched-walt.ko\nother.ko\n")
        directory = self.root / "lib/modules"
        directory.mkdir(parents=True)
        os.link(outside, directory / "modules.load")
        self.patch()
        self.assertEqual(outside.read_text(), "sched-walt.ko\nother.ko\n")
        self.assertEqual((directory / "modules.load").read_text(), "other.ko\n")

    def test_read_only_metadata_mode_is_preserved(self):
        path = self.write("lib/modules/modules.load", "sched-walt.ko\nother.ko\n", 0o444)
        self.patch()
        self.assertEqual(path.read_text(), "other.ko\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)


if __name__ == "__main__":
    unittest.main()
