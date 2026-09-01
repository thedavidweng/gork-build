#!/usr/bin/env bash
# privacy-simple.sh — pipeline unique, menu interactif en frontal
#
#   1) fetch le tip officiel GitHub (xai-org/grok-build)
#   2) REPATCH systématique de maint/patches dessus
#   3) finalize-sync si le lock est périmé (tip upstream OU série retouchée),
#      suivi du series guard : refuse une file de patches qui a maigri
#   4) contrats privacy : refuse d'installer un binaire non conforme
#      (sautés si l'arbre produit n'a pas bougé depuis la dernière vérif)
#   5) compile — nom du binaire lu dans `default-run`, jamais deviné
#   6) installe ~/.grok/bin/grok
#
# Sans argument et en terminal, un menu propose les mêmes choix que les flags :
# il ne fait que remplir MODE/TARGET_MODE/DO_*, puis rend la main au pipeline.
# Aucun second chemin d'exécution. Son sous-menu « aval » est le seul endroit
# d'où ce script pousse : branche courante, ou remplacement de main (backup
# automatique + confirmation tapée + --force-with-lease).
#
#   grok rebuild              → menu si TTY, sinon tip officiel + repatch + build
#   grok rebuild --lock       → rejouer le lock seulement
#   grok rebuild --check      → sonde
#   grok rebuild --no-install → apply sans build
#   grok rebuild --no-finalize
#   grok rebuild --sha HASH
#   grok rebuild --build-only
#   grok rebuild --port       → laisse le conflit dans l'arbre pour le porter
#   grok rebuild --continue   → reprend la série après résolution
#   grok rebuild --release    → publie une release GH (Linux + Windows croisé)
#   grok rebuild --menu       → menu même avec des arguments
#   grok rebuild --no-menu    → jamais de menu
#   FORCE_APPLY=1 grok rebuild
#
set -euo pipefail

VERSION_SCRIPT="2.6.1"

export PATH="${HOME}/.cargo/bin:${PATH}"

REPO="${GROK_PRIVACY_REPO:-$HOME/grok-privacy}"
UPSTREAM_REMOTE="${GROK_PRIVACY_UPSTREAM_REMOTE:-upstream}"
UPSTREAM_URL="${GROK_PRIVACY_UPSTREAM_URL:-https://github.com/xai-org/grok-build.git}"
UPSTREAM_BRANCH="${GROK_PRIVACY_UPSTREAM_BRANCH:-main}"
ORIGIN_REMOTE="${GROK_PRIVACY_ORIGIN_REMOTE:-origin}"
GH_REPO="${GROK_PRIVACY_GH_REPO:-lycaos/grok-privacy}"
BIN_INSTALL="${GROK_PRIVACY_BIN:-$HOME/.grok/bin/grok}"
STABLE_SCRIPTS="${GROK_PRIVACY_STABLE_SCRIPTS:-$HOME/.local/share/grok-privacy/scripts}"
PYTHON="${PYTHON:-python3}"
CARGO="${CARGO:-cargo}"

# Garde-fous : la série ne doit pas maigrir au finalize, le binaire ne doit pas
# s'installer si les contrats privacy tombent.
SERIES_SNAPSHOT=""
SERIES_MAX_SHRINK_PCT="${GROK_PRIVACY_MAX_SHRINK_PCT:-20}"
ALLOW_CONTRACT_FAIL="${ALLOW_CONTRACT_FAIL:-0}"
FORCE_CONTRACTS="${FORCE_CONTRACTS:-0}"

# Le menu masque le curseur pendant la sélection : s'il sort par Ctrl-C, le
# terminal resterait sans curseur. Le trap le rétablit quoi qu'il arrive.
MENU_CURSOR_HIDDEN=0

cleanup() {
  menu_cursor_show
  [[ -n "$SERIES_SNAPSHOT" && -f "$SERIES_SNAPSHOT" ]] && rm -f "$SERIES_SNAPSHOT"
  return 0
}
trap cleanup EXIT

if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; D=$'\033[2m'; Z=$'\033[0m'
  H=$'\033[7m'
else
  B= G= Y= R= C= D= Z= H=
fi

