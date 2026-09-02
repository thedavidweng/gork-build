"""Tests for `patchctl guard` and `patchctl fold`.

Each test builds a throwaway repository with a real two-patch queue: an
upstream base, a control commit carrying maint/, then one commit per patch
with its `Gork-Patch-Id` trailer. patchctl.py is copied into the fixture so
the versioned commit-msg hook resolves it the way it does in the real repo.

The property under test is the one that cost this repository a feature once:
a commit that changes the product tree without joining the queue is dropped
by the next `patchctl apply`, silently. `guard` must make that unreachable
and `fold` must make joining the queue a single command.

Run directly: python3 maint/scripts/tests/test_patchctl_guard_fold.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # maint/scripts
PATCHCTL = SCRIPTS / "patchctl.py"

APP_BASE = "fn app() {\n    upstream();\n}\n"
OTHER_BASE = "fn other() {\n    upstream();\n}\n"

CONTROL_FILES = """schema = 1
paths = ["maint"]
template_root = "maint/control"
"""

PATCHSET = """schema = 1

[[patch]]
id = "alpha"
file = "0001-alpha.patch"
critical = true
contracts = []

[[patch]]
id = "beta"
file = "0002-beta.patch"
critical = true
contracts = []
"""

LOCK = """schema = 1

repository = "https://example.invalid/upstream.git"
commit = "{commit}"
source_rev = "deadbeef"
version = "1.0.0"

