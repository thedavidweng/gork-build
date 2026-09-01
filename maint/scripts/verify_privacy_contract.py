#!/usr/bin/env python3
"""Run privacy contracts defined in maint/contracts/privacy-contract.toml."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[2]


def load_contracts(path: Path) -> list[dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return list(data.get("contract") or [])


TEST_RESULT_RE = re.compile(r"^test result: \w+\. (\d+) passed")


def run_contract(root: Path, contract: dict, *, skip_expensive: bool) -> int:
    cid = contract["id"]
    if contract.get("expensive") and skip_expensive:
        print(f"[skip] {cid} (expensive)")
        return 0
    cmd = contract["command"]
    env = os.environ.copy()
    for key, val in (contract.get("env") or {}).items():
        env[str(key)] = str(val)
    print(f"[run]  {cid}: {' '.join(cmd)}")

    # A filter that matches nothing makes cargo exit 0 having run zero tests:
    # the contract then reports [ok] while proving nothing. Stream the output
    # (so long suites stay readable) and total the `test result:` lines.
    passed = 0
    proc = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        sys.stdout.write(line)
        m = TEST_RESULT_RE.match(line)
        if m:
            passed += int(m.group(1))
    rc = proc.wait()

    if rc != 0:
        print(f"[fail] {cid} exit={rc}", file=sys.stderr)
        return rc

    floor = contract.get("min_tests")
    if floor is not None and passed < int(floor):
        print(
            f"[fail] {cid}: {passed} test(s) exécuté(s), plancher {floor} — "
            "le filtre ne matche plus rien (test renommé/supprimé), "
            "le contrat ne prouve plus sa garantie",
            file=sys.stderr,
        )
        return 1

    detail = f" ({passed} test(s))" if passed else ""
    print(f"[ok]   {cid}{detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contracts",
        type=Path,
        default=None,
        help="Path to privacy-contract.toml",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only run contracts with these ids (repeatable)",
    )
    parser.add_argument(
        "--exclude-group",
        action="append",
        default=[],
        help="Skip contracts in these groups (repeatable), e.g. supply-chain",
    )
    parser.add_argument(
        "--only-group",
        action="append",
        default=[],
        help="Only run contracts in these groups (repeatable)",
    )
    parser.add_argument(
        "--skip-expensive",
        action="store_true",
        help="Skip contracts marked expensive=true",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    contracts_path = args.contracts or (root / "maint/contracts/privacy-contract.toml")
    if not contracts_path.is_file():
        print(f"missing contracts file: {contracts_path}", file=sys.stderr)
        return 2

    contracts = load_contracts(contracts_path)

    if args.only:
        want = set(args.only)
        contracts = [c for c in contracts if c["id"] in want]
        missing = want - {c["id"] for c in contracts}
        if missing:
            print(f"unknown contract ids: {sorted(missing)}", file=sys.stderr)
            return 2

    if args.only_group:
        groups = set(args.only_group)
        contracts = [c for c in contracts if c.get("group", "privacy") in groups]

    if args.exclude_group:
        excluded = set(args.exclude_group)
        contracts = [
            c for c in contracts if c.get("group", "privacy") not in excluded
        ]

    if not contracts:
        print("no contracts to run", file=sys.stderr)
        return 2

    failed = 0
    for c in contracts:
        rc = run_contract(root, c, skip_expensive=args.skip_expensive)
        if rc != 0:
            failed += 1
    if failed:
        print(f"{failed} contract(s) failed", file=sys.stderr)
        return 1
    print("all contracts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