menu_cursor_hide() {
  if [[ -t 1 && "$MENU_CURSOR_HIDDEN" == "0" ]]; then printf '\033[?25l'; MENU_CURSOR_HIDDEN=1; fi
  return 0
}
menu_cursor_show() {
  if [[ "$MENU_CURSOR_HIDDEN" == "1" ]]; then printf '\033[?25h'; MENU_CURSOR_HIDDEN=0; fi
  return 0
}
info() { printf '%s>>%s %s\n' "$C" "$Z" "$*"; }
ok()   { printf '%sOK%s  %s\n' "$G" "$Z" "$*"; }
warn() { printf '%s!!%s  %s\n' "$Y" "$Z" "$*" >&2; }
err()  { printf '%sÉCHEC%s %s\n' "$R" "$Z" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() { printf '\n%s══ %s ══%s\n' "$B" "$*" "$Z"; }

usage() {
  cat <<EOF
grok-privacy simple v${VERSION_SCRIPT}

  grok rebuild              tip officiel + REPATCH + finalize + build
  grok rebuild --lock       rejouer la série sur le SHA du lock
  grok rebuild --check      sonde (fetch, aucune écriture)
  grok rebuild --no-install apply (+ finalize) sans compiler
  grok rebuild --no-finalize  apply tip sans maj du lock
  grok rebuild --sha HASH   apply sur un commit précis
  grok rebuild --build-only build+install worktree courant
  grok rebuild --no-contracts  sauter les contrats privacy (déconseillé)
  grok rebuild --no-auto-resolve  pas d'IA sur un conflit inédit (rerere
                            rejoue toujours les résolutions déjà connues)
  grok rebuild --port       apply qui laisse le conflit dans l'arbre (portage)
  grok rebuild --continue   reprend la série après résolution du conflit
  grok rebuild --release    publie une release GH (Linux + Windows croisé)
  grok rebuild --menu       menu même avec des arguments
  grok rebuild --no-menu    jamais de menu
  grok rebuild --help

Sans argument et en terminal, le menu s'ouvre : il remplit les mêmes réglages
que les flags ci-dessus, affiche la commande équivalente, puis lance le même
pipeline. Hors terminal (CI, pipe, cron), comportement inchangé.

Pipeline : fetch GitHub tip → patchctl apply → finalize-sync → series guard
           → contrats privacy → cargo build → install

Conflit sur un nouveau tip amont : --port pour obtenir le conflit dans le
worktree, résoudre, "git add" les fichiers, puis --continue.

FORCE_APPLY=1          force re-apply même si sync/* existe
ALLOW_CONTRACT_FAIL=1  installe malgré des contrats privacy en échec
FORCE_CONTRACTS=1      rejoue les contrats même si l'arbre est inchangé
GROK_PRIVACY_MAX_SHRINK_PCT=N  tolérance de perte de substance au finalize (défaut 20)
GROK_PRIVACY_MENU=1|0  force ou interdit le menu, indépendamment du TTY
GROK_PRIVACY_DRY_RUN=1 résout les réglages, imprime le plan, n'exécute rien
EOF
}

MODE=run
DO_INSTALL=1
DO_FINALIZE=1
DO_CONTRACTS=1
TARGET_MODE=tip
TARGET_SHA=""
APPLY_SHA=""
APPLY_VERSION=""
APPLY_BRANCH=""
OLD_LOCK_SHA=""
PORT_MODE=0
# Appel au résolveur IA pour un conflit que rerere n'a jamais vu. Le rejeu des
# résolutions déjà enregistrées, lui, n'est jamais coupé : il ne coûte rien et
# ne demande rien à un modèle. Réglable patch par patch dans patchset.toml.
AUTO_RESOLVE=1
# Le menu ne s'ouvre que si l'utilisateur n'a rien demandé de précis : un seul
# argument suffit à dire qu'il sait ce qu'il veut.
ARGC_INITIAL=$#
MENU_CHOICE="${GROK_PRIVACY_MENU:-auto}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    --version|-V) echo "privacy-simple $VERSION_SCRIPT"; exit 0 ;;
    --check|-c) MODE=check; shift ;;
    --menu|-i) MENU_CHOICE=1; shift ;;
    --no-menu) MENU_CHOICE=0; shift ;;
    --release) MODE=release; shift ;;
    --port) PORT_MODE=1; shift ;;
    --no-auto-resolve) AUTO_RESOLVE=0; shift ;;
    --auto-resolve) AUTO_RESOLVE=1; shift ;;
    --continue|--resume) MODE=continue; shift ;;
    --no-install|--apply-only) DO_INSTALL=0; shift ;;
    --no-finalize) DO_FINALIZE=0; shift ;;
    --no-contracts) DO_CONTRACTS=0; shift ;;
    --lock) TARGET_MODE=lock; shift ;;
    --tip|--latest|--upstream-tip) TARGET_MODE=tip; shift ;;
    --build-only|--install-only) MODE=build-only; shift ;;
    --sha)
      [[ $# -ge 2 ]] || die "--sha requiert un commit"
      TARGET_MODE=sha; TARGET_SHA="$2"; shift 2
      ;;
    *) die "option inconnue: $1 (voir --help)" ;;
  esac
done

[[ -d "$REPO/.git" ]] || die "dépôt introuvable: $REPO"
cd "$REPO"
[[ -f maint/scripts/patchctl.py ]] || die "maint/scripts/patchctl.py manquant"
[[ -f maint/patches/series ]] || die "maint/patches/series manquant"
[[ -f maint/upstream.lock.toml ]] || die "maint/upstream.lock.toml manquant"

# L'historique de ce dépôt porte une seule identité. On l'impose repo-local et
# via l'environnement avant toute opération qui committe (am, finalize-sync) :
# la config globale de la machine ne doit jamais fuiter dans les commits.
GIT_IDENT_NAME="${GROK_PRIVACY_GIT_NAME:-Lycaos}"
GIT_IDENT_EMAIL="${GROK_PRIVACY_GIT_EMAIL:-caraboune@gmail.com}"
git config user.name "$GIT_IDENT_NAME"
git config user.email "$GIT_IDENT_EMAIL"
export GIT_AUTHOR_NAME="$GIT_IDENT_NAME" GIT_AUTHOR_EMAIL="$GIT_IDENT_EMAIL"
export GIT_COMMITTER_NAME="$GIT_IDENT_NAME" GIT_COMMITTER_EMAIL="$GIT_IDENT_EMAIL"

install -d "$STABLE_SCRIPTS"
cp -f "$0" "$STABLE_SCRIPTS/privacy-simple.sh" 2>/dev/null || true
chmod +x "$STABLE_SCRIPTS/privacy-simple.sh" 2>/dev/null || true
cp -f "$0" "$STABLE_SCRIPTS/privacy-rebuild.sh" 2>/dev/null || true
[[ -f scripts/grok-wrapper.sh ]] && cp -f scripts/grok-wrapper.sh "$STABLE_SCRIPTS/" 2>/dev/null || true

ensure_upstream_remote() {
  if git remote get-url "$UPSTREAM_REMOTE" &>/dev/null; then
    local url; url="$(git remote get-url "$UPSTREAM_REMOTE")"
    if [[ "$url" == /* || "$url" == file:* ]]; then
      warn "remote $UPSTREAM_REMOTE local ($url) → GitHub"
      git remote set-url "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
    fi
  else
    info "Ajout remote $UPSTREAM_REMOTE → $UPSTREAM_URL"
    git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
  fi
}

fetch_official() {
  step "1/6  Fetch officiel GitHub"
  ensure_upstream_remote
  info "git fetch $UPSTREAM_REMOTE $UPSTREAM_BRANCH  ($UPSTREAM_URL)"
  git fetch --quiet "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"
  git remote get-url "$ORIGIN_REMOTE" &>/dev/null && git fetch --quiet "$ORIGIN_REMOTE" 2>/dev/null || true
}

# Même fetch, sans bannière d'étape et sans mourir hors ligne : l'en-tête du
# menu doit s'afficher même quand le réseau est tombé.
fetch_official_quiet() {
  ensure_upstream_remote >/dev/null 2>&1 || true
  if ! git fetch --quiet "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH" 2>/dev/null; then
    return 1
  fi
  if git remote get-url "$ORIGIN_REMOTE" &>/dev/null; then
    git fetch --quiet "$ORIGIN_REMOTE" 2>/dev/null || true
  fi
  return 0
}

lock_field() {
  local key="$1"
  grep -E "^${key}[[:space:]]*=" maint/upstream.lock.toml | head -1 \
    | sed -E 's/^[^=]+=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'
}

pkg_version_at() {
  local ref="$1"
  git show "${ref}:crates/codegen/xai-grok-version/Cargo.toml" 2>/dev/null \
    | grep -E '^version[[:space:]]*=' | head -1 \
    | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || echo "?"
}

do_check() {
  fetch_official
  step "Sonde"
  local lock_c lock_v tip tip_v
  lock_c="$(lock_field commit)"; lock_v="$(lock_field version)"
  tip="$(git rev-parse "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}")"
  tip_v="$(pkg_version_at "$tip")"
  printf '\n  Dépôt     %s\n  Branche   %s\n  Lock      %s  (%s)\n  Officiel  %s  (%s)  via %s\n\n' \
    "$REPO" "$(git branch --show-current 2>/dev/null || echo '?')" \
    "${lock_v:-?}" "${lock_c:0:12}" "$tip_v" "${tip:0:12}" "$UPSTREAM_URL"
  if [[ "$lock_c" == "$tip" ]]; then
    ok "Lock aligné sur l'officiel $tip_v — rebuild = repatch du même tip + build"
  else
    warn "Nouvel officiel : lock=$lock_v → tip=$tip_v"
    info "grok rebuild  → REPATCH systématique du tip $tip_v"
  fi
  if [[ -x "$BIN_INSTALL" || -L "$BIN_INSTALL" ]]; then
    info "Binaire: $($BIN_INSTALL --version 2>/dev/null | head -1 || echo '?')"
  fi
  downstream_state
}

# L'API GitHub date tout en UTC (suffixe Z). Sans conversion, les listes CI et
# releases affichaient deux heures de moins que l'horloge de la machine l'été à
# Paris. `gh --template` n'aide pas : son timefmt reformate sans changer de
# fuseau. On convertit donc ici, et on retombe sur la valeur brute si date(1)
# ne sait pas lire l'horodatage.
local_time() {
  local ts="${1:-}"
  if [[ -z "$ts" ]]; then printf '%s' '—'; return 0; fi
  date -d "$ts" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || printf '%s' "$ts"
}

# La sonde ne montrait que l'amont. Or ce pipeline s'arrête à l'install locale :
# ce qui manque en aval (branche non poussée, CI rouge, aucune release) est
# invisible ici alors que c'est là que ça bloque.
downstream_state() {
  step "Aval ($GH_REPO)"
  local br ahead
  br="$(git branch --show-current 2>/dev/null || true)"
  if [[ -n "$br" ]]; then
    if git rev-parse --verify --quiet "refs/remotes/${ORIGIN_REMOTE}/${br}" >/dev/null; then
      ahead="$(git rev-list --count "${ORIGIN_REMOTE}/${br}..${br}" 2>/dev/null || echo '?')"
      if [[ "$ahead" == "0" ]]; then
        ok "branche $br synchronisée avec $ORIGIN_REMOTE"
      else
        warn "branche $br : $ahead commit(s) non poussé(s) — ce script ne pousse jamais"
      fi
    else
      warn "branche $br absente de $ORIGIN_REMOTE (jamais poussée)"
    fi
  fi

  if ! command -v gh >/dev/null 2>&1; then
    info "gh absent — releases et CI non consultées"
    return 0
  fi
  # gh résout le remote par défaut sur upstream (xai-org/grok-build) et rend un
  # 404 muet : toujours passer -R explicitement.
  local rel runs
  # Jamais de champ vide en tête : IFS=tab est un séparateur « blanc », donc
  # read fusionne les tabulations qui se suivent et décale toute la ligne.
  rel="$(gh release list -R "$GH_REPO" --limit 3 \
           --json name,tagName,isLatest,publishedAt \
           --jq '.[] | [(if .isLatest then "latest" else "-" end), .tagName, .publishedAt, .name] | @tsv' \
           2>/dev/null || true)"
  if [[ -n "$rel" ]]; then
    info "Releases :"
    local flag tag pub name
    while IFS=$'\t' read -r flag tag pub name; do
      printf '    %-7s %-12s %s  %s\n' "$flag" "$tag" "$(local_time "$pub")" "$name"
    done <<< "$rel"
  else
    warn "aucune release publiée sur $GH_REPO"
  fi
  runs="$(gh run list -R "$GH_REPO" --limit 5 \
            --json displayTitle,workflowName,conclusion,status,createdAt \
            --jq '.[] | [((.conclusion // "") | if . == "" then null else . end) // (.status // "?"), .createdAt, .workflowName, .displayTitle] | @tsv' \
            2>/dev/null || true)"
  if [[ -n "$runs" ]]; then
    # Cinq « failure » d'avant la désactivation des workflows ressemblent à un
    # échec du jour. On dit d'abord si Actions tourne encore, et on nomme le
    # workflow : deux runs du même commit portent le même titre.
    local active_wf
    active_wf="$(gh workflow list -R "$GH_REPO" --all --json state \
                   --jq '[.[] | select(.state == "active")] | length' 2>/dev/null || true)"
    if [[ "$active_wf" == "0" ]]; then
      warn "Actions désactivé (aucun workflow actif) — derniers runs figés d'avant la coupure :"
    else
      info "Derniers runs CI :"
    fi
    local concl created wf title
    while IFS=$'\t' read -r concl created wf title; do
      printf '    %-12s %s  %-16s %s\n' "$concl" "$(local_time "$created")" "$wf" "$title"
    done <<< "$runs"
  else
    info "aucun run CI lisible (droits ou réseau) — voir l'onglet Actions"
  fi
}

# État d'un apply interrompu par un conflit — écrit par patchctl sous le git dir
# (jamais dans l'arbre produit).
apply_state_file() { printf '%s/grok-apply-state.json' "$(git rev-parse --absolute-git-dir)"; }
conflict_report_file() { printf '%s/grok-apply-conflict.diff' "$(git rev-parse --absolute-git-dir)"; }

state_field() {
  local f; f="$(apply_state_file)"
  [[ -f "$f" ]] || return 1
  "$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$f" "$1"
}

# Le message d'erreur affirmait « Branche restaurée » alors que le checkout de
# retour échoue toujours (le control plane est non suivi sur les branches sync/*
# et suivi sur main : git refuse de l'écraser). On dit où on est, et comment
# finir le portage.
report_conflict() {
  local rc="$1" f; f="$(apply_state_file)"
  err "REPATCH échoué (code $rc) — conflit de patches (critical)."
  if [[ -f "$f" ]]; then
    err "  patch en conflit : $(state_field conflicted)"
    err "  branche          : $(state_field branch)"
    err "  rapport          : $(conflict_report_file)"
    err ""
    if [[ -n "$(state_field keep_conflict)" ]]; then
      err "Le conflit est dans le worktree. Résoudre, puis :"
      err "  git add <fichiers résolus>"
      err "  grok rebuild --continue"
    else
      err "Porter à la main :"
      err "  grok rebuild --port      # rejoue l'apply et laisse le conflit en place"
      err "  # résoudre, puis : git add <fichiers>"
      err "  grok rebuild --continue  # reprend la série, finalize, contrats, build"
    fi
  fi
}

# Un apply détache HEAD : tout ce qui n'est pas commité serait écrasé, et le
# gestionnaire d'erreur (restore_branch) l'écrasait aussi. On refuse de démarrer
# plutôt que de détruire.
require_clean_worktree() {
  # Pas d'apostrophe dans ce défaut : bash ouvre une chaîne simple-quotée à
  # l'intérieur de ${1:-…}, même sous guillemets, et avale la suite du script.
  local why="${1:-un apply détacherait HEAD et écraserait ces fichiers}"
  local dirty
  dirty="$(git status --porcelain --untracked-files=no 2>/dev/null || true)"
  [[ -z "$dirty" ]] || {
    err "Worktree sale : ${why} :"
    printf '%s\n' "$dirty" | head -20 >&2
    die "Commiter ou remiser d'abord (git stash) — rien n'a été touché."
  }
  return 0
}

restore_branch() {
  local br="$1"
  git am --abort 2>/dev/null || true
  git merge --abort 2>/dev/null || true
  git cherry-pick --abort 2>/dev/null || true
  [[ -n "$br" ]] || return 0
  git show-ref --verify --quiet "refs/heads/$br" || return 0
  # Déjà sur la branche : un checkout -f ici ne « restaure » rien, il détruit le
  # worktree — y compris le travail non commité qui a fait échouer l'apply.
  [[ "$(git branch --show-current 2>/dev/null || true)" == "$br" ]] && return 0
  info "Retour branche: $br"
  # Le control plane est non suivi sur les branches sync/* et suivi sur main :
  # git refuse d'écraser des fichiers non suivis, donc ce retour échoue presque
  # toujours. On le tente, et on dit la vérité quand il échoue.
  if git checkout "$br" 2>/dev/null; then
    ok "Branche restaurée: $br"
  else
    warn "checkout $br refusé (control plane non suivi) — rien n'est écrasé"
    warn "worktree laissé sur: $(git branch --show-current 2>/dev/null || echo 'HEAD détachée')"
  fi
  return 0
}

do_apply() {
  require_clean_worktree
  fetch_official
  local tip lock_c expect_v sha short branch_name
  tip="$(git rev-parse "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}")"
  lock_c="$(lock_field commit)"

  case "$TARGET_MODE" in
    sha) sha="$(git rev-parse "$TARGET_SHA")" ;;
    tip) sha="$tip" ;;
    lock|*)
      if ! git cat-file -e "${lock_c}^{commit}" 2>/dev/null; then
        git fetch --quiet "$UPSTREAM_REMOTE" "$lock_c" 2>/dev/null || git fetch --quiet "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"
      fi
      sha="$(git rev-parse "$lock_c")"
      ;;
  esac
  short="${sha:0:7}"
  expect_v="$(pkg_version_at "$sha")"
  branch_name="sync/upstream-${expect_v}-${short}"
  APPLY_SHA="$sha"; APPLY_VERSION="$expect_v"; APPLY_BRANCH="$branch_name"; OLD_LOCK_SHA="$lock_c"

  step "2/6  REPATCH privacy sur ${expect_v} (${short})  [mode=${TARGET_MODE}]"
  info "Cible: $sha"
  info "Branche: $branch_name"
  [[ "$TARGET_MODE" == "tip" && "$sha" != "$lock_c" ]] && info "Nouveau build officiel : $(lock_field version) → $expect_v"

  # --force for patchctl when re-applying over a local branch
  local force_flag=()
  if [[ "${FORCE_APPLY:-0}" != "1" ]]; then
    # La branche distante était testée AVANT la locale, et rejointe avec
    # `checkout -B` : sur une branche locale en avance (le cas normal, ce script
    # ne poussant rien), ça réinitialisait le travail non poussé sur la version
    # distante périmée. Le local prime, toujours.
    local has_local=0 has_remote=0
    git show-ref --verify --quiet "refs/heads/${branch_name}" && has_local=1
    git show-ref --verify --quiet "refs/remotes/${ORIGIN_REMOTE}/${branch_name}" && has_remote=1

    if [[ "$has_local" == "1" ]]; then
      local n_trailers ahead=0
      # control-metadata (finalize, statut) n'est pas un patch : exclu du compte.
      n_trailers="$(git log --format=%B "${sha}..${branch_name}" 2>/dev/null | grep 'Gork-Patch-Id:' | grep -vc 'control-metadata' || true)"
      if [[ "$has_remote" == "1" ]]; then
        ahead="$(git rev-list --count "${ORIGIN_REMOTE}/${branch_name}..${branch_name}" 2>/dev/null || echo 0)"
      fi
      # Complète = tous les patchs critiques de la série y sont. Un seuil fixe
      # (ex-6) prenait pour complète une branche d'apply avorté à 7 patchs sur
      # 16 — et aurait construit un binaire sans les hard-offs suivants.
      local n_critical
      n_critical="$("$PYTHON" -c '
import tomllib
with open("maint/patchset.toml", "rb") as f:
    data = tomllib.load(f)
print(sum(1 for p in data.get("patch") or [] if p.get("critical", True)))
' 2>/dev/null || echo 9999)"
      if [[ "${n_trailers:-0}" -ge "${n_critical:-9999}" ]]; then
        info "Branche locale complète ($n_trailers patch-ids ≥ $n_critical critiques) → checkout"
        if [[ "${ahead:-0}" -gt 0 ]]; then
          info "  $ahead commit(s) d'avance sur $ORIGIN_REMOTE : conservés"
        fi
        git checkout "$branch_name"
        ok "Worktree = $branch_name"
        return 0
      fi
      # Orpheline / apply partiel (ex. 3 patches puis abort) : rejouer avec --force
      info "Branche locale incomplète ($n_trailers/$n_critical patch-ids critiques) → re-apply --force"
      force_flag=(--force)
    elif [[ "$has_remote" == "1" ]]; then
      info "Branche absente en local, présente sur $ORIGIN_REMOTE → checkout de suivi"
      git checkout -b "$branch_name" "${ORIGIN_REMOTE}/${branch_name}"
      ok "Worktree = ${ORIGIN_REMOTE}/${branch_name}"
      return 0
    fi
  else
    force_flag=(--force)
  fi

  local save_br; save_br="$(git branch --show-current 2>/dev/null || true)"
  [[ -f maint/patches/series ]] || die "pas de maint/patches — checkout une branche privacy d'abord"

  local port_flag=()
  [[ "$PORT_MODE" == "1" ]] && port_flag=(--keep-conflict)

  local ar_flag=()
  [[ "$AUTO_RESOLVE" == "1" ]] && ar_flag=(--auto-resolve-conflicts)
  info "patchctl apply --upstream $short ${ar_flag[*]:-} ${force_flag[*]:-} ${port_flag[*]:-}…"
  set +e
  "$PYTHON" maint/scripts/patchctl.py apply --upstream "$sha" --branch "$branch_name" --expect-version "$expect_v" ${ar_flag[@]+"${ar_flag[@]}"} ${force_flag[@]+"${force_flag[@]}"} ${port_flag[@]+"${port_flag[@]}"}
  local rc=$?
  set -e

  if [[ $rc -ne 0 ]]; then
    if [[ $rc -eq 3 ]]; then
      report_conflict "$rc"
      # Portage en cours : ni retour de branche, ni suppression. La branche
      # partielle et l'état d'apply sont exactement ce qu'il faut pour --continue.
      exit "$rc"
    elif [[ $rc -eq 1 ]]; then
      err "REPATCH échoué (code $rc) sur ${expect_v} — apply refusé (branche/version/meta)."
      err "Voir stderr patchctl ci-dessus. FORCE_APPLY=1 ou suppression de la branche sync/* peut aider."
    else
      err "REPATCH échoué (code $rc) sur ${expect_v}."
    fi
    restore_branch "$save_br"
    if git show-ref --verify --quiet "refs/heads/${branch_name}"; then
      local tip_now; tip_now="$(git rev-parse "${branch_name}" 2>/dev/null || true)"
      if [[ "$tip_now" == "$sha" ]]; then
        git branch -D "$branch_name" 2>/dev/null || true
      fi
    fi
    exit "$rc"
  fi
  ok "Repatch OK sur $branch_name"
}

# Reprise après résolution manuelle : patchctl rejoue la fin de la série puis la
# queue d'apply (overlays, lock policy, commits de control plane) dont dépend le
# roundtrip de finalize-sync. Sans ça, une résolution à la main n'a aucun moyen
# d'aboutir à un lock cohérent.
do_continue() {
  local f; f="$(apply_state_file)"
  [[ -f "$f" ]] || die "aucun apply interrompu à reprendre ($f absent)"
  APPLY_SHA="$(state_field upstream)"
  APPLY_VERSION="$(state_field version)"
  APPLY_BRANCH="$(state_field branch)"
  step "2/6  Reprise du portage sur ${APPLY_VERSION:-?} (${APPLY_SHA:0:7})"
  info "patch en conflit: $(state_field conflicted)"
  set +e
  "$PYTHON" maint/scripts/patchctl.py apply --continue
  local rc=$?
  set -e
  if [[ $rc -ne 0 && $rc -ne 4 ]]; then
    if [[ $rc -eq 3 ]]; then report_conflict "$rc"; else err "Reprise échouée (code $rc)."; fi
    exit "$rc"
  fi
  ok "Série reprise jusqu'au bout sur $APPLY_BRANCH"
}

# finalize-sync ré-exporte maint/patches depuis les commits de la branche : son
# roundtrip prouve la cohérence série↔arbre, pas la non-régression. Une série
# amputée reproduit fidèlement un arbre amputé. D'où ce relevé avant/après.
series_snapshot() {
  [[ -f maint/scripts/series_guard.py ]] || return 0
  SERIES_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/grok-series-XXXXXX.json")"
  "$PYTHON" maint/scripts/series_guard.py snapshot --out "$SERIES_SNAPSHOT" >/dev/null 2>&1 \
    || SERIES_SNAPSHOT=""
  return 0
}

series_compare() {
  [[ -n "$SERIES_SNAPSHOT" && -f "$SERIES_SNAPSHOT" ]] || return 0
  if ! "$PYTHON" maint/scripts/series_guard.py compare \
        --baseline "$SERIES_SNAPSHOT" --max-shrink-pct "$SERIES_MAX_SHRINK_PCT"; then
    err "La file de patches a perdu de la substance pendant le finalize."
    err "Le commit de finalize est en HEAD : inspecter avec"
    err "  git show HEAD --stat -- maint/patches"
    err "Restaurer les patchs amputés depuis gork avant de rebuilder."
    exit 5
  fi
}

# Les contrats coûtent plusieurs minutes de cargo test. Ce qu'ils mesurent ne
# dépend que de l'arbre produit — pas de HEAD, qui change à chaque apply (git am
# réhorodate les commits). Même arbre ⇒ même verdict : on ne rejoue pas.
contracts_mark_file() { printf '%s/contracts-verified' "$(dirname "$STABLE_SCRIPTS")"; }

do_verify_contracts() {
  [[ "$DO_CONTRACTS" == "1" ]] || { info "contrats ignorés (--no-contracts)"; return 0; }
  [[ -f maint/scripts/patchctl.py ]] || return 0

  local tree mark
  tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || true)"
  mark="$(contracts_mark_file)"
  if [[ "$FORCE_CONTRACTS" != "1" && -n "$tree" && -f "$mark" ]] \
     && [[ "$(cat "$mark" 2>/dev/null)" == "$tree" ]]; then
    step "4/6  Contrats privacy"
    ok "Arbre inchangé depuis la dernière vérification (${tree:0:12}) — contrats non rejoués"
    info "Forcer : FORCE_CONTRACTS=1 grok rebuild"
    return 0
  fi

  step "4/6  Contrats privacy"
  info "patchctl verify --only-group privacy"
  set +e
  "$PYTHON" maint/scripts/patchctl.py verify --only-group privacy
  local rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    rm -f "$mark"
    err "Contrats privacy en échec (code $rc)."
    if [[ "$ALLOW_CONTRACT_FAIL" == "1" ]]; then
      warn "ALLOW_CONTRACT_FAIL=1 → build et installation malgré tout"
      return 0
    fi
    err "Refus d'installer un binaire dont les garanties privacy ne sont pas prouvées."
    err "Outrepasser sciemment : ALLOW_CONTRACT_FAIL=1 grok rebuild"
    exit 6
  fi
  if [[ -n "$tree" ]]; then printf '%s\n' "$tree" > "$mark"; fi
  ok "Contrats privacy OK"
}

do_finalize_if_needed() {
  [[ "$DO_FINALIZE" == "1" ]] || { info "finalize ignoré (--no-finalize)"; return 0; }
  # `--lock` ne change pas de base upstream, mais la série, elle, peut avoir
  # bougé : sauter le finalize ici laissait patch_tip/product_tip périmés et
  # `patchctl lint` rouge. C'est l'état du produit qui décide, pas le mode.
  local new_lock; new_lock="$(lock_field commit 2>/dev/null || true)"
  # Le seul test « commit == tip » ne voit que les sauts d'upstream : une série
  # retouchée à tip constant laissait product_tip périmé et la file jamais
  # ré-exportée (il fallait un finalize-sync manuel). On compare donc au produit.
  #
  # product_tip désigne le dernier commit du PRODUIT, donc le parent du commit
  # de finalize qui écrit le lock : à jour, il vaut HEAD (aucun finalize depuis)
  # ou HEAD^ (HEAD est ce commit de finalize). Toute autre valeur = périmé.
  local lock_tip head_sha head_parent
  lock_tip="$(lock_field product_tip 2>/dev/null || true)"
  head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
  head_parent="$(git rev-parse 'HEAD^' 2>/dev/null || true)"
  if [[ -n "${APPLY_SHA:-}" && "$new_lock" == "${APPLY_SHA}" && -n "$lock_tip" ]] \
     && [[ "$lock_tip" == "$head_sha" || "$lock_tip" == "$head_parent" ]]; then
    ok "Lock déjà sur ${APPLY_VERSION:-?} (${APPLY_SHA:0:12}) — produit inchangé"
    return 0
  fi
  if [[ -n "$lock_tip" ]]; then
    info "product_tip=${lock_tip:0:12} hors de {HEAD, HEAD^}=${head_sha:0:12} → ré-export de la série"
  fi
  step "3/6  Finalize lock → ${APPLY_VERSION:-?} (${APPLY_SHA:0:12})"
  local src_rev=""; [[ -f SOURCE_REV ]] && src_rev="$(tr -d '[:space:]' < SOURCE_REV || true)"
  series_snapshot
  set +e
  if [[ -n "$src_rev" ]]; then
    "$PYTHON" maint/scripts/patchctl.py finalize-sync --upstream "${APPLY_SHA}" --version "${APPLY_VERSION}" --source-rev "$src_rev"
  else
    "$PYTHON" maint/scripts/patchctl.py finalize-sync --upstream "${APPLY_SHA}" --version "${APPLY_VERSION}"
  fi
  local rc=$?; set -e
  if [[ $rc -ne 0 ]]; then
    warn "finalize-sync échoué ($rc) — tree patché OK, lock non mis à jour"
    return 0
  fi
  ok "Lock = ${APPLY_VERSION:-?} — prochains rebuilds repartent de cette base"
  series_compare
}

PKG_MANIFEST="crates/codegen/xai-grok-pager-bin/Cargo.toml"

tree_pkg_version() {
  local f="$PKG_MANIFEST"
  [[ -f "$f" ]] || f="crates/codegen/xai-grok-version/Cargo.toml"
  grep -E '^version[[:space:]]*=' "$f" 2>/dev/null | head -1 \
    | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || true
}

# Le nom du binaire a bougé au fil des renames (grok → gork → grok). Ne jamais
# le deviner : `default-run` est la seule source de vérité, et cargo produit
# exactement ce nom.
cargo_bin_name() {
  grep -m1 -E '^default-run[[:space:]]*=' "$PKG_MANIFEST" 2>/dev/null \
    | sed -E 's/^default-run[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || true
}

# Tous les noms que le manifeste déclare (package + chaque [[bin]]).
# Sur-inclusif par construction : on ne supprimera donc jamais un artefact légitime.
declared_bin_names() {
  grep -E '^name[[:space:]]*=' "$PKG_MANIFEST" 2>/dev/null \
    | sed -E 's/^name[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || true
}

# Un ELF resté d’un nommage précédent est un piège : même version, mauvais
# contenu, et l’installateur ne voit pas la différence.
scrub_undeclared_bins() {
  [[ -d target/release ]] || return 0
  local declared f name
  declared="$(declared_bin_names)"
  for f in target/release/*; do
    [[ -f "$f" && -x "$f" ]] || continue
    name="$(basename "$f")"
    case "$name" in grok|grokp|gork|grok-privacy|xai-grok-pager*) ;; *) continue ;; esac
    grep -qxF "$name" <<<"$declared" && continue
    warn "Suppression artefact obsolète target/release/$name (non déclaré par le manifeste)"
    rm -f "$f" "${f}.d"
  done
}

do_build_install() {
  step "5/6  Build release"
  info "cargo build -p xai-grok-pager-bin --release"
  if ! $CARGO build -p xai-grok-pager-bin --release; then
    warn "fallback workspace release"
    $CARGO build --release -p xai-grok-shell 2>/dev/null || $CARGO build --release
  fi
  scrub_undeclared_bins
  local name; name="$(cargo_bin_name)"
  [[ -n "$name" ]] || die "default-run absent de $PKG_MANIFEST : nom du binaire indéterminable"
  local bin="target/release/$name"
  [[ -x "$bin" ]] || die "cargo n’a pas produit l’artefact attendu: $bin"
  info "Binaire retenu: $bin ($($bin --version 2>/dev/null | head -1 || echo '?'))"

  local tree_ver expect_ok=1
  tree_ver="$(tree_pkg_version)"
  if [[ -n "$tree_ver" ]]; then
    local reported
    reported="$($bin --version 2>/dev/null | head -1 || true)"
    if [[ "$reported" != *"$tree_ver"* ]]; then
      err "Version binaire ≠ tree: bin='$reported' tree=$tree_ver (fichier=$bin)"
      err "Souvent un build partiel. Relancer: cargo clean -p xai-grok-pager-bin && grok rebuild --build-only"
      expect_ok=0
    fi
  fi
  [[ "$expect_ok" == "1" ]] || die "refus d'installer un binaire désaligné du tree"

  step "6/6  Install → $BIN_INSTALL"
  install -d "$(dirname "$BIN_INSTALL")"
  local tmp="${BIN_INSTALL}.new.$$"
  install -m 755 "$bin" "$tmp"
  mv -f "$tmp" "$BIN_INSTALL"
  if [[ -d "$HOME/.local/bin" ]]; then
    install -m 755 "$bin" "$HOME/.local/bin/grokp" 2>/dev/null || true
  fi
  ok "Installé: $BIN_INSTALL"
  info "Version: $($BIN_INSTALL --version 2>/dev/null | head -1 || echo '?')"
}

# ── menu ────────────────────────────────────────────────────────────────────
#
# Le menu n'exécute rien lui-même : il remplit MODE/TARGET_MODE/TARGET_SHA/
# DO_*/PORT_MODE, exactement comme le parseur de flags, puis rend la main au
# dispatch ci-dessous. C'est ce qui garantit qu'il ne peut pas diverger du
# comportement des options en ligne de commande.
#
# Deux exceptions assumées : le sous-menu « aval » agit tout de suite (pousser
# n'est pas un réglage de rebuild), et l'abandon d'un portage aussi.

MENU_LOCK_C=""; MENU_LOCK_V=""; MENU_BRANCH=""; MENU_TIP=""; MENU_TIP_V=""
MENU_INSTALLED=""; MENU_NET_OK=1

manifest_version_at() {
  git show "$1:$PKG_MANIFEST" 2>/dev/null \
    | grep -m1 -E '^version[[:space:]]*=' \
    | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || true
}

menu_wanted() {
  case "$MENU_CHOICE" in
    1|on|yes|oui) return 0 ;;
    0|off|no|non) return 1 ;;
  esac
  [[ "$ARGC_INITIAL" -eq 0 && -t 0 && -t 1 ]]
}