patchset_revision = 1
patch_tip = ""
product_tip = ""
"""


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            # A developer's own guard config must not leak into the fixtures.
            "PATCHCTL_GUARD": "",
        }
    )
    env.pop("PATCHCTL_GUARD")
    return env


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, env=_env()
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


def patchctl(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "maint/scripts/patchctl.py"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env=_env(),
    )


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    # `--no-verify`: fixtures build history directly, the hook is exercised on
    # purpose in its own test.
    git(repo, "commit", "--no-verify", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class QueueRepo:
    """A repository whose history is a valid two-patch privacy queue."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "-q", "-b", "sync/test")
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.invalid")

        write(root, "app.rs", APP_BASE)
        write(root, "other.rs", OTHER_BASE)
        write(root, "README.md", "upstream readme\n")
        self.base = commit(root, "upstream base")

        write(root, "maint/control-files.toml", CONTROL_FILES)
        write(root, "maint/patchset.toml", PATCHSET)
        write(root, "maint/upstream.lock.toml", LOCK.format(commit=self.base))
        # An overlay-managed file: restored after every apply, so a commit
        # touching it is not an orphan even without a trailer.
        write(root, "maint/overlays/README.md", "fork readme\n")
        shutil.copytree(SCRIPTS, root / "maint/scripts")
        commit(root, "chore: control plane")

        write(root, "app.rs", APP_BASE.replace("upstream();", "alpha();"))
        self.alpha = commit(root, "alpha: patch one\n\nGork-Patch-Id: alpha")

        write(root, "other.rs", OTHER_BASE.replace("upstream();", "beta();"))
        self.beta = commit(root, "beta: patch two\n\nGork-Patch-Id: beta")

        exported = patchctl(root, "export", "--tip", "HEAD")
        assert exported.returncode == 0, exported.stdout + exported.stderr
        commit(root, "chore: export queue")

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def subjects(self) -> list[str]:
        out = git(self.root, "log", "--format=%s", f"{self.base}..HEAD").stdout
        return [line for line in out.splitlines() if line.strip()]

    def clean(self) -> bool:
        return not git(self.root, "status", "--porcelain").stdout.strip()


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = QueueRepo(Path(self.tmp.name) / "repo")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_queue_has_no_orphan(self) -> None:
        proc = patchctl(self.repo.root, "guard")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_control_and_overlay_commits_need_no_trailer(self) -> None:
        write(self.repo.root, "maint/notes.md", "control only\n")
        commit(self.repo.root, "chore: a control note")
        # README.md is mirrored by maint/overlays/README.md: the apply puts it
        # back, so it is not lost and must not be flagged.
        write(self.repo.root, "README.md", "fork readme, edited\n")
        commit(self.repo.root, "chore: overlay-managed file")
        proc = patchctl(self.repo.root, "guard")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_trailerless_product_commit_is_flagged(self) -> None:
        write(self.repo.root, "app.rs", APP_BASE + "// orphan\n")
        sha = commit(self.repo.root, "sneak a change past the queue")
        proc = patchctl(self.repo.root, "guard")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn(sha[:12], proc.stderr)
        self.assertIn("app.rs", proc.stderr)

    def test_unknown_trailer_id_is_flagged(self) -> None:
        write(self.repo.root, "app.rs", APP_BASE + "// typo\n")
        commit(self.repo.root, "gamma: nope\n\nGork-Patch-Id: gamma")
        proc = patchctl(self.repo.root, "guard")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("patchset.toml", proc.stderr)

    def test_hook_refuses_then_accepts(self) -> None:
        installed = patchctl(self.repo.root, "guard", "--install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

        write(self.repo.root, "app.rs", APP_BASE + "// blocked\n")
        git(self.repo.root, "add", "-A")
        refused = git(self.repo.root, "commit", "-m", "no trailer here", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("patchctl guard: refused", refused.stderr)

        accepted = git(
            self.repo.root,
            "commit",
            "-m",
            "alpha: with a trailer\n\nGork-Patch-Id: alpha",
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_hook_escape_hatch(self) -> None:
        patchctl(self.repo.root, "guard", "--install")
        write(self.repo.root, "app.rs", APP_BASE + "// deliberate\n")
        git(self.repo.root, "add", "-A")
        env = _env()
        env["PATCHCTL_GUARD"] = "0"
        proc = subprocess.run(
            ["git", "commit", "-m", "escape hatch"],
            cwd=self.repo.root,
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class FoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = QueueRepo(Path(self.tmp.name) / "repo")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_text(self, name: str) -> str:
        return (self.repo.root / "maint/patches" / name).read_text(encoding="utf-8")

    def test_fold_lands_the_change_in_the_target_patch(self) -> None:
        write(self.repo.root, "app.rs", APP_BASE.replace("upstream();", "alpha();\n    folded();"))
        proc = patchctl(self.repo.root, "fold", "alpha", "--no-lint")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertTrue(self.repo.clean(), "fold must leave a clean tree")
        # The change is inside the alpha commit, not on top of the queue.
        alpha_diff = git(
            self.repo.root, "show", "--format=", "-U0", ":/^alpha: patch one"
        ).stdout
        self.assertIn("folded()", alpha_diff)
        # And it reached the exported patch, which is what a resync replays.
        self.assertIn("folded()", self._patch_text("0001-alpha.patch"))
        # beta still comes after alpha, untouched.
        self.assertIn("beta: patch two", self.repo.subjects())
        self.assertNotIn("folded()", self._patch_text("0002-beta.patch"))
        # The queue is coherent again, and nothing is orphaned.
        self.assertEqual(patchctl(self.repo.root, "guard").returncode, 0)
        self.assertEqual(
            git(self.repo.root, "rev-parse", "--verify", "refs/patchctl/fold-backup").returncode,
            0,
            "the pre-fold tip must stay reachable",
        )

    def test_fold_folds_an_already_committed_wip(self) -> None:
        write(self.repo.root, "app.rs", APP_BASE.replace("upstream();", "alpha();\n    wip();"))
        commit(self.repo.root, "wip: not in the queue yet")
        before = len(self.repo.subjects())
        proc = patchctl(self.repo.root, "fold", "alpha", "--no-lint")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("wip()", self._patch_text("0001-alpha.patch"))
        self.assertNotIn("wip: not in the queue yet", self.repo.subjects())
        # The WIP commit is absorbed, not stacked: same count (it becomes the
        # re-export chore).
        self.assertEqual(len(self.repo.subjects()), before)

    def test_fold_refuses_a_file_owned_by_another_patch(self) -> None:
        tip_before = self.repo.head()
        write(self.repo.root, "other.rs", OTHER_BASE.replace("upstream();", "beta();\n    stray();"))
        proc = patchctl(self.repo.root, "fold", "alpha", "--no-lint")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("other.rs", proc.stderr)
        self.assertIn("beta", proc.stderr)
        self.assertIn("--allow-shared", proc.stderr)
        # Refusing must change nothing: the branch and the change stay put.
        self.assertEqual(self.repo.head(), tip_before)
        self.assertIn("stray()", (self.repo.root / "other.rs").read_text(encoding="utf-8"))

    def test_fold_refuses_a_change_that_mixes_control_plane(self) -> None:
        tip_before = self.repo.head()
        write(self.repo.root, "app.rs", APP_BASE + "// product side\n")
        write(self.repo.root, "maint/notes.md", "control side\n")
        proc = patchctl(self.repo.root, "fold", "alpha", "--no-lint")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("maint/notes.md", proc.stderr)
        # control-files.toml restores maint/ after an apply: a patch carrying it
        # would land the same change twice.
        self.assertEqual(self.repo.head(), tip_before)
        self.assertNotIn("control side", self._patch_text("0001-alpha.patch"))

    def test_fold_rejects_an_unknown_patch_id(self) -> None:
        write(self.repo.root, "app.rs", APP_BASE + "// x\n")
        proc = patchctl(self.repo.root, "fold", "nosuchpatch", "--no-lint")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("alpha", proc.stderr)
        self.assertIn("beta", proc.stderr)

    def test_fold_refuses_when_there_is_nothing_to_fold(self) -> None:
        proc = patchctl(self.repo.root, "fold", "alpha", "--no-lint")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("nothing to fold", proc.stderr)

    def test_abort_restores_the_pending_change_after_a_conflict(self) -> None:
        # beta rewrites the very line alpha owns, so replaying beta over a
        # rewritten alpha cannot merge.
        write(self.repo.root, "app.rs", APP_BASE.replace("upstream();", "beta_edit();"))
        commit(self.repo.root, "beta: also touches app\n\nGork-Patch-Id: beta")
        tip_before = self.repo.head()

        write(self.repo.root, "app.rs", APP_BASE.replace("upstream();", "folded_edit();"))
        proc = patchctl(self.repo.root, "fold", "alpha", "--allow-shared", "--no-lint")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("--abort", proc.stderr)

        aborted = patchctl(self.repo.root, "fold", "--abort")
        self.assertEqual(aborted.returncode, 0, aborted.stdout + aborted.stderr)
        self.assertEqual(
            git(self.repo.root, "rev-parse", "HEAD~1").stdout.strip(),
            tip_before,
            "abort must restore the branch, keeping the pending change as its tip",
        )
        self.assertIn("folded_edit()", (self.repo.root / "app.rs").read_text(encoding="utf-8"))
        self.assertTrue(self.repo.clean())


if __name__ == "__main__":
    unittest.main(verbosity=2)
