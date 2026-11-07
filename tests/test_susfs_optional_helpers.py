"""Regression coverage for SUSFS credentials helpers with the UID filter off."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_susfs as fixtures


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/patch-susfs-optional-helpers.sh"
GUARD = "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER"
# Keep the upstream helper bodies and the patch's surrounding context.
SOURCE = """void unrelated_function(void) {
	if (0) {
	}
}

/*
 * Switches the selinux domain of the current process to the KernelSU one, so
 * that the data paths of the apps can be accessed despite of the selinux
 * restrictions.
 * The original credentials are returned, they have to be restored with
 * susfs_restore_creds() afterwards.
 * Returns the original credentials on success, NULL on failure.
 */
static struct cred *susfs_switch_to_ksu_domain(void) {
	struct cred *orig_cred = NULL;
	struct cred *cred = NULL;

	/* a copy of the current credentials, which is restored later on */
	orig_cred = prepare_creds();
	if (!orig_cred) {
		SUSFS_LOGE("failed to prepare creds!");
		return NULL;
	}

	cred = prepare_creds();
	if (!cred) {
		SUSFS_LOGE("failed to prepare creds!");
		abort_creds(orig_cred);
		return NULL;
	}

	setup_selinux("u:r:ksu:s0", cred);
	commit_creds(cred);

	if (!susfs_is_current_ksu_domain()) {
		SUSFS_LOGE("domain is not ksu, restoring the original creds");
		commit_creds(orig_cred);
		return NULL;
	}

	return orig_cred;
}

/*
 * Restores the original credentials that were saved by
 * susfs_switch_to_ksu_domain().
 */
static void susfs_restore_creds(struct cred *orig_cred) {
	if (!orig_cred)
		return;

	commit_creds(orig_cred);
}

/* sus_path */
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
#endif
"""
STUBS = """#include <stddef.h>
#include <stdbool.h>
struct cred { int unused; };
extern struct cred *prepare_creds(void);
extern void abort_creds(struct cred *cred);
extern void commit_creds(struct cred *cred);
extern void setup_selinux(const char *domain, struct cred *cred);
extern bool susfs_is_current_ksu_domain(void);
#define SUSFS_LOGE(...) do {} while (0)
"""
CALLERS = """
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
void priority_uid_filter(void) {
    struct cred *saved = susfs_switch_to_ksu_domain();
    susfs_restore_creds(saved);
}
#endif
"""


class SusfsOptionalHelpersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = fixtures.find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is required; set BASH to its executable")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="susfs-optional-")
        self.addCleanup(temporary.cleanup)
        self.kernel = Path(temporary.name)
        self.target = self.kernel / "fs/susfs.c"
        self.target.parent.mkdir()
        self.target.write_text(SOURCE, encoding="utf-8", newline="\n")
        initialized = self.run_bash("-c", 'git init --quiet')
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def run_bash(self, *arguments):
        environment = os.environ.copy()
        environment.pop("BASH_ENV", None)
        return subprocess.run(
            [self.bash, *arguments], cwd=self.kernel, env=environment,
            capture_output=True, text=True, timeout=15, check=False,
        )

    def apply(self):
        return self.run_bash(fixtures.bash_path(SCRIPT))

    def apply_successfully(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.target.read_text(encoding="utf-8")

    def test_guards_only_optional_helpers_and_preserves_their_bodies(self):
        actual = self.apply_successfully()
        added_guard = f"#ifdef {GUARD}\n"
        end_guard = f"\n#endif /* {GUARD} */\n"
        self.assertEqual(actual.count(added_guard), 1)
        self.assertEqual(actual.count(end_guard), 1)
        self.assertEqual(actual.replace(added_guard, "").replace(end_guard, ""), SOURCE)

    def test_already_applied_guard_is_accepted_without_changing_source(self):
        self.apply_successfully()
        before = self.target.read_bytes()
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already present", result.stdout)
        self.assertEqual(self.target.read_bytes(), before)

    def test_unknown_layout_fails_without_partially_applying_first_hunk(self):
        self.target.write_text(SOURCE.replace("/* sus_path */", "/* changed upstream */"),
                               encoding="utf-8", newline="\n")
        before = self.target.read_bytes()
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upstream layout changed", result.stderr)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(list(self.kernel.rglob("*.rej")))

    def test_partial_guard_is_rejected_without_changing_source(self):
        self.target.write_text(SOURCE.replace("/*\n * Switches",
                               f"#ifdef {GUARD}\n/*\n * Switches", 1),
                               encoding="utf-8", newline="\n")
        before = self.target.read_bytes()
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(self.target.read_bytes(), before)

    def test_optional_helpers_compile_with_feature_disabled_and_enabled(self):
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            self.skipTest("C compiler required for the Werror regression")
        actual = self.apply_successfully()
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        for label, source, enabled, expected in (
            ("original-disabled", SOURCE, False, False),
            ("guarded-disabled", actual, False, True),
            ("guarded-enabled", actual, True, True),
        ):
            with self.subTest(case=label):
                prefix = f"#define {GUARD} 1\n" if enabled else ""
                result = subprocess.run(
                    [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                     "-x", "c", "-c", "-o", os.devnull, "-"],
                    input=prefix + STUBS + source + CALLERS, env=environment,
                    text=True, encoding="utf-8", capture_output=True, timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, expected, result.stderr)
                if not expected:
                    self.assertIn("unused-function", result.stderr)
                    self.assertIn("susfs_switch_to_ksu_domain", result.stderr)
                    self.assertIn("susfs_restore_creds", result.stderr)


if __name__ == "__main__":
    unittest.main()