menu_port_pending() { [[ -f "$(apply_state_file)" ]]; }

menu_probe() {
  MENU_BRANCH="$(git branch --show-current 2>/dev/null || echo '(HEAD détachée)')"
  MENU_LOCK_C="$(lock_field commit)"
  MENU_LOCK_V="$(lock_field version)"
  if fetch_official_quiet; then
    MENU_NET_OK=1
    MENU_TIP="$(git rev-parse "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}" 2>/dev/null || true)"
    MENU_TIP_V="$(pkg_version_at "$MENU_TIP")"
  else
    MENU_NET_OK=0; MENU_TIP=""; MENU_TIP_V=""
  fi
  if [[ -x "$BIN_INSTALL" || -L "$BIN_INSTALL" ]]; then
    MENU_INSTALLED="$("$BIN_INSTALL" --version 2>/dev/null | head -1 || true)"
  else
    MENU_INSTALLED="(aucun)"
  fi
  return 0
}

# Pas de bordure droite : la largeur d'affichage des accents ne se calcule pas
# avec printf, un cadre fermé finirait décalé.
menu_header() {
  local up_line aval
  if [[ "$MENU_NET_OK" != "1" ]]; then
    up_line="${Y}injoignable${Z}  (réseau)"
  elif [[ "$MENU_TIP" == "$MENU_LOCK_C" ]]; then
    up_line="${MENU_TIP_V}  (${MENU_TIP:0:12})   ${G}aligné${Z}"
  else
    up_line="${MENU_TIP_V}  (${MENU_TIP:0:12})   ${Y}nouveau build xAI${Z}"
  fi
  aval="$(menu_aval_summary)"
  printf '\n%s╭─ grok-privacy · rebuild · v%s ─%s\n' "$B" "$VERSION_SCRIPT" "$Z"
  printf '%s│%s  Branche   %s\n' "$D" "$Z" "$MENU_BRANCH"
  printf '%s│%s  Lock      %s  (%s)\n' "$D" "$Z" "${MENU_LOCK_V:-?}" "${MENU_LOCK_C:0:12}"
  printf '%s│%s  Officiel  %s\n' "$D" "$Z" "$up_line"
  printf '%s│%s  Installé  %s\n' "$D" "$Z" "${MENU_INSTALLED:-?}"
  printf '%s│%s  Aval      %s\n' "$D" "$Z" "$aval"
  printf '%s╰%s\n' "$D" "$Z"
}

