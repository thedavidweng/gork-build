# Distribution — contrat rebuild / update / releases

**Canon** pour ce fork (`lycaos/grok-privacy`).  
Toute autre doc ne doit que **résumer** et **pointer ici**.

## Contrat

| Commande | Qui | Fait | Ne fait pas |
|----------|-----|------|-------------|
| **`grok rebuild`** | Atelier (mainteneur) | Tip officiel xAI → patches privacy → lock → (cible) **publish** Linux + Windows sur ce repo GH | Mettre à jour les machines clientes |
| **`grok update`** | Client (toute machine) | Télécharge l’asset de **notre** GitHub Release pour la plateforme | Sync patches, cargo atelier, installers x.ai |
| **`grok`** (binaire) | Runtime | TUI / agent privacy | Se mettre à jour via canaux x.ai |

Jamais d’installers `x.ai/cli`. Le vendor auto-update est hard-off dans le binaire.

### Flux cible

```text
upstream (xai-org/grok-build)
        │  grok rebuild
        ▼
  apply patches + finalize lock
        │
        ▼
  push branche / tag origin
        │
   CI .github/workflows/release.yml
   matrix: linux-x86_64 + windows-x86_64
        │
        ▼
  GitHub Release (assets stables ci-dessous)
        │  grok update
        ▼
  ~/.grok/bin/grok   (ou équivalent Windows)
```

## Noms d’assets (contrat dur)

Doivent rester alignés entre la CI, `scripts/privacy-client-update.sh` et
`scripts/privacy-client-update.ps1` :

| Plateforme | Nom d’asset |
|------------|-------------|
| Linux x86_64 | `grok-linux-x86_64` |
| Windows x86_64 | `grok-windows-x86_64.exe` |

Aliases acceptés côté client (fuzzy) : `grok-privacy-linux-x86_64`, etc.  
**Préférer les noms du tableau.**

## Remotes & chemins

| Remote | URL typique | Rôle |
|--------|-------------|------|
| `upstream` | `xai-org/grok-build` | Fetch only (tip officiel) |
| `origin` | `lycaos/grok-privacy` | Vérité distante + **Releases** |

| Chemin | Rôle |
|--------|------|
| `~/.grok/bin/grok` | Binaire installé (privacy) |
| `~/.local/bin/grok` | Wrapper : route `rebuild` / `update` / sinon le binaire |
| `~/.local/share/grok-privacy/scripts/` | Copie stable des scripts atelier |

## Commandes (état code)

### `grok rebuild` → `scripts/privacy-simple.sh`

Pipeline actuel (sans menu) :

```bash
grok rebuild              # tip officiel + repatch + finalize + build + install local
grok rebuild --check      # sonde lock vs tip (aucune écriture)
grok rebuild --lock       # rejouer le SHA du lock seulement
grok rebuild --sha HASH   # apply sur un commit précis
grok rebuild --no-install # apply (+ finalize) sans compiler
grok rebuild --no-finalize
grok rebuild --build-only # build+install du worktree courant
```

**Aujourd’hui** le défaut s’arrête à *install local*.  
**Cible** : après finalize, pousser + déclencher / s’appuyer sur `release.yml` (tag ou `workflow_dispatch`).  
Smoke local seul : `--build-only` / install sans publish.

**Retiré** (ne plus documenter comme vivant) : menu ↑↓ atelier, `--push-repo`, `--upstream` menu, flags de l’ancien `privacy-rebuild.sh` 1200 lignes.

### `grok update` → `scripts/privacy-client-update.sh`

```bash
grok update           # télécharge la dernière release GH pour cette plateforme
grok update --check   # versions installée / disponible (aucune mutation)
grok update -y        # non interactif
```

- **Chemin normal** = GitHub Releases de **ce** repo uniquement.
- **`--from-source`** = secours **dev** (clone + cargo). Hors contrat multi-OS.  
  Ne pas l’utiliser pour « se mettre à jour comme un client ». Préfère le tip privacy courant / lock, pas un `main` en retard.

### Windows → `scripts/privacy-client-update.ps1`

Le wrapper est un script bash : sous PowerShell il n’existe pas, donc `grok`
résout directement vers `grok.exe` et `grok update` tombe sur la sous-commande
**xAI**, verrouillée par `vendor-updater-hard-off`. Le message de refus est
correct — il n’y a simplement pas de chemin de mise à jour derrière. D’où
l’équivalent PowerShell, même contrat :

```powershell
.\privacy-client-update.ps1            # télécharge la dernière release GH
.\privacy-client-update.ps1 --check    # versions installée / disponible
.\privacy-client-update.ps1 -y         # non interactif
.\privacy-client-update.ps1 --force    # retélécharge même si déjà à jour
```

Installation sur une machine cliente (elle ne clone pas le dépôt) :

