"""Synthetic v4 fixtures with optional real unpack/repack; no device writes."""

import hashlib
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/anykernel/walt-vendor-boot.sh"
PAGE = 4096


def align(value):
    return (value + PAGE - 1) // PAGE * PAGE


def make_image(entries=None, dtb=b"test-device-tree", bootconfig=b"androidboot.test=1\n"):
    if entries is None:
        entries = [(b"platform", 1, b"ramdisk-data", bytes(64))]
    header = bytearray(2128)
    header[:8] = b"VNDRBOOT"
    payload = bytearray()
    table = bytearray()
    for name, kind, data, board in entries:
        if len(name) > 32 or len(board) != 64:
            raise ValueError("invalid fixture metadata")
        table += struct.pack("<III32s64s", len(data), len(payload), kind, name, board)
        payload += data
    struct.pack_into("<5I", header, 8, 4, PAGE, 0x10008000, 0x11000000, len(payload))
    header[28:28 + 12] = b"console=test"
    struct.pack_into("<I", header, 2076, 0x10000100)
    header[2080:2086] = b"waffle"
    struct.pack_into("<IIQIIII", header, 2096, 2128, len(dtb), 0x123456789ABCDEF0,
                     len(table), len(entries), 108, len(bootconfig))
    image = bytearray()
    for part in (header, payload, dtb, table, bootconfig):
        image += part
        image += bytes(align(len(image)) - len(image))
    return image


def table_offset(image):
    ramdisk, = struct.unpack_from("<I", image, 24)
    dtb, = struct.unpack_from("<I", image, 2100)
    return align(2128) + align(ramdisk) + align(dtb)


def make_cpio(files):
    """A real newc archive for the optional magiskboot integration test."""
    archive = bytearray()
    items = [("lib", 0o40755, b""), ("lib/modules", 0o40755, b"")]
    items += [(name, 0o100644, content) for name, content in files.items()]
    items.append(("TRAILER!!!", 0, b""))
    for inode, (name, mode, data) in enumerate(items, 1):
        name = name.encode() + b"\0"
        fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)
        archive += b"070701" + "".join(f"{value:08x}" for value in fields).encode()
        archive += name
        archive += bytes(-len(archive) % 4)
        archive += data
        archive += bytes(-len(archive) % 4)
    archive += bytes(-len(archive) % 512)
    return archive


def make_dtb():
    """An FDT v17 with an empty root node and a terminated reserve map."""
    structure = struct.pack(">I", 1) + bytes(4) + struct.pack(">II", 2, 9)
    return struct.pack(">10I", 0xD00DFEED, 72, 56, 72, 40, 17, 16, 0, 0, 16) + bytes(16) + structure


def shells():
    custom = os.environ.get("WALT_TEST_SHELL")
    if custom:
        return [shlex.split(custom)]
    result = []
    if shutil.which("bash"):
        result.append(["bash"])
    busybox = os.environ.get("BUSYBOX") or shutil.which("busybox")
    if busybox:
        result.append([busybox, "ash"])
    elif shutil.which("ash"):
        result.append(["ash"])
    return result


class VendorBootLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shells = shells()
        if not cls.shells:
            raise unittest.SkipTest("bash or BusyBox ash is required")

    def run_layout(self, image, valid=True):
        outputs = []
        with tempfile.TemporaryDirectory(prefix="walt-layout-") as tmp:
            path = Path(tmp) / "vendor boot.img"
            path.write_bytes(image)
            for shell in self.shells:
                with self.subTest(shell=shell):
                    result = subprocess.run(
                        shell + ["-c", '. "$1"; walt_vendor_layout "$2"', "test", str(SCRIPT), str(path)],
                        capture_output=True, text=True, timeout=20,
                    )
                    if valid:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertFalse(result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "", "invalid image leaked a partial manifest")
                        self.assertIn("WALT vendor_boot:", result.stderr)
                    outputs.append(result.stdout)
            self.assertEqual(path.read_bytes(), image, "validator changed its input")
        self.assertTrue(all(output == outputs[0] for output in outputs))
        return outputs[0]

    def test_single_fragment_and_hashes(self):
        layout = self.run_layout(make_image())
        self.assertIn("entry_count 1\n", layout)
        self.assertIn("cpio platform.cpio\n", layout)
        self.assertIn("dtb_sha256 " + hashlib.sha256(b"test-device-tree").hexdigest(), layout)
        self.assertIn("bootconfig_sha256 " + hashlib.sha256(b"androidboot.test=1\n").hexdigest(), layout)

    def test_multiple_fragments_empty_name_and_board_ids(self):
        image = make_image([(b"", 1, b"a", bytes(64)),
                            (b"dlkm", 3, b"bb", struct.pack("<16I", *range(16)))])
        layout = self.run_layout(image)
        self.assertIn("entry_count 2\n", layout)
        self.assertIn("entry 0 ramdisk 1 ", layout)
        self.assertIn("cpio ramdisk.cpio\n", layout)
        self.assertIn("entry 1 dlkm 3 ", layout)
        self.assertIn("cpio dlkm.cpio\n", layout)
        self.assertIn(struct.pack("<16I", *range(16)).hex(), layout)

    def test_duplicate_and_empty_name_collision(self):
        for names in ((b"x", b"x"), (b"", b""), (b"", b"ramdisk")):
            with self.subTest(names=names):
                self.run_layout(make_image([(name, 1, b"x", bytes(64)) for name in names]), False)

    def test_unsafe_and_unterminated_names(self):
        for name in (b"../escape", b"/absolute", b"a/b", b"..", b".hidden", b"-flag",
                     b"a\\b", b"a b", b"a\nb", b"a;cmd", b"a\x00hidden", b"x" * 32):
            with self.subTest(name=name):
                self.run_layout(make_image([(name, 1, b"x", bytes(64))]), False)

    def test_invalid_headers_and_section_bounds(self):
        self.run_layout(bytearray(100), False)
        for offset, value in ((8, 3), (12, 0), (12, 4095), (12, 131072),
                              (2096, 2112), (2112, 107), (2116, 0), (2116, 0xFFFFFFFF),
                              (2120, 112), (24, 0xFFFFFFFF), (2100, 0xFFFFFFFF), (2124, 0xFFFFFFFF)):
            with self.subTest(offset=offset, value=value):
                image = make_image()
                struct.pack_into("<I", image, offset, value)
                self.run_layout(image, False)
        image = make_image()
        image[0] = 0
        self.run_layout(image, False)
        self.run_layout(make_image()[:-1], False)

    def test_bad_entry_ranges_types_and_overlap(self):
        for relative, value in ((0, 0), (0, 0xFFFFFFFF), (4, 0xFFFFFFFF), (8, 4)):
            with self.subTest(relative=relative):
                image = make_image()
                struct.pack_into("<I", image, table_offset(image) + relative, value)
                self.run_layout(image, False)
        image = make_image([(b"a", 1, b"aa", bytes(64)), (b"b", 2, b"bb", bytes(64))])
        struct.pack_into("<I", image, table_offset(image) + 108 + 4, 1)
        self.run_layout(image, False)

    def test_repack_size_and_offsets_are_ignored(self):
        original = make_image([(b"a", 1, b"a", bytes(64)), (b"b", 3, b"b", bytes(64))])
        repacked = make_image([(b"a", 1, b"a" * 9000, bytes(64)), (b"b", 3, b"bb", bytes(64))])
        self.assertEqual(self.run_layout(original), self.run_layout(repacked))

    def test_type_order_name_and_board_id_changes_are_detected(self):
        entries = [(b"a", 1, b"aa", bytes(64)), (b"b", 3, b"b", bytes(64))]
        baseline = self.run_layout(make_image(entries))
        variants = [list(reversed(entries)), [(b"a", 2, b"aa", bytes(64)), entries[1]],
                    [(b"c", 1, b"aa", bytes(64)), entries[1]],
                    [(b"a", 1, b"aa", b"\x01" + bytes(63)), entries[1]]]
        for changed in variants:
            self.assertNotEqual(baseline, self.run_layout(make_image(changed)))
        self.assertNotEqual(self.run_layout(make_image([(b"", 1, b"x", bytes(64))])),
                            self.run_layout(make_image([(b"ramdisk", 1, b"x", bytes(64))])))

    def test_header_dtb_and_bootconfig_changes_are_detected(self):
        baseline = self.run_layout(make_image())
        for offset in (16, 20, 28, 2076, 2080, 2104):
            image = make_image()
            image[offset] ^= 1
            self.assertNotEqual(baseline, self.run_layout(image))
        self.assertNotEqual(baseline, self.run_layout(make_image(dtb=b"changed-device-tree")))
        self.assertNotEqual(baseline, self.run_layout(make_image(bootconfig=b"androidboot.test=2\n")))

    def test_empty_optional_sections(self):
        layout = self.run_layout(make_image(dtb=b"", bootconfig=b""))
        empty_hash = hashlib.sha256(b"").hexdigest()
        self.assertIn("dtb_sha256 " + empty_hash, layout)
        self.assertIn("bootconfig_sha256 " + empty_hash, layout)

    def test_magiskboot_v31_unpack_modify_repack(self):
        # Supply the official v31.0 APK x86_64 libmagiskboot.so path to opt in.
        # The test never downloads or installs a tool, and only writes temp files.
        tool = os.environ.get("WALT_TEST_MAGISKBOOT")
        if not tool:
            self.skipTest("set WALT_TEST_MAGISKBOOT to the pinned v31.0 executable")
        with tempfile.TemporaryDirectory(prefix="walt-magiskboot-") as tmp:
            root = Path(tmp)

            def magisk(*args, cwd=root, code=0):
                result = subprocess.run([tool, *map(str, args)], cwd=cwd,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return result

            archives = [make_cpio({"platform": b"platform content\n"}),
                        make_cpio({"recovery": b"recovery content\n"}),
                        make_cpio({"lib/modules/modules.load": b"qcom-cpufreq-hw.ko\nother.ko\n"})]
            entries = []
            for index, (name, kind) in enumerate(((b"", 1), (b"recovery", 2), (b"dlkm", 3))):
                source = root / f"fragment{index}.cpio"
                compressed = root / f"fragment{index}.lz4"
                source.write_bytes(archives[index])
                magisk("compress=lz4_legacy", source, compressed)
                entries.append((name, kind, compressed.read_bytes(), struct.pack("<16I", *range(index, index+16))))
            image = make_image(entries, dtb=make_dtb())
            baseline = self.run_layout(image)
            original = root / "original.img"
            original.write_bytes(image)
            unpacked = root / "unpacked"
            unpacked.mkdir()
            magisk("unpack", "-h", original, cwd=unpacked, code=3)
            expected = sorted(line.split()[1] for line in baseline.splitlines() if line.startswith("cpio "))
            actual = sorted(path.name for path in (unpacked / "vendor_ramdisk").iterdir())
            self.assertEqual(expected, actual)
            for name, archive in zip(("ramdisk", "recovery", "dlkm"), archives):
                self.assertEqual((unpacked / "vendor_ramdisk" / f"{name}.cpio").read_bytes(), archive)
            # Grow one ramdisk across several pages with valid, poorly compressible data.
            data = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(400))
            modified = make_cpio({"lib/modules/modules.load": b"other.ko\n", "payload": data})
            (unpacked / "vendor_ramdisk/dlkm.cpio").write_bytes(modified)
            repacked = root / "repacked.img"
            magisk("repack", original, repacked, cwd=unpacked)
            new_image = repacked.read_bytes()
            self.assertEqual(baseline, self.run_layout(new_image))
            self.assertNotEqual(image[24:28], new_image[24:28])
            self.assertEqual(len(new_image) % PAGE, 0)
            second = root / "reunpacked"
            second.mkdir()
            magisk("unpack", repacked, cwd=second, code=3)
            self.assertEqual((second / "vendor_ramdisk/dlkm.cpio").read_bytes(), modified)
            self.assertEqual((second / "dtb").read_bytes(), make_dtb())
            self.assertEqual((second / "bootconfig").read_bytes(), b"androidboot.test=1\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