menu_aval_summary() {
  local br main_sha remote_sha
  br="$(git branch --show-current 2>/dev/null || true)"
  main_sha="$(git rev-parse --verify --quiet main || true)"
  remote_sha="$(git rev-parse --verify --quiet "refs/remotes/${ORIGIN_REMOTE}/main" || true)"
  if [[ -z "$remote_sha" ]]; then
    printf '%s/main inconnue' "$ORIGIN_REMOTE"
  elif [[ "$main_sha" == "$remote_sha" ]]; then
    if [[ -n "$br" && "$(git rev-parse "$br")" == "$remote_sha" ]]; then
      printf 'main publié = branche courante'
    else
      printf 'main publié %s, branche courante non publiée' "${remote_sha:0:7}"
    fi
  else
    printf 'main local %s ≠ %s/main %s' "${main_sha:0:7}" "$ORIGIN_REMOTE" "${remote_sha:0:7}"
  fi
}

menu_ask() {
  local prompt="$1" __out="$2" ans=""
  printf '%s%s%s ' "$C" "$prompt" "$Z"
  if ! IFS= read -r ans; then printf '\n'; return 1; fi
  printf -v "$__out" '%s' "$ans"
  return 0
}

# Sélecteur au clavier. Flèches ↑↓ (ou j/k) pour se déplacer, Entrée pour
# valider, Échap pour revenir, q pour sortir ; les chiffres restent des
# accélérateurs directs. La liste est redessinée sur place : on remonte de n
# lignes et on réécrit, chaque ligne préfixée d'un effacement de fin de ligne.
#
#   $1 = nom de la variable qui reçoit l'indice choisi
#   $2 = indice initialement surligné
#   $@ = libellés
#   retour : 0 choix validé · 1 Échap · 2 q ou fin d'entrée
menu_select() {
  local __out="$1" cur="$2"; shift 2
  local items=("$@")
  local n=${#items[@]} drawn=0 key rest i
  if (( n == 0 )); then return 2; fi
  if (( cur < 0 || cur >= n )); then cur=0; fi
  menu_cursor_hide
  while true; do
    if [[ "$drawn" == "1" ]]; then printf '\033[%dA' "$n"; fi
    for (( i = 0; i < n; i++ )); do
      if (( i == cur )); then
        printf '\033[K %s❯ %s %s\n' "$H" "${items[i]}" "$Z"
      else
        printf '\033[K   %s\n' "${items[i]}"
      fi
    done
    drawn=1
    if ! IFS= read -rsn1 key; then menu_cursor_show; return 2; fi
    case "$key" in
      $'\x1b')
        # Échap seul ou début d'une séquence de flèche : c'est le délai qui
        # tranche, une flèche envoie ses deux octets suivants immédiatement.
        if IFS= read -rsn2 -t 0.1 rest; then
          case "$rest" in
            '[A') cur=$(( (cur - 1 + n) % n )) ;;
            '[B') cur=$(( (cur + 1) % n )) ;;
          esac
        else
          menu_cursor_show; return 1
        fi
        ;;
      k|K) cur=$(( (cur - 1 + n) % n )) ;;
      j|J) cur=$(( (cur + 1) % n )) ;;
      [1-9])
        i=$(( key - 1 ))
        if (( i < n )); then
          cur=$i
          printf -v "$__out" '%s' "$cur"
          menu_cursor_show
          return 0
        fi
        ;;
      q|Q) menu_cursor_show; return 2 ;;
      '') printf -v "$__out" '%s' "$cur"; menu_cursor_show; return 0 ;;
    esac
  done
}

