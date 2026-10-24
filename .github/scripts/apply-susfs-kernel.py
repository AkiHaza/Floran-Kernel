#!/usr/bin/env python3
"""Apply upstream SUSFS in a staging tree; accept only known ROM differences.

Run in the kernel tree with the upstream 50_*.patch as argv[1]. Requires GNU
patch and Python's standard library. Existing .rej files are never touched.
Unknown rejects are printed and saved under unique *.susfs-*.rej names.
"""

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple

    def additions(self):
        blocks, block = [], []
        for line in self.lines:
            if line.startswith("+"):
                block.append(line[1:])
            elif block:
                blocks.append("".join(block))
                block = []
        if block:
            blocks.append("".join(block))
        return blocks


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple

    def render(self):
        return (f"--- a/{self.path}\n+++ b/{self.path}\n" +
                "".join(h.header + "".join(h.lines) for h in self.hunks))


def parse_patch(text):
    """Parse unified patches, rejecting path changes and malformed hunks."""
    lines = text.splitlines(keepends=True)
    files, path, hunks = [], None, []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if path is not None:
                files.append(FilePatch(path, tuple(hunks)))
            old = line[4:].strip().split("\t")[0]
            i += 1
            if i == len(lines) or not lines[i].startswith("+++ "):
                raise PatchError("Missing unified patch new-file header")
            new = lines[i][4:].strip().split("\t")[0]
            if old.startswith("a/") and new == "b/" + old[2:]:
                path = old[2:]
            elif old == new and not old.startswith("/"):
                path = old  # GNU patch rejects may use unprefixed paths.
            else:
                raise PatchError(f"Unsupported patch paths: {old!r}, {new!r}")
            parts = PurePosixPath(path)
            if parts.is_absolute() or ".." in parts.parts or "\\" in path or ":" in path:
                raise PatchError(f"Unsafe patch path: {path}")
            hunks = []
        elif line.startswith("@@ "):
            if path is None:
                raise PatchError("Patch hunk has no file header")
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise PatchError(f"Invalid hunk header: {line.rstrip()}")
            old_count, new_count = int(match[2] or 1), int(match[4] or 1)
            old_seen = new_seen = 0
            body = []
            i += 1
            while i < len(lines) and (old_seen < old_count or new_seen < new_count):
                item = lines[i]
                if not item or item[0] not in " +-":
                    raise PatchError(f"Malformed hunk for {path}: {item!r}")
                old_seen += item[0] in " -"
                new_seen += item[0] in " +"
                body.append(item)
                i += 1
            if (old_seen, new_seen) != (old_count, new_count):
                raise PatchError(f"Incorrect hunk lengths for {path}")
            hunks.append(Hunk(line, tuple(body)))
            continue
        i += 1
    if path is not None:
        files.append(FilePatch(path, tuple(hunks)))
    if not files or any(not f.hunks for f in files):
        raise PatchError("Patch contains no complete file hunks")
    if len({f.path for f in files}) != len(files):
        raise PatchError("Duplicate file sections in patch")
    return tuple(files)


