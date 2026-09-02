#!/usr/bin/env python3
"""Grok Privacy patch control.

Commands: detect, export, apply, verify, report, roundtrip, bootstrap-stack,
lint, finalize-sync.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
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
        "product-cli-grok",
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
    # Identité unique du dépôt : tout commit (local ou CI) est signé Lycaos.
    merged.setdefault("GIT_AUTHOR_NAME", "Lycaos")
    merged.setdefault("GIT_AUTHOR_EMAIL", "caraboune@gmail.com")
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
            "# Locked upstream base for the Grok Privacy patch queue.",
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
    body = "# Grok Privacy patch series (apply order)\n" + "\n".join(files) + "\n"
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


# ── interrupted apply (manual port) ─────────────────────────────────────────
#
# A critical conflict used to `git am --abort` immediately: the maintainer was
# told "manual port required" while every trace of the conflict had just been
# destroyed, and no supported path existed to finish the apply tail (overlays,
# lock policy, control commits) that finalize-sync's roundtrip depends on.
# The state below is written under the git dir so it never reaches the product
# tree, and `apply --continue` replays the rest of the series from it.

APPLY_STATE_FILE = "grok-apply-state.json"
APPLY_PATCHES_DIR = "grok-apply-patches"
APPLY_CONFLICT_FILE = "grok-apply-conflict.diff"


# ── automatic conflict resolution (--auto-resolve-conflicts) ────────────────
#
# Fail-closed moves one step later: a textual conflict is no longer fatal by
# itself — an unproven privacy invariant is. A resolution is accepted only when
# the patch's declared contracts pass on the applied tree (verify_auto_resolved
# below); anything else falls back to the manual-port path above. Recorded
# resolutions (git rerere) replay without the resolver; a fresh conflict is
# sent to the resolver command only for patches that declare contracts.

RERERE_GIT_FLAGS = ["-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=true"]
DEFAULT_RESOLVER_CMD = "claude -p"
RESOLVER_TIMEOUT_SECONDS = 600


def resolver_cmd() -> list[str]:
    raw = os.environ.get("PATCHCTL_CONFLICT_RESOLVER_CMD", "").strip()
    return shlex.split(raw or DEFAULT_RESOLVER_CMD)


def resolver_allowed(entry: dict) -> bool:
    """Whether the conflict resolver may run for this patch.

    `auto_resolve` in `maint/patchset.toml` decides. The declared contracts are
    only the *default* when that key is absent — a patch that can prove itself
    is resolvable, one that cannot is not. Stating the key overrides that
    default in either direction: `true` accepts a resolution no contract can
    vouch for (the apply says so and records it), `false` keeps a patch on the
    manual path even though it declares contracts, when a human read is the
    point.
    """
    explicit = entry.get("auto_resolve")
    if explicit is not None:
        return bool(explicit)
    return bool(entry.get("contracts"))


def resolver_disabled_reason(entry: dict, patch_name: str) -> str:
    key = "auto_resolve = false" if entry.get("auto_resolve") is not None else (
        "patch declares no contracts and does not set auto_resolve"
    )
    return f"resolver disabled for {patch_name} ({key}; recorded resolutions only)"


def split_conflict_file(conflicted: str) -> tuple[list[str], list[str]]:
    """Split a conflicted file into (commons, regions).

    Regions keep their conflict markers; commons is one element longer than
    regions, so the file is commons[0] + regions[0] + commons[1] + …
    """
    commons: list[str] = []
    regions: list[str] = []
    cur: list[str] = []
    depth = 0
    for line in conflicted.splitlines(keepends=True):
        if depth == 0:
            if line.startswith("<<<<<<< "):
                commons.append("".join(cur))
                cur = [line]
                depth = 1
            else:
                cur.append(line)
            continue
        cur.append(line)
        if line.startswith("<<<<<<< "):
            depth += 1
        elif line.startswith(">>>>>>> "):
            depth -= 1
            if depth == 0:
                regions.append("".join(cur))
                cur = []
    commons.append("".join(cur))
    return commons, regions


def conflict_common_segments(conflicted: str) -> list[str]:
    """Text of a conflicted file outside its conflict regions, in order."""
    return split_conflict_file(conflicted)[0]


# Share of the out-of-region text a replacement may reproduce before it reads
# as a dump of the file rather than an answer for the region.
CONFLICT_ECHO_MAX_SHARE = 0.5
CONFLICT_ECHO_MIN_LINES = 2


def distinctive_lines(text: str) -> list[str]:
    """Stripped lines carrying enough text to say where they came from."""
    return [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 12]


def replacement_echoes_common(commons: list[str], replacement: str) -> bool:
    """True when a region replacement reproduces most of the text living
    outside the region — the signature of a resolver answering with the whole
    file instead of with the region.

    Judged as a share, not line by line. A correct resolution routinely repeats
    an `assert!(…)` the same test uses elsewhere, and this fork's chokepoint
    idiom repeats the same three lines at every install path; keying the guard
    on isolated echoed lines rejected those correct answers and made whole
    classes of conflict unresolvable, so every upstream sync fell back to a
    manual port. Reproducing half of what sits outside the region is a
    different act. What proves a resolution keeps the privacy invariant is the
    contract gate downstream (`verify_auto_resolved`), not this heuristic.
    """
    outside = set(distinctive_lines("".join(commons)))
    if not outside:
        return False
    echoed = outside & set(distinctive_lines(replacement))
    if len(echoed) < CONFLICT_ECHO_MIN_LINES:
        return False
    return len(echoed) / len(outside) >= CONFLICT_ECHO_MAX_SHARE


def resolution_preserves_common(conflicted: str, resolved: str) -> bool:
    """A valid resolution may only differ inside the conflict regions: every
    common segment must reappear intact and in order."""
    pos = 0
    for seg in conflict_common_segments(conflicted):
        if not seg:
            continue
        idx = resolved.find(seg, pos)
        if idx < 0:
            return False
        pos = idx + len(seg)
    return True


def strip_code_fence(text: str) -> str:
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]) + "\n"
    return text


def build_resolver_prompt(
    patch_name: str,
    rel: str,
    mail: str,
    conflicted: str,
    region: str,
    idx: int,
    total: int,
) -> str:
    return (
        "You are resolving a `git am --3way` merge conflict while a privacy "
        "patch series is re-applied onto a new upstream release.\n\n"
        f"Failing patch: {patch_name}. Patch mail (the commit message states "
        "the intent; the diff targets the old upstream base):\n"
        "--- PATCH START ---\n"
        f"{mail}"
        "--- PATCH END ---\n\n"
        f"Full conflicted file `{rel}`, for context only:\n"
        "--- FILE START ---\n"
        f"{conflicted}"
        "--- FILE END ---\n\n"
        f"Conflict region {idx} of {total} (the <<<<<<< side is the new "
        "upstream code, the >>>>>>> side is the patch):\n"
        "--- REGION START ---\n"
        f"{region}"
        "--- REGION END ---\n\n"
        "Resolve this region by keeping the new upstream structure and "
        "re-applying the patch's exact intent — its privacy guarantee must "
        "hold.\n"
        "Output ONLY the text that replaces this region in the final file: "
        "no conflict markers, no markdown fences, no commentary, and none of "
        "the file content that surrounds the region.\n"
    )


def attempt_auto_resolve(
    root: Path,
    patch_name: str,
    entry: dict,
    files: list[str],
    report: Path,
    *,
    allow_ai: bool = True,
) -> tuple[dict | None, str]:
    """Resolve an in-progress `git am` conflict without human input.

    Two stages, and they are worth keeping apart. rerere replays a resolution
    this repository has already recorded — no model, no network — and it runs
    unconditionally: a conflict answered once is answered for good. Only what
    rerere has never seen reaches the resolver command, and `allow_ai` decides
    whether it may. Only unmerged files are ever written.

    Returns (record, "") on success or (None, reason) to fail closed.
    """
    pending = unmerged_paths(root)
    method = "rerere" if not pending else "ai"
    if pending:
        if not allow_ai:
            return None, (
                f"{patch_name} has a conflict rerere has never seen, and the "
                "resolver is off for this run (--no-auto-resolve)"
            )
        if not resolver_allowed(entry):
            return None, resolver_disabled_reason(entry, patch_name)
        if not entry.get("contracts"):
            print(
                f"auto-resolve: {patch_name} opted in through `auto_resolve` "
                "with no contract to prove the result — this resolution rests "
                "on the build and its tests alone",
                file=sys.stderr,
            )
        mail = (
            git(
                ["am", "--show-current-patch=raw"],
                cwd=root,
                check=False,
                capture=True,
            ).stdout
            or ""
        )
        cmd = resolver_cmd()
        for rel in pending:
            target = root / rel
            conflicted = target.read_text(encoding="utf-8", errors="replace")
            commons, regions = split_conflict_file(conflicted)
            if not regions:
                return None, f"{rel} is unmerged but carries no conflict markers"
            replacements: list[str] = []
            for idx, region in enumerate(regions, 1):
                prompt = build_resolver_prompt(
                    patch_name, rel, mail, conflicted, region, idx, len(regions)
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        input=prompt,
                        cwd=root,
                        text=True,
                        capture_output=True,
                        timeout=RESOLVER_TIMEOUT_SECONDS,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return None, f"resolver command failed for {rel}: {exc}"
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                    return None, (
                        f"resolver exited {proc.returncode} for {rel}: {tail}"
                    )
                content = strip_code_fence(proc.stdout)
                where = f"{rel} (region {idx}/{len(regions)})"
                if not content.strip():
                    return None, f"resolver returned empty output for {where}"
                if not content.endswith("\n"):
                    content += "\n"
                if text_has_conflict_markers(content):
                    with report.open("a", encoding="utf-8") as f:
                        f.write(
                            f"\n## rejected resolver proposal for {where}\n"
                            f"{content}\n"
                        )
                    return None, (
                        f"resolver output for {where} still contains conflict "
                        "markers"
                    )
                if replacement_echoes_common(commons, content):
                    with report.open("a", encoding="utf-8") as f:
                        f.write(
                            f"\n## rejected resolver proposal for {where}\n"
                            f"{content}\n"
                        )
                    return None, (
                        f"resolver output for {where} repeats file content "
                        "from outside the region (full-file dump?)"
                    )
                replacements.append(content)
            resolved = (
                "".join(c + r for c, r in zip(commons, replacements))
                + commons[-1]
            )
            # Belt over the by-construction guarantee.
            if not resolution_preserves_common(conflicted, resolved):
                return None, f"reconstruction lost common segments for {rel}"
            target.write_text(resolved, encoding="utf-8")
        git(["add", "--", *pending], cwd=root, check=False)
    if unmerged_paths(root):
        return None, "unresolved paths remain after resolution attempt"
    proc = git(
        [*RERERE_GIT_FLAGS, "-c", "core.editor=true", "am", "--continue"],
        cwd=root,
        check=False,
        capture=True,
    )
    print(proc.stdout or "", end="")
    if proc.returncode != 0:
        print(proc.stderr or "", end="", file=sys.stderr)
        return None, f"`git am --continue` failed ({proc.returncode})"
    return {
        "patch": patch_name,
        "method": method,
        "files": files,
        # Recorded so the status file shows which resolutions no contract covers.
        "unverified": method == "ai" and not entry.get("contracts"),
    }, ""


def verify_auto_resolved(
    root: Path, meta: dict[str, dict], auto_resolved: list[dict]
) -> int:
    """Contract gate for auto-resolved patches: fail closed unless every
    declared contract passes on the applied tree."""
    if not auto_resolved:
        return 0
    ids: list[str] = []
    for rec in auto_resolved:
        for cid in (meta.get(rec["patch"]) or {}).get("contracts") or []:
            if cid not in ids:
                ids.append(cid)
    unverified = [rec["patch"] for rec in auto_resolved if rec.get("unverified")]
    if unverified:
        print(
            "auto-resolve: no contract covers " + ", ".join(unverified) + " — "
            "accepted on the `auto_resolve` opt-in in maint/patchset.toml; the "
            "build and its tests are all that stand behind these resolutions",
            file=sys.stderr,
        )
    if not ids:
        if {rec["method"] for rec in auto_resolved} <= {"rerere"}:
            print(
                "auto-resolve: recorded resolutions only; "
                "no contracts declared to run"
            )
            return 0
        # An opted-in patch answers for itself. Anything else reaching here has
        # no contract and no opt-in, which is the case the gate exists for.
        if all(rec.get("unverified") for rec in auto_resolved if rec["method"] != "rerere"):
            return 0
        print(
            "auto-resolve: no contracts cover the auto-resolved patches; "
            "fail-closed",
            file=sys.stderr,
        )
        return 3
    print(f"auto-resolve: verifying contracts {', '.join(ids)}")
    rc = cmd_verify(argparse.Namespace(skip_expensive=False, only=ids))
    if rc != 0:
        print(
            "auto-resolve: contract verification FAILED after automatic "
            "resolution; fail-closed — branch left for inspection",
            file=sys.stderr,
        )
        return 3
    print("auto-resolve: contracts passed")
    return 0


def git_dir(root: Path) -> Path:
    out = git(["rev-parse", "--absolute-git-dir"], cwd=root, capture=True).stdout.strip()
    return Path(out)


def save_apply_state(root: Path, state: dict, patch_src: Path) -> Path:
    """Persist enough to resume: patches are copied out of the temp snapshot.

    A resume re-enters here with `patch_src` already pointing at `dest` (the
    state file records the copy, not the vanished temp dir). Copying it onto
    itself would delete the patches first, so the second conflict of a sync
    used to crash with FileNotFoundError and lose the queue.
    """
    gd = git_dir(root)
    dest = gd / APPLY_PATCHES_DIR
    if patch_src.resolve() != dest.resolve():
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(patch_src, dest)
    payload = dict(state, patch_src=str(dest))
    path = gd / APPLY_STATE_FILE
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_apply_state(root: Path) -> dict | None:
    path = git_dir(root) / APPLY_STATE_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_apply_state(root: Path) -> None:
    gd = git_dir(root)
    (gd / APPLY_STATE_FILE).unlink(missing_ok=True)
    (gd / APPLY_CONFLICT_FILE).unlink(missing_ok=True)
    shutil.rmtree(gd / APPLY_PATCHES_DIR, ignore_errors=True)


def text_has_conflict_markers(text: str) -> bool:
    for line in text.splitlines():
        if line.startswith("<<<<<<< ") or line.startswith(">>>>>>> "):
            return True
    return False


def has_conflict_markers(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return text_has_conflict_markers(text)


def unmerged_paths(root: Path) -> list[str]:
    out = git(
        ["diff", "--name-only", "--diff-filter=U"], cwd=root, check=False, capture=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def write_conflict_report(root: Path, patch_name: str, out: str, err: str) -> tuple[Path, list[str]]:
    """Capture the conflict *before* any abort; returns (report, unmerged files)."""
    files = unmerged_paths(root)
    failing = git(
        ["am", "--show-current-patch=diff"], cwd=root, check=False, capture=True
    ).stdout
    body = (
        f"# conflict on {patch_name}\n\n"
        "## unmerged files\n"
        + ("".join(f"{f}\n" for f in files) or "(none — patch did not apply at all)\n")
        + "\n## git am output\n"
        + (out or "")
        + (err or "")
        + "\n## failing patch\n"
        + (failing or "(unavailable)\n")
    )
    path = git_dir(root) / APPLY_CONFLICT_FILE
    path.write_text(body, encoding="utf-8")
    return path, files


def apply_series(
    root: Path,
    series: list[str],
    patch_src: Path,
    meta: dict[str, dict],
    *,
    start: int,
    applied: list[str],
    skipped: list[str],
    state_base: dict,
    keep_conflict: bool,
    auto_resolve: bool = False,
    auto_resolved: list[dict] | None = None,
) -> tuple[int | None, str | None]:
    """Run `git am` over series[start:].

    Returns (rc, conflicted): rc is None when the series is exhausted (success
    or trailing non-critical skips), otherwise the exit code to propagate.
    """
    for idx in range(start, len(series)):
        patch_name = series[idx]
        patch_file = patch_src / patch_name
        if not patch_file.is_file():
            print(f"missing patch: {patch_file}", file=sys.stderr)
            return 1, None
        print(f"am: {patch_name}")
        proc = git(
            [
                # rerere is not part of the opt-in: recording a resolution and
                # replaying it costs nothing and needs no model. `auto_resolve`
                # governs the resolver command alone.
                *RERERE_GIT_FLAGS,
                "am",
                "--3way",
                str(patch_file),
            ],
            cwd=root,
            check=False,
            capture=True,
        )
        if proc.returncode == 0:
            applied.append(patch_name)
            continue

        print(proc.stdout or "", end="")
        print(proc.stderr or "", end="", file=sys.stderr)

        entry = meta.get(patch_name, {})
        critical = entry.get("critical", True)
        skippable = not critical and is_trailing_skippable(series, idx, meta)
        # Always worth a try, whatever the run's settings: rerere may already
        # hold this exact resolution, and a trailing patch that can be replayed
        # should be applied rather than skipped — a silent skip is what leaves
        # the fork's branding to be ported by hand afterwards. A failure still
        # falls through to the skip; trying must not turn a skippable patch
        # into a blocker.
        report, files = write_conflict_report(
            root, patch_name, proc.stdout or "", proc.stderr or ""
        )
        record, reason = attempt_auto_resolve(
            root, patch_name, entry, files, report, allow_ai=auto_resolve
        )
        if record is not None:
            applied.append(patch_name)
            if auto_resolved is not None:
                auto_resolved.append(record)
            print(
                f"AUTO-RESOLVED {patch_name} via {record['method']}"
                f" ({', '.join(record['files']) or 'no unmerged files'});"
                " contracts are verified after the series"
            )
            continue
        print(f"auto-resolve failed: {reason}", file=sys.stderr)
        if files:
            # Partial resolver writes must not reach the maintainer: restore
            # the conflicted state of every unmerged file.
            git(["checkout", "-m", "--", *files], cwd=root, check=False)
        if not skippable:
            state = dict(
                state_base,
                series=series,
                series_index=idx,
                applied=applied,
                skipped=skipped,
                conflicted=patch_name,
                unmerged=files,
                keep_conflict=keep_conflict,
                auto_resolve=auto_resolve,
                auto_resolved=auto_resolved or [],
            )
            print(
                f"CONFLICT on critical/non-trailing patch {patch_name}; fail-closed",
                file=sys.stderr,
            )
            print(f"conflict report: {report}", file=sys.stderr)
            if keep_conflict:
                # Resume state only exists when the conflict is left in
                # place: after an abort there is nothing to continue, and a
                # plain re-run must stay possible.
                save_apply_state(root, state, patch_src)
                print(
                    "am left in progress: resolve the files above, `git add` them, "
                    "then run `patchctl apply --continue`",
                    file=sys.stderr,
                )
            else:
                git(["am", "--abort"], cwd=root, check=False)
                print(
                    "am aborted; re-run with --keep-conflict to resolve in place",
                    file=sys.stderr,
                )
            print(
                f"branch={state_base.get('branch')} applied={applied} "
                f"skipped={skipped} conflicted={patch_name}"
            )
            return 3, patch_name

        git(["am", "--abort"], cwd=root, check=False)
        # Trailing non-critical: skip this and remaining non-critical
        print(f"SKIP non-critical trailing patch {patch_name}")
        skipped.append(patch_name)
        for rest in series[idx + 1 :]:
            print(f"SKIP non-critical trailing patch {rest}")
            skipped.append(rest)
        return None, None

    return None, None


def finish_apply(
    root: Path,
    *,
    branch: str,
    upstream_sha: str,
    version: str,
    source_rev: str,
    applied: list[str],
    skipped: list[str],
    auto_resolved: list[dict] | None = None,
) -> int:
    """Overlays, lock policy and control-plane commits — the tail of an apply."""
    apply_overlays(root)
    try:
        run_lock_policy(root)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
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
                "conflicted": None,
                "auto_resolved": auto_resolved or [],
                "branding_required": bool(skipped),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git(["add", "maint/last-apply-status.json"], cwd=root, check=False)
    st2 = git(
        ["status", "--porcelain", "--", "maint/last-apply-status.json"],
        cwd=root,
        capture=True,
    )
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

    clear_apply_state(root)
    print(
        f"applied {len(applied)} patches on {branch} "
        f"(upstream {upstream_sha[:12]}); skipped={skipped}"
    )
    print(f"upstream version={version or '?'} source_rev={source_rev or '(empty)'}")
    return 0


def cmd_apply_continue(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_apply_state(root)
    if state is None:
        print(
            "no interrupted apply to continue "
            f"({git_dir(root) / APPLY_STATE_FILE} missing)",
            file=sys.stderr,
        )
        return 1

    branch = state["branch"]
    current = git(["branch", "--show-current"], cwd=root, capture=True).stdout.strip()
    if current != branch:
        print(
            f"interrupted apply is on {branch}, worktree is on "
            f"{current or 'a detached HEAD'} — checkout {branch} first",
            file=sys.stderr,
        )
        return 1

    auto_resolve = bool(state.get("auto_resolve"))
    auto_resolved = list(state.get("auto_resolved") or [])
    if (git_dir(root) / "rebase-apply").exists():
        # `git add` is what clears an unmerged index entry, so a path edited but
        # not staged still reads as unmerged. Staging the recorded paths here is
        # the common case after an editor pass — but never over live markers.
        recorded = [f for f in state.get("unmerged", []) if (root / f).is_file()]
        marked = [f for f in recorded if has_conflict_markers(root / f)]
        if marked:
            print("conflict markers still present:", file=sys.stderr)
            for f in marked:
                print(f"  {f}", file=sys.stderr)
            return 3
        if recorded:
            git(["add", "--", *recorded], cwd=root, check=False)
        left = unmerged_paths(root)
        if left:
            print("unresolved conflicts:", file=sys.stderr)
            for f in left:
                print(f"  {f}", file=sys.stderr)
            return 3
        proc = git(
            [
                # Record the manual resolution so a later run replays it
                # without the resolver — worth doing whatever this run's
                # settings, since replaying costs nothing and asks no model.
                *RERERE_GIT_FLAGS,
                "-c",
                "core.editor=true",
                "am",
                "--continue",
            ],
            cwd=root,
            check=False,
            capture=True,
        )
        print(proc.stdout or "", end="")
        print(proc.stderr or "", end="", file=sys.stderr)
        if proc.returncode != 0:
            return 3
    meta = patch_meta_by_file(root)
    if not (git_dir(root) / "rebase-apply").exists():
        # The maintainer may have run `git am --continue` themselves. Resuming
        # at index+1 without checking would silently drop the conflicted patch.
        pid = (meta.get(state["conflicted"]) or {}).get("id")
        head_msg = git(["log", "-1", "--format=%B"], cwd=root, capture=True).stdout
        if not pid or f"{TRAILER_ID}: {pid}" not in head_msg:
            print(
                f"no am in progress and HEAD does not carry {TRAILER_ID}: {pid}: "
                f"{state['conflicted']} was neither resolved nor committed",
                file=sys.stderr,
            )
            return 3
        print(f"no am in progress: {state['conflicted']} already committed", file=sys.stderr)

    series = state["series"]
    applied = list(state["applied"]) + [state["conflicted"]]
    skipped = list(state["skipped"])
    patch_src = Path(state["patch_src"])
    state_base = {
        "branch": branch,
        "upstream": state["upstream"],
        "version": state["version"],
        "source_rev": state["source_rev"],
    }
    rc, _ = apply_series(
        root,
        series,
        patch_src,
        meta,
        start=state["series_index"] + 1,
        applied=applied,
        skipped=skipped,
        state_base=state_base,
        keep_conflict=state.get("keep_conflict", True),
        auto_resolve=auto_resolve,
        auto_resolved=auto_resolved,
    )
    if rc is not None:
        return rc

    rc = verify_auto_resolved(root, meta, auto_resolved)
    if rc != 0:
        return rc

    rc = finish_apply(
        root,
        branch=branch,
        upstream_sha=state["upstream"],
        version=state["version"],
        source_rev=state["source_rev"],
        applied=applied,
        skipped=skipped,
        auto_resolved=auto_resolved,
    )
    if rc != 0:
        return rc
    if skipped:
        return 4
    return 0


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


# ── Guard: a product commit outside the queue is a commit that will vanish ──
#
# `patchctl apply` rebuilds a sync branch from upstream plus maint/patches/
# alone. Anything committed to the product tree without a `Gork-Patch-Id`
# trailer is therefore dropped at the next sync, and dropped *silently* — that
# is how the /prompts feature was lost once already. The guard makes that
# failure mode unreachable instead of merely documented.

GUARD_HOOK_DIR = "maint/hooks"
GUARD_ENV = "PATCHCTL_GUARD"
FOLD_STATE_FILE = "grok-fold-state.json"
FOLD_PATCH_FILE = "grok-fold-pending.patch"
FOLD_BACKUP_REF = "refs/patchctl/fold-backup"
CONTROL_REEXPORT_SUBJECT = "chore(sync): re-export patch queue"

GUARD_HOOK = """#!/usr/bin/env sh
# Versioned hook, installed by `patchctl guard --install`.
# Refuses a commit that changes the product tree with no Gork-Patch-Id
# trailer: `patchctl apply` rebuilds the branch from upstream + the patch
# queue alone, so such a commit disappears at the next sync.
root=$(git rev-parse --show-toplevel) || exit 0
exec python3 "$root/maint/scripts/patchctl.py" guard --commit-msg "$1"
"""


def current_branch(root: Path) -> str:
    return git(
        ["branch", "--show-current"], cwd=root, capture=True, check=False
    ).stdout.strip()


def overlay_managed_paths(root: Path) -> set[str]:
    """Product paths that `apply_overlays` rewrites after every apply.

    A commit touching one of these is not lost by a resync — the overlay puts
    it back — so the guard must not treat it as an orphan.
    """
    overlay = root / "maint" / "overlays"
    if not overlay.is_dir():
        return set()
    return {
        str(p.relative_to(overlay)).replace(os.sep, "/")
        for p in overlay.rglob("*")
        if p.is_file()
    }


def guard_exempt(path: str, control_cfg: dict, overlays: set[str]) -> bool:
    """True when `path` survives a rebuild without belonging to the queue."""
    if control_path_allowed(path, control_cfg):
        return True
    if path in overlays:
        return True
    # lock-policy.toml keeps Cargo.lock inherited from upstream, deliberately
    # out of the functional series.
    return path == "Cargo.lock"


def orphan_product_files(root: Path, paths: list[str]) -> list[str]:
    control_cfg = load_control_files(root)
    overlays = overlay_managed_paths(root)
    return [p for p in paths if not guard_exempt(p, control_cfg, overlays)]


def commit_changed_files(root: Path, sha: str) -> list[str]:
    # Merges print nothing here, so they read as control-only and are skipped:
    # the queue is a linear series, a merge never carries a patch.
    out = git(
        ["show", "--pretty=format:", "--name-only", sha], cwd=root, capture=True
    ).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def range_changed_files(root: Path, a: str, b: str) -> list[str]:
    out = git(["diff", "--name-only", a, b], cwd=root, capture=True).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def commit_trailer_id(root: Path, sha: str) -> str | None:
    body = git(["log", "-1", "--format=%B", sha], cwd=root, capture=True).stdout
    m = re.search(rf"^{TRAILER_ID}:\s*(\S+)\s*$", body, re.M)
    return m.group(1) if m else None


def known_patch_ids(root: Path) -> set[str]:
    return {p["id"] for p in load_patchset(root / "maint/patchset.toml")}


def guard_offenders(root: Path, base: str, tip: str) -> list[tuple[str, str, str]]:
    """(sha, subject, reason) for commits the next apply would silently drop."""
    known = known_patch_ids(root)
    out: list[tuple[str, str, str]] = []
    log = git(
        ["log", "--reverse", "--format=%H%x00%s", f"{base}..{tip}"],
        cwd=root,
        capture=True,
    ).stdout
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\0")
        touched = orphan_product_files(root, commit_changed_files(root, sha))
        if not touched:
            continue  # control-only / overlay-only: legitimately trailerless
        pid = commit_trailer_id(root, sha)
        sample = ", ".join(touched[:3]) + (" …" if len(touched) > 3 else "")
        if pid is None:
            out.append(
                (
                    sha,
                    subject,
                    f"changes {len(touched)} product file(s) with no {TRAILER_ID} "
                    f"trailer ({sample})",
                )
            )
        elif pid not in known and pid not in EXCLUDE_PATCH_IDS:
            out.append(
                (sha, subject, f"{TRAILER_ID}={pid} is absent from maint/patchset.toml")
            )
    return out


GUARD_ADVICE = f"""
`patchctl apply` rebuilds a sync branch from upstream plus maint/patches/
alone, so a product commit outside that queue is dropped at the next sync —
silently. That is exactly how the /prompts feature was lost once.