# Une action destructrice ne se confirme pas par « o » : on tape le mot.
menu_confirm_word() {
  local word="$1" ans=""
  printf '%s Taper exactement %s pour confirmer :%s ' "$Y" "$word" "$Z"
  if ! IFS= read -r ans; then printf '\n'; return 1; fi
  [[ "$ans" == "$word" ]]
}

menu_onoff() { if [[ "$1" == "1" ]]; then printf '%sactivé%s' "$G" "$Z"; else printf '%sdésactivé%s' "$R" "$Z"; fi; }
menu_yesno() { if [[ "$1" == "1" ]]; then printf '%soui%s' "$Y" "$Z"; else printf 'non'; fi; }

# Ce que le menu vient de régler, dit en flags : le menu apprend la ligne de
# commande au lieu de s'y substituer.
menu_equivalent() {
  local f=() env=""
  case "$MODE" in
    check) f+=(--check) ;;
    build-only) f+=(--build-only) ;;
    continue) f+=(--continue) ;;
    run)
      case "$TARGET_MODE" in
        lock) f+=(--lock) ;;
        sha)  f+=(--sha "$TARGET_SHA") ;;
      esac
      if [[ "$DO_INSTALL" != "1" ]]; then f+=(--no-install); fi
      if [[ "$DO_FINALIZE" != "1" ]]; then f+=(--no-finalize); fi
      if [[ "$PORT_MODE" == "1" ]]; then f+=(--port); fi
      ;;
  esac
  if [[ "$DO_CONTRACTS" != "1" ]]; then f+=(--no-contracts); fi
  if [[ "$AUTO_RESOLVE" != "1" ]]; then f+=(--no-auto-resolve); fi
  if [[ "${FORCE_APPLY:-0}" == "1" ]]; then env+="FORCE_APPLY=1 "; fi
  if [[ "$FORCE_CONTRACTS" == "1" ]]; then env+="FORCE_CONTRACTS=1 "; fi
  info "équivalent : ${env}grok rebuild ${f[*]:-}"
}

