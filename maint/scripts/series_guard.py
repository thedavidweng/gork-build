#!/usr/bin/env python3
"""Non-regression guard for the privacy patch queue.

`patchctl finalize-sync` re-exports maint/patches from the commits present on
the current branch, then roundtrips the result against the product tree. That
proves series/tree *coherence* — it cannot prove *non-regression*: an amputated
commit re-exports into an amputated patch that roundtrips perfectly against an
amputated tree. Substance lost on the branch is silently frozen into the queue.

This guard closes that gap by comparing the queue against a snapshot taken
before the re-export.

  series_guard.py snapshot --out BASE.json
  series_guard.py compare  --baseline BASE.json [--max-shrink-pct N]

compare exits 1 when a patch vanished, stopped touching a file it used to
touch, or lost more than --max-shrink-pct of its added lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


DEFAULT_MAX_SHRINK_PCT = 20


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def patches_dir(root: Path) -> Path:
    return root / "maint/patches"


def measure(path: Path) -> dict:
    """Substance metrics for one .patch file.

    added   — payload lines (`+foo`), excluding the `+++ b/...` file headers.
    targets — files the patch actually touches.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    added = 0
    targets: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if name and name != "/dev/null":
                targets.append(name)
        elif line.startswith("+"):
            added += 1
    return {
        "lines": len(text.splitlines()),
        "added": added,
        "targets": sorted(set(targets)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def collect(root: Path) -> dict[str, dict]:
    d = patches_dir(root)
    if not d.is_dir():
        return {}
    return {p.name: measure(p) for p in sorted(d.glob("*.patch"))}


def cmd_snapshot(args: argparse.Namespace) -> int:
    root = repo_root()
    data = {"schema": 1, "patches": collect(root)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"series snapshot: {len(data['patches'])} patch(es) -> {out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    root = repo_root()
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"series guard: no baseline at {baseline_path} — skipped", file=sys.stderr)
        return 0

    baseline = json.loads(baseline_path.read_text()).get("patches", {})
    if not baseline:
        print("series guard: empty baseline — skipped", file=sys.stderr)
        return 0

    current = collect(root)
    limit = args.max_shrink_pct
    regressions: list[str] = []
    changed = 0

    for name, was in sorted(baseline.items()):
        now = current.get(name)
        if now is None:
            regressions.append(f"{name}: DISPARU (etait {was['added']} lignes ajoutees)")
            continue
        if now["sha256"] == was["sha256"]:
            continue
        changed += 1

        lost = sorted(set(was["targets"]) - set(now["targets"]))
        if lost:
            regressions.append(
                f"{name}: ne touche plus {len(lost)} fichier(s): {', '.join(lost)}"
            )

        before, after = was["added"], now["added"]
        if before > 0 and after < before:
            shrink = round((before - after) * 100 / before)
            if shrink > limit:
                regressions.append(
                    f"{name}: -{shrink}% de substance "
                    f"({before} -> {after} lignes ajoutees, seuil {limit}%)"
                )

    for name in sorted(set(current) - set(baseline)):
        print(f"series guard: nouveau patch {name}")

    if regressions:
        print(
            f"\nseries guard: {len(regressions)} regression(s) dans la file de patches",
            file=sys.stderr,
        )
        for r in regressions:
            print(f"  [regression] {r}", file=sys.stderr)
        return 1

    print(f"series guard OK ({changed} patch(es) modifie(s), aucune perte de substance)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="series_guard",
        description="Detect substance loss in the privacy patch queue.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Record current queue metrics")
    p_snap.add_argument("--out", required=True, help="Snapshot JSON path")
    p_snap.set_defaults(func=cmd_snapshot)

    p_cmp = sub.add_parser("compare", help="Compare queue against a snapshot")
    p_cmp.add_argument("--baseline", required=True, help="Snapshot JSON path")
    p_cmp.add_argument(
        "--max-shrink-pct",
        type=int,
        default=DEFAULT_MAX_SHRINK_PCT,
        help=f"Tolerated loss of added lines (default {DEFAULT_MAX_SHRINK_PCT})",
    )
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
