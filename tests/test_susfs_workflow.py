"""Exercise real workflow SUSFS source blocks against an offline YAAP fixture."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import test_susfs as fixtures


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/_build-kernel.yml",
    ROOT / ".github/workflows/matrixbuild.yml",
)


def source_block(path):
    """Read the source-patching statements from the actual workflow."""
    lines = path.read_text(encoding="utf-8").splitlines(True)
    start = next(i for i, line in enumerate(lines)
                 if "# Apply the original SUSFS gki-android14-6.1 patch" in line)
    command = next(i for i in range(start, len(lines))
                   if "python3 ../.github/scripts/apply-susfs-kernel.py" in lines[i])
    end = command + 1
    while lines[end - 1].rstrip().endswith(chr(92)):
        end += 1
    return textwrap.dedent("".join(lines[start:end]))


@unittest.skipUnless(fixtures.PATCH, "GNU patch is required")
class SusfsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = fixtures.find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is required; set BASH to its executable")

    def run_source_block(self, workflow, source, patch):
        with tempfile.TemporaryDirectory(prefix="susfs-workflow-") as temporary:
            workspace = Path(temporary)
            kernel = workspace / "kernel"
            (kernel / "fs").mkdir(parents=True)
            (kernel / "include/linux").mkdir(parents=True)
            target = kernel / "fs/exec.c"
            target.write_text(source, encoding="utf-8", newline="\n")
            old_reject = kernel / "fs/exec.c.rej"
            old_reject.write_bytes(b"previous optional reject\r\n")
            upstream = kernel / "susfs/kernel_patches"
            (upstream / "fs").mkdir(parents=True)
            (upstream / "include/linux").mkdir(parents=True)
            (upstream / "50_add_susfs_in_gki-android14-6.1.patch").write_text(
                patch, encoding="utf-8", newline="\n"
            )
            (upstream / "fs/susfs.c").write_text("/* fixture */\n", encoding="utf-8")
            (upstream / "include/linux/susfs_def.h").write_text(
                "/* fixture */\n", encoding="utf-8"
            )
            helper = workspace / ".github/scripts/apply-susfs-kernel.py"
            helper.parent.mkdir(parents=True)
            shutil.copyfile(fixtures.MODULE_PATH, helper)
            # Stub only acquisition; run the workflow copy/patch statements
            # using the real Python helper and GNU patch.
            script = (
                'set -e\n'
                'python_for_test=$1\n'
                'python3() { "$python_for_test" "$@"; }\n'
                'git() { [[ "$1" == clone ]]; }\n'
                + source_block(workflow)
            )
            environment = os.environ.copy()
            environment.pop("BASH_ENV", None)
            environment["PATH"] = str(Path(fixtures.PATCH).parent) + os.pathsep + environment["PATH"]
            result = subprocess.run(
                [self.bash, "-c", script, "fixture", fixtures.bash_path(sys.executable)],
                cwd=kernel, env=environment, text=True, capture_output=True,
                timeout=20, check=False,
            )
            return result, target.read_text(encoding="utf-8"), old_reject.read_bytes()

    def test_matrix_and_reusable_workflows_preserve_yaap_exec_hook(self):
        before, _, patch = fixtures.exec_fixture()
        source = before.replace("struct linux_binprm *bprm;", "struct linux_binprm bprm;")
        source = source.replace("bprm->argc", "bprm.argc")
        source = source.replace("bprm_execve(bprm,", "bprm_execve(&bprm,")
        source = source.replace("free_bprm(bprm)", "free_bprm(&bprm)")
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                result, actual, old_reject = self.run_source_block(workflow, source, patch)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = fixtures.POST_HOOK.replace("free_bprm(bprm)", "free_bprm(&bprm)")
                self.assertIn(expected, actual)
                self.assertIn("struct linux_binprm bprm;", actual)
                self.assertEqual(old_reject, b"previous optional reject\r\n")

    def test_workflows_stop_when_a_required_target_is_missing(self):
        before = "int vendor_exec(void) { return 0; }\n"
        after = before + "int susfs_required_hook(void) { return 1; }\n"
        patch = fixtures.unified("fs/namei.c", before, after)
        source, _, _ = fixtures.exec_fixture()
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                result, actual, old_reject = self.run_source_block(workflow, source, patch)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Missing or unsafe kernel target", result.stderr)
                self.assertEqual(actual, source)
                self.assertEqual(old_reject, b"previous optional reject\r\n")


if __name__ == "__main__":
    unittest.main()