menu_advanced() {
  local idx cur=0 rc
  local -a items
  while true; do
    printf '\n%s── Options avancées ──%s\n' "$B" "$Z"
    items=(
      "Contrats privacy            [$(menu_onoff "$DO_CONTRACTS")]"
      "Finalize du lock            [$(menu_onoff "$DO_FINALIZE")]"
      "Re-apply forcé              [$(menu_yesno "${FORCE_APPLY:-0}")]   FORCE_APPLY"
      "Rejouer les contrats        [$(menu_yesno "$FORCE_CONTRACTS")]   FORCE_CONTRACTS"
      "Garder le conflit en place  [$(menu_yesno "$PORT_MODE")]   --port"
      "Résolveur IA si conflit inédit [$(menu_onoff "$AUTO_RESOLVE")]   --no-auto-resolve"
      "Retour"
    )
    rc=0; menu_select idx "$cur" "${items[@]}" || rc=$?
    if [[ "$rc" != "0" ]]; then return 0; fi
    cur="$idx"
    case "$idx" in
      0)
        if [[ "$DO_CONTRACTS" == "1" ]]; then
          warn "Sans contrats, rien ne prouve que le binaire installé tient ses garanties privacy."
          if menu_confirm_word "SANS-CONTRATS"; then DO_CONTRACTS=0; else info "inchangé"; fi
        else
          DO_CONTRACTS=1
        fi
        ;;
      1) if [[ "$DO_FINALIZE" == "1" ]]; then DO_FINALIZE=0; else DO_FINALIZE=1; fi ;;
      2) if [[ "${FORCE_APPLY:-0}" == "1" ]]; then FORCE_APPLY=0; else FORCE_APPLY=1; fi ;;
      3) if [[ "$FORCE_CONTRACTS" == "1" ]]; then FORCE_CONTRACTS=0; else FORCE_CONTRACTS=1; fi ;;
      4) if [[ "$PORT_MODE" == "1" ]]; then PORT_MODE=0; else PORT_MODE=1; fi ;;
      5) if [[ "$AUTO_RESOLVE" == "1" ]]; then AUTO_RESOLVE=0; else AUTO_RESOLVE=1; fi ;;
      6) return 0 ;;
    esac
  done
}

menu_push_branch() {
  local br
  br="$(git branch --show-current 2>/dev/null || true)"
  if [[ -z "$br" ]]; then warn "HEAD détachée — rien à pousser"; return 0; fi
  info "git push -u $ORIGIN_REMOTE $br"
  if git push -u "$ORIGIN_REMOTE" "$br"; then
    ok "branche $br publiée"
  else
    err "push refusé — rien n'a changé côté $ORIGIN_REMOTE"
  fi
  return 0
}

WIN_TARGET="x86_64-pc-windows-gnu"
ASSET_LINUX="grok-linux-x86_64"
ASSET_WINDOWS="grok-windows-x86_64.exe"

