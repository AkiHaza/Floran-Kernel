"""Exercise branch selection against real offline Git repositories."""

from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

import test_susfs as fixtures


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / ".github/scripts/setup-kernelsu.sh"
SUSFS_SOURCE = ROOT / ".github/scripts/susfs-source.sh"


class BranchFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = fixtures.find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is required")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="custom-branches-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.kernel = self.root / "kernel source"
        self.kernel.mkdir()
        self.remote = self.root / "upstream source"
        self.remote.mkdir()
        self.environment = os.environ.copy()
        for name in ("BASH_ENV", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "SUSFS_BRANCH"):
            self.environment.pop(name, None)
        self.environment["GIT_ALLOW_PROTOCOL"] = "file"
        self.environment["GIT_TERMINAL_PROMPT"] = "0"
        self.environment["GIT_CONFIG_NOSYSTEM"] = "1"
        self.environment["GIT_CONFIG_GLOBAL"] = os.devnull
        self.environment["FIXTURE_PYTHON"] = fixtures.bash_path(sys.executable)
        self.command(
            'git init --quiet --initial-branch=main "$1" && '
            'git -C "$1" config user.name Fixture && '
            'git -C "$1" config user.email fixture@example.invalid', self.remote
        )

    def command(self, script, *arguments, cwd=None, check=True):
        script = ('python3() { ' + shlex.quote(fixtures.bash_path(sys.executable))
                  + ' "$@"; }\n' + script)
        result = subprocess.run(
            [self.bash, "-c", script, "fixture",
             *(fixtures.bash_path(a) if isinstance(a, Path) else a for a in arguments)],
            cwd=cwd or self.kernel, env=self.environment,
            text=True, capture_output=True, timeout=20, check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commit(self):
        self.command('git -C "$1" add .; git -C "$1" commit --quiet -m fixture', self.remote)

    def branch(self, name):
        self.command('git -C "$1" branch "$2"', self.remote, name)

    def redirect(self, *urls):
        # Real clone/checkout/ref validation, with HTTPS redirected locally.
        self.environment["GIT_CONFIG_COUNT"] = str(len(urls))
        for index, url in enumerate(urls):
            self.environment[f"GIT_CONFIG_KEY_{index}"] = f"url.{self.remote.as_uri()}.insteadOf"
            self.environment[f"GIT_CONFIG_VALUE_{index}"] = url


class KernelSUBranchTests(BranchFixture):
    def prepare(self, behavior="correct"):
        (self.kernel / "drivers").mkdir()
        (self.kernel / "drivers/Makefile").write_text("# fixture\n", encoding="utf-8")
        (self.kernel / "drivers/Kconfig").write_text("endmenu\n", encoding="utf-8")
        (self.remote / "kernel").mkdir()
        script = (
            '#!/usr/bin/env bash\nset -eu\n'
            'checkout=KernelSU\n'
            '[[ -d KernelSU-Next ]] && checkout=KernelSU-Next\n'
            'printf "%s\\n" "$1" > selected-branch\n'
            'git -C "$checkout" checkout --quiet "$1"\n'
            'ln -s "../$checkout/kernel" drivers/kernelsu\n'
        )
        if os.name == "nt":
            link = (
                '"$FIXTURE_PYTHON" -c \'import pathlib, subprocess, sys; '
                'source=pathlib.Path(sys.argv[1]).resolve(); '
                'target=pathlib.Path(sys.argv[2]).resolve(); '
                'subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)], '
                'check=True, capture_output=True)\' "$checkout/kernel" drivers/kernelsu'
            )
            script = script.replace('ln -s "../$checkout/kernel" drivers/kernelsu', link)
        if behavior == "fallback":
            script += 'git -C "$checkout" checkout --quiet -b default-fallback HEAD\n'
        elif behavior == "wrong-link":
            script += 'rm -rf drivers/kernelsu; mkdir drivers/kernelsu\n'
        (self.remote / "kernel/setup.sh").write_text(script, encoding="utf-8", newline="\n")
        (self.remote / "kernel/Kconfig").write_text("config KSU\n", encoding="utf-8")
        self.commit()
        for branch in ("sync-up", "dev-susfs", "feature/custom-branch"):
            self.branch(branch)
        urls = []
        for repository in ("Baka-SU/BakaSU", "tiann/KernelSU", "pershoot/KernelSU-Next"):
            urls.extend((f"https://github.com/{repository}", f"https://github.com/{repository}.git"))
        self.redirect(*urls)

    def setup(self, kind, branch=""):
        return self.command('bash "$1" "$2" "$3"', SETUP, kind, branch, check=False)

    def test_bakasu_custom_branch_is_used_by_checkout_and_setup(self):
        self.prepare()
        result = self.setup("BakaSU-susfs", "sync-up")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.kernel / "selected-branch").read_text().strip(), "sync-up")
        selected = self.command('git -C KernelSU symbolic-ref --short HEAD').stdout.strip()
        self.assertEqual(selected, "sync-up")

    def test_custom_branch_can_contain_a_slash(self):
        self.prepare()
        result = self.setup("BakaSU", "feature/custom-branch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.kernel / "selected-branch").read_text().strip(), "feature/custom-branch")

    def test_defaults_select_existing_upstream_branches(self):
        self.prepare()
        for kind, expected in (("KernelSU-Official", "main"),
                               ("KernelSU-Official-susfs", "main"),
                               ("KernelSU-Next", "dev-susfs"),
                               ("KernelSU-Next-susfs", "dev-susfs"),
                               ("BakaSU", "main")):
            with self.subTest(kind=kind):
                work = self.root / kind
                (work / "drivers").mkdir(parents=True)
                result = self.command('bash "$1" "$2"', SETUP, kind, cwd=work, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((work / "selected-branch").read_text().strip(), expected)

    def test_none_does_not_acquire_or_execute_a_manager(self):
        result = self.setup("None", "sync-up")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.kernel.iterdir()), [])

    def test_none_detaches_inherited_hooks_and_preserves_existing_sources(self):
        drivers = self.kernel / "drivers"
        drivers.mkdir()
        kconfig = drivers / "Kconfig"
        makefile = drivers / "Makefile"
        retained_kconfig = 'source "drivers/other/Kconfig"\nendmenu\n'
        retained_makefile = 'obj-$(CONFIG_OTHER) += other/\n# kernelsu source retained\n'
        kconfig.write_text('source "drivers/kernelsu/Kconfig"\n' + retained_kconfig,
                           encoding="utf-8", newline="\n")
        makefile.write_text('obj-$(CONFIG_KSU) += kernelsu/\n' + retained_makefile,
                            encoding="utf-8", newline="\n")
        # Reproduce an uninitialized submodule: its Kconfig does not exist.
        result = self.setup("None")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(kconfig.read_text(), retained_kconfig)
        self.assertEqual(makefile.read_text(), retained_makefile)

        source = self.kernel / "KernelSU-Next/kernel/Kconfig"
        source.parent.mkdir(parents=True)
        source.write_text("config KSU\n", encoding="utf-8")
        submodules = self.kernel / ".gitmodules"
        submodules.write_text('[submodule "KernelSU-Next"]\n', encoding="utf-8")
        # Repeated None builds must also preserve initialized source trees.
        result = self.setup("None")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(kconfig.read_text(), retained_kconfig)
        self.assertEqual(makefile.read_text(), retained_makefile)
        self.assertEqual(source.read_text(), "config KSU\n")
        self.assertEqual(submodules.read_text(), '[submodule "KernelSU-Next"]\n')

    def test_invalid_and_missing_branches_fail_before_setup(self):
        self.prepare()
        for branch in ("--cleanup", "missing-branch", "$(touch injected)", "main;touch injected"):
            with self.subTest(branch=branch):
                result = self.setup("BakaSU", branch)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.kernel / "selected-branch").exists())
                self.assertFalse((self.kernel / "injected").exists())

    def test_upstream_success_cannot_hide_a_wrong_checkout(self):
        self.prepare("fallback")
        result = self.setup("BakaSU", "sync-up")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tag_without_matching_branch_cannot_execute_setup(self):
        self.prepare()
        self.command('git -C "$1" tag tag-only', self.remote)
        result = self.setup("BakaSU", "tag-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Requested KernelSU branch does not exist", result.stderr)
        self.assertFalse((self.kernel / "selected-branch").exists())

    def test_upstream_success_cannot_hide_a_wrong_driver_source(self):
        self.prepare("wrong-link")
        result = self.setup("BakaSU", "sync-up")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


