"""Tests for `patchctl apply --auto-resolve-conflicts`.

Each test builds a throwaway git repo: a locked upstream base, a one-patch
privacy queue whose patch conflicts with the new upstream commit, and a stub
conflict resolver injected via PATCHCTL_CONFLICT_RESOLVER_CMD. The resolver
protocol is region-based: the stub receives one prompt per conflict region and
must print only that region's replacement. The fixture contract greps for the
patch's guard, so a resolution that drops the privacy guarantee must fail the
contract gate, not just the merge.

Run directly: python3 maint/scripts/tests/test_patchctl_auto_resolve.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # maint/scripts
PATCHCTL = SCRIPTS / "patchctl.py"

# `fn other` is identical everywhere: it is never part of the conflict, so a
# resolution has no business touching it.
BASE_APP = """fn accept() {
    record_local();
    record_upstream();
}

fn other() {
    keep_me();
}
"""

PATCHED_APP = """fn accept() {
    record_local();
    if !privacy() {
        record_upstream();
    }
}

fn other() {
    keep_me();
}
"""

UPSTREAM_APP = """fn accept() {
    if has_account() {
        record_local();
    }
    record_upstream();
}

fn other() {
    keep_me();
}
"""

MERGED_APP = """fn accept() {
    if has_account() {
        record_local();
    }
    if !privacy() {
        record_upstream();
    }
}

fn other() {
    keep_me();
}
"""

# Replacement for the single conflict region of the fixture above.
GOOD_REGION = """    if has_account() {
        record_local();
    }
    if !privacy() {
        record_upstream();
    }
"""

# Committable (differs from upstream) but drops the privacy guard: only the
# contract gate can catch this one.
DROPS_GUARD_REGION = """    if has_account() {
        record_local();
    }
    // resolved
    record_upstream();
"""

# Two-region variant: `fn mid` separates two independent conflicts.
BASE_TWO = """fn alpha() {
    one();
}

fn mid() {
    stable_one();
    stable_two();
}

fn beta() {
    two();
}
"""

PATCHED_TWO = """fn alpha() {
    if !privacy() {
        one();
    }
}

fn mid() {
    stable_one();
    stable_two();
}

fn beta() {
    if !privacy() {
        two();
    }
}
"""

UPSTREAM_TWO = """fn alpha() {
    prepare();
    one_v2();
}

fn mid() {
    stable_one();
    stable_two();
}

fn beta() {
    prepare();
    two_v2();
}
"""

MERGED_TWO = """fn alpha() {
    prepare();
    if !privacy() {
        one_v2();
    }
}

fn mid() {
    stable_one();
    stable_two();
}

fn beta() {
    prepare();
    if !privacy() {
        two_v2();
    }
}
"""

REGION_ONE = """    prepare();
    if !privacy() {
        one_v2();
    }
"""

REGION_TWO = """    prepare();
    if !privacy() {
        two_v2();
    }
"""

VERSION_CARGO = '[package]\nname = "xai-grok-version"\nversion = "0.1.0"\n'

CONTRACTS_TOML = """schema = 1