# Publier une release, c'est ce qui permet à `grok update` de fonctionner sur
# les autres machines — Windows en particulier, où compiler n'est pas une
# option raisonnable. L'asset Windows est produit ici par compilation croisée
# (mingw), ce qui n'attend pas la CI.
#
# Trois refus avant toute publication : arbre sale (artefact non reproductible),
# commit non joignable depuis origin/main (release qui pointe dans le vide),
# contrats non prouvés sur cet arbre (on ne distribue pas ce qu'on n'a pas
# vérifié — publier engage plus qu'installer).
do_publish_release() {
  local version tag head sha tree mark bin_linux
  version="$(tree_pkg_version)"
  [[ -n "$version" ]] || die "version produit indéterminable depuis $PKG_MANIFEST"
  tag="v${version}"

  step "Publication de release — $tag"
  require_clean_worktree "un artefact publié doit être reproductible depuis un commit"

  head="$(git rev-parse HEAD)"
  git fetch --quiet "$ORIGIN_REMOTE" main 2>/dev/null || warn "fetch $ORIGIN_REMOTE échoué"
  if ! git merge-base --is-ancestor "$head" "refs/remotes/${ORIGIN_REMOTE}/main" 2>/dev/null; then
    err "HEAD (${head:0:12}) n'est pas joignable depuis ${ORIGIN_REMOTE}/main."
    die "Publier main d'abord — une release doit pointer sur un commit public."
  fi

  tree="$(git rev-parse 'HEAD^{tree}')"
  mark="$(contracts_mark_file)"
  if [[ "$(cat "$mark" 2>/dev/null)" != "$tree" ]]; then
    err "Les contrats privacy n'ont pas été prouvés sur cet arbre (${tree:0:12})."
    die "Lancer d'abord : grok rebuild --build-only"
  fi

  command -v gh >/dev/null 2>&1 || die "gh requis pour publier une release"
  command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1 \
    || die "chaîne mingw absente — installer gcc-mingw-w64-x86-64 pour produire l'asset Windows"

  bin_linux="target/release/$(cargo_bin_name)"
  [[ -x "$bin_linux" ]] || die "binaire Linux absent : $bin_linux — lancer grok rebuild --build-only"

  info "Build croisé $WIN_TARGET (quelques minutes)…"
  "${CARGO}" --version >/dev/null 2>&1 || die "cargo introuvable"
  rustup target add "$WIN_TARGET" >/dev/null 2>&1 || true
  $CARGO build -p xai-grok-pager-bin --release --target "$WIN_TARGET" \
    || die "build croisé Windows échoué"
  local bin_win="target/${WIN_TARGET}/release/$(cargo_bin_name).exe"
  [[ -f "$bin_win" ]] || die "cargo n'a pas produit $bin_win"

  local stage; stage="$(mktemp -d "${TMPDIR:-/tmp}/grok-release-XXXXXX")"
  install -m 755 "$bin_linux" "${stage}/${ASSET_LINUX}"
  install -m 755 "$bin_win" "${stage}/${ASSET_WINDOWS}"

  local exists=0
  gh release view "$tag" -R "$GH_REPO" >/dev/null 2>&1 && exists=1

  printf '\n%s── Publication de release ──%s\n' "$B" "$Z"
  printf '  dépôt      %s\n' "$GH_REPO"
  printf '  tag        %s   %s\n' "$tag" \
    "$([[ "$exists" == "1" ]] && printf 'existe déjà → assets remplacés' || printf 'sera créé sur %s' "${head:0:12}")"
  printf '  linux      %-26s %s\n' "$ASSET_LINUX" "$(du -h "${stage}/${ASSET_LINUX}" | cut -f1)"
  printf '  windows    %-26s %s  (croisé %s)\n' "$ASSET_WINDOWS" \
    "$(du -h "${stage}/${ASSET_WINDOWS}" | cut -f1)" "$WIN_TARGET"
  warn "Les contrats privacy ont tourné sur l'hôte Linux ; l'asset Windows sort du même arbre mais n'a pas été exécuté."

  if ! menu_confirm_word "PUBLIER"; then
    info "annulé — rien n'a été publié"
    rm -rf "$stage"
    return 0
  fi

  local rc=0
  if [[ "$exists" == "1" ]]; then
    gh release upload "$tag" -R "$GH_REPO" --clobber \
      "${stage}/${ASSET_LINUX}" "${stage}/${ASSET_WINDOWS}" || rc=$?
  else
    gh release create "$tag" -R "$GH_REPO" --target "$head" --generate-notes \
      --title "$tag" \
      "${stage}/${ASSET_LINUX}" "${stage}/${ASSET_WINDOWS}" || rc=$?
  fi
  rm -rf "$stage"
  if [[ "$rc" -ne 0 ]]; then
    err "publication refusée (code $rc) — rien n'a changé sur $GH_REPO"
    return 0
  fi
  ok "release $tag publiée — grok update la verra sur les autres machines"
  return 0
}

# Deux situations que rien ne permet de confondre, et que le menu doit
# distinguer lui-même : empiler des commits sur la branche sync laisse
# origin/main ancêtre de la branche (avance rapide, rien de réécrit), alors
# qu'un saut de version amont rejoue la série sur une base neuve et rend les
# histoires disjointes (remplacement, force push, sauvegarde).
#
#   ff        origin/main est un ancêtre       → push simple, pas de sauvegarde
#   replace   histoires disjointes             → sauvegarde + --force-with-lease
#   create    origin/main n'existe pas         → push simple
#   uptodate  déjà publié                      → rien
main_publish_kind() {
  local br target remote_sha
  br="$(git branch --show-current 2>/dev/null || true)"
  if [[ -z "$br" || "$br" == "main" ]]; then printf 'none'; return 0; fi
  remote_sha="$(git rev-parse --verify --quiet "refs/remotes/${ORIGIN_REMOTE}/main" || true)"
  if [[ -z "$remote_sha" ]]; then printf 'create'; return 0; fi
  target="$(git rev-parse "$br")"
  if [[ "$target" == "$remote_sha" ]]; then printf 'uptodate'; return 0; fi
  if git merge-base --is-ancestor "$remote_sha" "$target" 2>/dev/null; then
    printf 'ff'
  else
    printf 'replace'
  fi
}

menu_confirm_yes() {
  local ans=""
  printf '%s Confirmer ? [o/N]%s ' "$C" "$Z"
  if ! IFS= read -r ans; then printf '\n'; return 1; fi
  [[ "$ans" == o || "$ans" == O || "$ans" == oui || "$ans" == y || "$ans" == Y ]]
}

# Sauvegarde d'abord, pousse ensuite, et ne déplace main en local qu'une fois
# le distant accepté : un push refusé ne doit pas laisser un main déplacé
# derrière lui.
menu_replace_main() {
  local br target main_sha remote_sha old_v new_v backup existing kind
  br="$(git branch --show-current 2>/dev/null || true)"
  if [[ -z "$br" ]]; then warn "HEAD détachée — rien à publier"; return 0; fi
  if [[ "$br" == "main" ]]; then warn "déjà sur main — utiliser « pousser la branche courante »"; return 0; fi

  git fetch --quiet "$ORIGIN_REMOTE" main 2>/dev/null \
    || warn "fetch $ORIGIN_REMOTE échoué — le bail est calculé sur l'état local connu"

  target="$(git rev-parse "$br")"
  main_sha="$(git rev-parse --verify --quiet main || true)"
  remote_sha="$(git rev-parse --verify --quiet "refs/remotes/${ORIGIN_REMOTE}/main" || true)"
  kind="$(main_publish_kind)"
  if [[ "$kind" == "uptodate" ]]; then ok "origin/main pointe déjà sur $br"; return 0; fi

  old_v="$(manifest_version_at "${remote_sha:-${main_sha:-HEAD}}")"
  new_v="$(manifest_version_at "$target")"

  # Avance rapide : rien n'est réécrit, l'ancien main reste joignable depuis le
  # nouveau. Ni sauvegarde ni mot de passe cérémoniel — ce serait mentir sur la
  # nature du geste.
  if [[ "$kind" == "ff" || "$kind" == "create" ]]; then
    printf '\n%s── Publication de main (avance rapide) ──%s\n' "$B" "$Z"
    printf '  avant  %s/main = %s  (%s)\n' "$ORIGIN_REMOTE" "${remote_sha:0:12}" "${old_v:-?}"
    printf '  après  %s/main = %s  (%s)   ← %s\n' "$ORIGIN_REMOTE" "${target:0:12}" "${new_v:-?}" "$br"
    printf '  %s commit(s) ajouté(s), aucun réécrit :\n' "$(git rev-list --count "${remote_sha:-$target}..$target" 2>/dev/null || echo '?')"
    git log --oneline "${remote_sha:-$target}..$target" 2>/dev/null | sed 's/^/    /'
    if ! menu_confirm_yes; then info "annulé — rien n'a été poussé"; return 0; fi
    local ff_rc=0
    git push "$ORIGIN_REMOTE" "${br}:main" || ff_rc=$?
    if [[ "$ff_rc" -ne 0 ]]; then
      err "push refusé (code $ff_rc) — rien n'a changé"
      return 0
    fi
    git branch -f main "$target"
    git branch --set-upstream-to="${ORIGIN_REMOTE}/main" main >/dev/null 2>&1 || true
    ok "main = $br (${target:0:12}) — avance rapide publiée sur $ORIGIN_REMOTE"
    return 0
  fi

  printf '\n%s── Remplacement de main ──%s\n' "$B" "$Z"
  printf '  avant  main = %s  (%s)\n' "${main_sha:0:12}" "${old_v:-?}"
  printf '  après  main = %s  (%s)   ← %s\n' "${target:0:12}" "${new_v:-?}" "$br"
  printf '  %s/main = %s  → sera réécrit (histoires disjointes)\n' "$ORIGIN_REMOTE" "${remote_sha:0:12}"

  backup=""
  if [[ -n "$main_sha" ]]; then
    # Une branche qui « contient » l'ancien main ne le sauvegarde pas pour
    # autant : main elle-même, et surtout celle qu'on est en train de publier,
    # ne comptent pas — sinon on se retrouve sans filet explicite le jour où
    # cette branche est supprimée.
    existing="$(git for-each-ref --contains "$main_sha" --format='%(refname:short)' refs/heads 2>/dev/null \
                | grep -v -x -e "main" -e "$br" | head -1 || true)"
    if [[ -n "$existing" ]]; then
      printf '  sauvegarde : %s (déjà présente)\n' "$existing"
    else
      backup="backup/main-${old_v:-$(printf '%s' "${main_sha:0:7}")}"
      if git show-ref --verify --quiet "refs/heads/$backup"; then
        backup="${backup}-${main_sha:0:7}"
      fi
      printf '  sauvegarde : %s (sera créée)\n' "$backup"
    fi
  fi

  if ! menu_confirm_word "REMPLACER"; then info "annulé — rien n'a été touché"; return 0; fi

  if [[ -n "$backup" ]]; then
    if git branch "$backup" "$main_sha"; then
      ok "sauvegarde $backup = ${main_sha:0:12}"
    else
      err "sauvegarde impossible — on ne réécrit rien sans filet"
      return 0
    fi
  fi

  local push_rc=0
  git push --force-with-lease=main:"$remote_sha" "$ORIGIN_REMOTE" "${br}:main" || push_rc=$?
  if [[ "$push_rc" -ne 0 ]]; then
    err "push refusé (code $push_rc) — main local n'a pas bougé, $ORIGIN_REMOTE non plus"
    return 0
  fi
  git branch -f main "$target"
  git branch --set-upstream-to="${ORIGIN_REMOTE}/main" main >/dev/null 2>&1 || true
  ok "main = $br (${target:0:12}) — publié sur $ORIGIN_REMOTE"
  return 0
}