class SusfsBranchTests(BranchFixture):
    def prepare(self, metadata="build.config.constants", kmi="android14-6.1", missing_patch=False):
        version, patchlevel = kmi.split("-")[-1].split(".")
        (self.kernel / "Makefile").write_text(
            f"VERSION = {version}\nPATCHLEVEL = {patchlevel}\n", encoding="utf-8"
        )
        if metadata:
            (self.kernel / metadata).write_text(f'BRANCH={kmi}\n', encoding="utf-8")
        patches = self.remote / "kernel_patches"
        (patches / "fs").mkdir(parents=True)
        (patches / "include/linux").mkdir(parents=True)
        if not missing_patch:
            (patches / f"50_add_susfs_in_gki-{kmi}.patch").write_text("fixture patch\n", encoding="utf-8")
        (patches / "fs/susfs.c").write_text("/* fixture */\n", encoding="utf-8")
        (patches / "include/linux/susfs.h").write_text("/* fixture */\n", encoding="utf-8")
        self.commit()
        self.branch(f"gki-{kmi}")
        self.branch(f"gki-{kmi}-dev")
        self.redirect("https://gitlab.com/simonpunk/susfs4ksu",
                      "https://gitlab.com/simonpunk/susfs4ksu.git")

    def resolve(self, branch="", cwd=None):
        return self.command(
            'set -eu; source "$1"; prepare_susfs_source "$2"; '
            'printf "RESULT:%s:%s:%s\\n" "$SUSFS_BRANCH" "$SUSFS_KMI" "$SUSFS_PATCH"',
            SUSFS_SOURCE, branch, cwd=cwd, check=False,
        )

    def test_default_uses_kernel_kmi_stable_branch(self):
        self.prepare()
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT:gki-android14-6.1:android14-6.1:susfs/kernel_patches/50_add_susfs_in_gki-android14-6.1.patch", result.stdout)

    def test_dev_uses_source_kmi_and_patch_name_without_dev_suffix(self):
        self.prepare()
        self.environment["ANDROID_VERSION"] = "17"
        result = self.resolve("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT:gki-android14-6.1-dev:android14-6.1:susfs/kernel_patches/50_add_susfs_in_gki-android14-6.1.patch", result.stdout)

    def test_explicit_matching_dev_branch_is_used(self):
        self.prepare()
        result = self.resolve("gki-android14-6.1-dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT:gki-android14-6.1-dev:", result.stdout)

    def test_tag_without_matching_branch_cannot_select_a_patch(self):
        self.prepare()
        self.command('git -C "$1" tag gki-android14-6.1-tag-only', self.remote)
        result = self.resolve("gki-android14-6.1-tag-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Requested SUSFS branch does not exist", result.stderr)
        self.assertNotIn("RESULT:", result.stdout)

    def test_other_kernel_kmi_uses_its_matching_dev_branch(self):
        self.prepare(kmi="android15-6.6")
        result = self.resolve("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT:gki-android15-6.6-dev:android15-6.6:", result.stdout)

    def test_makefile_version_mismatch_is_rejected_before_clone(self):
        self.prepare()
        (self.kernel / "Makefile").write_text("VERSION = 6\nPATCHLEVEL = 6\n", encoding="utf-8")
        result = self.resolve("dev")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.kernel / "susfs").exists())

    def test_branch_for_a_different_kmi_is_rejected_before_clone(self):
        self.prepare()
        result = self.resolve("gki-android15-6.6-dev")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.kernel / "susfs").exists())

    def test_missing_kmi_metadata_is_rejected_before_clone(self):
        self.prepare(metadata=None)
        result = self.resolve("dev")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.kernel / "susfs").exists())

    def test_common_metadata_supports_quoted_branch(self):
        self.prepare(metadata="build.config.common")
        (self.kernel / "build.config.common").write_text('BRANCH="android14-6.1"\n', encoding="utf-8")
        result = self.resolve("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT:gki-android14-6.1-dev:", result.stdout)

    def test_metadata_is_parsed_without_execution(self):
        self.prepare()
        (self.kernel / "build.config.constants").write_text(
            'BRANCH=android14-6.1\ntouch injected\n', encoding="utf-8"
        )
        result = self.resolve("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.kernel / "injected").exists())

    def test_missing_matching_patch_is_rejected(self):
        self.prepare(missing_patch=True)
        result = self.resolve("dev")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malicious_branch_is_never_evaluated(self):
        self.prepare()
        result = self.resolve('gki-android14-6.1;touch injected')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.kernel / "injected").exists())


if __name__ == "__main__":
    unittest.main()