Pick one:
  * fold it into an existing patch   ->  do not commit; leave the change in the
                                         working tree and run:
                                             patchctl fold <patch-id>
  * it deserves a patch of its own   ->  add a [[patch]] block to
                                         maint/patchset.toml, then commit with a
                                         `{TRAILER_ID}: <id>` trailer
  * it is control plane after all    ->  keep the change under maint/
  * you know better, just this once  ->  {GUARD_ENV}=0 git commit …
"""


def guard_pending_commit(root: Path, msg_path: Path) -> int:
    """commit-msg hook mode: judge the commit that is about to be created."""
    if not (root / "maint/patchset.toml").is_file():
        return 0  # not a queue repository
    staged = git(
        ["diff", "--cached", "--name-only"], cwd=root, capture=True
    ).stdout.splitlines()
    touched = orphan_product_files(root, [s.strip() for s in staged if s.strip()])
    if not touched:
        return 0
    body = msg_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf"^{TRAILER_ID}:\s*(\S+)\s*$", body, re.M)
    if m:
        pid = m.group(1)
        if pid in known_patch_ids(root) or pid in EXCLUDE_PATCH_IDS:
            return 0
        sys.stderr.write(
            f"\npatchctl guard: refused.\n\n"
            f"  {TRAILER_ID}: {pid}\n"
            f"is not listed in maint/patchset.toml, so the export has no file to\n"
            f"write this patch to and the commit is dropped at the next apply.\n"
            f"Add its [[patch]] block first, or use an id that exists:\n"
            f"  {', '.join(sorted(known_patch_ids(root)))}\n"
        )
        return 1
    listing = "\n".join(f"    {p}" for p in touched[:20])
    more = f"\n    … and {len(touched) - 20} more" if len(touched) > 20 else ""
    sys.stderr.write(
        f"\npatchctl guard: refused.\n\n"
        f"This commit changes {len(touched)} product file(s) and carries no "
        f"{TRAILER_ID} trailer:\n{listing}{more}\n{GUARD_ADVICE}"
    )
    return 1


def cmd_guard(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.install:
        hooks = root / GUARD_HOOK_DIR
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "commit-msg"
        hook.write_text(GUARD_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        existing = git(
            ["config", "--get", "core.hooksPath"], cwd=root, capture=True, check=False
        ).stdout.strip()
        if existing and existing != GUARD_HOOK_DIR:
            print(
                f"guard: core.hooksPath is already {existing!r} — left untouched.\n"
                f"       Chain the guard yourself from {existing}/commit-msg:\n"
                f"         python3 maint/scripts/patchctl.py guard --commit-msg \"$1\"",
                file=sys.stderr,
            )
            return 2
        git(["config", "core.hooksPath", GUARD_HOOK_DIR], cwd=root)
        print(f"guard installed: {GUARD_HOOK_DIR}/commit-msg (core.hooksPath set)")
        return 0
    if args.uninstall:
        git(["config", "--unset", "core.hooksPath"], cwd=root, check=False)
        print("guard uninstalled (core.hooksPath unset; the hook file stays versioned)")
        return 0
    if os.environ.get(GUARD_ENV) == "0":
        return 0
    if args.commit_msg:
        return guard_pending_commit(root, Path(args.commit_msg))

    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    base = args.base or lock.commit
    offenders = guard_offenders(root, base, args.tip)
    if not offenders:
        print(f"guard: no orphan product commit in {base[:12]}..{args.tip}")
        return 0
    for sha, subject, reason in offenders:
        print(f"guard: {sha[:12]} {subject}\n       {reason}", file=sys.stderr)
    sys.stderr.write(GUARD_ADVICE)
    return 3


# ── Fold: land a change inside an existing queue patch ─────────────────────


def fold_state_path(root: Path) -> Path:
    return git_dir(root) / FOLD_STATE_FILE


def save_fold_state(root: Path, state: dict) -> None:
    fold_state_path(root).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_fold_state(root: Path) -> dict | None:
    path = fold_state_path(root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_fold_state(root: Path) -> None:
    fold_state_path(root).unlink(missing_ok=True)
    (git_dir(root) / FOLD_PATCH_FILE).unlink(missing_ok=True)


def fold_abort(root: Path) -> int:
    state = load_fold_state(root)
    if not state:
        print("fold: no fold in progress", file=sys.stderr)
        return 2
    git(["rebase", "--abort"], cwd=root, check=False, capture=True)
    git(["checkout", "-f", state["branch"], "-q"], cwd=root, check=False, capture=True)
    # `-f` is safe here and only here: every byte of the pending change was
    # committed into `backup` before the fold touched anything.
    git(["reset", "--hard", state["backup"], "-q"], cwd=root, check=False)
    clear_fold_state(root)
    print(
        f"fold aborted — {state['branch']} is back at {state['backup'][:12]}.\n"
        f"Your pending change is the commit at the tip; `git reset --soft HEAD~1`\n"
        f"puts it back in the working tree."
    )
    return 0


def fold_finish(root: Path, state: dict) -> int:
    branch = state["branch"]
    git(["branch", "-f", branch, "HEAD"], cwd=root)
    git(["checkout", branch, "-q"], cwd=root)

    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    pairs = commits_with_patch_id(root, lock.commit, "HEAD")
    if not pairs:
        print("fold: no functional commit left to export", file=sys.stderr)
        return 3
    functional_tip = pairs[-1][0]
    # Export from the functional tip, never from HEAD: control commits sitting
    # after it (overlay restore, apply status) trip the export's control-file
    # guard, which is the trap this command exists to remove.
    rc = cmd_export(argparse.Namespace(tip=functional_tip, base=None))
    if rc != 0:
        print("fold: export failed — state kept for --continue", file=sys.stderr)
        return int(rc)

    if git(["status", "--porcelain"], cwd=root, capture=True).stdout.strip():
        head_subject = git(
            ["log", "-1", "--format=%s"], cwd=root, capture=True
        ).stdout.strip()
        head_is_reexport = head_subject.startswith(
            CONTROL_REEXPORT_SUBJECT
        ) and not orphan_product_files(root, commit_changed_files(root, "HEAD"))
        git(["add", "-A"], cwd=root)
        if head_is_reexport:
            git(
                ["commit", "--amend", "--no-edit", "--no-verify", "-q"],
                cwd=root,
                env={GUARD_ENV: "0"},
            )
        else:
            git(
                [
                    "commit",
                    "--no-verify",
                    "-q",
                    "-m",
                    f"{CONTROL_REEXPORT_SUBJECT} after fold into {state['patch_id']}",
                ],
                cwd=root,
                env={GUARD_ENV: "0"},
            )

    backup = state["backup"]
    patch_id = state["patch_id"]
    no_lint = state.get("no_lint", False)
    clear_fold_state(root)
    print(
        f"\nfold: change folded into {patch_id}; queue re-exported from "
        f"{functional_tip[:12]}.\n"
        f"      previous tip kept at {FOLD_BACKUP_REF} ({backup[:12]}) — roll back with\n"
        f"        git reset --hard {backup[:12]}"
    )
    if no_lint:
        return 0
    return int(cmd_lint(argparse.Namespace(skip_roundtrip=False, compare_to=None)))


def fold_after_apply(root: Path, state: dict, message_file: str | None) -> int:
    amend = ["commit", "--amend", "--no-verify", "-q"]
    amend += ["-F", message_file] if message_file else ["--no-edit"]
    git(amend, cwd=root, env={GUARD_ENV: "0"})
    state["new_target"] = git(
        ["rev-parse", "HEAD"], cwd=root, capture=True
    ).stdout.strip()
    state["phase"] = "rebase"
    save_fold_state(root, state)

    if state["old_tip"] != state["target"]:
        proc = git(
            ["rebase", "--onto", state["new_target"], state["target"], state["old_tip"]],
            cwd=root,
            check=False,
            capture=True,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout or "")
            sys.stderr.write(proc.stderr or "")
            print(
                "\nfold: the replay of the commits after "
                f"{state['patch_id']} conflicts.\n"
                "  resolve, `git add`, `git rebase --continue`, then:\n"
                "    patchctl fold --continue\n"
                "  or give up with: patchctl fold --abort",
                file=sys.stderr,
            )
            return 3
    return fold_finish(root, state)


def cmd_fold(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.abort:
        return fold_abort(root)

    state = load_fold_state(root)
    if args.continue_fold:
        if not state:
            print("fold: nothing to continue", file=sys.stderr)
            return 2
        if unmerged_paths(root):
            print(
                "fold: unresolved conflicts remain — resolve and `git add` first",
                file=sys.stderr,
            )
            return 2
        if state.get("phase") == "apply":
            return fold_after_apply(root, state, args.message_file)
        return fold_finish(root, state)
    if state:
        print(
            "fold: a fold is already in progress (--continue or --abort)",
            file=sys.stderr,
        )
        return 2
    if not args.patch_id:
        print("fold: a patch id is required (see maint/patchset.toml)", file=sys.stderr)
        return 2

    lock = UpstreamLock.load(root / "maint/upstream.lock.toml")
    base = lock.commit
    branch = current_branch(root)
    if not branch:
        print("fold: detached HEAD — check the sync branch out first", file=sys.stderr)
        return 2

    by_id = {pid: sha for sha, pid in commits_with_patch_id(root, base, "HEAD")}
    if args.patch_id not in by_id:
        print(
            f"fold: no commit carries {TRAILER_ID}={args.patch_id} in "
            f"{base[:12]}..HEAD",
            file=sys.stderr,
        )
        print("  available: " + ", ".join(sorted(by_id)), file=sys.stderr)
        return 2
    target = by_id[args.patch_id]

    # Normalize both entry points to "a trailerless commit sits at the tip":
    # nothing is ever stashed or reset away, so every state is recoverable.
    if git(["status", "--porcelain"], cwd=root, capture=True).stdout.strip():
        git(["add", "-A"], cwd=root)
        git(
            ["commit", "--no-verify", "-q", "-m", f"fold-wip: {args.patch_id}"],
            cwd=root,
            env={GUARD_ENV: "0"},
        )
    wip = git(["rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()
    if commit_trailer_id(root, wip) is not None:
        print(
            "fold: nothing to fold — the tree is clean and HEAD is already a "
            "queue commit",
            file=sys.stderr,
        )
        return 2
    parents = git(
        ["rev-list", "--parents", "-n", "1", wip], cwd=root, capture=True
    ).stdout.split()
    if len(parents) != 2:
        print("fold: HEAD is a merge or a root commit — unsupported", file=sys.stderr)
        return 2
    old_tip = parents[1]

    pending = range_changed_files(root, old_tip, wip)
    shared: dict[str, str] = {}
    for sha, pid in commits_with_patch_id(root, base, old_tip):
        if sha == target:
            continue
        for f in commit_changed_files(root, sha):
            if f in pending:
                shared.setdefault(f, pid)
    if shared and not args.allow_shared:
        print(
            f"fold: refused — {len(shared)} file(s) in this change are also "
            f"carried by another patch:",
            file=sys.stderr,
        )
        for f, pid in sorted(shared.items()):
            print(f"       {f}  (also in {pid})", file=sys.stderr)
        print(
            "\n  Folding here would still apply, but the patch would then claim a\n"
            "  change that belongs to its neighbour — and the next conflict lands\n"
            "  on whoever reads it. Split the change by hand, or accept the\n"
            "  attribution with --allow-shared.",
            file=sys.stderr,
        )
        return 3

    git(["update-ref", FOLD_BACKUP_REF, wip], cwd=root)
    patch_path = git_dir(root) / FOLD_PATCH_FILE
    patch_path.write_text(
        git(
            ["diff", "--binary", "--full-index", old_tip, wip], cwd=root, capture=True
        ).stdout,
        encoding="utf-8",
    )
    state = {
        "patch_id": args.patch_id,
        "branch": branch,
        "target": target,
        "old_tip": old_tip,
        "backup": wip,
        "phase": "apply",
        "no_lint": bool(args.no_lint),
    }
    save_fold_state(root, state)

    git(["checkout", "--detach", target, "-q"], cwd=root)
    proc = git(
        ["apply", "--3way", "--index", str(patch_path)],
        cwd=root,
        check=False,
        capture=True,
    )
    if proc.returncode != 0 or unmerged_paths(root):
        sys.stderr.write(proc.stderr or proc.stdout or "")
        print(
            f"\nfold: the change does not apply cleanly onto {args.patch_id} "
            f"({target[:12]}).\n"
            "  resolve the conflicts, `git add`, then:\n"
            "    patchctl fold --continue\n"
            "  or give up with: patchctl fold --abort",
            file=sys.stderr,
        )
        return 3
    return fold_after_apply(root, state, args.message_file)


def cmd_apply(args: argparse.Namespace) -> int:
    if getattr(args, "continue_apply", False):
        return cmd_apply_continue(args)
    root = repo_root()
    # A pending manual port is work in progress: restarting from scratch would
    # silently throw away the resolution. Only --force discards it.
    stale = load_apply_state(root)
    if stale is not None:
        if not args.force:
            print(
                f"interrupted apply pending on {stale['branch']} "
                f"(conflict on {stale['conflicted']}) — finish it with "
                "`apply --continue`, or discard it with --force",
                file=sys.stderr,
            )
            return 1
        git(["am", "--abort"], cwd=root, check=False)
        clear_apply_state(root)
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

    control_tmp = Path(tempfile.mkdtemp(prefix="grok-control-"))
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
        auto_resolved: list[dict] = []

        rc, _conflicted = apply_series(
            root,
            series,
            patch_src,
            meta,
            start=0,
            applied=applied,
            skipped=skipped,
            state_base={
                "branch": branch,
                "upstream": upstream_sha,
                "version": version,
                "source_rev": source_rev,
            },
            keep_conflict=args.keep_conflict,
            auto_resolve=args.auto_resolve_conflicts,
            auto_resolved=auto_resolved,
        )
        if rc is not None:
            # Control plane stays on disk for debugging; state lives in the git dir.
            return rc

        rc = verify_auto_resolved(root, meta, auto_resolved)
        if rc != 0:
            return rc

        rc = finish_apply(
            root,
            branch=branch,
            upstream_sha=upstream_sha,
            version=version,
            source_rev=source_rev,
            applied=applied,
            skipped=skipped,
            auto_resolved=auto_resolved,
        )
        if rc != 0:
            return rc

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

    with tempfile.TemporaryDirectory(prefix="grok-roundtrip-") as tmp:
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

    # No product commit outside the queue: the next apply would drop it.
    if git_resolvable(root, lock.commit):
        for sha, subject, reason in guard_offenders(root, lock.commit, "HEAD"):
            err(f"orphan commit {sha[:12]} ({subject}): {reason}")

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
        # Never carry fork/upstream CI workflows into functional patches;
        # control plane owns grok-privacy + watch/replay via control-files.
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
    a.add_argument("--upstream", default=None)
    a.add_argument("--branch", default=None)
    a.add_argument("--force", action="store_true")
    a.add_argument("--verify", action="store_true")
    a.add_argument("--skip-expensive", action="store_true")
    a.add_argument("--expect-version", default=None)
    a.add_argument("--expect-source-rev", default=None)
    a.add_argument(
        "--auto-resolve-conflicts",
        action="store_true",
        help="Allow the resolver command (PATCHCTL_CONFLICT_RESOLVER_CMD, "
        f"default '{DEFAULT_RESOLVER_CMD}') on a conflict rerere has never "
        "seen; a resolution is accepted only if the patch's declared contracts "
        "pass on the applied tree. Recorded resolutions replay with or without "
        "this flag — rerere needs no model and is never off",
    )
    a.add_argument(
        "--keep-conflict",
        action="store_true",
        help="On a critical conflict, leave the am in progress so it can be "
        "resolved in place (default: abort, keeping only the report)",
    )
    a.add_argument(
        "--continue",
        dest="continue_apply",
        action="store_true",
        help="Resume an apply interrupted by a conflict (see --keep-conflict)",
    )
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

    g = sub.add_parser(
        "guard",
        help="Refuse product commits the next apply would silently drop",
    )
    g.add_argument(
        "--install",
        action="store_true",
        help=f"Install the versioned commit-msg hook ({GUARD_HOOK_DIR}) via core.hooksPath",
    )
    g.add_argument("--uninstall", action="store_true", help="Unset core.hooksPath")
    g.add_argument("--commit-msg", default=None, help=argparse.SUPPRESS)
    g.add_argument("--base", default=None, help="Range start (default lock.commit)")
    g.add_argument("--tip", default="HEAD")
    g.set_defaults(func=cmd_guard)

    fo = sub.add_parser(
        "fold",
        help="Fold the working tree into an existing queue patch and re-export",
    )
    fo.add_argument(
        "patch_id",
        nargs="?",
        help=f"{TRAILER_ID} of the target patch (see maint/patchset.toml)",
    )
    fo.add_argument(
        "--allow-shared",
        action="store_true",
        help="Proceed even when a touched file is also carried by another patch",
    )
    fo.add_argument(
        "--message-file",
        default=None,
        help="Replace the target patch's commit message (keep its trailer!)",
    )
    fo.add_argument("--no-lint", action="store_true", help="Skip the closing lint")
    fo.add_argument(
        "--continue",
        dest="continue_fold",
        action="store_true",
        help="Resume a fold interrupted by a conflict",
    )
    fo.add_argument("--abort", action="store_true", help="Restore the pre-fold tip")
    fo.set_defaults(func=cmd_fold)

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
