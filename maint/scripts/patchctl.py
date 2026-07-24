#!/usr/bin/env python3
"""Gork Build patch control.

Commands: detect, export, apply, verify, report, roundtrip, bootstrap-stack,
lint, finalize-sync.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


TRAILER_ID = "Gork-Patch-Id"
TRAILER_INVARIANT = "Gork-Invariant"
TRAILER_RISK = "Gork-Risk"

EXCLUDE_PATCH_IDS = frozenset({"cargo-lock", "overlays", "control-metadata"})

BOOTSTRAP_GROUPS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "privacy-core",
        "privacy-core: PRIVACY_BUILD and product identity",
        "privacy-build-enabled",
        "critical",
        ["crates/codegen/xai-grok-version/"],
    ),
    (
        "telemetry-hard-off",
        "telemetry-hard-off: Mixpanel and product telemetry no-ops",
        "product-telemetry-disabled",
        "critical",
        [
            "crates/codegen/xai-mixpanel/",
            "crates/codegen/xai-grok-telemetry/",
        ],
    ),
    (
        "research-upload-hard-off",
        "research-upload-hard-off: resolver gates for trace/research upload",
        "research-upload-unreachable",
        "critical",
        ["crates/codegen/xai-grok-shell/src/agent/config.rs"],
    ),
    (
        "retention-opt-out",
        "retention-opt-out: lock coding-data retention to opt-out",
        "retention-locked-opt-out",
        "critical",
        [
            "crates/codegen/xai-grok-pager/src/settings/defs.rs",
            "crates/codegen/xai-grok-pager/src/settings/registry.rs",
            "crates/codegen/xai-grok-pager/src/slash/commands/privacy.rs",
            "crates/codegen/xai-grok-shell/src/extensions/privacy.rs",
            "crates/codegen/xai-grok-shell/src/auth/model.rs",
            "crates/codegen/xai-grok-shell/src/auth/manager.rs",
            "crates/codegen/xai-grok-shell/src/auth/manager_tests.rs",
            "crates/codegen/xai-grok-pager/tests/settings_e2e.rs",
        ],
    ),
    (
        "vendor-updater-hard-off",
        "vendor-updater-hard-off: install chokepoint and leader/min-version gates",
        "vendor-install-unreachable",
        "critical",
        ["crates/codegen/xai-grok-update/"],
    ),
    (
        "privacy-contract-tests",
        "privacy-contract-tests: resolver and privacy regression tests",
        "privacy-contracts-present",
        "critical",
        ["crates/codegen/xai-grok-shell/tests/privacy_resolvers.rs"],
    ),
    (
        "egress-guard",
        "egress-guard: release binary network egress smoke",
        "egress-denylist-enforced",
        "critical",
        [
            "scripts/privacy_egress_check.sh",
            "scripts/privacy_egress_proxy.py",
        ],
    ),
    (
        "supply-chain-policy",
        "supply-chain-policy: cargo audit hard gate",
        "cargo-audit-policy",
        "critical",
        [".cargo/audit.toml"],
    ),
    (
        "product-identity",
        "product-identity: CLI binary name and entry surface",
        "product-cli-gork",
        "medium",
        [
            "crates/codegen/xai-grok-pager-bin/",
        ],
    ),
    (
        "package-publishing",
        "package-publishing: npm package metadata",
        "npm-community-packages",
        "medium",
        ["crates/codegen/xai-grok-pager/npm/"],
    ),
]


def repo_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path(__file__).resolve().parents[2]


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    # CI runners often have no git identity; git am/commit need one.
    merged.setdefault("GIT_AUTHOR_NAME", "Gork CI")
    merged.setdefault("GIT_AUTHOR_EMAIL", "gork-ci@users.noreply.github.com")
    merged.setdefault("GIT_COMMITTER_NAME", merged["GIT_AUTHOR_NAME"])
    merged.setdefault("GIT_COMMITTER_EMAIL", merged["GIT_AUTHOR_EMAIL"])
    if env:
        merged.update(env)
    proc = subprocess.run(
        args,
        cwd=cwd or repo_root(),
        check=False,
        text=True,
        capture_output=capture,
        env=merged,
    )
    if check and proc.returncode != 0:
        if capture:
            sys.stderr.write(proc.stderr or proc.stdout or "")
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(args)}")
    return proc


def git(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], **kwargs)


def git_resolvable(root: Path, rev: str) -> bool:
    if not rev:
        return False
    proc = git(
        ["cat-file", "-e", f"{rev}^{{commit}}"],
        cwd=root,
        check=False,
        capture=True,
    )
    return proc.returncode == 0


def git_is_ancestor(root: Path, maybe_ancestor: str, rev: str) -> bool:
    if not maybe_ancestor or not rev:
        return False
    proc = git(
        ["merge-base", "--is-ancestor", maybe_ancestor, rev],
        cwd=root,
        check=False,
        capture=True,
    )
    return proc.returncode == 0


@dataclass
class UpstreamLock:
    schema: int
    repository: str
    commit: str
    source_rev: str
    version: str
    patchset_revision: int
    patch_tip: str
    # Full product tree after patches + overlays (+ optional skipped branding).
    product_tip: str = ""
    patch_ref: str = ""

    @classmethod
    def load(cls, path: Path) -> UpstreamLock:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            schema=int(data.get("schema", 1)),
            repository=str(data["repository"]),
            commit=str(data["commit"]),
            source_rev=str(data.get("source_rev") or ""),
            version=str(data["version"]),
            patchset_revision=int(data.get("patchset_revision", 1)),
            patch_tip=str(data.get("patch_tip") or ""),
            product_tip=str(data.get("product_tip") or ""),
            patch_ref=str(data.get("patch_ref") or ""),
        )

    def write(self, path: Path) -> None:
        lines = [
            "# Locked upstream base for the Gork privacy patch queue.",
            f"schema = {self.schema}",
            "",
            f'repository = "{self.repository}"',
            f'commit = "{self.commit}"',
            f'source_rev = "{self.source_rev}"',
            f'version = "{self.version}"',
            "",
            f"patchset_revision = {self.patchset_revision}",
            f'patch_tip = "{self.patch_tip}"',
            f'product_tip = "{self.product_tip}"',
        ]
        if self.patch_ref:
            lines.append(f'patch_ref = "{self.patch_ref}"')
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")


def load_patchset(path: Path) -> list[dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return list(data.get("patch") or [])


def load_control_files(root: Path) -> dict:
    path = root / "maint/control-files.toml"
    if not path.is_file():
        return {
            "paths": ["maint"],
            "template_root": "maint/control",
        }
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_lock_policy(root: Path) -> dict:
    path = root / "maint/lock-policy.toml"
    if not path.is_file():
        return {"mode": "inherit-upstream", "post_apply_commands": []}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def patches_dir(root: Path) -> Path:
    return root / "maint" / "patches"


def series_path(root: Path) -> Path:
    return patches_dir(root) / "series"


def read_series(root: Path) -> list[str]:
    sp = series_path(root)
    if not sp.is_file():
        return []
    out: list[str] = []
    for line in sp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def write_series(root: Path, files: list[str]) -> None:
    body = "# Gork privacy patch series (apply order)\n" + "\n".join(files) + "\n"
    series_path(root).write_text(body, encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(root: Path, files: list[str]) -> None:
    lines = [f"{sha256_file(patches_dir(root) / name)}  {name}" for name in files]
    (patches_dir(root) / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_sha256sums(root: Path) -> None:
    sums = patches_dir(root) / "SHA256SUMS"
    if not sums.is_file():
        raise SystemExit("missing maint/patches/SHA256SUMS")
    listed: set[str] = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(None, 1)
        name = name.lstrip("*").strip()
        listed.add(name)
        p = patches_dir(root) / name
        if not p.is_file():
            raise SystemExit(f"missing patch file listed in SHA256SUMS: {name}")
        actual = sha256_file(p)
        if actual != digest:
            raise SystemExit(f"SHA256 mismatch for {name}: expected {digest}, got {actual}")
    extras = {p.name for p in patches_dir(root).glob("*.patch")} - listed
    if extras:
        raise SystemExit(f"patch files not listed in SHA256SUMS: {sorted(extras)}")


def resolve_upstream_meta(root: Path, sha: str) -> tuple[str, str]:
    version = ""
    vt = run(
        ["git", "show", f"{sha}:crates/codegen/xai-grok-version/Cargo.toml"],
        cwd=root,
        check=False,
        capture=True,
    )
    if vt.returncode == 0:
        m = re.search(r'^version\s*=\s*"([^"]+)"', vt.stdout, re.M)
        if m:
            version = m.group(1)
    source_rev = ""
    sr = run(
        ["git", "show", f"{sha}:SOURCE_REV"],
        cwd=root,
        check=False,
        capture=True,
    )
    if sr.returncode == 0 and sr.stdout.strip():
        source_rev = sr.stdout.strip().splitlines()[0].strip()
    return version, source_rev


def commits_with_patch_id(root: Path, base: str, tip: str) -> list[tuple[str, str]]:
    log = git(
        ["log", "--reverse", "--format=%H%x00%B%x00", f"{base}..{tip}"],
        cwd=root,
        capture=True,
    )
    results: list[tuple[str, str]] = []
    parts = log.stdout.split("\0")
    i = 0
    while i + 1 < len(parts):
        sha = parts[i].strip()
        body = parts[i + 1]
        i += 2
        if not sha:
            continue
        m = re.search(rf"^{TRAILER_ID}:\s*(\S+)\s*$", body, re.M)
        if not m:
            continue
        results.append((sha, m.group(1)))
    return results


def control_path_allowed(path: str, control_cfg: dict) -> bool:
    if path == "maint" or path.startswith("maint/"):
        return True
    for p in control_cfg.get("paths") or []:
        if path == p or path.startswith(str(p).rstrip("/") + "/"):
            return True
    return False


def snapshot_control_plane(root: Path, dest: Path) -> None:
    """Copy control plane into dest for restore after checkout of pure upstream."""
    cfg = load_control_files(root)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in cfg.get("paths") or ["maint"]:
        src = root / rel
        if not src.exists():
            print(f"warning: control path missing on control tree: {rel}", file=sys.stderr)
            continue
        target = dest / rel
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def install_control_plane(root: Path, snapshot: Path) -> list[str]:
    """Restore control plane onto root from snapshot + optional templates."""
    cfg = load_control_files(root if (root / "maint/control-files.toml").is_file() else snapshot)
    # Prefer config from snapshot maint
    snap_cfg_path = snapshot / "maint/control-files.toml"
    if snap_cfg_path.is_file():
        cfg = tomllib.loads(snap_cfg_path.read_text(encoding="utf-8"))

    restored: list[str] = []
    for rel in cfg.get("paths") or ["maint"]:
        src = snapshot / rel
        if not src.exists():
            continue
        target = root / rel
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        restored.append(rel)
        print(f"control: restored {rel}")

    # Templates under maint/control/ overwrite live paths after restore
    template_root = cfg.get("template_root") or "maint/control"
    # template lives inside restored maint
    tmpl = root / template_root
    if tmpl.is_dir():
        for path in tmpl.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(tmpl)
            # skip nested maint/control copies of themselves
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            restored.append(str(rel))
            print(f"control template: {rel}")
    return restored


def apply_overlays(root: Path) -> None:
    overlay = root / "maint" / "overlays"
    if not overlay.is_dir():
        return
    for path in overlay.rglob("*"):
        if path.is_file():
            rel = path.relative_to(overlay)
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            print(f"overlay: {rel}")


def run_lock_policy(root: Path) -> None:
    policy = load_lock_policy(root)
    for cmd in policy.get("post_apply_commands") or []:
        print(f"lock-policy: {' '.join(cmd)}")
        proc = subprocess.run(list(cmd), cwd=root)
        if proc.returncode != 0:
            raise SystemExit(f"lock-policy command failed: {cmd}")
    for cmd in policy.get("cargo_update_pins") or []:
        print(f"lock-policy pin: {' '.join(cmd)}")
        proc = subprocess.run(list(cmd), cwd=root)
        if proc.returncode != 0:
            raise SystemExit(f"lock-policy pin failed: {cmd}")


def patch_meta_by_file(root: Path) -> dict[str, dict]:
    """Map series filename -> patchset entry."""
    out: dict[str, dict] = {}
    for p in load_patchset(root / "maint/patchset.toml"):
        out[p["file"]] = p
    return out


def is_trailing_skippable(series: list[str], index: int, meta: dict[str, dict]) -> bool:
    """True if series[index:] are all non-critical (may skip from here)."""
    for name in series[index:]:
        entry = meta.get(name)
        if entry is None:
            # unknown patch treated as critical
            return False
        if entry.get("critical", True):
            return False
    return True


# ── commands ────────────────────────────────────────────────────────────────


def cmd_detect(args: argparse.Namespace) -> int:
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    remote_url = args.repository or lock.repository

    ls = run(
        ["git", "ls-remote", remote_url, "refs/heads/main"],
        cwd=root,
        capture=True,
        check=False,
    )
    if ls.returncode != 0:
        print(ls.stderr, file=sys.stderr)
        return 1
    line = (ls.stdout or "").strip().splitlines()
    if not line:
        print("upstream main not found via ls-remote", file=sys.stderr)
        return 1
    remote_sha = line[0].split()[0]

    fetch = run(
        ["git", "fetch", "--no-tags", remote_url, remote_sha],
        cwd=root,
        check=False,
        capture=True,
    )
    if fetch.returncode != 0:
        print(fetch.stderr, file=sys.stderr)
        return 1

    version, source_rev = resolve_upstream_meta(root, remote_sha)
    print(f"lock.repository = {lock.repository}")
    print(f"lock.commit     = {lock.commit}")
    print(f"lock.version    = {lock.version}")
    print(f"lock.source_rev = {lock.source_rev or '(empty)'}")
    print(f"lock.patch_tip  = {lock.patch_tip or '(empty)'}")
    print("---")
    print(f"remote.main     = {remote_sha}")
    print(f"remote.version  = {version or '(unknown)'}")
    print(f"remote.source_rev = {source_rev or '(empty)'}")

    same = (
        remote_sha == lock.commit
        and version == lock.version
        and source_rev == lock.source_rev
    )
    if same:
        print("status: up-to-date")
        return 0
    print("status: drift")
    return 2


def validated_functional_commits(
    root: Path, base_sha: str, tip_sha: str
) -> list[tuple[str, str]]:
    """Return ordered unique (sha, id) for functional commits; hard-fail on issues."""
    commits = commits_with_patch_id(root, base_sha, tip_sha)
    functional = [(sha, pid) for sha, pid in commits if pid not in EXCLUDE_PATCH_IDS]
    if not functional:
        raise SystemExit(
            f"no commits with {TRAILER_ID} in {base_sha[:12]}..{tip_sha[:12]}"
        )

    seen: dict[str, str] = {}
    ordered: list[tuple[str, str]] = []
    for sha, pid in functional:
        if pid in seen:
            raise SystemExit(
                f"duplicate {TRAILER_ID}={pid}: {seen[pid][:12]} and {sha[:12]}"
            )
        seen[pid] = sha
        ordered.append((sha, pid))

    patchset = load_patchset(root / "maint/patchset.toml")
    manifest_ids = [p["id"] for p in patchset]
    if len(manifest_ids) != len(set(manifest_ids)):
        raise SystemExit("duplicate patch ids in patchset.toml")
    critical_ids = {p["id"] for p in patchset if p.get("critical", True)}
    by_id = {pid: sha for sha, pid in ordered}

    missing_critical = [pid for pid in manifest_ids if pid in critical_ids and pid not in by_id]
    if missing_critical:
        raise SystemExit(
            f"critical manifest patch ids missing from commit trailers: {missing_critical}"
        )

    extra = [pid for pid in by_id if pid not in manifest_ids]
    if extra:
        raise SystemExit(
            f"commit trailers not listed in patchset.toml (add them or exclude): {extra}"
        )

    # Topology: commits must be a prefix of manifest order (trailing non-critical
    # may be absent after skip-on-conflict apply).
    ordered_ids = [pid for _, pid in ordered]
    if ordered_ids != manifest_ids[: len(ordered_ids)]:
        raise SystemExit(
            "commit trailer order is not a prefix of patchset.toml order:\n"
            f"  commits:  {ordered_ids}\n"
            f"  manifest: {manifest_ids}"
        )
    # Missing entries after the prefix must all be non-critical
    missing_tail = manifest_ids[len(ordered_ids) :]
    for pid in missing_tail:
        entry = next(p for p in patchset if p["id"] == pid)
        if entry.get("critical", True):
            raise SystemExit(
                f"missing patch {pid} is critical; only trailing non-critical may be absent"
            )

    # After last functional commit, only control files allowed up to tip
    functional_tip = ordered[-1][0]
    control_cfg = load_control_files(root)
    if functional_tip != tip_sha:
        names = git(
            ["diff", "--name-only", functional_tip, tip_sha],
            cwd=root,
            capture=True,
        ).stdout.splitlines()
        bad = [n for n in names if n and not control_path_allowed(n, control_cfg)]
        if bad:
            raise SystemExit(
                "non-control files after functional tip "
                f"{functional_tip[:12]}..{tip_sha[:12]}:\n  "
                + "\n  ".join(bad)
            )

    return ordered


def cmd_export(args: argparse.Namespace) -> int:
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    base = args.base or lock.commit
    tip = args.tip or lock.patch_tip or "HEAD"
    if not tip:
        print("patch_tip is empty; pass --tip or set lock.patch_tip", file=sys.stderr)
        return 2

    tip_sha = git(["rev-parse", tip], cwd=root, capture=True).stdout.strip()
    base_sha = git(["rev-parse", base], cwd=root, capture=True).stdout.strip()

    ordered = validated_functional_commits(root, base_sha, tip_sha)
    patchset = load_patchset(root / "maint/patchset.toml")
    id_to_file = {p["id"]: p["file"] for p in patchset}
    by_id = {pid: sha for sha, pid in ordered}
    # Export only commits present (prefix of manifest; trailing non-critical may be absent)
    export_ids = [pid for _, pid in ordered]

    out_dir = patches_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.patch"):
        old.unlink()

    series_files: list[str] = []
    for pid in export_ids:
        sha = by_id[pid]
        fname = id_to_file[pid]
        proc = subprocess.run(
            [
                "git",
                "format-patch",
                "-1",
                sha,
                "--stdout",
                "--zero-commit",
                "--full-index",
                "--binary",
                f"--base={base_sha}",
            ],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            sys.stderr.buffer.write(proc.stderr)
            raise SystemExit(f"format-patch failed for {pid} ({sha[:12]})")
        (out_dir / fname).write_bytes(proc.stdout)
        series_files.append(fname)
        print(f"exported {pid} -> {fname} ({sha[:12]})")

    write_series(root, series_files)
    write_sha256sums(root, series_files)

    functional_tip = ordered[-1][0]
    lock.patch_tip = functional_tip
    lock.write(root / "maint/upstream.lock.toml")
    print(f"updated lock.patch_tip = {functional_tip} (functional tip, not request tip)")
    if functional_tip != tip_sha:
        print(f"note: requested tip {tip_sha[:12]} has control commits after functional tip")
    print(f"series ({len(series_files)} patches) written to maint/patches/")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    upstream_sha = args.upstream
    if not upstream_sha:
        print("--upstream SHA is required", file=sys.stderr)
        return 2

    verify_sha256sums(root)
    series = read_series(root)
    if not series:
        print("empty maint/patches/series", file=sys.stderr)
        return 1

    meta = patch_meta_by_file(root)
    git(["cat-file", "-e", f"{upstream_sha}^{{commit}}"], cwd=root)

    version, source_rev = resolve_upstream_meta(root, upstream_sha)
    if args.expect_version and args.expect_version != version:
        print(
            f"version mismatch: expected {args.expect_version} got {version}",
            file=sys.stderr,
        )
        return 1
    if args.expect_source_rev is not None and args.expect_source_rev != source_rev:
        print(
            f"SOURCE_REV mismatch: expected {args.expect_source_rev!r} got {source_rev!r}",
            file=sys.stderr,
        )
        return 1

    short = upstream_sha[:7]
    branch = args.branch or f"sync/upstream-{(version or 'unknown').replace('/', '-')}-{short}"

    control_tmp = Path(tempfile.mkdtemp(prefix="gork-control-"))
    try:
        snapshot_control_plane(root, control_tmp)
        patch_src = control_tmp / "maint" / "patches"

        git(["checkout", "--detach", upstream_sha], cwd=root)
        exists = git(
            ["show-ref", "--verify", f"refs/heads/{branch}"],
            cwd=root,
            check=False,
            capture=True,
        )
        if exists.returncode == 0:
            if not args.force:
                print(
                    f"branch {branch} already exists (pass --force to replace)",
                    file=sys.stderr,
                )
                return 1
            git(["branch", "-D", branch], cwd=root)
        git(["switch", "-c", branch], cwd=root)

        install_control_plane(root, control_tmp)

        applied: list[str] = []
        skipped: list[str] = []
        conflicted: str | None = None

        for idx, patch_name in enumerate(series):
            patch_file = patch_src / patch_name
            if not patch_file.is_file():
                print(f"missing patch: {patch_file}", file=sys.stderr)
                return 1
            print(f"am: {patch_name}")
            proc = git(
                ["am", "--3way", str(patch_file)],
                cwd=root,
                check=False,
                capture=True,
            )
            if proc.returncode == 0:
                applied.append(patch_name)
                continue

            print(proc.stdout or "", end="")
            print(proc.stderr or "", end="", file=sys.stderr)
            git(["am", "--abort"], cwd=root, check=False)

            entry = meta.get(patch_name, {})
            critical = entry.get("critical", True)
            if critical or not is_trailing_skippable(series, idx, meta):
                conflicted = patch_name
                print(
                    f"CONFLICT on critical/non-trailing patch {patch_name}; fail-closed",
                    file=sys.stderr,
                )
                print(
                    f"branch={branch} applied={applied} "
                    f"skipped={skipped} conflicted={conflicted}"
                )
                # still leave control plane on disk for debugging
                return 3

            # Trailing non-critical: skip this and remaining non-critical
            print(f"SKIP non-critical trailing patch {patch_name}")
            skipped.append(patch_name)
            for rest in series[idx + 1 :]:
                print(f"SKIP non-critical trailing patch {rest}")
                skipped.append(rest)
            break

        apply_overlays(root)
        try:
            run_lock_policy(root)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            # non-fatal for lock policy metadata-only if cargo missing? keep hard fail
            return 1

        # Stage control plane + overlays.
        # Remove CI-only artifacts written to the worktree (e.g. upstream-sensitive.json
        # from upstream-replay.yml) so they never enter the product tree — otherwise
        # finalize-sync roundtrip reports a spurious tree mismatch.
        for artifact in ("upstream-sensitive.json",):
            p = root / artifact
            if p.exists():
                p.unlink()
        git(["add", "-A"], cwd=root)
        status = git(["status", "--porcelain"], cwd=root, capture=True)
        if status.stdout.strip():
            body = (
                "chore(sync): restore control plane and overlays\n\n"
                f"{TRAILER_ID}: control-metadata\n"
                f"{TRAILER_RISK}: low\n"
            )
            if skipped:
                body += f"\nSkipped non-critical patches: {', '.join(skipped)}\n"
            git(["commit", "-m", body], cwd=root)

        # Write status for CI
        status_path = root / "maint" / "last-apply-status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "branch": branch,
                    "upstream": upstream_sha,
                    "version": version,
                    "source_rev": source_rev,
                    "applied": applied,
                    "skipped": skipped,
                    "conflicted": conflicted,
                    "branding_required": bool(skipped),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git(["add", "maint/last-apply-status.json"], cwd=root, check=False)
        st2 = git(["status", "--porcelain", "--", "maint/last-apply-status.json"], cwd=root, capture=True)
        if st2.stdout.strip():
            git(
                [
                    "commit",
                    "-m",
                    f"chore(sync): record apply status\n\n{TRAILER_ID}: control-metadata\n",
                ],
                cwd=root,
                check=False,
            )

        print(
            f"applied {len(applied)} patches on {branch} "
            f"(upstream {upstream_sha[:12]}); skipped={skipped}"
        )
        print(
            f"upstream version={version or '?'} "
            f"source_rev={source_rev or '(empty)'}"
        )
        if args.verify:
            rc = cmd_verify(
                argparse.Namespace(skip_expensive=args.skip_expensive, only=[])
            )
            if rc != 0:
                return rc
        # Exit 4 signals branding/non-critical skips (still success for draft PR)
        if skipped:
            return 4
        return 0
    finally:
        shutil.rmtree(control_tmp, ignore_errors=True)


def cmd_verify(args: argparse.Namespace) -> int:
    root = repo_root()
    script = root / "maint/scripts/verify_privacy_contract.py"
    cmd = [sys.executable, str(script)]
    if args.skip_expensive:
        cmd.append("--skip-expensive")
    for only in args.only or []:
        cmd.extend(["--only", only])
    for g in getattr(args, "exclude_group", None) or []:
        cmd.extend(["--exclude-group", g])
    for g in getattr(args, "only_group", None) or []:
        cmd.extend(["--only-group", g])
    return subprocess.run(cmd, cwd=root).returncode


def cmd_report(args: argparse.Namespace) -> int:
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    old = args.old or lock.commit
    new = args.new
    if not new:
        print("--new SHA is required", file=sys.stderr)
        return 2
    script = root / "maint/scripts/upstream_diff_report.py"
    cmd = [sys.executable, str(script), old, new]
    if args.json:
        cmd.append("--json")
    if args.fail_on_sensitive:
        cmd.append("--fail-on-sensitive")
    proc = subprocess.run(cmd, cwd=root)
    print("--- series ---")
    for p in read_series(root):
        print(p)
    return proc.returncode


def strip_control_paths(root: Path, cfg: dict) -> None:
    """Remove control-plane paths from a worktree so product trees can be compared."""
    if (root / "maint").exists():
        shutil.rmtree(root / "maint")
    for rel in cfg.get("paths") or []:
        if rel == "maint":
            continue
        p = root / rel
        if p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p)


def cmd_roundtrip(args: argparse.Namespace) -> int:
    """Replay series (+ overlays) on locked base and compare to a product tree.

    Defaults:
      expected / series base tip: lock.patch_tip (functional tip)
      compare_to: lock.product_tip if set, else lock.patch_tip
      When --compare-to HEAD: detects product drift after patch_tip.
    """
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    apply_only = bool(getattr(args, "apply_only", False))
    expected = args.expected or lock.patch_tip
    base = args.base or lock.commit
    # Product tree = patches + overlays. Prefer explicit compare_to / product_tip.
    compare_to = args.compare_to
    if not apply_only and not compare_to:
        compare_to = lock.product_tip or expected
    lock_from = args.lock_from or compare_to or expected

    with tempfile.TemporaryDirectory(prefix="gork-roundtrip-") as tmp:
        wt = Path(tmp) / "wt"
        git(["worktree", "add", "--detach", str(wt), base], cwd=root)
        try:
            shutil.copytree(root / "maint", wt / "maint")
            series = read_series(root)
            verify_sha256sums(root)
            meta = patch_meta_by_file(root)
            for idx, patch_name in enumerate(series):
                patch_file = wt / "maint" / "patches" / patch_name
                print(f"roundtrip am: {patch_name}")
                proc = run(
                    ["git", "am", "--3way", str(patch_file)],
                    cwd=wt,
                    check=False,
                    capture=True,
                )
                if proc.returncode != 0:
                    print(proc.stdout or "", end="")
                    print(proc.stderr or "", end="", file=sys.stderr)
                    run(["git", "am", "--abort"], cwd=wt, check=False)
                    if is_trailing_skippable(series, idx, meta) and not meta.get(
                        patch_name, {}
                    ).get("critical", True):
                        print(f"roundtrip skip non-critical {patch_name}")
                        break
                    print(f"roundtrip CONFLICT on {patch_name}", file=sys.stderr)
                    return 3

            apply_overlays(wt)
            if apply_only:
                print(
                    f"roundtrip apply-only OK: series applies on {base[:12]} "
                    f"(+ overlays; tree not compared)"
                )
                return 0

            cfg = load_control_files(root)
            strip_control_paths(wt, cfg)

            run(["git", "add", "-A"], cwd=wt)
            if lock_from and git_resolvable(root, str(lock_from)):
                run(
                    ["git", "checkout", str(lock_from), "--", "Cargo.lock"],
                    cwd=wt,
                    check=False,
                )
                run(["git", "add", "-A", "--", "Cargo.lock"], cwd=wt, check=False)

            compare_sha = run(
                ["git", "rev-parse", str(compare_to)], cwd=root, capture=True
            ).stdout.strip()

            names = run(
                ["git", "diff", "--cached", "--name-only", compare_sha],
                cwd=wt,
                capture=True,
            )
            changed = [
                n
                for n in names.stdout.splitlines()
                if n.strip()
                and n.strip() != "Cargo.lock"
                and not control_path_allowed(n, cfg)
            ]
            if changed:
                print(
                    "roundtrip tree mismatch vs product tree:",
                    file=sys.stderr,
                )
                print("\n".join(changed), file=sys.stderr)
                return 1

            print(
                f"roundtrip OK: base {base[:12]} + series(+overlays) "
                f"matches product tree {compare_sha[:12]} "
                f"(Cargo.lock + control files excluded)"
            )
            return 0
        finally:
            git(["worktree", "remove", "--force", str(wt)], cwd=root, check=False)


def cmd_lint(args: argparse.Namespace) -> int:
    """Hard checks for patch queue integrity."""
    root = repo_root()
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(msg)
        print(f"lint error: {msg}", file=sys.stderr)

    lock_path = root / "maint/upstream.lock.toml"
    if not lock_path.is_file():
        err("missing maint/upstream.lock.toml")
        return 1
    lock = UpstreamLock.load(lock_path)
    patchset_path = root / "maint/patchset.toml"
    if not patchset_path.is_file():
        err("missing maint/patchset.toml")
        return 1
    patchset = load_patchset(patchset_path)
    ids = [p["id"] for p in patchset]
    if len(ids) != len(set(ids)):
        err(f"duplicate patchset ids: {ids}")
    files = [p["file"] for p in patchset]
    if len(files) != len(set(files)):
        err(f"duplicate patchset files: {files}")

    series = read_series(root)
    # series must be a prefix of manifest files; missing tail must be non-critical
    if series != files[: len(series)]:
        err(f"series is not a prefix of manifest files\n  series={series}\n  manifest={files}")
    else:
        for fname in files[len(series) :]:
            entry = next(p for p in patchset if p["file"] == fname)
            if entry.get("critical", True):
                err(f"series missing critical patch file {fname}")

    try:
        verify_sha256sums(root)
    except SystemExit as exc:
        err(str(exc))

    # critical patches present
    for p in patchset:
        if p.get("critical", True) and p["file"] not in series:
            err(f"critical patch missing from series: {p['id']}")

    # contracts resolve
    contracts_path = root / "maint/contracts/privacy-contract.toml"
    if contracts_path.is_file():
        cdata = tomllib.loads(contracts_path.read_text(encoding="utf-8"))
        known = {c["id"] for c in cdata.get("contract") or []}
        for p in patchset:
            for cid in p.get("contracts") or []:
                if cid not in known:
                    err(f"patch {p['id']} references unknown contract {cid}")
    else:
        err("missing privacy-contract.toml")

    # trailer / order when both base and patch_tip are present in this clone
    # (control-plane-only PRs may only carry patches, not authoring commits).
    if lock.patch_tip and lock.commit:
        if git_resolvable(root, lock.commit) and git_resolvable(root, lock.patch_tip):
            try:
                ordered = validated_functional_commits(
                    root, lock.commit, lock.patch_tip
                )
                tip_sha = git(
                    ["rev-parse", lock.patch_tip], cwd=root, capture=True
                ).stdout.strip()
                if ordered[-1][0] != tip_sha:
                    err(
                        f"lock.patch_tip {tip_sha[:12]} is not last functional "
                        f"commit {ordered[-1][0][:12]}"
                    )
            except SystemExit as exc:
                err(str(exc))
            except Exception as exc:  # noqa: BLE001
                err(f"history checks failed: {exc}")
        else:
            print(
                "lint note: lock.patch_tip/commit not in clone; "
                "skipping trailer history checks (series SHA256 still enforced)"
            )

    # Replay patches+overlays and compare product trees.
    #
    # Default target is always current HEAD (minus control paths) so that
    # unexported product edits after product_tip still fail CI.
    #
    # When lock.product_tip is resolvable and differs from HEAD, also verify
    # the recorded product_tip still rebuilds (lock integrity).
    if not args.skip_roundtrip:
        if not git_resolvable(root, lock.commit):
            err(
                f"lock.commit {lock.commit[:12]} not resolvable; "
                "cannot roundtrip (fetch full history or tag the base)"
            )
        else:
            head = git(["rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()
            explicit = getattr(args, "compare_to", None)
            # Decide which product trees to compare:
            # - Explicit --compare-to: only that target.
            # - Authoring/sync (product_tip ancestor of HEAD): HEAD (drift) +
            #   product_tip when distinct.
            # - Control-plane-only (product_tip missing or not ancestor of
            #   HEAD): do not compare to main's product tree; require clean
            #   apply, and product_tip rebuild if the tip is resolvable.
            product_tip_sha = ""
            if lock.product_tip and git_resolvable(root, lock.product_tip):
                product_tip_sha = git(
                    ["rev-parse", lock.product_tip], cwd=root, capture=True
                ).stdout.strip()

            if explicit:
                compare_targets = [("compare-to", explicit)]
            elif product_tip_sha and git_is_ancestor(
                root, product_tip_sha, head
            ):
                compare_targets = [("HEAD", head)]
                if product_tip_sha != head:
                    compare_targets.append(
                        ("lock.product_tip", product_tip_sha)
                    )
            elif product_tip_sha:
                print(
                    "lint note: product_tip is not an ancestor of HEAD "
                    "(control-plane or foreign history) — comparing only "
                    "to product_tip, not HEAD"
                )
                compare_targets = [("lock.product_tip", product_tip_sha)]
            else:
                print(
                    "lint note: product_tip not in clone — verifying clean "
                    "series apply only (no product tree equality)"
                )
                compare_targets = []

            if not compare_targets:
                rc = cmd_roundtrip(
                    argparse.Namespace(
                        expected=lock.patch_tip,
                        compare_to=None,
                        lock_from=None,
                        base=None,
                        apply_only=True,
                    )
                )
                if rc != 0:
                    err(f"series apply failed on lock.commit (exit {rc})")
            else:
                for label, target in compare_targets:
                    print(f"lint roundtrip vs {label} ({target[:12]})")
                    rc = cmd_roundtrip(
                        argparse.Namespace(
                            expected=lock.patch_tip,
                            compare_to=target,
                            lock_from=None,
                            base=None,
                            apply_only=False,
                        )
                    )
                    if rc != 0:
                        err(
                            f"roundtrip vs {label} failed (exit {rc}); "
                            "export/finalize product tree or revert "
                            "unexported edits"
                        )

    if errors:
        print(f"lint failed: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("lint OK")
    return 0


def cmd_finalize_sync(args: argparse.Namespace) -> int:
    """Update lock + re-export series against new upstream on current branch.

    Call this *after* apply (patches + overlays + control commits). Uses:
      patch_tip   = last functional Gork-Patch-Id commit
      product_tip = HEAD after overlays/control (full product tree for roundtrip)
    """
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    upstream = args.upstream
    git(["cat-file", "-e", f"{upstream}^{{commit}}"], cwd=root)
    version = args.version or resolve_upstream_meta(root, upstream)[0]
    source_rev = (
        args.source_rev
        if args.source_rev is not None
        else resolve_upstream_meta(root, upstream)[1]
    )

    # product_tip = current HEAD (includes overlays applied by cmd_apply)
    product_tip = git(["rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()
    functional = commits_with_patch_id(root, upstream, product_tip)
    functional = [(s, p) for s, p in functional if p not in EXCLUDE_PATCH_IDS]
    if not functional:
        print("no functional patches applied on this branch", file=sys.stderr)
        return 1
    functional_tip = functional[-1][0]

    lock.commit = upstream
    lock.version = version
    lock.source_rev = source_rev
    lock.patch_tip = functional_tip
    lock.product_tip = product_tip
    lock.write(root / "maint/upstream.lock.toml")
    print(
        f"lock updated: {version} {upstream[:12]} "
        f"SOURCE_REV={source_rev or '(empty)'}"
    )
    print(f"patch_tip={functional_tip}")
    print(f"product_tip={product_tip}")

    # Re-export only applied functional commits against new base
    rc = cmd_export(argparse.Namespace(base=upstream, tip=functional_tip))
    if rc != 0:
        return rc

    # Roundtrip: series on new base + overlays must match product_tip
    rc = cmd_roundtrip(
        argparse.Namespace(
            expected=functional_tip,
            compare_to=product_tip,
            lock_from=product_tip,
            base=upstream,
        )
    )
    if rc != 0:
        print("finalize-sync: roundtrip failed", file=sys.stderr)
        return rc

    git(["add", "maint"], cwd=root)
    st = git(["status", "--porcelain"], cwd=root, capture=True)
    if st.stdout.strip():
        git(
            [
                "commit",
                "-m",
                "chore(sync): finalize upstream lock and re-export patch queue\n\n"
                f"{TRAILER_ID}: control-metadata\n"
                f"Upstream: {upstream}\n"
                f"Version: {version}\n"
                f"SOURCE_REV: {source_rev}\n"
                f"patch_tip: {functional_tip}\n"
                f"product_tip: {product_tip}\n",
            ],
            cwd=root,
        )
    print("finalize-sync complete")
    return 0


def path_matches(path: str, prefixes: list[str]) -> bool:
    for p in prefixes:
        if p.endswith("/"):
            if path.startswith(p) or path == p.rstrip("/"):
                return True
        elif path == p:
            return True
    return False


def cmd_bootstrap_stack(args: argparse.Namespace) -> int:
    root = repo_root()
    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    base = args.base or lock.commit
    tip = args.tip or "HEAD"
    branch = args.branch or "patch-authoring-v1"

    base_sha = git(["rev-parse", base], cwd=root, capture=True).stdout.strip()
    tip_sha = git(["rev-parse", tip], cwd=root, capture=True).stdout.strip()

    names = git(
        ["diff", "--name-only", base_sha, tip_sha],
        cwd=root,
        capture=True,
    ).stdout.splitlines()
    names = [
        n
        for n in names
        if n
        and n != "Cargo.lock"
        and not n.startswith("maint/")
        # Never carry Gork/upstream CI workflows into functional patches;
        # control plane owns gork-privacy + watch/replay via control-files.
        and not n.startswith(".github/workflows/")
    ]

    # Community docs/assets go to overlays, not the patch series
    overlay_prefixes = (
        "docs/assets/",
        "PRIVACY.md",
        "NOTICE",
        "SECURITY.md",
        "CONTRIBUTING.md",
    )
    overlay_files = [
        n
        for n in names
        if n in overlay_prefixes or any(n.startswith(p) for p in overlay_prefixes if p.endswith("/"))
        or n in {"PRIVACY.md", "NOTICE", "SECURITY.md", "CONTRIBUTING.md"}
    ]
    # README stays as non-critical branding patch or overlay — use overlay
    if "README.md" in names:
        overlay_files.append("README.md")

    assigned: dict[str, list[str]] = {gid: [] for gid, *_ in BOOTSTRAP_GROUPS}
    assigned["branding-docs"] = []
    for path in names:
        if path in overlay_files or path == "README.md":
            continue
        hit = None
        for gid, _s, _i, _r, prefixes in BOOTSTRAP_GROUPS:
            if path_matches(path, prefixes):
                hit = gid
                break
        if hit is None:
            hit = "branding-docs"
        assigned[hit].append(path)

    order = [g[0] for g in BOOTSTRAP_GROUPS] + ["branding-docs"]
    meta = {g[0]: g for g in BOOTSTRAP_GROUPS}
    meta["branding-docs"] = (
        "branding-docs",
        "branding-docs: residual community rebrand (non-critical)",
        "community-branding",
        "medium",
        [],
    )

    exists = git(
        ["show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=root,
        check=False,
        capture=True,
    )
    if exists.returncode == 0:
        if not args.force:
            print(f"branch {branch} exists (pass --force)", file=sys.stderr)
            return 1
        cur = git(["branch", "--show-current"], cwd=root, capture=True).stdout.strip()
        if cur == branch:
            git(["switch", "--detach", base_sha], cwd=root)
        git(["branch", "-D", branch], cwd=root)

    git(["switch", "-c", branch, base_sha], cwd=root)

    for gid in order:
        paths = assigned.get(gid) or []
        if not paths:
            print(f"skip empty group {gid}")
            continue
        _id, subject, invariant, risk, _ = meta[gid]
        git(["checkout", tip_sha, "--", *paths], cwd=root)
        deleted = []
        for p in paths:
            chk = git(
                ["cat-file", "-e", f"{tip_sha}:{p}"],
                cwd=root,
                check=False,
                capture=True,
            )
            if chk.returncode != 0:
                deleted.append(p)
        if deleted:
            git(["rm", "-f", "--ignore-unmatch", *deleted], cwd=root, check=False)
        git(["reset", "-q"], cwd=root)
        git(["add", "-A", "--", *paths], cwd=root)
        status = git(["status", "--porcelain", "--", *paths], cwd=root, capture=True)
        if not status.stdout.strip():
            print(f"skip no-op group {gid}")
            continue
        msg = (
            f"{subject}\n\n"
            f"{TRAILER_ID}: {gid}\n"
            f"{TRAILER_INVARIANT}: {invariant}\n"
            f"{TRAILER_RISK}: {risk}\n"
        )
        git(["commit", "-m", msg, "--", *paths], cwd=root)
        print(f"committed {gid} ({len(paths)} paths)")

    # Materialize overlays from tip (binary-safe)
    ov = root / "maint" / "overlays"
    for path in sorted(set(overlay_files)):
        chk = subprocess.run(
            ["git", "cat-file", "-e", f"{tip_sha}:{path}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if chk.returncode != 0:
            continue
        dest = ov / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "show", f"{tip_sha}:{path}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            dest.write_bytes(proc.stdout)
            print(f"overlay staged: {path}")

    tip_new = git(["rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()
    lock.patch_tip = tip_new
    if (root / "maint").exists():
        lock.write(root / "maint/upstream.lock.toml")

    print(f"bootstrap complete on {branch}; HEAD={tip_new}")
    print("next: python maint/scripts/patchctl.py export --tip HEAD")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchctl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="Compare lock to upstream main")
    d.add_argument("--repository", default=None)
    d.set_defaults(func=cmd_detect)

    e = sub.add_parser("export", help="Export functional commits to maint/patches")
    e.add_argument("--base", default=None)
    e.add_argument("--tip", default=None)
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("apply", help="Apply patch series onto an upstream SHA")
    a.add_argument("--upstream", required=True)
    a.add_argument("--branch", default=None)
    a.add_argument("--force", action="store_true")
    a.add_argument("--verify", action="store_true")
    a.add_argument("--skip-expensive", action="store_true")
    a.add_argument("--expect-version", default=None)
    a.add_argument("--expect-source-rev", default=None)
    a.set_defaults(func=cmd_apply)

    v = sub.add_parser("verify", help="Run privacy contracts")
    v.add_argument("--skip-expensive", action="store_true")
    v.add_argument("--only", action="append", default=[])
    v.add_argument(
        "--exclude-group",
        action="append",
        default=[],
        help="Skip contract groups (e.g. supply-chain)",
    )
    v.add_argument(
        "--only-group",
        action="append",
        default=[],
        help="Only run these contract groups",
    )
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("report", help="Sensitive upstream diff report")
    r.add_argument("--old", default=None)
    r.add_argument("--new", required=True)
    r.add_argument("--json", action="store_true")
    r.add_argument("--fail-on-sensitive", action="store_true")
    r.set_defaults(func=cmd_report)

    rt = sub.add_parser("roundtrip", help="Replay series on locked base")
    rt.add_argument("--expected", default=None, help="Functional tip (default lock.patch_tip)")
    rt.add_argument(
        "--compare-to",
        default=None,
        help="Product tree to compare (default lock.product_tip or patch_tip). "
        "Use HEAD to detect post-tip product drift.",
    )
    rt.add_argument("--lock-from", default=None)
    rt.add_argument("--base", default=None, help="Upstream base (default lock.commit)")
    rt.set_defaults(func=cmd_roundtrip)

    ln = sub.add_parser("lint", help="Hard integrity checks for patch queue")
    ln.add_argument(
        "--skip-roundtrip",
        action="store_true",
        help="Skip expensive tree replay (still runs static checks)",
    )
    ln.add_argument(
        "--compare-to",
        default=None,
        help="Override product tree for a single roundtrip comparison. "
        "Default: HEAD (drift detection), and also lock.product_tip when set "
        "and different from HEAD.",
    )
    ln.set_defaults(func=cmd_lint)

    fs = sub.add_parser(
        "finalize-sync",
        help="Update lock to new upstream and re-export patch queue on current branch",
    )
    fs.add_argument("--upstream", required=True)
    fs.add_argument("--version", default=None)
    fs.add_argument("--source-rev", default=None)
    fs.set_defaults(func=cmd_finalize_sync)

    b = sub.add_parser("bootstrap-stack", help="One-time path-group stack rebuild")
    b.add_argument("--base", default=None)
    b.add_argument("--tip", default="HEAD")
    b.add_argument("--branch", default="patch-authoring-v1")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_bootstrap_stack)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
