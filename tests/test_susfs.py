"""Offline regression tests for Official SUSFS patching and build verification."""

import contextlib
import difflib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / ".github/scripts/apply-susfs-kernel.py"
SPEC = importlib.util.spec_from_file_location("apply_susfs_kernel", MODULE_PATH)
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)
PATCH = shutil.which("patch") or (r"E:\Git\usr\bin\patch.exe" if Path(r"E:\Git\usr\bin\patch.exe").is_file() else None)

POST_HOOK = """#ifdef CONFIG_KSU_SUSFS
	if (unlikely(is_su_session)) {
		(void)ksu_handle_post_execveat_sucompat(&fd, &filename, &argv, &envp, &flags, &retval);
	}

#ifndef KSU_NO_PREINIT
	if (unlikely(is_ksu_preinit_session)) {
		if (ksu_preinit_handle_post_execveat(&fd, &filename, &argv, &envp, &flags, &retval, &preinit_fd)) {
			free_bprm(bprm);
			is_ksu_preinit_session = false;
			goto orig_flow;
		}
		goto out_free;
	}
#endif // #ifndef KSU_NO_PREINIT
#endif // #ifdef CONFIG_KSU_SUSFS
"""

INCLUDE = """#ifdef CONFIG_KSU_SUSFS
#include <linux/susfs_def.h>
#endif // #ifdef CONFIG_KSU_SUSFS
"""
EXTERNS = """#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
extern bool susfs_is_current_ksu_domain(void);
extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;
"""
NAMESPACE_ADDITIONS = [INCLUDE, EXTERNS + """
#define CL_COPY_MNT_NS BIT(25) /* used by copy_mnt_ns() */
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT

"""]


def unified(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile="a/" + path, tofile="b/" + path))


def exec_fixture():
    before = """#include <linux/coredump.h>

static int do_execveat_common(int fd, struct filename *filename,
			      struct user_arg_ptr argv,
			      struct user_arg_ptr envp,
			      int flags)
{
	struct linux_binprm *bprm;
	int retval;

	if (IS_ERR(filename))
		return PTR_ERR(filename);

""" + "".join(f"\t/* unrelated exec line {i} */\n" for i in range(20)) + """	if (bprm->argc == 0) {
		bprm->argc = 1;
	}

	retval = bprm_execve(bprm, fd, filename, flags);
out_free:
	free_bprm(bprm);

	putname(filename);
	return retval;
}
"""
    after = before.replace("\tint retval;\n", "\tint retval;\n#ifdef CONFIG_KSU_SUSFS\n\tbool is_su_session = false;\n#endif\n", 1)
    after = after.replace("out_free:\n", POST_HOOK + "out_free:\n", 1)
    return before, after, unified("fs/exec.c", before, after)


