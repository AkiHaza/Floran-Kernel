"""Prepare a temporary installer; git revision lookup is mocked for offline CI."""

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_yaap_anykernel.py"
SPEC = importlib.util.spec_from_file_location("prepare_yaap_anykernel_under_test", SCRIPT)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)

BOUNDARY = "  blockdev --setrw $BLOCK 2>/dev/null;"
STAGE_GUARD = '  [ "$AK3_STAGE_ONLY" = 1 ] && return 0;'
CORE = "#!/sbin/sh\nunpack_vendorrd() {\n  :\n}\nflash_boot() {\n" + BOUNDARY + "\n}\n"
INSTALLER_SCRIPTS = ("anykernel.sh", "walt-ramdisk.sh", "walt-vendor-boot.sh")
EXPECTED_AK3_REV = "020dfeccf9d7e962a48400fc94d3e451df92eead"


class PrepareAnyKernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prepare-yaap-anykernel-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.destination = self.base / "AnyKernel fixture"
        (self.destination / "tools").mkdir(parents=True)
        self.core = self.destination / "tools/ak3-core.sh"
        self.core.write_text(CORE)
        for name in ("ramdisk", "patch", "modules"):
            directory = self.destination / name
            directory.mkdir()
            (directory / "template.txt").write_text("upstream placeholder\n")
        (self.destination / "anykernel.sh").write_text("upstream installer\n")
        self.image = self.base / "input Image"
        self.image.write_bytes(b"ARM64-IMAGE\x00\xff\n")

    def snapshot(self):
        snapshot = {}
        for path in self.destination.rglob("*"):
            relative = str(path.relative_to(self.destination))
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            elif path.is_dir():
                snapshot[relative] = ("directory",)
        return snapshot

    def prepare(self, revision=None):
        revision = EXPECTED_AK3_REV if revision is None else revision
        with mock.patch.object(PREPARE.subprocess, "check_output", return_value=revision + "\n") as git:
            PREPARE.prepare(self.destination, self.image)
        git.assert_called_once_with(
            ["git", "-C", str(self.destination.resolve()), "rev-parse", "HEAD"], text=True,
        )

    def assert_rejected_without_mutation(self, revision=None):
        before = self.snapshot()
        with self.assertRaises((ValueError, OSError)):
            self.prepare(revision=revision)
        self.assertEqual(self.snapshot(), before)

    def test_pinned_fixture_gets_one_stage_guard_and_boot_only_image(self):
        self.assertEqual(PREPARE.AK3_REV, EXPECTED_AK3_REV)
        self.prepare()
        patched = self.core.read_text()
        self.assertEqual(patched.count(BOUNDARY), 1)
        self.assertEqual(patched.count(STAGE_GUARD), 1)
        self.assertIn(STAGE_GUARD + "\n" + BOUNDARY, patched)
        self.assertEqual(patched.replace(STAGE_GUARD + "\n", ""), CORE)
        self.assertEqual((self.destination / "boot-files/Image").read_bytes(), self.image.read_bytes())
        self.assertEqual(
            [str(path.relative_to(self.destination)) for path in self.destination.rglob("Image")],
            ["boot-files/Image"],
        )
        for name in ("ramdisk", "patch", "modules"):
            self.assertFalse((self.destination / name).exists())
        for name in INSTALLER_SCRIPTS:
            output = self.destination / name
            self.assertEqual(output.read_text(), (SCRIPT.parent / "anykernel" / name).read_text())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
        self.assertIn(PREPARE.AK3_REV, (self.destination / "version").read_text())
        self.assertIn("vendor_boot", (self.destination / "version").read_text())

    def test_rejects_wrong_revision(self):
        self.assert_rejected_without_mutation(revision="0" * 40)

    def test_rejects_empty_image(self):
        self.image.write_bytes(b"")
        self.assert_rejected_without_mutation()

    def test_rejects_missing_image(self):
        self.image.unlink()
        self.assert_rejected_without_mutation()

    def test_rejects_missing_boundary(self):
        self.core.write_text(CORE.replace(BOUNDARY, "  :"))
        self.assert_rejected_without_mutation()

    def test_rejects_multiple_boundaries(self):
        self.core.write_text(CORE + BOUNDARY + "\n")
        self.assert_rejected_without_mutation()

    def test_rejects_missing_vendor_ramdisk_support(self):
        self.core.write_text(CORE.replace("unpack_vendorrd()", "other_function()"))
        self.assert_rejected_without_mutation()

    def test_rejects_missing_core(self):
        self.core.unlink()
        self.assert_rejected_without_mutation()

    def test_rejects_missing_replacement_scripts_before_mutating_template(self):
        with mock.patch.object(PREPARE, "__file__", str(self.base / "missing/prepare.py")):
            self.assert_rejected_without_mutation()

    def test_rejects_already_staged_core(self):
        self.core.write_text(CORE.replace(BOUNDARY, STAGE_GUARD + "\n" + BOUNDARY))
        self.assert_rejected_without_mutation()

    def test_rejects_top_level_image(self):
        (self.destination / "Image").write_bytes(b"unexpected template image")
        self.assert_rejected_without_mutation()

    def test_rejects_dangling_top_level_image_symlink(self):
        (self.destination / "Image").symlink_to(self.base / "absent-image")
        self.assert_rejected_without_mutation()

    def test_rejects_template_directory_symlink(self):
        directory = self.destination / "ramdisk"
        (directory / "template.txt").unlink()
        directory.rmdir()
        outside = self.base / "outside-template"
        outside.mkdir()
        evidence = outside / "preserve.txt"
        evidence.write_bytes(b"preserve outside template")
        directory.symlink_to(outside, target_is_directory=True)
        self.assert_rejected_without_mutation()
        self.assertEqual(evidence.read_bytes(), b"preserve outside template")

    def test_rejects_template_regular_file(self):
        directory = self.destination / "modules"
        (directory / "template.txt").unlink()
        directory.rmdir()
        directory.write_text("unexpected file in place of template directory\n")
        self.assert_rejected_without_mutation()

    def test_rejects_core_symlink_without_writing_outside(self):
        self.core.unlink()
        outside = self.base / "outside-core.sh"
        outside.write_text(CORE)
        self.core.symlink_to(outside)
        self.assert_rejected_without_mutation()
        self.assertEqual(outside.read_text(), CORE)

    def test_rejects_tools_directory_symlink(self):
        self.core.unlink()
        (self.destination / "tools").rmdir()
        outside = self.base / "outside-tools"
        outside.mkdir()
        (outside / "ak3-core.sh").write_text(CORE)
        (self.destination / "tools").symlink_to(outside, target_is_directory=True)
        self.assert_rejected_without_mutation()
        self.assertEqual((outside / "ak3-core.sh").read_text(), CORE)

    def test_rejects_existing_boot_files_directory(self):
        (self.destination / "boot-files").mkdir()
        self.assert_rejected_without_mutation()

    def test_rejects_boot_files_directory_symlink(self):
        outside = self.base / "outside-boot-files"
        outside.mkdir()
        (self.destination / "boot-files").symlink_to(outside, target_is_directory=True)
        self.assert_rejected_without_mutation()
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_boot_image_output_symlink(self):
        (self.destination / "boot-files").mkdir()
        outside = self.base / "outside-image"
        outside.write_bytes(b"keep existing image")
        (self.destination / "boot-files/Image").symlink_to(outside)
        self.assert_rejected_without_mutation()
        self.assertEqual(outside.read_bytes(), b"keep existing image")

    def test_rejects_installer_output_symlink(self):
        (self.destination / "anykernel.sh").unlink()
        outside = self.base / "outside-installer.sh"
        outside.write_text("keep existing script\n")
        (self.destination / "anykernel.sh").symlink_to(outside)
        self.assert_rejected_without_mutation()
        self.assertEqual(outside.read_text(), "keep existing script\n")

    def test_rejects_version_output_symlink(self):
        outside = self.base / "outside-version"
        outside.write_text("keep existing version\n")
        (self.destination / "version").symlink_to(outside)
        self.assert_rejected_without_mutation()
        self.assertEqual(outside.read_text(), "keep existing version\n")


if __name__ == "__main__":
    unittest.main()