[[contract]]
id = "guard-present"
group = "privacy"
min_tests = 1
command = [
  "sh", "-c",
  "grep -q 'if !privacy()' src/app.rs && echo 'test result: ok. 1 passed'",
]
"""


def sh_quote(text: str) -> str:
    return text.replace("'", "'\\''")


def patchset_toml(
    contracts: tuple[str, ...],
    auto_resolve: bool | None = None,
    critical: bool = True,
) -> str:
    ids = ", ".join(f'"{c}"' for c in contracts)
    opt_in = "" if auto_resolve is None else f"auto_resolve = {str(auto_resolve).lower()}\n"
    return (
        "schema = 1\n\n"
        "[[patch]]\n"
        'id = "guard-upstream"\n'
        'file = "0001-guard-upstream.patch"\n'
        f"critical = {str(critical).lower()}\n"
        f"contracts = [{ids}]\n"
        f"{opt_in}"
    )


class Fixture:
    def __init__(
        self,
        tmp: Path,
        *,
        contracts: tuple[str, ...] = ("guard-present",),
        auto_resolve: bool | None = None,
        critical: bool = True,
        base: str = BASE_APP,
        patched: str = PATCHED_APP,
        upstream: str = UPSTREAM_APP,
    ):
        self.auto_resolve = auto_resolve
        self.critical = critical
        self.tmp = tmp
        self.root = tmp / "repo"
        self.root.mkdir()
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
        )
        self._build(contracts, base, patched, upstream)

    def sh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            list(args), cwd=self.root, env=self.env, text=True, capture_output=True
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"fixture command failed: {args}\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.sh("git", *args, check=check)

    def _build(
        self, contracts: tuple[str, ...], base: str, patched: str, upstream: str
    ) -> None:
        self.git("init", "-b", "main")
        # Upstream base.
        cargo = self.root / "crates/codegen/xai-grok-version/Cargo.toml"
        cargo.parent.mkdir(parents=True)
        cargo.write_text(VERSION_CARGO, encoding="utf-8")
        app = self.root / "src/app.rs"
        app.parent.mkdir(parents=True)
        app.write_text(base, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD").stdout.strip()

        # The privacy patch, exported then reset away.
        app.write_text(patched, encoding="utf-8")
        self.git("add", "-A")
        self.git(
            "commit",
            "-m",
            "guard-upstream: keep acks local\n\nGork-Patch-Id: guard-upstream\n",
        )
        patch_out = self.tmp / "patch-out"
        self.git("format-patch", "-1", "HEAD", "-o", str(patch_out))
        exported = sorted(patch_out.glob("*.patch"))
        assert len(exported) == 1, exported
        self.git("reset", "--hard", self.base_sha)

        # The new upstream commit rewrites the same lines: guaranteed conflict.
        self.git("switch", "-c", "upstream-main")
        app.write_text(upstream, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "upstream rework")
        self.up_sha = self.git("rev-parse", "HEAD").stdout.strip()

        # Control plane on the work branch (where apply is launched from).
        self.git("switch", "-c", "work", "main")
        maint = self.root / "maint"
        patches = maint / "patches"
        patches.mkdir(parents=True)
        patch_file = patches / "0001-guard-upstream.patch"
        shutil.copy2(exported[0], patch_file)
        digest = hashlib.sha256(patch_file.read_bytes()).hexdigest()
        (patches / "SHA256SUMS").write_text(
            f"{digest}  0001-guard-upstream.patch\n", encoding="utf-8"
        )
        (patches / "series").write_text(
            "# test series\n0001-guard-upstream.patch\n", encoding="utf-8"
        )
        (maint / "patchset.toml").write_text(
            patchset_toml(contracts, self.auto_resolve, self.critical),
            encoding="utf-8",
        )
        (maint / "control-files.toml").write_text(
            'paths = ["maint"]\ntemplate_root = "maint/control"\n', encoding="utf-8"
        )
        (maint / "upstream.lock.toml").write_text(
            "schema = 1\n"
            'repository = "local"\n'
            f'commit = "{self.base_sha}"\n'
            'source_rev = ""\n'
            'version = "0.1.0"\n'
            "patchset_revision = 1\n"
            'patch_tip = ""\n',
            encoding="utf-8",
        )
        contracts_dir = maint / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "privacy-contract.toml").write_text(
            CONTRACTS_TOML, encoding="utf-8"
        )
        scripts_dir = maint / "scripts"
        scripts_dir.mkdir()
        shutil.copy2(SCRIPTS / "verify_privacy_contract.py", scripts_dir)
        self.git("add", "-A")
        self.git("commit", "-m", "control plane")

    def resolver(self, name: str, body: str) -> str:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def resolver_printing(self, name: str, content: str) -> str:
        return self.resolver(
            name,
            f"#!/bin/sh\ncat > /dev/null\nprintf '%s' '{sh_quote(content)}'\n",
        )

    def resolver_two_regions(self, name: str, first: str, second: str) -> str:
        return self.resolver(
            name,
            "#!/bin/sh\n"
            "input=$(cat)\n"
            'case "$input" in\n'
            f"  *'region 1 of 2'*) printf '%s' '{sh_quote(first)}' ;;\n"
            f"  *'region 2 of 2'*) printf '%s' '{sh_quote(second)}' ;;\n"
            "  *) exit 9 ;;\n"
            "esac\n",
        )

    def apply(
        self, resolver_cmd: str, *extra: str, auto_resolve: bool = True
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env, PATCHCTL_CONFLICT_RESOLVER_CMD=resolver_cmd)
        return subprocess.run(
            [
                sys.executable,
                str(PATCHCTL),
                "apply",
                "--upstream",
                self.up_sha,
                *(["--auto-resolve-conflicts"] if auto_resolve else []),
                *extra,
            ],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
        )

    def app_content(self) -> str:
        return (self.root / "src/app.rs").read_text(encoding="utf-8")

    def am_in_progress(self) -> bool:
        return (self.root / ".git/rebase-apply").exists()

    def status_json(self) -> dict:
        return json.loads(
            (self.root / "maint/last-apply-status.json").read_text(encoding="utf-8")
        )


class AutoResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="patchctl-autoresolve-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_resolves_conflict_and_passes_contracts(self) -> None:
        fx = Fixture(self.tmp)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(
            proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        self.assertEqual(fx.app_content(), MERGED_APP)
        subjects = fx.git("log", "--format=%s").stdout
        self.assertIn("guard-upstream: keep acks local", subjects)
        self.assertIn("AUTO-RESOLVED", proc.stdout)
        status = fx.status_json()
        self.assertEqual(len(status["auto_resolved"]), 1)
        self.assertEqual(
            status["auto_resolved"][0]["patch"], "0001-guard-upstream.patch"
        )
        self.assertEqual(status["auto_resolved"][0]["method"], "ai")

    def test_resolves_two_regions_in_one_file(self) -> None:
        fx = Fixture(
            self.tmp, base=BASE_TWO, patched=PATCHED_TWO, upstream=UPSTREAM_TWO
        )
        stub = fx.resolver_two_regions("resolver-two.sh", REGION_ONE, REGION_TWO)
        proc = fx.apply(stub)
        self.assertEqual(
            proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        self.assertEqual(fx.app_content(), MERGED_TWO)
        self.assertEqual(fx.status_json()["auto_resolved"][0]["method"], "ai")

    def test_rejects_output_with_conflict_markers(self) -> None:
        fx = Fixture(self.tmp)
        bad = fx.resolver_printing(
            "resolver-markers.sh",
            "<<<<<<< ours\n" + GOOD_REGION + ">>>>>>> theirs\n",
        )
        proc = fx.apply(bad)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertFalse(fx.am_in_progress())
        report = self.tmp / "repo/.git/grok-apply-conflict.diff"
        self.assertTrue(report.is_file())
        self.assertIn(
            "rejected resolver proposal", report.read_text(encoding="utf-8")
        )

    def test_rejects_full_file_output(self) -> None:
        # Even a CORRECT whole file must be rejected: the protocol demands the
        # region replacement only, and echoing content from outside the region
        # is how truncations and rewrites slip in.
        fx = Fixture(self.tmp)
        dumper = fx.resolver_printing("resolver-dumps-file.sh", MERGED_APP)
        proc = fx.apply(dumper)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("outside the region", proc.stderr)
        self.assertFalse(fx.am_in_progress())

    def test_resolver_failure_falls_back_to_fail_closed(self) -> None:
        fx = Fixture(self.tmp)
        failing = fx.resolver("resolver-fails.sh", "#!/bin/sh\nexit 7\n")
        proc = fx.apply(failing)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertFalse(fx.am_in_progress())
        self.assertIn("auto-resolve failed", proc.stderr)

    def test_contract_failure_fails_closed(self) -> None:
        # Resolver silently drops the privacy guard: the merge succeeds but the
        # contract gate must catch it.
        fx = Fixture(self.tmp)
        dropper = fx.resolver_printing(
            "resolver-drops-guard.sh", DROPS_GUARD_REGION
        )
        proc = fx.apply(dropper)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("contract", (proc.stdout + proc.stderr).lower())
        self.assertFalse(fx.am_in_progress())

    def test_no_contracts_disables_ai_resolution(self) -> None:
        fx = Fixture(self.tmp, contracts=())
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("no contracts", proc.stdout + proc.stderr)
        self.assertFalse(fx.am_in_progress())

    def test_rerere_replays_even_with_the_resolver_off(self) -> None:
        # The split that matters: turning the resolver off must not cost the
        # repository the resolutions it already recorded. Replaying one asks no
        # model, so it happens whatever the run's settings.
        fx = Fixture(self.tmp)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        first = fx.apply(good)
        self.assertEqual(first.returncode, 0, first.stderr)
        # Second run without --auto-resolve-conflicts, and with a resolver that
        # would fail if it were ever called.
        failing = fx.resolver("resolver-fails.sh", "#!/bin/sh\nexit 7\n")
        second = fx.apply(failing, "--force", auto_resolve=False)
        self.assertEqual(
            second.returncode, 0, f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
        )
        self.assertEqual(fx.app_content(), MERGED_APP)
        self.assertEqual(fx.status_json()["auto_resolved"][0]["method"], "rerere")

    def test_unseen_conflict_is_left_alone_with_the_resolver_off(self) -> None:
        # Nothing recorded, resolver off: the conflict goes to the maintainer.
        fx = Fixture(self.tmp)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good, auto_resolve=False)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("--no-auto-resolve", proc.stdout + proc.stderr)

    def test_auto_resolve_opt_in_allows_a_contractless_patch(self) -> None:
        # `auto_resolve = true` is the maintainer taking responsibility for a
        # patch no contract can vouch for: the resolution lands, and the run
        # says out loud that nothing proved it.
        fx = Fixture(self.tmp, contracts=(), auto_resolve=True)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(fx.app_content(), MERGED_APP)
        record = fx.status_json()["auto_resolved"][0]
        self.assertEqual(record["method"], "ai")
        self.assertTrue(record["unverified"])
        self.assertIn("no contract", proc.stdout + proc.stderr)

    def test_trailing_patch_is_skipped_without_the_opt_in(self) -> None:
        # Baseline: a non-critical trailing patch conflicts and is dropped.
        fx = Fixture(self.tmp, contracts=(), critical=False)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("SKIP non-critical trailing patch", proc.stdout)

    def test_trailing_patch_opted_in_is_resolved_not_skipped(self) -> None:
        # The branding case: skipping used to happen before the resolver was
        # ever consulted, which made `auto_resolve = true` a dead letter here.
        fx = Fixture(self.tmp, contracts=(), critical=False, auto_resolve=True)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(fx.app_content(), MERGED_APP)
        self.assertNotIn("SKIP non-critical trailing patch", proc.stdout)
        self.assertTrue(fx.status_json()["auto_resolved"][0]["unverified"])

    def test_trailing_patch_opted_in_still_skips_when_resolution_fails(self) -> None:
        # Opting in must not turn a skippable patch into a blocker.
        fx = Fixture(self.tmp, contracts=(), critical=False, auto_resolve=True)
        failing = fx.resolver("resolver-fails.sh", "#!/bin/sh\nexit 7\n")
        proc = fx.apply(failing)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("SKIP non-critical trailing patch", proc.stdout)

    def test_auto_resolve_false_keeps_a_contracted_patch_manual(self) -> None:
        # The opt-out direction: contracts are declared, but a human read is
        # the point, so the resolver must not run.
        fx = Fixture(self.tmp, auto_resolve=False)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        proc = fx.apply(good)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("auto_resolve = false", proc.stdout + proc.stderr)
        self.assertFalse(fx.am_in_progress())

    def test_rerere_replays_recorded_resolution(self) -> None:
        fx = Fixture(self.tmp)
        good = fx.resolver_printing("resolver-good.sh", GOOD_REGION)
        first = fx.apply(good)
        self.assertEqual(first.returncode, 0, first.stderr)
        # Second run from scratch: the resolver is broken on purpose — only the
        # recorded resolution can succeed.
        failing = fx.resolver("resolver-fails.sh", "#!/bin/sh\nexit 7\n")
        second = fx.apply(failing, "--force")
        self.assertEqual(
            second.returncode, 0, f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
        )
        self.assertEqual(fx.app_content(), MERGED_APP)
        self.assertEqual(fx.status_json()["auto_resolved"][0]["method"], "rerere")


# Modelled on the real blocker: `AuthManager` predicate tests, where the only
# correct answer to a conflict region is an `assert!(…)` line the same test
# also uses outside the region. Those lines recur by construction.
PREDICATE_COMMONS = [
    (
        "fn manager_predicates() {\n"
        "    let dir = tempfile::tempdir().unwrap();\n"
        "    let mgr = Arc::new(AuthManager::new(dir.path(), Config::default()));\n"
    ),
    (
        "    mgr.hot_swap(GrokAuth {\n"
        "        coding_data_retention_opt_out: true,\n"
        "        ..GrokAuth::test_default()\n"
        "    });\n"
        "    assert!(mgr.is_data_collection_disabled());\n"
        "    assert!(!mgr.allows_data_collection());\n"
        "    mgr.hot_swap(GrokAuth::test_default());\n"
        "    mgr.clear_in_memory();\n"
        "    assert!(!mgr.allows_data_collection());\n"
        "    assert!(mgr.is_data_collection_disabled());\n"
        "}\n"
    ),
]

# What the resolver correctly proposes for one region of that test: two lines
# the test also uses outside the region, because they are what the region says.
PREDICATE_GOOD_REGION = """    mgr.hot_swap(GrokAuth::test_default());
    assert!(mgr.is_data_collection_disabled());
    assert!(
        !mgr.allows_data_collection(),
        "normal user must not allow research collection"
    );
