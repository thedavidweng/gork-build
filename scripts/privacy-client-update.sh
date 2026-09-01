#!/usr/bin/env bash
# privacy-client-update.sh — Mise à jour du binaire client installé
#
# Entrée :  grok update […]
#
# Ce n'est PAS l'atelier mainteneur (sync officiel / patches / push repo).
# Pour ça :  grok rebuild
#
# Stratégie :
#   1. Si une GitHub Release de lycaos/grok-privacy a un asset pour cette plateforme
#      → télécharger + installer dans ~/.grok/bin/grok
#   2. Sinon, fallback optionnel --from-source : pull main + cargo build local
#      (machine de dev avec le clone ; pas le chemin multi-OS « propre »)
#
set -euo pipefail

VERSION_SCRIPT="1.4.0"

export PATH="${HOME}/.cargo/bin:${PATH}"
export LANG="${LANG:-fr_FR.UTF-8}"
export LC_ALL="${LC_ALL:-$LANG}"

REPO="${GROK_PRIVACY_REPO:-$HOME/grok-privacy}"

# Sous Windows (Git Bash / MSYS), un fichier sans `.exe` n'est pas exécutable :
# le suffixe fait partie du nom d'installation, pas d'un détail cosmétique.
# Sans ça, `grok update` installait un `~/.grok/bin/grok` que rien ne pouvait
# lancer — la seule plateforme où ce script sert vraiment.
case "$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')" in
  mingw*|msys*|cygwin*|windows*) EXE_SUFFIX=".exe"; IS_WINDOWS=1 ;;
  *) EXE_SUFFIX=""; IS_WINDOWS=0 ;;
esac

BIN_INSTALL="${GROK_PRIVACY_BIN:-$HOME/.grok/bin/grok${EXE_SUFFIX}}"
BIN_PRIVACY_COPY="${GROK_PRIVACY_BIN_COPY:-$HOME/.local/bin/grokp${EXE_SUFFIX}}"
GH_REPO="${GROK_PRIVACY_GH_REPO:-lycaos/grok-privacy}"
PREFERRED_BRANCH="${GROK_PRIVACY_PREFERRED_BRANCH:-main}"
CARGO_BIN="${CARGO:-cargo}"
PYTHON_BIN="${PYTHON:-python3}"

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_CYAN=$'\033[36m'
  C_RESET=$'\033[0m'
else
  C_BOLD= C_DIM= C_GREEN= C_YELLOW= C_RED= C_CYAN= C_RESET=
fi

log()  { printf '%s\n' "$*"; }
info() { printf '%s>>%s %s\n' "$C_CYAN" "$C_RESET" "$*"; }
ok()   { printf '%sOK%s  %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s!!%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%sÉCHEC%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() { printf '\n%s── %s ──%s\n' "$C_BOLD" "$*" "$C_RESET"; }

banner() {
  printf '%s╭─ grok-privacy · update client · v%s ─%s\n' "$C_BOLD" "$VERSION_SCRIPT" "$C_RESET"
  printf '%s│%s  Met à jour le binaire installé (%s).\n' "$C_DIM" "$C_RESET" "$BIN_INSTALL"
  printf '%s│%s  Atelier mainteneur (repo / checks / push) : %sgrok rebuild%s\n' "$C_DIM" "$C_RESET" "$C_CYAN" "$C_RESET"
  printf '%s╰%s\n' "$C_DIM" "$C_RESET"
}

usage() {
  cat <<EOF
grok-privacy update client v${VERSION_SCRIPT}

Met à jour le binaire Grok privacy installé sur CETTE machine.
Ne touche pas à la série de patches ni au lock upstream.

Usage :
  grok update [OPTIONS]

Options :
  --check, check     Afficher versions installée / disponible (aucune mutation)
  --from-source      Fallback : pull ${PREFERRED_BRANCH} + cargo build (clone local)
  --yes, -y          Non interactif (accepte le téléchargement / install)
  -h, --help         Cette aide
  --version          Version du script

Variables :
  GROK_PRIVACY_BIN           défaut: ~/.grok/bin/grok
  GROK_PRIVACY_GH_REPO       défaut: lycaos/grok-privacy
  GROK_PRIVACY_REPO          défaut: ~/grok-privacy (pour --from-source)

Séparation :
  grok update    → binaire client (ce script)
  grok rebuild   → atelier : sonde, sync officiel, finalize, push repo
EOF
}

MODE="update"
AUTO_YES=0
FROM_SOURCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|aide) usage; exit 0 ;;
    --version) echo "grok-privacy-client-update $VERSION_SCRIPT"; exit 0 ;;
    --check|check) MODE=check ;;
    --from-source) FROM_SOURCE=1 ;;
    --yes|-y) AUTO_YES=1 ;;
    # Ne jamais confondre avec l'atelier
    --upstream|--finalize|--push-repo|--privacy-from-fork|--all|--menu|--verify-only)
      die "option d’atelier « $1 » — utilisez : grok rebuild $1"
      ;;
    *)
      die "argument inconnu : $1 (essayez --help ; atelier = grok rebuild)"
      ;;
  esac
  shift