menu_downstream() {
  local idx cur=0 rc
  local -a items
  while true; do
    downstream_state
    printf '\n'
    # Le libellé dit ce qui va réellement se passer : annoncer « force push »
    # sur une avance rapide fait couper à raison.
    local main_lbl
    case "$(main_publish_kind)" in
      ff)       main_lbl="Avancer main sur la branche courante (avance rapide)" ;;
      replace)  main_lbl="Remplacer main par la branche courante (force push)" ;;
      create)   main_lbl="Créer main sur $ORIGIN_REMOTE depuis la branche courante" ;;
      uptodate) main_lbl="main est déjà publié sur la branche courante" ;;
      *)        main_lbl="Publier main (indisponible : HEAD détachée ou déjà sur main)" ;;
    esac
    items=(
      "Pousser la branche courante sur $ORIGIN_REMOTE"
      "$main_lbl"
      "Publier une release (Linux + Windows)"
      "Rafraîchir"
      "Retour"
    )
    rc=0; menu_select idx "$cur" "${items[@]}" || rc=$?
    if [[ "$rc" != "0" ]]; then return 0; fi
    cur="$idx"
    case "$idx" in
      0) menu_push_branch ;;
      1) menu_replace_main ;;
      2) do_publish_release ;;
      3) fetch_official_quiet || warn "fetch échoué" ;;
      4) return 0 ;;
    esac
  done
}

menu_abort_port() {
  warn "Abandon du portage : le conflit résolu et l'état de reprise seront perdus."
  if ! menu_confirm_word "ABANDONNER"; then info "annulé"; return 0; fi
  git am --abort 2>/dev/null || true
  rm -f "$(apply_state_file)" "$(conflict_report_file)"
  rm -rf "$(git rev-parse --absolute-git-dir)/grok-apply-patches"
  ok "portage abandonné — la branche partielle est conservée"
  return 0
}

# Un portage en attente rend tout apply neuf impossible (patchctl le refuse) :
# le menu ne propose donc que ce qui a un sens dans cet état.
menu_port_screen() {
  local idx cur=0 rc
  local -a items
  while true; do
    menu_header
    printf '%s!!%s portage interrompu sur %s\n' "$Y" "$Z" "$(state_field conflicted)"
    printf '   branche %s · rapport %s\n\n' "$(state_field branch)" "$(conflict_report_file)"
    items=(
      "Reprendre le portage (après résolution + git add)"
      "Abandonner le portage"
      "Sonde détaillée"
      "Quitter"
    )
    rc=0; menu_select idx "$cur" "${items[@]}" || rc=$?
    if [[ "$rc" == "2" ]]; then exit 0; fi
    if [[ "$rc" == "1" ]]; then continue; fi
    cur="$idx"
    case "$idx" in
      0) MODE=continue; return 0 ;;
      1) menu_abort_port; if ! menu_port_pending; then menu_probe; return 1; fi ;;
      2) MODE=check; return 0 ;;
      3) exit 0 ;;
    esac
  done
}

menu_main() {
  local idx cur=0 rc sha label
  local -a items
  menu_probe
  while true; do
    if menu_port_pending; then
      if menu_port_screen; then return 0; fi
      continue
    fi
    menu_header
    label="Rebuild complet — repatch du tip officiel + build"
    if [[ "$MENU_NET_OK" == "1" && -n "$MENU_TIP" && "$MENU_TIP" != "$MENU_LOCK_C" ]]; then
      label="Rebuild complet — ${MENU_LOCK_V} → ${MENU_TIP_V}"
    fi
    items=(
      "$label"
      "Rejouer le lock (${MENU_LOCK_V:-?})"
      "Apply seul, sans build"
      "Build + install seuls (worktree courant)"
      "Sonde détaillée"
      "Cible précise (SHA)…"
      "Options avancées…"
      "Aval : push, main, CI…"
      "Quitter"
    )
    rc=0; menu_select idx "$cur" "${items[@]}" || rc=$?
    if [[ "$rc" != "0" ]]; then exit 0; fi
    cur="$idx"
    case "$idx" in
      0) MODE=run; TARGET_MODE=tip;  DO_INSTALL=1; return 0 ;;
      1) MODE=run; TARGET_MODE=lock; DO_INSTALL=1; return 0 ;;
      2) MODE=run; TARGET_MODE=tip;  DO_INSTALL=0; return 0 ;;
      3) MODE=build-only; return 0 ;;
      4) MODE=check; return 0 ;;
      5)
        menu_ask 'SHA amont (vide = annuler) >' sha || continue
        if [[ -z "$sha" ]]; then continue; fi
        if ! git rev-parse --verify --quiet "${sha}^{commit}" >/dev/null; then
          warn "commit inconnu: $sha"; continue
        fi
        MODE=run; TARGET_MODE=sha; TARGET_SHA="$sha"; DO_INSTALL=1; return 0
        ;;
      6) menu_advanced ;;
      7) menu_downstream; menu_probe ;;
      8) exit 0 ;;
    esac
  done
}

if menu_wanted; then
  menu_main
  menu_equivalent
fi

if [[ "${GROK_PRIVACY_DRY_RUN:-0}" == "1" ]]; then
  printf 'PLAN mode=%s cible=%s sha=%s install=%s finalize=%s contrats=%s port=%s auto_resolve=%s force_apply=%s force_contracts=%s\n' \
    "$MODE" "$TARGET_MODE" "${TARGET_SHA:-}" "$DO_INSTALL" "$DO_FINALIZE" \
    "$DO_CONTRACTS" "$PORT_MODE" "$AUTO_RESOLVE" "${FORCE_APPLY:-0}" "$FORCE_CONTRACTS"
  exit 0
fi

printf '%s╭─ grok-privacy · simple · v%s ─%s\n' "$B" "$VERSION_SCRIPT" "$Z"
printf '%s│%s  fetch tip officiel → REPATCH systématique → build\n' "$D" "$Z"
printf '%s│%s  (GitHub only · défaut = dernier build xAI)\n' "$D" "$Z"
printf '%s╰%s\n' "$D" "$Z"

case "$MODE" in
  check) do_check ;;
  release) do_publish_release ;;
  build-only)
    do_verify_contracts
    do_build_install
    step "Terminé"; ok "Branche: $(git branch --show-current 2>/dev/null || echo '?')"
    ;;
  continue)
    do_continue
    do_finalize_if_needed
    if [[ "$DO_INSTALL" == "1" ]]; then
      do_verify_contracts
      do_build_install
    else ok "Reprise terminée (--no-install)."
    fi
    step "Terminé"
    ok "Branche: $(git branch --show-current 2>/dev/null || echo '?')"
    ok "Lock: $(lock_field version 2>/dev/null || echo '?') ($(lock_field commit 2>/dev/null | cut -c1-12 || echo '?'))"
    ;;
  run)
    do_apply
    do_finalize_if_needed
    if [[ "$DO_INSTALL" == "1" ]]; then
      do_verify_contracts
      do_build_install
    else ok "Apply terminé (--no-install)."
    fi
    step "Terminé"
    ok "Branche: $(git branch --show-current 2>/dev/null || echo '?')"
    ok "HEAD: $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
    ok "Lock: $(lock_field version 2>/dev/null || echo '?') ($(lock_field commit 2>/dev/null | cut -c1-12 || echo '?'))"
    ;;
esac