@unittest.skipUnless(PATCH, "GNU patch is required")
class ApplyKernelPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-susfs-")
        self.root = Path(self.temporary.name)
        (self.root / "fs").mkdir()
        self.patch_path = self.root / "upstream.patch"

    def tearDown(self):
        self.temporary.cleanup()

    def apply(self, patch):
        self.patch_path.write_text(patch, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            HELPER.apply_kernel_patch(self.root, self.patch_path, PATCH)

    def test_exec_pointer_and_stack_variants_keep_the_complete_hook(self):
        before, _, patch = exec_fixture()
        for stack in (False, True):
            for single_argument in (False, True):
                with self.subTest(stack=stack, single_argument=single_argument):
                    source = before
                    argument = "&bprm" if stack else "bprm"
                    if stack:
                        source = source.replace("struct linux_binprm *bprm;", "struct linux_binprm bprm;")
                        source = source.replace("bprm->argc", "bprm.argc")
                        source = source.replace("bprm_execve(bprm,", "bprm_execve(&bprm,")
                        source = source.replace("free_bprm(bprm)", "free_bprm(&bprm)")
                    if single_argument:
                        source = source.replace(argument + ", fd, filename, flags)", argument + ")")
                    (self.root / "fs/exec.c").write_text(source, encoding="utf-8")
                    self.apply(patch)
                    result = (self.root / "fs/exec.c").read_text(encoding="utf-8")
                    self.assertIn(POST_HOOK.replace("free_bprm(bprm)", f"free_bprm({argument})"), result)
                    self.assertEqual(result.count("ksu_handle_post_execveat_sucompat("), 1)
                    self.assertEqual(result.count(f"free_bprm({argument});"), 2)

    def test_current_upstream_hook_content_is_preserved(self):
        before, after, _ = exec_fixture()
        after = after.replace("#ifdef CONFIG_KSU_SUSFS\n\tif (unlikely(is_su_session))", "#ifdef CONFIG_KSU_SUSFS\n\t/* upstream revision content */\n\tif (unlikely(is_su_session))")
        (self.root / "fs/exec.c").write_text(before, encoding="utf-8")
        self.apply(unified("fs/exec.c", before, after))
        self.assertEqual((self.root / "fs/exec.c").read_text(encoding="utf-8"), after)

    def test_unknown_exec_call_fails_without_changing_source(self):
        before, _, patch = exec_fixture()
        before = before.replace("bprm_execve(bprm, fd, filename, flags)", "bprm_execve(bprm, new_argument)")
        target = self.root / "fs/exec.c"
        target.write_text(before, encoding="utf-8")
        with self.assertRaisesRegex(HELPER.PatchError, "Unsupported bprm_execve"):
            self.apply(patch)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_namespace_declarations_relocated_only_after_exact_hunk_match(self):
        before = """#include <linux/fs_context.h>
#include <linux/shmem_fs.h>
#include <linux/mnt_idmapping.h>

#include "pnode.h"
#include "internal.h"
#include <trace/hooks/blk.h>

/* Maximum number of mounts in a mount namespace */
static unsigned int sysctl_mount_max __read_mostly = 100000;

"""
        after = before.replace("#include <linux/mnt_idmapping.h>\n", "#include <linux/mnt_idmapping.h>\n" + NAMESPACE_ADDITIONS[0])
        after = after.replace("/* Maximum", NAMESPACE_ADDITIONS[1] + "/* Maximum")
        # Force the entire header context to differ, as on a downstream ROM.
        current = '#include "internal.h"\n\n/* downstream include layout */\n'
        target = self.root / "fs/namespace.c"
        target.write_text(current, encoding="utf-8")
        previous_reject = self.root / "fs/namespace.c.rej"
        previous_reject.write_text("previous optional patch reject\n", encoding="utf-8")
        self.apply(unified("fs/namespace.c", before, after))
        result = target.read_text(encoding="utf-8")
        for block in NAMESPACE_ADDITIONS:
            self.assertIn(block, result)
        self.assertEqual(previous_reject.read_text(encoding="utf-8"), "previous optional patch reject\n")
        self.assertEqual(list(self.root.glob("fs/*.susfs-*.rej")), [])

    def test_super_include_and_extern_declaration_hunks_are_exact(self):
        before = '#include <linux/lockdep.h>\n#include <linux/user_namespace.h>\n#include <linux/fs_context.h>\n#include <uapi/linux/mount.h>\n#include "internal.h"\n\n'
        after = before.replace("#include <linux/fs_context.h>\n", "#include <linux/fs_context.h>\n" + INCLUDE)
        target = self.root / "fs/super.c"
        target.write_text('#include "internal.h"\n/* downstream */\n', encoding="utf-8")
        self.apply(unified("fs/super.c", before, after))
        self.assertIn(INCLUDE, target.read_text(encoding="utf-8"))
        hunk = HELPER.Hunk("", tuple("+" + line for line in (EXTERNS + "#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n\n").splitlines(True)))
        self.assertTrue(HELPER.declaration_hunk("fs/super.c", hunk))

    def test_functional_reject_with_susfs_keyword_is_never_discarded(self):
        before = "int downstream_function(void)\n{\n\treturn old_value;\n}\n"
        after = before.replace("\treturn old_value;", "\treturn susfs_required_function();")
        target = self.root / "fs/namespace.c"
        source = '#include "internal.h"\nint custom(void) { return 0; }\n'
        target.write_text(source, encoding="utf-8")
        reject = self.root / "fs/namespace.c.rej"
        reject.write_text("old reject\n", encoding="utf-8")
        with self.assertRaisesRegex(HELPER.PatchError, "Unresolved SUSFS rejects"):
            self.apply(unified("fs/namespace.c", before, after))
        self.assertEqual(target.read_text(encoding="utf-8"), source)
        self.assertEqual(reject.read_text(encoding="utf-8"), "old reject\n")
        diagnostics = list(self.root.glob("fs/namespace.c.susfs-*.rej"))
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("+\treturn susfs_required_function();", diagnostics[0].read_text(encoding="utf-8"))

    def test_additional_function_in_declaration_reject_fails_closed(self):
        hunk = HELPER.Hunk("", tuple("+" + line for line in INCLUDE.splitlines(True)) + ("+void susfs_functional_code(void) {}\n",))
        self.assertFalse(HELPER.declaration_hunk("fs/super.c", hunk))
        with self.assertRaises(HELPER.PatchError):
            HELPER.relocate_declarations("fs/super.c", hunk, '#include "internal.h"\n')

    def test_one_bad_file_does_not_leave_other_files_partially_patched(self):
        before, _, good = exec_fixture()
        (self.root / "fs/exec.c").write_text(before, encoding="utf-8")
        source = '#include "internal.h"\n/* unmatched source */\n'
        (self.root / "fs/super.c").write_text(source, encoding="utf-8")
        bad = unified("fs/super.c", "old_target\n", "susfs_functional_target\n")
        with self.assertRaisesRegex(HELPER.PatchError, "Unresolved SUSFS rejects"):
            self.apply(good + bad)
        self.assertEqual((self.root / "fs/exec.c").read_text(encoding="utf-8"), before)
        self.assertEqual((self.root / "fs/super.c").read_text(encoding="utf-8"), source)

    def test_success_preserves_old_exec_reject_bytes(self):
        before, _, patch = exec_fixture()
        (self.root / "fs/exec.c").write_text(before, encoding="utf-8")
        old_reject = self.root / "fs/exec.c.rej"
        contents = b"old optional patch reject\r\n\xff"
        old_reject.write_bytes(contents)
        self.apply(patch)
        self.assertEqual(old_reject.read_bytes(), contents)

    def test_missing_addition_is_detected_even_when_patch_reports_success(self):
        before, _, patch = exec_fixture()
        target = self.root / "fs/exec.c"
        target.write_text(before, encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout="fake patch success\n")
        with mock.patch.object(HELPER.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(HELPER.PatchError, "Missing complete SUSFS addition"):
                self.apply(patch)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_relocated_declarations_reject_partial_existing_block(self):
        hunk = HELPER.Hunk("", tuple("+" + line for line in INCLUDE.splitlines(True)))
        with self.assertRaisesRegex(HELPER.PatchError, "Partial or conflicting"):
            HELPER.relocate_declarations("fs/super.c", hunk, '#include "internal.h"\n#include <linux/susfs_def.h>\n')

    def test_relocated_declarations_reject_stale_type_and_macro(self):
        # namespace's two blocks must stay distinct in its original hunk.
        include_hunk = HELPER.Hunk("", tuple("+" + line for line in NAMESPACE_ADDITIONS[0].splitlines(True)) +
                                  (" \n",) + tuple("+" + line for line in NAMESPACE_ADDITIONS[1].splitlines(True)))
        for stale in ("extern bool susfs_is_sdcard_android_data_not_decrypted;\n", "#define CL_COPY_MNT_NS BIT(26)\n"):
            with self.subTest(stale=stale):
                with self.assertRaisesRegex(HELPER.PatchError, "Conflicting SUSFS"):
                    HELPER.relocate_declarations("fs/namespace.c", include_hunk, '#include "internal.h"\n' + stale)

    def test_reject_path_traversal(self):
        with self.assertRaisesRegex(HELPER.PatchError, "Unsafe patch path"):
            HELPER.parse_patch('--- a/../escape\n+++ b/../escape\n@@ -1 +1,2 @@\n old\n+new\n')


ROOT = Path(__file__).resolve().parents[1]
SHELL_HELPER = ROOT / ".github" / "scripts" / "integrate-susfs.sh"
OFFICIAL = "KernelSU-Official-susfs"
UNSUPPORTED_TYPES = ("KernelSU-Next-susfs", "BakaSU-susfs", "Unknown-susfs")
BEFORE_KCONFIG = 'config KSU\n\tbool "KernelSU"\n'
AFTER_KCONFIG = BEFORE_KCONFIG + '\nconfig KSU_SUSFS\n\tbool "SUSFS"\n'
BEFORE_SOURCE = "void ksu_init(void) {}\n"
ANCHORS = (
    "susfs_init(",
    "susfs_is_current_ksu_domain(",
    "ksu_handle_post_execveat_sucompat(",
    "CMD_SUSFS_SHOW_VERSION",
    "ksu_setup_preinit_file(",
    "ksu_preinit_handle_post_execveat(",
)
AFTER_SOURCE = (
    "void ksu_init(void) { susfs_init(); }\n"
    "bool susfs_is_current_ksu_domain(void) { return true; }\n"
    "int ksu_handle_post_execveat_sucompat(void) { return 0; }\n"
    "int command = CMD_SUSFS_SHOW_VERSION;\n"
    "int ksu_setup_preinit_file(void) { return 0; }\n"
    "int ksu_preinit_handle_post_execveat(void) { return 0; }\n"
)


def find_bash():
    """Follow the project's explicit Bash, PATH, Windows fallback convention."""
    configured = os.environ.get("BASH")
    if configured:
        return configured
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    windows_bash = Path(r"E:\Git\bin\bash.exe")
    return str(windows_bash) if windows_bash.is_file() else None


def bash_path(path):
    """Convert Windows paths to Git Bash/MSYS paths."""
    path = Path(path).resolve()
    if os.name == "nt" and path.drive:
        return "/" + path.drive[0].lower() + path.as_posix()[2:]
    return str(path)


def fixture_patch(kconfig=AFTER_KCONFIG, source=AFTER_SOURCE):
    """Use the upstream patch's repository-relative kernel/ paths."""
    return "".join(
        "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile="a/" + path,
                tofile="b/" + path,
            )
        )
        for path, before, after in (
            ("kernel/Kconfig", BEFORE_KCONFIG, kconfig),
            ("kernel/source.c", BEFORE_SOURCE, source),
        )
    )


class SusfsKernelSUTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is required; set BASH to its executable")
        environment = os.environ.copy()
        environment.pop("BASH_ENV", None)
        probe = subprocess.run(
            [cls.bash, "-c", "command -v git"],
            env=environment, capture_output=True, text=True, timeout=15, check=False,
        )
        if probe.returncode:
            raise unittest.SkipTest("Git is required in Bash's PATH")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="susfs-ksu-")
        self.addCleanup(self.temporary.cleanup)
        workspace = Path(self.temporary.name)
        self.checkout = workspace / "kernel checkout"
        self.repo = workspace / "KernelSU source"
        self.ksu = self.repo / "kernel"
        self.ksu.mkdir(parents=True)
        (self.checkout / "drivers").mkdir(parents=True)
        self.write_integration(BEFORE_KCONFIG, BEFORE_SOURCE)
        initialized = self.run_bash(
            "-c", 'git -C "$1" init --quiet', "fixture", bash_path(self.repo)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        link = self.checkout / "drivers" / "kernelsu"
        try:
            link.symlink_to(self.ksu, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Directory junctions need no Windows Developer Mode/admin rights.
            linked = subprocess.run(
                f'cmd.exe /d /c mklink /J "{link}" "{self.ksu}"',
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)
        self.susfs = workspace / "susfs source"
        self.patch = (
            self.susfs / "kernel_patches" / "KernelSU"
            / "10_enable_susfs_for_ksu.patch"
        )
        self.patch.parent.mkdir(parents=True)
        self.patch.write_text(fixture_patch(), encoding="utf-8")

    def run_bash(self, *arguments):
        environment = os.environ.copy()
        environment.pop("BASH_ENV", None)
        return subprocess.run(
            [self.bash, *arguments], cwd=self.checkout, env=environment,
            capture_output=True, text=True, timeout=15, check=False,
        )

    def write_integration(self, kconfig=AFTER_KCONFIG, source=AFTER_SOURCE):
        (self.ksu / "Kconfig").write_text(kconfig, encoding="utf-8")
        (self.ksu / "source.c").write_text(source, encoding="utf-8")

    def apply(self, ksu_type=OFFICIAL):
        return self.run_bash(
            "-c", 'source "$1"; apply_ksu_patch "$2" "$3"', "fixture",
            bash_path(SHELL_HELPER), ksu_type, bash_path(self.susfs),
        )

    def assert_failed(self, result, diagnostic=None):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("integration verified", result.stdout)
        if diagnostic:
            self.assertIn(diagnostic, result.stderr)

    def test_official_applies_kernel_su_patch_before_verifying(self):
        # Original regression: kernel-side patch/config alone left no KSU_SUSFS.
        self.assertNotIn("KSU_SUSFS", (self.ksu / "Kconfig").read_text())
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.ksu / "Kconfig").read_text(), AFTER_KCONFIG)
        self.assertEqual((self.ksu / "source.c").read_text(), AFTER_SOURCE)
        self.assertIn(f"integration verified: {OFFICIAL}", result.stdout)

    def test_official_conflict_fails_without_applying_other_hunks(self):
        # Kconfig would apply, but a later C-file hunk conflicts. Neither may change.
        (self.ksu / "source.c").write_text(
            "void incompatible_ksu_init(void) {}\n", encoding="utf-8"
        )
        before = {p.name: p.read_bytes() for p in self.ksu.iterdir()}
        result = self.apply()
        self.assert_failed(result)
        self.assertEqual({p.name: p.read_bytes() for p in self.ksu.iterdir()}, before)
        self.assertFalse(list(self.ksu.rglob("*.rej")))

    def test_official_patch_without_kconfig_option_fails(self):
        self.patch.write_text(
            fixture_patch(kconfig=BEFORE_KCONFIG), encoding="utf-8"
        )
        self.assert_failed(self.apply(), "no SUSFS Kconfig support")

    def test_missing_kconfig_file_fails(self):
        (self.ksu / "Kconfig").unlink()
        before = {p.name: p.read_bytes() for p in self.ksu.iterdir()}
        self.assert_failed(self.apply())
        self.assertEqual({p.name: p.read_bytes() for p in self.ksu.iterdir()}, before)

    def test_official_patch_missing_each_interface_fails(self):
        for anchor in ANCHORS:
            with self.subTest(anchor=anchor):
                self.write_integration(BEFORE_KCONFIG, BEFORE_SOURCE)
                source = "".join(
                    line for line in AFTER_SOURCE.splitlines(True)
                    if anchor not in line
                )
                self.patch.write_text(fixture_patch(source=source), encoding="utf-8")
                self.assert_failed(self.apply(), f"missing {anchor}")

    def test_official_patch_with_legacy_anchors_without_preinit_fails(self):
        source = "".join(
            line for line in AFTER_SOURCE.splitlines(True)
            if "ksu_setup_preinit_file(" not in line
            and "ksu_preinit_handle_post_execveat(" not in line
        )
        self.patch.write_text(fixture_patch(source=source), encoding="utf-8")
        for anchor in ANCHORS[:4]:
            self.assertIn(anchor, source)
        self.assert_failed(self.apply(), "missing ksu_setup_preinit_file(")

    def test_preinit_declarations_without_implementations_fail(self):
        for anchor in ANCHORS[4:]:
            for declaration in ("extern int", "int"):
                with self.subTest(anchor=anchor, declaration=declaration):
                    self.write_integration(BEFORE_KCONFIG, BEFORE_SOURCE)
                    source = "".join(
                        f"{declaration} {anchor}void);\n" if anchor in line else line
                        for line in AFTER_SOURCE.splitlines(True)
                    )
                    self.patch.write_text(
                        fixture_patch(source=source), encoding="utf-8"
                    )
                    self.assert_failed(self.apply(), f"missing {anchor}")

    def test_nonofficial_types_are_rejected_without_changing_source(self):
        # A valid, applicable patch must not run for any unsupported selection.
        before = {p.name: p.read_bytes() for p in self.ksu.iterdir()}
        for ksu_type in UNSUPPORTED_TYPES:
            with self.subTest(ksu_type=ksu_type):
                self.assert_failed(self.apply(ksu_type), "Unsupported SUSFS KernelSU type")
                self.assertEqual(
                    {p.name: p.read_bytes() for p in self.ksu.iterdir()}, before
                )

    def test_nonofficial_scope_guard_runs_before_source_validation(self):
        missing_source = self.susfs / "missing checkout"
        for ksu_type in UNSUPPORTED_TYPES:
            with self.subTest(ksu_type=ksu_type):
                result = self.run_bash(
                    "-c", 'source "$1"; apply_ksu_patch "$2" "$3"', "fixture",
                    bash_path(SHELL_HELPER), ksu_type, bash_path(missing_source)
                )
                self.assert_failed(result, "Unsupported SUSFS KernelSU type")


