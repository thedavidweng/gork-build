#!/usr/bin/env python3
"""Apply security minimum-version floors to the upstream-inherited Cargo.lock.

Reads maint/security-floors.toml and, for each floor, runs
`cargo update -p <package>@<locked> --precise <precise>` ONLY when the locked
version is strictly lower than the floor. Never downgrades; exits non-zero on
ambiguity (multiple locked versions of a floored package) so the sync fails
closed instead of guessing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def parse_ver(v: str) -> tuple:
    """Best-effort semver key; pre-release tails compare as strings."""
    parts = []
    for piece in v.split("+")[0].split("-", 1)[0].split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return (tuple(parts), v)


def main() -> int:
    control = Path(os.environ.get("GORK_CONTROL_ROOT") or Path(__file__).resolve().parents[2])
    tree = Path(os.environ.get("GORK_WORK_SRC") or control)
    floors_path = control / "maint" / "security-floors.toml"
    lock_path = tree / "Cargo.lock"
    if not floors_path.is_file():
        print("security-floors: no maint/security-floors.toml — nothing to do")
        return 0
    floors = tomllib.loads(floors_path.read_text(encoding="utf-8")).get("floor") or []
    if not floors:
        print("security-floors: empty floor list — nothing to do")
        return 0

    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    locked: dict[str, list[str]] = {}
    for pkg in lock.get("package") or []:
        locked.setdefault(pkg["name"], []).append(pkg["version"])

    rc = 0
    for floor in floors:
        name = floor["package"]
        precise = floor["precise"]
        advisory = floor.get("advisory", "")
        versions = locked.get(name)
        if not versions:
            print(f"security-floors: {name} not in Cargo.lock — skip ({advisory})")
            continue
        if len(versions) > 1:
            print(
                f"security-floors: {name} has multiple locked versions {versions}; "
                f"refusing to guess — resolve manually ({advisory})",
                file=sys.stderr,
            )
            rc = 1
            continue
        current = versions[0]
        if parse_ver(current)[0] >= parse_ver(precise)[0]:
            print(f"security-floors: {name} {current} >= {precise} — no-op ({advisory})")
            continue
        print(f"security-floors: {name} {current} -> {precise} ({advisory})")
        proc = subprocess.run(
            ["cargo", "update", "-p", f"{name}@{current}", "--precise", precise],
            cwd=tree,
        )
        if proc.returncode != 0:
            print(f"security-floors: cargo update failed for {name}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
