"""Prepare a temporary installer; git revision lookup is mocked for offline CI."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
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
INSTALLER_SCRIPTS = ("anykernel.sh", "walt-ramdisk.sh", "walt-vendor-boot.sh",
                     "walt-external-modules.sh")
EXPECTED_AK3_REV = "020dfeccf9d7e962a48400fc94d3e451df92eead"


def arm64_tool_bytes(label=b"test tool"):
    """Synthetic ELF header for packaging checks only; never execute this data."""
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 3, 183, 1)
    struct.pack_into("<H", header, 52, 64)
    return bytes(header) + label


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
        self.tools = {name: self.base / f"arm64-{name}" for name in ("busybox", "magiskboot")}
        for name, path in self.tools.items():
            path.write_bytes(arm64_tool_bytes(name.encode()))
        self.tool_info = self.base / "tool-info.json"
        self.write_manifest()
        # Source fixtures let this unit suite validate all four copies without
        # executing or making assumptions about the installer implementations.
        self.source = self.base / "scripts/anykernel"
        self.source.mkdir(parents=True)
        for name in INSTALLER_SCRIPTS:
            (self.source / name).write_text(f"#!/system/bin/sh\n# fixture: {name}\n")
        patcher = mock.patch.object(PREPARE, "__file__", str(self.source.parent / "prepare.py"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_manifest(self):
        self.manifest = {
            name: {"source": f"fixture://{name}", "version": "test-v1",
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in self.tools.items()
        }
        self.tool_info.write_text(json.dumps(self.manifest))

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
            PREPARE.prepare(self.destination, self.image, self.tools["busybox"],
                            self.tools["magiskboot"], self.tool_info)
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
            self.assertEqual(output.read_text(), (self.source / name).read_text())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
        self.assertIn(PREPARE.AK3_REV, (self.destination / "version").read_text())
        self.assertIn("vendor_boot", (self.destination / "version").read_text())
        self.assertEqual({path.name for path in (self.destination / "tools").iterdir()},
                         {"ak3-core.sh", "busybox", "magiskboot", "toolchain.json"})
        for name, path in self.tools.items():
            output = self.destination / "tools" / name
            self.assertEqual(output.read_bytes(), path.read_bytes())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
        self.assertEqual(json.loads((self.destination / "tools/toolchain.json").read_text()), self.manifest)

    def test_removes_original_elf32_tools_and_unused_extras(self):
        elf32 = bytearray(arm64_tool_bytes())
        elf32[4] = 1
        struct.pack_into("<H", elf32, 18, 40)
        for name in ("busybox", "magiskboot", "dumpimage", "futility", "unknown-tool"):
            (self.destination / "tools" / name).write_bytes(elf32)
        (self.destination / "tools/unused.txt").write_text("template extra\n")
        self.prepare()
        self.assertEqual({path.name for path in (self.destination / "tools").iterdir()},
                         {"ak3-core.sh", "busybox", "magiskboot", "toolchain.json"})
        self.assertEqual((self.destination / "tools/busybox").read_bytes(), self.tools["busybox"].read_bytes())

    def test_rejects_non_arm64_tool_headers_before_mutating_template(self):
        mutations = {
            "ELF32": (4, b"\x01"), "big endian": (5, b"\x02"),
            "bad ident version": (6, b"\x00"), "x86_64": (18, struct.pack("<H", 62)),
            "ARM32": (18, struct.pack("<H", 40)), "relocatable": (16, struct.pack("<H", 1)),
            "bad ELF version": (20, struct.pack("<I", 0)),
            "wrong header size": (52, struct.pack("<H", 52)),
        }
        for name, path in self.tools.items():
            original = path.read_bytes()
            for description, (offset, replacement) in mutations.items():
                with self.subTest(tool=name, problem=description):
                    invalid = bytearray(original)
                    invalid[offset:offset + len(replacement)] = replacement
                    path.write_bytes(invalid)
                    self.write_manifest()
                    self.assert_rejected_without_mutation()
            path.write_bytes(original)
            self.write_manifest()

    def test_rejects_missing_empty_truncated_and_non_elf_tools(self):
        for name, path in self.tools.items():
            original = path.read_bytes()
            for bad in (b"", b"\x7fELF", b"#!/bin/sh\nexit 0\n"):
                with self.subTest(tool=name, data=bad):
                    path.write_bytes(bad)
                    self.write_manifest()
                    self.assert_rejected_without_mutation()
            path.write_bytes(original)
            self.write_manifest()
            path.unlink()
            with self.subTest(tool=name, missing=True):
                self.assert_rejected_without_mutation()
            path.write_bytes(original)

    def test_rejects_tool_digest_mismatch(self):
        self.manifest["busybox"]["sha256"] = "0" * 64
        self.tool_info.write_text(json.dumps(self.manifest))
        self.assert_rejected_without_mutation()

    def test_rejects_missing_manifest_provenance(self):
        del self.manifest["magiskboot"]["source"]
        self.tool_info.write_text(json.dumps(self.manifest))
        self.assert_rejected_without_mutation()

    def test_rejects_unknown_manifest_fields(self):
        self.manifest["unexpected"] = {}
        self.tool_info.write_text(json.dumps(self.manifest))
        self.assert_rejected_without_mutation()

    def test_rejects_invalid_manifest_values(self):
        for invalid in ([], {"source": "", "version": "v1", "sha256": "0" * 64},
                        {"source": "fixture://busybox", "version": 1, "sha256": "0" * 64},
                        {"source": "fixture://busybox", "version": "v1", "sha256": "invalid"}):
            with self.subTest(entry=invalid):
                self.write_manifest()
                self.manifest["busybox"] = invalid
                self.tool_info.write_text(json.dumps(self.manifest))
                self.assert_rejected_without_mutation()

    def test_rejects_missing_manifest(self):
        self.tool_info.unlink()
        self.assert_rejected_without_mutation()

    def test_rejects_extra_tool_directory(self):
        (self.destination / "tools/extra").mkdir()
        self.assert_rejected_without_mutation()

    def test_rejects_extra_tool_symlink_without_touching_target(self):
        outside = self.base / "outside-extra"
        outside.write_bytes(b"preserve external tool")
        (self.destination / "tools/extra").symlink_to(outside)
        self.assert_rejected_without_mutation()
        self.assertEqual(outside.read_bytes(), b"preserve external tool")

    def test_rejects_dangling_tool_symlink(self):
        (self.destination / "tools/busybox").symlink_to(self.base / "missing-tool")
        self.assert_rejected_without_mutation()

    def test_cli_passes_explicit_tool_inputs(self):
        arguments = [str(self.destination), str(self.image), "--busybox", str(self.tools["busybox"]),
                     "--magiskboot", str(self.tools["magiskboot"]), "--tool-info", str(self.tool_info)]
        with mock.patch.object(PREPARE, "prepare") as prepare:
            PREPARE.main(arguments)
        prepare.assert_called_once_with(str(self.destination), str(self.image),
                                        str(self.tools["busybox"]), str(self.tools["magiskboot"]),
                                        str(self.tool_info))

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

    def test_rejects_script_or_version_output_directory(self):
        for name in (*INSTALLER_SCRIPTS, "version"):
            with self.subTest(name=name):
                path = self.destination / name
                previous = path.read_bytes() if path.exists() else None
                if path.exists():
                    path.unlink()
                path.mkdir()
                self.assert_rejected_without_mutation()
                path.rmdir()
                if previous is not None:
                    path.write_bytes(previous)


if __name__ == "__main__":
    unittest.main()