"""

# The second real blocker: the fork's install chokepoint, repeated verbatim at
# every install path. Re-inserting it is the whole point of the patch.
CHOKEPOINT = (
    "    if vendor_auto_update_forbidden() {\n"
    "        return Err(vendor_update_blocked_err());\n"
    "    }\n"
)

CHOKEPOINT_COMMONS = [
    "fn install_from_npm() -> Result<()> {\n" + CHOKEPOINT + "    npm_install()\n}\n",
    "fn install_from_release() -> Result<()> {\n"
    + CHOKEPOINT
    + "    release_install()\n}\n",
    "fn install_from_leader() -> Result<()> {\n"
    + CHOKEPOINT
    + "    leader_install()\n}\n",
]

ECHOING_COMMONS = [
    "fn accept() {\n",
    "}\n\nfn other() {\n    keep_me();\n}\n",
]


class DumpGuardTests(unittest.TestCase):
    """Unit coverage for `replacement_echoes_common`, the dump guard.

    The guard exists to catch a resolver that answers with the whole file
    instead of the region. Keying it on single echoed lines made whole classes
    of conflict unresolvable — the resolver proposed the right text and the
    guard refused it — so every upstream sync fell back to a manual port.
    """

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("patchctl", PATCHCTL)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["patchctl"] = mod
        spec.loader.exec_module(mod)
        cls.patchctl = mod

    def echoes(self, commons: list[str], replacement: str) -> bool:
        return self.patchctl.replacement_echoes_common(commons, replacement)

    def test_single_echoed_line_is_allowed(self) -> None:
        self.assertFalse(
            self.echoes(
                PREDICATE_COMMONS,
                "    assert!(mgr.is_data_collection_disabled());\n",
            )
        )

    def test_scattered_echoed_lines_are_allowed(self) -> None:
        self.assertFalse(self.echoes(PREDICATE_COMMONS, PREDICATE_GOOD_REGION))

    def test_repeated_chokepoint_idiom_is_allowed(self) -> None:
        # Re-inserting the guard at one more install path repeats three lines
        # that, by design, already stand at every other install path.
        self.assertFalse(
            self.echoes(CHOKEPOINT_COMMONS, CHOKEPOINT + "    internal_install()\n")
        )

    def test_whole_file_dump_is_rejected(self) -> None:
        self.assertTrue(self.echoes(ECHOING_COMMONS, MERGED_APP))

    def test_dump_of_a_larger_file_is_rejected(self) -> None:
        self.assertTrue(self.echoes(PREDICATE_COMMONS, "".join(PREDICATE_COMMONS)))

    def test_commons_without_distinctive_text_never_reject(self) -> None:
        # Near-empty lines say nothing about where a replacement came from.
        self.assertFalse(self.echoes(["}\n\n}\n\n}\n"], "}\n\n}\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