def adapt_exec(file_patch, source):
    """Keep the ROM's bprm representation and use the complete upstream hook."""
    if file_patch.path != "fs/exec.c":
        return file_patch
    functions = re.findall(r"^static int do_execveat_common\(.*?^}\n", source, re.M | re.S)
    if len(functions) != 1:
        raise PatchError("Cannot uniquely identify do_execveat_common in fs/exec.c")
    function = functions[0]
    declarations = re.findall(r"^\tstruct linux_binprm (\*?bprm);$", function, re.M)
    if declarations not in (["*bprm"], ["bprm"]):
        raise PatchError("Unsupported linux_binprm declaration in do_execveat_common")
    argument = "&bprm" if declarations == ["bprm"] else "bprm"
    calls = re.findall(r"^\tretval = bprm_execve\(([^;]+)\);$", function, re.M)
    if len(calls) != 1 or calls[0] not in (argument, argument + ", fd, filename, flags"):
        raise PatchError("Unsupported bprm_execve call in do_execveat_common")
    if function.count(f"\tfree_bprm({argument});\n") != 1:
        raise PatchError("Unsupported bprm cleanup in do_execveat_common")
    old_call = " \tretval = bprm_execve(bprm, fd, filename, flags);\n"
    candidates = [h for h in file_patch.hunks if old_call in h.lines]
    if len(candidates) != 1:
        raise PatchError("Upstream patch does not contain the expected post-exec hunk")
    post = candidates[0]
    expected_context = (" \t}\n", " \n", old_call, " out_free:\n",
                        " \tfree_bprm(bprm);\n", " \n")
    if tuple(line for line in post.lines if not line.startswith("+")) != expected_context:
        raise PatchError("Upstream post-exec context changed; refusing an unverified repair")
    additions = "".join(post.additions())
    required = ("ksu_handle_post_execveat_sucompat(", "ksu_preinit_handle_post_execveat(",
                "\t\t\tfree_bprm(bprm);\n", "\t\t\tgoto orig_flow;\n",
                "\t\tgoto out_free;\n")
    if any(additions.count(token) != 1 for token in required):
        raise PatchError("Upstream post-exec hook structure changed; refusing repair")
    hunks = []
    for hunk in file_patch.hunks:
        adapted = []
        for line in hunk.lines:
            if line == " \tstruct linux_binprm *bprm;\n":
                line = f" \tstruct linux_binprm {declarations[0]};\n"
            elif hunk == post:
                if line == old_call:
                    line = f" \tretval = bprm_execve({calls[0]});\n"
                elif "free_bprm(bprm);" in line:
                    line = line.replace("free_bprm(bprm);", f"free_bprm({argument});")
            adapted.append(line)
        hunks.append(replace(hunk, lines=tuple(adapted)))
    if calls[0] != "bprm, fd, filename, flags":
        print(f"SUSFS: adapting complete exec hook to bprm_execve({calls[0]})")
    return replace(file_patch, hunks=tuple(hunks))


def declaration_hunk(path, hunk):
    """Only these exact upstream declaration hunks may be relocated."""
    if any(line.startswith("-") for line in hunk.lines):
        return False
    blocks = hunk.additions()
    include = ("#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>\n"
               "#endif // #ifdef CONFIG_KSU_SUSFS\n")
    externs = ("#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n"
               "extern bool susfs_is_current_ksu_domain(void);\n"
               "extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;\n")
    if path == "fs/namespace.c":
        expected = [include, externs + "\n#define CL_COPY_MNT_NS BIT(25) /* used by copy_mnt_ns() */\n"
                    "#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n\n"]
        return blocks == expected
    if path == "fs/super.c":
        return blocks in ([include], [externs +
                          "#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n\n"])
    return False


def relocate_declarations(path, hunk, source):
    if not declaration_hunk(path, hunk):
        raise PatchError(f"Unexpected rejected functional hunk in {path}")
    anchor = '#include "internal.h"\n'
    if source.count(anchor) != 1 or anchor not in "\n".join(source.splitlines()[:128]) + "\n":
        raise PatchError(f"Cannot uniquely locate include block in {path}")
    missing = []
    for block in hunk.additions():
        if source.count(block) == 1:
            continue
        if block in source:
            raise PatchError(f"Duplicate upstream declaration block in {path}")
        for line in block.splitlines():
            if line.startswith(("extern ", "#include ", "#define ")) and line in source.splitlines():
                raise PatchError(f"Partial or conflicting existing SUSFS declarations in {path}: {line}")
            symbol = re.search(r"\b([A-Za-z_]\w*)\s*(?:\(void\))?;", line)
            if line.startswith("extern ") and symbol and re.search(
                    r"^\s*extern\b[^;\n]*\b" + re.escape(symbol[1]) + r"\b[^;\n]*;", source, re.M):
                raise PatchError(f"Conflicting SUSFS declaration in {path}: {symbol[1]}")
            if line.startswith("#define ") and re.search(
                    r"^\s*#\s*define\s+" + re.escape(line.split()[1]) + r"\b", source, re.M):
                raise PatchError(f"Conflicting SUSFS macro in {path}: {line.split()[1]}")
        missing.append(block)
    result = source.replace(anchor, anchor + "\n" + "".join(missing), 1)
    if any(result.count(block) != 1 for block in hunk.additions()):
        raise PatchError(f"Failed to verify complete upstream declarations in {path}")
    print(f"SUSFS: relocated exact upstream declaration hunk in {path}")
    return result