REQUIRED_CONFIG = (
    "KSU",
    "KSU_SUSFS",
    "KSU_SUSFS_SUS_PATH",
    "KSU_SUSFS_SUS_MOUNT",
    "KSU_SUSFS_SUS_KSTAT",
    "KSU_SUSFS_SPOOF_UNAME",
    "KSU_SUSFS_ENABLE_LOG",
    "KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS",
    "KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG",
    "KSU_SUSFS_SUS_MAP",
)
REQUIRED_ARTIFACTS = ("fs/susfs.o", "vmlinux", "arch/arm64/boot/Image")
REQUIRED_SYMBOLS = (
    "susfs_is_sdcard_android_data_not_decrypted",
    "ksu_is_input_hook_enabled",
    "ksu_su_compat_enabled",
)


class SusfsVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is required; set BASH to its executable")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="susfs-verification-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "kernel output"
        self.output.mkdir()
        self.make_output()

    def make_output(self, disabled=()):
        """Create a complete build fixture; callers remove one prerequisite."""
        self.write_config(disabled)
        for relative in REQUIRED_ARTIFACTS:
            artifact = self.output / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"nonempty kernel build artifact\n")
        self.write_symbols()

    def write_symbols(self, missing=()):
        missing = set(missing)
        (self.output / "System.map").write_text(
            "".join(
                f"ffffffc08001{index:04x} D {symbol}\n"
                for index, symbol in enumerate(REQUIRED_SYMBOLS)
                if symbol not in missing
            ),
            encoding="utf-8",
        )

    def write_config(self, disabled=()):
        disabled = set(disabled)
        lines = [
            f"# CONFIG_{option} is not set"
            if option in disabled
            else f"CONFIG_{option}=y"
            for option in REQUIRED_CONFIG
        ]
        (self.output / ".config").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def verify(self, mode):
        environment = os.environ.copy()
        # Do not inherit shell startup files that can affect a noninteractive gate.
        environment.pop("BASH_ENV", None)
        return subprocess.run(
            [self.bash, bash_path(SHELL_HELPER), mode, bash_path(self.output)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def assert_passes(self, mode):
        result = self.verify(mode)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"SUSFS {mode} verification passed.", result.stdout)

    def assert_fails(self, mode, diagnostic):
        result = self.verify(mode)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertNotIn("verification passed", result.stdout)

    def test_complete_config_passes_without_build_artifacts(self):
        for relative in REQUIRED_ARTIFACTS:
            (self.output / relative).unlink()
        (self.output / "System.map").unlink()
        self.assert_passes("config")

    def test_each_required_config_option_must_be_enabled(self):
        for option in REQUIRED_CONFIG:
            with self.subTest(option=option):
                self.write_config(disabled=(option,))
                self.assert_fails("config", f"CONFIG_{option}=y missing")

    def test_config_option_set_to_module_is_rejected(self):
        config = self.output / ".config"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "CONFIG_KSU_SUSFS=y\n", "CONFIG_KSU_SUSFS=m\n"
            ),
            encoding="utf-8",
        )
        self.assert_fails("config", "CONFIG_KSU_SUSFS=y missing")

    def test_built_mode_also_checks_configuration(self):
        self.write_config(disabled=("KSU_SUSFS",))
        self.assert_fails("built", "CONFIG_KSU_SUSFS=y missing")

    def test_complete_build_passes(self):
        self.assert_passes("built")

    def test_each_missing_build_artifact_is_rejected(self):
        for relative in REQUIRED_ARTIFACTS:
            with self.subTest(artifact=relative):
                artifact = self.output / relative
                original = artifact.read_bytes()
                artifact.unlink()
                try:
                    self.assert_fails("built", relative)
                finally:
                    artifact.write_bytes(original)

    def test_each_empty_build_artifact_is_rejected(self):
        for relative in REQUIRED_ARTIFACTS:
            with self.subTest(artifact=relative):
                artifact = self.output / relative
                original = artifact.read_bytes()
                artifact.write_bytes(b"")
                try:
                    self.assert_fails("built", relative)
                finally:
                    artifact.write_bytes(original)

    def test_missing_system_map_is_rejected(self):
        (self.output / "System.map").unlink()
        self.assert_fails("built", "System.map")

    def test_empty_system_map_is_rejected(self):
        (self.output / "System.map").write_bytes(b"")
        self.assert_fails("built", "System.map")

    def test_each_missing_linked_symbol_is_rejected(self):
        for symbol in REQUIRED_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.write_symbols(missing=(symbol,))
                self.assert_fails("built", symbol)

    def test_similar_symbol_names_do_not_satisfy_link_check(self):
        symbol = REQUIRED_SYMBOLS[0]
        for replacement in (
            f"{symbol}_extra",
            f"prefix_{symbol}",
            f"{symbol}.llvm.",
            f"{symbol}.llvm.123abc",
            f"{symbol}.llvm.123.extra",
        ):
            with self.subTest(replacement=replacement):
                self.write_symbols(missing=(symbol,))
                with (self.output / "System.map").open("a", encoding="utf-8") as stream:
                    stream.write(f"ffffffc080010100 D {replacement}\n")
                self.assert_fails("built", symbol)

    def test_lto_internalized_symbols_pass(self):
        (self.output / "System.map").write_text(
            "".join(
                f"ffffffc08001{index:04x} d {symbol}.llvm.{index + 123456}\n"
                for index, symbol in enumerate(REQUIRED_SYMBOLS)
            ),
            encoding="utf-8",
        )
        self.assert_passes("built")

    def test_unknown_mode_is_rejected(self):
        self.assert_fails("unsupported", "Unknown SUSFS verification mode")


if __name__ == "__main__":
    unittest.main()
