#!/usr/bin/env bash
# Smoke a user-facing Gork Build release archive the way a downloader would:
# extract, check the binary, and (when the host can run it) execute --version,
# --help, and `update` (must refuse vendor installers).
#
#   smoke_release_archive.sh ARCHIVE [--expect-version VER] [--platform PLATFORM]
#
# Exit 0 only if the archive is a usable install package.
set -euo pipefail

ARCHIVE=""
EXPECT_VERSION="${GORK_EXPECT_VERSION:-}"
PLATFORM="${GORK_SMOKE_PLATFORM:-}"
MIN_BYTES="${GORK_SMOKE_MIN_BYTES:-5000000}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-version) EXPECT_VERSION="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
    *)
      if [[ -z "$ARCHIVE" ]]; then ARCHIVE="$1"; shift
      else echo "unexpected arg: $1" >&2; exit 2
      fi
      ;;
  esac
done

if [[ -z "$ARCHIVE" ]]; then
  echo "usage: $0 ARCHIVE [--expect-version VER] [--platform PLATFORM]" >&2
  exit 2
fi
if [[ ! -f "$ARCHIVE" ]]; then
  echo "FAIL: archive not found: $ARCHIVE" >&2
  exit 1
fi

if [[ -z "$PLATFORM" ]]; then
  base="$(basename "$ARCHIVE")"
  # gork-<tag>-<platform>.tar.gz | .zip
  if [[ "$base" =~ -(linux-x64|linux-arm64|darwin-x64|darwin-arm64|win32-x64|win32-arm64)\.(tar\.gz|zip)$ ]]; then
    PLATFORM="${BASH_REMATCH[1]}"
  fi
fi

SIZE=$(wc -c <"$ARCHIVE" | tr -d ' ')
echo "archive: $ARCHIVE"
echo "size:    $SIZE bytes"
echo "platform:${PLATFORM:- (unknown)}"
if [[ "$SIZE" -lt "$MIN_BYTES" ]]; then
  echo "FAIL: archive is ${SIZE} bytes; expected at least ${MIN_BYTES} (truncated or empty package)" >&2
  exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/gork-smoke.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

case "$ARCHIVE" in
  *.tar.gz|*.tgz)
    tar -C "$WORKDIR" -xzf "$ARCHIVE"
    EXPECT_NAME="gork"
    ;;
  *.zip)
    if command -v unzip >/dev/null 2>&1; then
      unzip -q -o "$ARCHIVE" -d "$WORKDIR"
    elif command -v 7z >/dev/null 2>&1; then
      7z x -y -o"$WORKDIR" "$ARCHIVE" >/dev/null
    else
      echo "FAIL: need unzip or 7z to extract $ARCHIVE" >&2
      exit 1
    fi
    EXPECT_NAME="gork.exe"
    ;;
  *)
    echo "FAIL: unknown archive type (want .tar.gz or .zip): $ARCHIVE" >&2
    exit 1
    ;;
esac

# Users get a single top-level binary. Nested layouts or extra payload fail.
# (bash 3.2 on macOS has no mapfile)
ENTRIES=()
while IFS= read -r entry; do
  ENTRIES+=("$entry")
done < <(find "$WORKDIR" -mindepth 1 -maxdepth 1 -print | sort)
if [[ "${#ENTRIES[@]}" -ne 1 ]]; then
  echo "FAIL: archive must contain exactly one top-level entry, got ${#ENTRIES[@]}:" >&2
  printf '  %s\n' "${ENTRIES[@]}" >&2
  exit 1
fi
BIN="${ENTRIES[0]}"
GOT_NAME="$(basename "$BIN")"
if [[ "$GOT_NAME" != "$EXPECT_NAME" ]]; then
  echo "FAIL: expected top-level '$EXPECT_NAME', got '$GOT_NAME'" >&2
  exit 1
fi
if [[ ! -f "$BIN" ]]; then
  echo "FAIL: $GOT_NAME is not a regular file" >&2
  exit 1
fi

