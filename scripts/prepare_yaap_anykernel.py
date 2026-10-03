#!/usr/bin/env python3
"""Prepare the pinned AnyKernel installer for the YAAP built-in WALT image."""
from pathlib import Path
import shutil
import subprocess
import sys

AK3_REV = "020dfeccf9d7e962a48400fc94d3e451df92eead"


def prepare(destination, image):
    destination = Path(destination).resolve()
    image = Path(image).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(destination), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != AK3_REV:
        raise ValueError("Unexpected AnyKernel revision")
    if not image.is_file() or not image.stat().st_size:
        raise ValueError("Missing kernel Image")

    # Validate the fresh clone before writing or removing any template content.
    for name in ("tools", "tools/ak3-core.sh", "anykernel.sh",
                 "walt-ramdisk.sh", "walt-vendor-boot.sh", "version",
                 "ramdisk", "patch", "modules", "boot-files"):
        if (destination / name).is_symlink():
            raise ValueError(f"Unexpected template symlink: {destination / name}")
    for name in ("ramdisk", "patch", "modules"):
        path = destination / name
        if path.exists() and not path.is_dir():
            raise ValueError(f"Unexpected template file: {path}")
    for name in ("Image", "boot-files"):
        path = destination / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"Unexpected existing kernel path: {path}")

    core = destination / "tools/ak3-core.sh"
    text = core.read_text()
    boundary = "  blockdev --setrw $BLOCK 2>/dev/null;"
    if text.count(boundary) != 1 or "unpack_vendorrd()" not in text:
        raise ValueError("AnyKernel staging boundary or vendor v4 support changed")
    if "AK3_STAGE_ONLY" in text:
        raise ValueError("AnyKernel core is already staged")
    source = Path(__file__).parent / "anykernel"
    scripts = {name: (source / name).read_text() for name in
               ("anykernel.sh", "walt-ramdisk.sh", "walt-vendor-boot.sh")}
    text = text.replace(boundary,
                        '  [ "$AK3_STAGE_ONLY" = 1 ] && return 0;\n' + boundary)
    core.write_text(text, newline="\n")

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
    shutil.copyfile(image, destination / "boot-files/Image")
    (destination / "version").write_text(
        "YAAP-17: built-in WALT + Qualcomm CPUFreq HW\n"
        "Updates boot and module metadata in vendor_boot/shared-kernel recovery.\n"
        "Original images are saved under /sdcard/YAAP-WALT-backups.\n"
        f"AnyKernel revision: {AK3_REV}\n"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_yaap_anykernel.py ANYKERNEL_DIR IMAGE")
    prepare(*sys.argv[1:])
