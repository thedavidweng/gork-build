# Rebuild vs Update

| Commande | Script | Rôle |
|----------|--------|------|
| **`grok rebuild`** | `privacy-rebuild.sh` | Atelier mainteneur : sonde, sync officiel, verify, finalize lock, **push repo** (SHA unique Linux+Windows) |
| **`grok update`** | `privacy-client-update.sh` | Met à jour le **binaire installé** sur cette machine (releases GH ; fallback `--from-source`) |

Jamais d’installers x.ai.

## Rebuild (atelier)

```bash
grok rebuild                 # menu ↑↓  (actions lourdes = confirmation o/N)
grok rebuild --check
grok rebuild --upstream
grok rebuild --finalize
grok rebuild --push-repo     # confirm ; source de vérité distante
grok rebuild --install-only  # smoke Linux local seulement
```

### Remotes (GitHub only)

| Remote | URL | Rôle |
|--------|-----|------|
| `origin` | `lycaos/grok-privacy` | ton fork |
| `upstream` | `xai-org/grok-build` | tip officiel (fetch only) |
| `gork-upstream` | `thedavidweng/gork-build` | optionnel, fetch only dans `.git` |

**Pas de clone local** `~/grok-build` / remote `xai-local`.

### Sync intelligente

Si `origin/sync/upstream-<ver>-<sha>` existe déjà pour le tip officiel,
« Synchroniser » **checkoute** cette branche au lieu de re-apply des patches
obsolètes depuis `main` (qui provoquait le conflit 0020).

Menu : **↑↓** naviguer, **Entrée** ou **l** lancer, **1–9** déplacent le curseur *sans* lancer,
**r** sonde, **q** quitter. Sync / push / finalize demandent une confirmation.

Si un `apply` échoue, retour à la branche d’origine. Copie stable des scripts :

`~/.local/share/grok-privacy/scripts/`

Flux :

1. Synchroniser le produit sur l’officiel  
2. Finaliser le lock  
3. Pousser le repo (`origin`) → **même SHA pour Linux et Windows**  
4. (lot suivant) tag + CI → assets `grok-linux-*` + `grok-windows-*`  
5. Sur chaque machine : `grok update`

## Update (client)

```bash
grok update              # télécharge la dernière release pour cette plateforme
grok update --check      # versions installée / disponible
grok update --from-source  # dev : pull main + cargo build
grok update -y
```

Tant qu’aucune release GH n’a d’assets, `grok update` l’explique et propose le fallback source.

## Compat

- `scripts/privacy-update.sh` → redirige vers **rebuild** avec un avertissement  
- `grok-privacy-update` (PATH) peut pointer vers rebuild ou le script compat  

## Cohérence multi-OS

- **Rebuild une fois** (atelier) → un commit partagé  
- **Update** sur Linux et Windows = consommer le **même tag** de release  
- Ne pas lancer un apply/patchctl séparé par OS
