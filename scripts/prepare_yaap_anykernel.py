#!/usr/bin/env python3
"""Prepare the pinned AnyKernel installer for the YAAP built-in WALT image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess

AK3_REV = "020dfeccf9d7e962a48400fc94d3e451df92eead"
INSTALLER_SCRIPTS = ("anykernel.sh", "walt-ramdisk.sh", "walt-vendor-boot.sh",
                     "walt-external-modules.sh")
TOOL_NAMES = ("busybox", "magiskboot")


def read_arm64_tool(path, name, expected_sha256):
    """Check target architecture without executing a phone binary on the host."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Missing {name} executable: {path}")
    data = path.read_bytes()
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError(f"{name} must be ELF64 little-endian, ELF version 1")
    elf_type, machine, elf_version = struct.unpack_from("<HHI", data, 16)
    header_size, = struct.unpack_from("<H", data, 52)
    if machine != 183 or elf_type not in (2, 3) or elf_version != 1 or header_size != 64:
        raise ValueError(f"{name} must be an AArch64 executable (ELF e_machine 183)")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(f"{name} SHA256 does not match the tool manifest")
    return data


def read_tool_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != set(TOOL_NAMES):
        raise ValueError("Tool manifest must describe exactly busybox and magiskboot")
    for name in TOOL_NAMES:
        info = manifest[name]
        if not isinstance(info, dict) or set(info) != {"source", "version", "sha256"}:
            raise ValueError(f"Invalid manifest fields for {name}")
        if any(not isinstance(info[key], str) or not info[key].strip()
               for key in ("source", "version", "sha256")):
            raise ValueError(f"Missing tool provenance for {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", info["sha256"]):
            raise ValueError(f"Invalid SHA256 for {name}")
    return manifest


def prepare(destination, image, busybox, magiskboot, tool_info):
    if Path(destination).is_symlink():
        raise ValueError("AnyKernel destination must not be a symlink")
    destination = Path(destination).resolve()
    image = Path(image).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(destination), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != AK3_REV:
        raise ValueError("Unexpected AnyKernel revision")
    if not image.is_file() or not image.stat().st_size:
        raise ValueError("Missing kernel Image")
    image_data = image.read_bytes()

    # Validate the fresh clone before writing or removing any template content.
    for name in ("tools", "tools/ak3-core.sh", *INSTALLER_SCRIPTS, "version",
                 "ramdisk", "patch", "modules", "boot-files"):
        if (destination / name).is_symlink():
            raise ValueError(f"Unexpected template symlink: {destination / name}")
    for name in ("ramdisk", "patch", "modules"):
        path = destination / name
        if path.exists() and not path.is_dir():
            raise ValueError(f"Unexpected template file: {path}")
    for name in (*INSTALLER_SCRIPTS, "version"):
        path = destination / name
        if path.exists() and not path.is_file():
            raise ValueError(f"Unexpected template output: {path}")
    for name in ("Image", "boot-files"):
        path = destination / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"Unexpected existing kernel path: {path}")

    # The pinned core is useful, but its bundled ELF32 ARM executables cannot run
    # on SM8650. Retain only the core and the two explicitly supplied ARM64 tools.
    tool_paths = list((destination / "tools").iterdir())
    for path in tool_paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Unexpected non-regular template tool: {path}")
    manifest = read_tool_manifest(tool_info)
    tool_data = {
        name: read_arm64_tool(path, name, manifest[name]["sha256"])
        for name, path in (("busybox", busybox), ("magiskboot", magiskboot))
    }

    core = destination / "tools/ak3-core.sh"
    text = core.read_text()
    boundary = "  blockdev --setrw $BLOCK 2>/dev/null;"
    if text.count(boundary) != 1 or "unpack_vendorrd()" not in text:
        raise ValueError("AnyKernel staging boundary or vendor v4 support changed")
    if "AK3_STAGE_ONLY" in text:
        raise ValueError("AnyKernel core is already staged")
    source = Path(__file__).parent / "anykernel"
    scripts = {name: (source / name).read_text() for name in INSTALLER_SCRIPTS}
    text = text.replace(boundary,
                        '  [ "$AK3_STAGE_ONLY" = 1 ] && return 0;\n' + boundary)
    core.write_text(text, newline="\n")

    for path in tool_paths:
        if path != core:
            path.unlink()
    for name, data in tool_data.items():
        output = destination / "tools" / name
        output.write_bytes(data)
        output.chmod(0o755)
    (destination / "tools/toolchain.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n",
    )

    # Remove only the known template directories in this fresh, pinned clone.
    for name in ("ramdisk", "patch", "modules"):
        path = destination / name
        if path.exists():
            shutil.rmtree(path)
    for name, contents in scripts.items():
        output = destination / name
        output.write_text(contents, newline="\n")
        output.chmod(0o755)
    (destination / "boot-files").mkdir(exist_ok=True)
    (destination / "boot-files/Image").write_bytes(image_data)
    (destination / "version").write_text(
        "YAAP-17: built-in WALT + Qualcomm CPUFreq HW\n"
        "Updates boot and module metadata in vendor_boot/shared-kernel recovery.\n"
        "Original images are saved under /sdcard/YAAP-WALT-backups.\n"
        f"AnyKernel revision: {AK3_REV}\n"
        f"ARM64 BusyBox: {manifest['busybox']['version']}\n"
        f"ARM64 magiskboot: {manifest['magiskboot']['version']}\n"
        "Tool sources and SHA256: tools/toolchain.json\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="fresh checkout of the pinned AnyKernel revision")
    parser.add_argument("image", help="compiled ARM64 kernel Image")
    parser.add_argument("--busybox", required=True, help="ARM64 BusyBox executable")
    parser.add_argument("--magiskboot", required=True, help="ARM64 magiskboot executable")
    parser.add_argument("--tool-info", required=True, help="JSON source, version and SHA256 for both tools")
    args = parser.parse_args(argv)
    prepare(args.destination, args.image, args.busybox, args.magiskboot, args.tool_info)


if __name__ == "__main__":
    main()
