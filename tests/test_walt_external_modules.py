"""Exercise Android module-load reachability using BusyBox, without devices."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts/anykernel/walt-external-modules.sh"
BUSYBOX = os.environ.get("BUSYBOX") or shutil.which("busybox")


@unittest.skipUnless(BUSYBOX, "BusyBox is required for the Android ash runtime")
class WaltExternalModulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="walt-external-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.vendor = self.base / "vendor modules"
        self.system = self.base / "system modules"
        self.vdir = self.tree(self.vendor)
        self.sdir = self.tree(self.system / "android17-6.1")
        self.applets = self.base / "applets"
        self.applets.mkdir()
        for name in ("awk", "find", "grep", "mktemp", "rm"):
            (self.applets / name).symlink_to(Path(BUSYBOX).resolve())

    def tree(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "modules.load").write_text("safe.ko\n")
        (directory / "modules.dep").write_text("safe.ko:\n")
        return directory

    def write(self, name, contents, directory=None):
        ((directory or self.vdir) / name).write_text(contents)

    def check(self, success=True, vendor=None):
        result = subprocess.run(
            [str(BUSYBOX), "ash", "-c", '. "$1"; walt_check_external_modules "$2" "$3"',
             "test", str(HELPER), str(vendor or self.vendor), str(self.system)],
            env={**os.environ, "PATH": str(self.applets), "TMPDIR": str(self.base)},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_flat_vendor_and_nested_system_load_lists(self):
        self.check()

    def test_resolves_root_symlink_and_handles_spaces(self):
        alias = self.base / "vendor link"
        alias.symlink_to(self.vendor, target_is_directory=True)
        self.check(vendor=alias)

    def test_unreachable_target_dep_alias_and_softdep_inventory_is_allowed(self):
        self.write("modules.dep", "safe.ko:\nqcom-cpufreq-hw.ko: sched-walt.ko\nsched-walt.ko:\n")
        self.write("modules.alias", "alias of:N*T*Cqcom,cpufreq-hw qcom_cpufreq_hw\n")
        self.write("modules.softdep", "softdep unused pre: qcom_cpufreq_hw\n")
        self.check()

    def test_direct_targets_and_compressed_paths_are_rejected(self):
        for token in ("qcom-cpufreq-hw", "sched_walt", "/lib/modules/qcom_cpufreq_hw.ko.zst",
                      "kernel/sched-walt.ko.gz"):
            with self.subTest(token=token):
                self.write("modules.load", token + "\n")
                self.check(False)

    def test_debug_modules_and_comments_do_not_match_targets(self):
        self.write("modules.load", "# qcom-cpufreq-hw.ko\nqcom-cpufreq-hw-debug.ko\nsched_walt_debug\n")
        self.write("modules.dep", "qcom-cpufreq-hw-debug.ko:\nsched-walt-debug.ko:\n")
        self.check()

    def test_hard_dependency_chain_reaches_target(self):
        self.write("modules.dep", "safe.ko: middle.ko\nmiddle.ko: /lib/modules/sched-walt.ko\n")
        result = self.check(False)
        self.assertIn("sched_walt from middle", result.stderr)

    def test_pre_and_post_soft_dependencies_reach_target(self):
        for section in ("pre:", "post:"):
            with self.subTest(section=section):
                self.write("modules.softdep", f"softdep safe {section} qcom-cpufreq-hw\n")
                self.check(False)

    def test_softdep_prefix_can_touch_first_dependency_and_sections_can_be_empty(self):
        self.write("modules.softdep", "softdep oplus_bsp_uff_fp_driver pre:mtk_disp_notify\n"
                   "softdep safe pre: post:\n")
        self.check()
        self.write("modules.softdep", "softdep safe pre:\n")
        self.check()
        for section in ("pre:", "post:"):
            with self.subTest(section=section):
                self.write("modules.softdep", f"softdep safe {section}qcom_cpufreq_hw\n")
                self.check(False)

    def test_reachable_aliases_resolve_exact_wildcard_and_class(self):
        self.write("modules.load", "device7\n")
        for pattern in ("device7", "device*", "device?", "device[0-9]", "device[!0-6]"):
            with self.subTest(pattern=pattern):
                self.write("modules.alias", f"alias {pattern} qcom_cpufreq_hw\n")
                self.check(False)

    def test_alias_then_dependency_is_traversed(self):
        self.write("modules.load", "device\n")
        self.write("modules.alias", "alias device middle\n")
        self.write("modules.dep", "middle.ko: qcom-cpufreq-hw.ko\n")
        self.check(False)

    def test_hardware_alias_preserves_hyphens_in_load_and_softdep_requests(self):
        request = "of:Ncpu-frequencyTnullCqcom,cpufreq-hw"
        self.write("modules.alias", "alias of:N*T*Cqcom,cpufreq-hw qcom_cpufreq_hw\n")
        self.write("modules.load", request + "\n")
        self.check(False)
        self.write("modules.load", "safe.ko\n")
        self.write("modules.softdep", f"softdep safe pre:{request}\n")
        self.check(False)

    def test_alias_chain_preserves_hyphens_in_alias_targets(self):
        self.write("modules.load", "device-name\n")
        self.write("modules.alias", "alias device-name second-device\n"
                   "alias second-device qcom-cpufreq-hw\n")
        self.check(False)

    def test_dependency_and_alias_cycles_terminate(self):
        self.write("modules.dep", "safe.ko: middle.ko\nmiddle.ko: safe.ko\n")
        self.write("modules.alias", "alias safe middle\nalias middle safe\n")
        self.check()

    def test_blocklist_removes_explicit_load_root(self):
        self.write("modules.load", "qcom-cpufreq-hw.ko\nsafe.ko\n")
        self.write("modules.blocklist", "blocklist qcom_cpufreq_hw\n")
        self.check()

    def test_blocklist_cannot_hide_unsafe_dependency(self):
        self.write("modules.dep", "safe.ko: qcom-cpufreq-hw.ko\n")
        self.write("modules.blocklist", "blocklist qcom_cpufreq_hw\n")
        self.check(False)

    def test_every_nested_load_list_is_checked(self):
        second = self.tree(self.system / "second kernel")
        self.write("modules.load", "sched-walt.ko\n", second)
        self.check(False)

    def test_missing_load_list_or_dependency_file_fails(self):
        for name in ("modules.load", "modules.dep"):
            with self.subTest(name=name):
                path = self.sdir / name
                before = path.read_bytes()
                path.unlink()
                self.check(False)
                path.write_bytes(before)

    def test_binary_index_and_malformed_text_fail(self):
        index = self.vdir / "modules.dep.bin"
        index.write_bytes(b"\0binary\0")
        self.check(False)
        index.unlink()
        self.write("modules.dep", "safe.ko missing-colon\n")
        self.check(False)

    def test_metadata_is_never_modified(self):
        before = {p: p.read_bytes() for root in (self.vendor, self.system)
                  for p in root.rglob("*") if p.is_file()}
        self.check()
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