def verify_additions(file_patch, source):
    for index, hunk in enumerate(file_patch.hunks, 1):
        for block in hunk.additions():
            if block not in source:
                raise PatchError(f"Missing complete SUSFS addition from {file_patch.path}, hunk {index}")


def apply_kernel_patch(root, patch_path, patch_command="patch"):
    root = Path(root).resolve()
    files = parse_patch(Path(patch_path).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="susfs-kernel-") as temporary:
        stage = Path(temporary)
        adapted = []
        for file_patch in files:
            source_path = root / file_patch.path
            if not source_path.is_file() or source_path.is_symlink() or not source_path.resolve().is_relative_to(root):
                raise PatchError(f"Missing or unsafe kernel target: {file_patch.path}")
            source = source_path.read_text(encoding="utf-8")
            adapted.append(adapt_exec(file_patch, source))
            staged_path = stage / file_patch.path
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, staged_path)
        command = [patch_command, "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch", "-p1"]
        result = subprocess.run(command, input="".join(f.render() for f in adapted),
                                cwd=stage, text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="")
        if result.returncode > 1:
            raise PatchError(f"GNU patch failed with fatal status {result.returncode}")
        reject_paths = list(stage.rglob("*.rej"))
        unresolved = []
        for rejected in reject_paths:
            reject_text = rejected.read_text(encoding="utf-8")
            relative = rejected.relative_to(stage).as_posix()[:-4]
            try:
                rejected_files = parse_patch(reject_text)
                if len(rejected_files) != 1 or rejected_files[0].path != relative:
                    raise PatchError("Reject target does not match patched target")
                original = next(f for f in adapted if f.path == relative)
                source_path = stage / relative
                source = source_path.read_text(encoding="utf-8")
                for hunk in rejected_files[0].hunks:
                    matches = [h for h in original.hunks if h.lines == hunk.lines]
                    if len(matches) != 1:
                        raise PatchError("Reject is not an exact upstream hunk")
                    source = relocate_declarations(relative, matches[0], source)
                source_path.write_text(source, encoding="utf-8", newline="\n")
                rejected.unlink()
            except (PatchError, StopIteration) as error:
                unresolved.append((relative, reject_text, str(error)))
        if unresolved:
            for relative, reject_text, error in unresolved:
                target = root / relative
                descriptor, diagnostic = tempfile.mkstemp(prefix=target.name + ".susfs-",
                                                           suffix=".rej", dir=target.parent)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(reject_text)
                print(f"SUSFS REJECT: {relative}: {error}\n{reject_text}Saved reject: {diagnostic}", file=sys.stderr)
            raise PatchError("Unresolved SUSFS rejects; kernel source files were not changed")
        if result.returncode and not reject_paths:
            raise PatchError("GNU patch reported failure without a reject; refusing success")
        for file_patch in adapted:
            verify_additions(file_patch, (stage / file_patch.path).read_text(encoding="utf-8"))
        # Validation is complete. Existing rejects/backups in root are untouched.
        for file_patch in adapted:
            shutil.copyfile(stage / file_patch.path, root / file_patch.path)
    print(f"SUSFS: all {len(files)} patched files and complete additions verified")


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {Path(sys.argv[0]).name} UPSTREAM_50_SUSFS_PATCH", file=sys.stderr)
        return 2
    try:
        apply_kernel_patch(Path.cwd(), Path(sys.argv[1]).resolve())
    except (PatchError, OSError, UnicodeError) as error:
        print(f"SUSFS ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