```powershell
$dst = "$env:USERPROFILE\.grok\bin\privacy-client-update.ps1"
Invoke-WebRequest -UseBasicParsing `
  -Uri "https://raw.githubusercontent.com/lycaos/grok-privacy/main/scripts/privacy-client-update.ps1" `
  -OutFile $dst
powershell -ExecutionPolicy Bypass -File $dst --check
```

Pour retrouver le geste `grok update`, ajouter au profil PowerShell
(`$PROFILE`) l’équivalent du wrapper bash :

```powershell
function grok {
    if ($args.Count -gt 0 -and $args[0] -eq 'update') {
        & powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.grok\bin\privacy-client-update.ps1" @($args | Select-Object -Skip 1)
    } else {
        & "$env:USERPROFILE\.grok\bin\grok.exe" @args
    }
}
```

Deux écarts assumés avec la version bash, documentés dans l’en-tête du script :
`--from-source` n’est pas supporté (il compile depuis un clone : c’est le rôle
de la machine atelier), et la version installée est comparée au tag avant de
télécharger — l’asset Windows pèse ~263 Mo, le reprendre à chaque exécution
non interactive serait un coût inutile. `--force` passe outre.

### Confusion xAI

Avec le **wrapper** privacy, `grok update` n’est **pas** l’updater officiel.  
`~/.grok/bin/grok update` (binaire nu) = sous-commande xAI → **ne pas utiliser** pour ce fork.  
Sous PowerShell il n’y a pas de wrapper : c’est exactement ce qu’on atteint sans
la fonction de profil ci-dessus.

## Politique git (atelier)

| Branche | Rôle |
|---------|------|
| `main` | Tree privacy **publiable** (canon distant) |
| `sync/upstream-<ver>-<sha>` | Apply d’un tip officiel + patches |

Un rebuild de tip produit une branche `sync/*`. Elle doit être **poussée** puis intégrée selon la politique d’atelier avant / avec la release.  
Les machines clientes ne clonent pas ces branches : elles font `grok update`.

## État de livraison

| Étape | Statut |
|-------|--------|
| Apply + finalize (`privacy-simple`) | **Fait** |
| Install local post-rebuild | **Fait** |
| Client `update` (download GH) | **Fait** — bash + PowerShell |
| Workflow `release.yml` Linux | **Vert** (smoke OK) |
| Workflow `release.yml` Windows | **Bloqué** — MSVC **LNK4319** (limite PDB / public symbols) sur runners VS 18 ; job en `continue-on-error`. Contourné : l'asset Windows est produit en **cross depuis Linux** (`x86_64-pc-windows-gnu`) par `grok rebuild` → « Publier une release ». |
| Releases GH avec assets | **Fait** — Linux + Windows depuis `v1.0.3` (2026-08-13) |
| Rebuild → push/tag/publish automatique | **TODO** (après tag manuel stable) |
| Tip 0.2.112+ sur `main` / origin | **À clôturer** (branches sync locales possibles) |

**Windows (détail)** : protoc + parse makefile deps sont OK ; le link `gork.exe` échoue
(`LINK : fatal error LNK4319`). `/DEBUG:FASTLINK` n’est plus supporté sur le toolchain
du runner et retombe en `/DEBUG:FULL`. Pistes : `rust-lld`, cross depuis Linux
(`cargo-xwin`), ou réduire massivement les symboles.

Les deux assets existent depuis `v1.0.3` : `grok update` a désormais un chemin réel sur
les deux plateformes. Le contournement dev (`--from-source`) reste réservé à une machine
qui a le clone et cargo — ce n’est **pas** le chemin produit.

## CI release (amorce)

Fichier : [`.github/workflows/release.yml`](../.github/workflows/release.yml)

- **Triggers** : tag `v*` · `workflow_dispatch`
- **Matrix** : `ubuntu-latest` → asset Linux · `windows-latest` → asset Windows
- **Build** : `cargo build -p xai-grok-pager-bin --release` puis renommage vers les noms d’assets
- **Publish** : `gh release` / softprops sur tag (permissions `contents: write`)

Vérifier après première release réelle :

```bash
gh release list -R lycaos/grok-privacy
grok update --check
```

## Anti-dérive (pour les prochains changements)

1. Toute mention de `grok rebuild` / `grok update` hors de ce fichier = **≤ 3 phrases + lien ici**.
2. Changer un nom d’asset ou un flag → **même PR** : ce canon + scripts + `release.yml`.
3. Pas de second README « atelier menu » vivant.
4. Si le code n’a pas encore publish : la section **État** reste honnête (WIP), pas un « lot suivant » sans propriétaire.

## Voir aussi

- [`scripts/README.md`](../scripts/README.md) — résumé ops
- [`maint/README.md`](../maint/README.md) — patchctl bas niveau
- [`PRIVACY.md`](../PRIVACY.md) — hard-offs (dont vendor update)
- [`README.md`](../README.md) — face publique
