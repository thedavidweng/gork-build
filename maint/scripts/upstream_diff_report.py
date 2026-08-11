#!/usr/bin/env python3
"""Report sensitive-path changes between two upstream commits."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


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


def load_patterns(path: Path) -> list[str]:
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def git_diff_names(root: Path, old: str, new: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", old, new],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode or 1)
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def glob_to_regex(pat: str) -> re.Pattern[str]:
    """Translate a simplified git-style glob (with **) to a fullmatch regex."""
    i = 0
    out: list[str] = ["^"]
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pat[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def match_sensitive(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        # Directory prefix without glob
        if "**" not in pat and "*" not in pat and "?" not in pat:
            if path == pat or path.startswith(pat.rstrip("/") + "/"):
                return True
            continue
        if glob_to_regex(pat).match(path):
            return True
        # Also allow directory-style patterns that omit trailing **
        if pat.endswith("/**") and glob_to_regex(pat[: -len("/**")]).match(path):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", help="Old upstream commit SHA")
    parser.add_argument("new", help="New upstream commit SHA")
    parser.add_argument(
        "--sensitive-paths",
        type=Path,
        default=None,
        help="Path to sensitive-paths.txt",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary",
    )
    parser.add_argument(
        "--fail-on-sensitive",
        action="store_true",
        help="Exit 2 when any sensitive path changed (gate mode)",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    sens_path = args.sensitive_paths or (root / "maint/sensitive-paths.txt")
    patterns = load_patterns(sens_path)
    names = git_diff_names(root, args.old, args.new)
    sensitive = [n for n in names if match_sensitive(n, patterns)]

    payload = {
        "old": args.old,
        "new": args.new,
        "total_files_changed": len(names),
        "sensitive_count": len(sensitive),
        "paths": sensitive,
        "security_review_required": len(sensitive) > 0,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"upstream diff {args.old[:12]}..{args.new[:12]}")
        print(f"total files changed: {len(names)}")
        print(f"sensitive files changed: {len(sensitive)}")
        print("--- sensitive ---")
        for n in sensitive:
            print(n)
        if not sensitive:
            print("(none)")
        if sensitive:
            print("security_review_required: true")

    if args.fail_on_sensitive and sensitive:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