# File-format magic: catch a shell script / empty file shipped as the binary.
hexdump_head() { od -An -tx1 -N 4 "$1" 2>/dev/null | tr -s ' ' | sed 's/^ //'; }
MAGIC="$(hexdump_head "$BIN")"
echo "magic:   $MAGIC"
case "$EXPECT_NAME" in
  gork.exe)
    if [[ "$MAGIC" != 4d\ 5a* ]]; then
      echo "FAIL: $GOT_NAME is not a PE executable (missing MZ header)" >&2
      exit 1
    fi
    ;;
  gork)
    case "$PLATFORM" in
      darwin-*)
        # Mach-O 64-bit LE (cf fa ed fe) or fat (ca fe ba be)
        if [[ "$MAGIC" != cf\ fa\ ed\ fe* && "$MAGIC" != ca\ fe\ ba\ be* && "$MAGIC" != ce\ fa\ ed\ fe* ]]; then
          echo "FAIL: $GOT_NAME is not a Mach-O binary" >&2
          exit 1
        fi
        ;;
      linux-*|"")
        if [[ "$MAGIC" != 7f\ 45\ 4c\ 46* ]]; then
          echo "FAIL: $GOT_NAME is not an ELF binary" >&2
          exit 1
        fi
        ;;
    esac
    ;;
esac

chmod +x "$BIN" 2>/dev/null || true

host_can_run() {
  case "$(uname -s)-$(uname -m)-${PLATFORM}" in
    Linux-x86_64-linux-x64) return 0 ;;
    Linux-aarch64-linux-arm64) return 0 ;;
    Linux-arm64-linux-arm64) return 0 ;;
    Darwin-arm64-darwin-arm64) return 0 ;;
    Darwin-x86_64-darwin-x64) return 0 ;;
    MINGW*-win32-x64|MSYS*-win32-x64|CYGWIN*-win32-x64) return 0 ;;
    *-win32-x64)
      # GitHub windows-latest reports MINGW via uname in bash.
      if [[ "${OS:-}" == "Windows_NT" || "${RUNNER_OS:-}" == "Windows" ]]; then
        return 0
      fi
      ;;
  esac
  # Last resort: try exec; Exec format error means skip runtime.
  if "$BIN" --version >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if host_can_run && [[ "$EXPECT_NAME" == "gork" && "$(uname -s)" == "Linux" ]] && command -v ldd >/dev/null 2>&1; then
  if ldd "$BIN" 2>/dev/null | grep -q 'not found'; then
    echo "FAIL: binary is missing shared libraries:" >&2
    ldd "$BIN" || true
    exit 1
  fi
fi

if ! host_can_run; then
  echo "structure ok; skipping runtime smoke (host cannot execute ${PLATFORM:-this} binary)"
  echo "PASS: archive layout + format"
  exit 0
fi

export GROK_HOME="$WORKDIR/grok-home"
mkdir -p "$GROK_HOME"

echo "==> $GOT_NAME --version"
VERSION_OUT="$("$BIN" --version 2>&1)" || {
  echo "FAIL: --version exited $?:" >&2
  echo "$VERSION_OUT" >&2
  exit 1
}
echo "$VERSION_OUT"
if ! echo "$VERSION_OUT" | grep -qiE 'gork'; then
  echo "FAIL: --version must mention gork/Gork" >&2
  exit 1
fi
if [[ -n "$EXPECT_VERSION" ]] && ! echo "$VERSION_OUT" | grep -Fq "$EXPECT_VERSION"; then
  echo "FAIL: --version must contain '${EXPECT_VERSION}'" >&2
  exit 1
fi

echo "==> $GOT_NAME --help"
HELP_OUT="$("$BIN" --help 2>&1)" || {
  echo "FAIL: --help exited $?:" >&2
  echo "$HELP_OUT" >&2
  exit 1
}
echo "$HELP_OUT" | head -20
if ! echo "$HELP_OUT" | grep -q 'Gork Build TUI'; then
  echo "FAIL: --help must contain 'Gork Build TUI'" >&2
  exit 1
fi
if ! echo "$HELP_OUT" | grep -q 'Usage: gork'; then
  echo "FAIL: --help must contain 'Usage: gork'" >&2
  exit 1
fi

echo "==> $GOT_NAME update (must refuse vendor install)"
UPDATE_EC=0
UPDATE_OUT="$("$BIN" update 2>&1)" || UPDATE_EC=$?
echo "$UPDATE_OUT" | head -40
# A crash or network error is not a privacy refusal. Require the hard-off text.
if ! echo "$UPDATE_OUT" | grep -qiE 'never installs from vendor|rebuild from source|Auto-update is not available|privacy build never'; then
  echo "FAIL: gork update did not print a vendor/privacy refusal (exit ${UPDATE_EC})" >&2
  echo "$UPDATE_OUT" >&2
  exit 1
fi
echo "update refused vendor install (ok, exit ${UPDATE_EC})"
if echo "$UPDATE_OUT" | grep -qE 'curl -fsSL https://x\.ai/cli|irm https://x\.ai/cli'; then
  echo "FAIL: update output recommends a vendor installer" >&2
  exit 1
fi

echo "PASS: extracted $GOT_NAME from $(basename "$ARCHIVE") is usable"
