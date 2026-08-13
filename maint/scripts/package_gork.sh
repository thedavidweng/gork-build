#!/usr/bin/env bash
# Build a user-facing GitHub Release archive from a compiled gork binary.
#
#   package_gork.sh --bin PATH --platform linux-x64 --tag v1.0.3-gork.1 [--output-dir DIR]
#
# Writes gork-<tag>-<platform>.tar.gz (unix) or .zip (win32-*) to the output dir
# and prints the archive path on stdout. Layout matches what users download:
# a single top-level `gork` / `gork.exe`.
set -euo pipefail

BIN=""
PLATFORM=""
TAG=""
OUTDIR="."

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin) BIN="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --output-dir) OUTDIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BIN" || -z "$PLATFORM" || -z "$TAG" ]]; then
  echo "usage: $0 --bin PATH --platform PLATFORM --tag TAG [--output-dir DIR]" >&2
  exit 2
fi
if [[ ! -f "$BIN" ]]; then
  echo "missing binary: $BIN" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/gork-pkg.XXXXXX")"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

if [[ "$PLATFORM" == win32-* ]]; then
  cp "$BIN" "${STAGE}/gork.exe"
  ASSET="gork-${TAG}-${PLATFORM}.zip"
  if command -v 7z >/dev/null 2>&1; then
    (cd "$STAGE" && 7z a -tzip "${OUTDIR}/${ASSET}" gork.exe >/dev/null)
  elif command -v zip >/dev/null 2>&1; then
    (cd "$STAGE" && zip -q "${OUTDIR}/${ASSET}" gork.exe)
  else
    echo "need 7z or zip to package win32" >&2
    exit 1
  fi
else
  cp "$BIN" "${STAGE}/gork"
  chmod +x "${STAGE}/gork"
  ASSET="gork-${TAG}-${PLATFORM}.tar.gz"
  tar -C "$STAGE" -czf "${OUTDIR}/${ASSET}" gork
fi

echo "${OUTDIR}/${ASSET}"