done

detect_platform() {
  local os arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
  esac
  case "$os" in
    linux) PLATFORM="linux-${arch}" ;;
    darwin) PLATFORM="darwin-${arch}" ;;
    mingw*|msys*|cygwin*) PLATFORM="windows-${arch}" ;;
    *) PLATFORM="${os}-${arch}" ;;
  esac
  # Noms d’assets attendus (alignés release future)
  ASSET_CANDIDATES=(
    "grok-${PLATFORM}"
    "grokp-${PLATFORM}"
    "grok-${PLATFORM}.tar.gz"
    "grokp-${PLATFORM}.tar.gz"
    "grok-${PLATFORM}.zip"
  )
  if [[ "$os" == mingw* || "$os" == msys* || "$os" == cygwin* || "$os" == windows* ]]; then
    ASSET_CANDIDATES+=("grokp-windows-x86_64.exe" "grok-windows-x86_64.exe" "grokp.exe" "grok.exe")
  fi
}

installed_version() {
  if [[ -x "$BIN_INSTALL" ]] || [[ -L "$BIN_INSTALL" ]]; then
    "$BIN_INSTALL" --version 2>/dev/null | head -1 || echo "(illisible)"
  else
    echo "(absent : $BIN_INSTALL)"
  fi
}

confirm_yes() {
  local prompt="$1"
  if [[ "$AUTO_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    err "Confirmation requise (stdin non interactif). Relancez avec -y."
    return 1
  fi
  local ans
  printf '%s\n' "$prompt"
  read -r -p "Confirmer ? [o/N] : " ans || true
  case "${ans:-}" in
    o|O|oui|OUI|y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

# Renvoie JSON latest release via gh ou curl
fetch_latest_release_json() {
  if command -v gh >/dev/null 2>&1; then
    gh api "repos/${GH_REPO}/releases/latest" 2>/dev/null && return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "https://api.github.com/repos/${GH_REPO}/releases/latest" 2>/dev/null && return 0
  fi
  return 1
}

# Le parsing passait par Python « pour la portabilité ». Git Bash ne fournit
# pas Python : c'était précisément la plateforme où ce script sert. On garde
# Python quand il est là (appariement plus fin), avec un repli sans dépendance.
pick_release_asset() {
  local json="$1" out=""
  if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    out="$(pick_release_asset_python "$json" || true)"
  fi
  if [[ -z "${out//[[:space:]]/}" ]]; then
    out="$(pick_release_asset_shell "$json" || true)"
  fi
  printf '%s' "$out"
}

# Repli sans Python : le nom d'un asset est le dernier segment de son URL de
# téléchargement, donc une seule extraction suffit — inutile d'apparier deux
# champs JSON à coups d'expressions régulières.
pick_release_asset_shell() {
  local json="$1" tag urls u base c token
  tag="$(printf '%s' "$json" | grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
         | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/')"
  urls="$(printf '%s' "$json" | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]*"' \
          | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\1/')"
  for c in "${ASSET_CANDIDATES[@]}"; do
    while IFS= read -r u; do
      [[ -n "$u" ]] || continue
      base="${u##*/}"
      if [[ "$base" == "$c" ]]; then printf '%s\t%s\t%s\n' "$tag" "$base" "$u"; return 0; fi
    done <<< "$urls"
  done
  token="${PLATFORM%%-*}"
  while IFS= read -r u; do
    [[ -n "$u" ]] || continue
    base="${u##*/}"
    case "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" in
      *"$token"*) printf '%s\t%s\t%s\n' "$tag" "$base" "$u"; return 0 ;;
    esac
  done <<< "$urls"
  printf '%s\t\t\n' "$tag"
  return 0
}

pick_release_asset_python() {
  local json="$1"
  "$PYTHON_BIN" - "$json" <<'PY' || true
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    sys.exit(1)
tag = data.get("tag_name") or ""
assets = data.get("assets") or []
# candidates passed via env
import os
cands = os.environ.get("GROK_ASSET_CANDIDATES", "").split("|")
names = {a.get("name", ""): a.get("browser_download_url", "") for a in assets}
# exact match first
for c in cands:
    c = c.strip()
    if not c:
        continue
    if c in names and names[c]:
        print(f"{tag}\t{c}\t{names[c]}")
        sys.exit(0)
# fuzzy: contains platform token
plat = os.environ.get("GROK_PLATFORM", "")
for name, url in names.items():
    low = name.lower()
    if plat and plat.replace("-", "").lower() in low.replace("-", "").replace("_", ""):
        print(f"{tag}\t{name}\t{url}")
        sys.exit(0)
    if "linux" in plat and "linux" in low and ("x86_64" in low or "amd64" in low):
        print(f"{tag}\t{name}\t{url}")
        sys.exit(0)
    if "windows" in plat and "windows" in low:
        print(f"{tag}\t{name}\t{url}")
        sys.exit(0)
# no match but release exists
print(f"{tag}\t\t")
sys.exit(0)
PY
}

install_binary_file() {
  local src="$1"
  mkdir -p "$(dirname "$BIN_INSTALL")"
  if [[ -L "$BIN_INSTALL" ]]; then
    rm -f "$BIN_INSTALL"
  fi
  local tmp="${BIN_INSTALL}.new.$$"
  install -m 755 "$src" "$tmp"
  # Windows verrouille un exécutable en cours d'exécution : on ne peut pas
  # l'écraser, mais on peut le renommer. On décale l'ancien puis on met le neuf
  # en place ; le nettoyage du décalé échouera tant qu'il tourne, sans importance.
  if [[ "$IS_WINDOWS" == "1" && -e "$BIN_INSTALL" ]]; then
    mv -f "$BIN_INSTALL" "${BIN_INSTALL}.old" 2>/dev/null || true
  fi
  mv -f "$tmp" "$BIN_INSTALL"
  if [[ "$IS_WINDOWS" == "1" ]]; then
    rm -f "${BIN_INSTALL}.old" 2>/dev/null || true
  fi
  mkdir -p "$(dirname "$BIN_PRIVACY_COPY")"
  install -m 755 "$src" "$BIN_PRIVACY_COPY"
  ok "Installé : $BIN_INSTALL"
  "$BIN_INSTALL" --version || true
}

try_gh_release_update() {
  step "Recherche d’une release GitHub ($GH_REPO)"
  detect_platform
  info "Plateforme détectée : $PLATFORM"
  info "Binaire actuel     : $(installed_version)"

  local json
  if ! json="$(fetch_latest_release_json)"; then
    warn "Impossible d’interroger les releases (gh/curl ou réseau)."
    return 1
  fi

  if echo "$json" | grep -q 'Not Found\|"message": "Not Found"'; then
    warn "Aucune release publiée sur $GH_REPO pour l’instant."
    return 1
  fi

  export GROK_PLATFORM="$PLATFORM"
  export GROK_ASSET_CANDIDATES
  GROK_ASSET_CANDIDATES="$(IFS='|'; echo "${ASSET_CANDIDATES[*]}")"
  export GROK_ASSET_CANDIDATES

  PYTHON_BIN="${PYTHON:-python3}"
  local pick tag name url
  pick="$(pick_release_asset "$json")"
  tag="$(printf '%s' "$pick" | cut -f1)"
  name="$(printf '%s' "$pick" | cut -f2)"
  url="$(printf '%s' "$pick" | cut -f3)"

  if [[ -z "$tag" ]]; then
    warn "Réponse release illisible."
    return 1
  fi
  info "Dernière release : $tag"

  if [[ -z "$url" || -z "$name" ]]; then
    warn "Release $tag trouvée, mais aucun asset pour $PLATFORM."
    info "Assets attendus (ex.) : grok-linux-x86_64 , grok-windows-x86_64.exe"
    info "Publier les binaires via l’atelier (grok rebuild + CI release)."
    return 1
  fi

  info "Asset : $name"
  if ! confirm_yes "Télécharger et installer $name ($tag) → $BIN_INSTALL ?"; then
    warn "Mise à jour annulée."
    return 1
  fi

  local tmpdir
  tmpdir="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmpdir'" RETURN

  info "Téléchargement…"
  if command -v gh >/dev/null 2>&1 && [[ "$url" == https://github.com/* ]]; then
    # gh release download is cleaner when possible
    if gh release download "$tag" --repo "$GH_REPO" --pattern "$name" --dir "$tmpdir" 2>/dev/null; then
      :
    else
      curl -fsSL -o "$tmpdir/$name" "$url"
    fi
  else
    curl -fsSL -o "$tmpdir/$name" "$url"
  fi

  local binpath="$tmpdir/$name"
  if [[ "$name" == *.tar.gz ]]; then
    tar -xzf "$binpath" -C "$tmpdir"
    binpath="$(find "$tmpdir" -type f -executable \( -name 'grok' -o -name 'grokp' \) | head -1 || true)"
    [[ -n "$binpath" ]] || die "archive sans binaire grok"
  elif [[ "$name" == *.zip ]]; then
    command -v unzip >/dev/null || die "unzip requis pour .zip"
    unzip -q "$binpath" -d "$tmpdir"
    binpath="$(find "$tmpdir" -type f \( -name 'grok' -o -name 'grok.exe' -o -name 'grokp' -o -name 'grokp.exe' \) | head -1 || true)"
    [[ -n "$binpath" ]] || die "zip sans binaire grok"
    chmod +x "$binpath" || true
  else
    chmod +x "$binpath"
  fi

  install_binary_file "$binpath"
  ok "Mise à jour client depuis la release $tag"
  return 0
}

try_from_source() {
  step "Fallback source (clone local)"
  [[ -d "$REPO/.git" ]] || die "clone introuvable : $REPO (GROK_PRIVACY_REPO)"
  cd "$REPO"

  info "Fetch + ff-only origin/$PREFERRED_BRANCH…"
  git fetch origin "$PREFERRED_BRANCH" 2>&1 | tail -5 || warn "fetch origin échoué"
  local br
  br="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$br" != "$PREFERRED_BRANCH" ]]; then
    warn "HEAD=$br — checkout $PREFERRED_BRANCH pour un update source « prod »"
    if confirm_yes "Basculer sur $PREFERRED_BRANCH ?"; then
      git checkout "$PREFERRED_BRANCH" || die "checkout impossible"
    fi
  fi
  if git rev-parse --verify --quiet "origin/$PREFERRED_BRANCH" &>/dev/null; then
    git merge --ff-only "origin/$PREFERRED_BRANCH" || \
      die "ff-only impossible — arbre local divergent (résoudre à la main ou grok rebuild)"
  fi

  info "Compilation release…"
  "$CARGO_BIN" build -p xai-grok-pager-bin --release
  # Le nom du binaire a bougé au fil des renames : ne jamais le deviner, cargo
  # produit exactement ce que `default-run` déclare.
  local manifest="crates/codegen/xai-grok-pager-bin/Cargo.toml"
  local binname
  binname="$(grep -m1 -E '^default-run[[:space:]]*=' "$manifest" 2>/dev/null \
    | sed -E 's/^default-run[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')"
  [[ -n "$binname" ]] || die "default-run absent de $manifest"
  local built="target/release/$binname"
  [[ -x "$built" ]] || die "binaire release introuvable : $built"
  info "Binaire retenu : $built"
  install_binary_file "$built"
  ok "Mise à jour client depuis les sources ($REPO @ $(git rev-parse --short HEAD))"
}

cmd_check() {
  banner
  detect_platform
  step "État client"
  printf '  Installé     %s\n' "$(installed_version)"
  printf '  Chemin       %s\n' "$BIN_INSTALL"
  printf '  Plateforme   %s\n' "$PLATFORM"
  printf '  Dépôt GH     %s\n' "$GH_REPO"
  printf '\n'

  local json
  if json="$(fetch_latest_release_json)" && ! echo "$json" | grep -q 'Not Found'; then
    PYTHON_BIN="${PYTHON:-python3}"
    export GROK_PLATFORM="$PLATFORM"
    GROK_ASSET_CANDIDATES="$(IFS='|'; echo "${ASSET_CANDIDATES[*]}")"
    export GROK_ASSET_CANDIDATES
    local pick tag name
    pick="$(pick_release_asset "$json")"
    tag="$(printf '%s' "$pick" | cut -f1)"
    name="$(printf '%s' "$pick" | cut -f2)"
    if [[ -n "$name" ]]; then
      ok "Release disponible : $tag  asset=$name"
    else
      warn "Release $tag sans asset pour $PLATFORM"
    fi
  else
    warn "Pas de release GH utilisable pour l’instant."
    info "Les binaires multi-OS seront publiés après grok rebuild + CI."
    info "En attendant sur une machine de dev : grok update --from-source"
  fi
  info "Atelier mainteneur : grok rebuild"
}

cmd_update() {
  banner
  if try_gh_release_update; then
    exit 0
  fi

  if [[ "$FROM_SOURCE" == "1" ]]; then
    try_from_source
    exit 0
  fi

  step "Aucune release binaire disponible"
  log "  « grok update » consomme les assets GitHub Release (Linux + Windows)."
  log "  Ils ne sont pas encore publiés (ou absents pour cette plateforme)."
  log ""
  log "  Options :"
  log "    • Sur la machine atelier :  grok rebuild   (préparer + pousser le repo)"
  log "    • Dev local Linux :         grok update --from-source"
  log "    • Plus tard :               grok update     (télécharge le bon binaire)"
  log ""
  if [[ -t 0 && -t 1 ]] && [[ -d "$REPO/.git" ]]; then
    if confirm_yes "Tenter le fallback --from-source maintenant ?"; then
      try_from_source
      exit 0
    fi
  fi
  exit 2
}

case "$MODE" in
  check) cmd_check ;;
  update) cmd_update ;;
  *) die "mode invalide : $MODE" ;;
esac
