# Scripts atelier / client

**Canon détaillé :** [`docs/DISTRIBUTION.md`](../docs/DISTRIBUTION.md)

| Commande | Script | Rôle |
|----------|--------|------|
| `grok rebuild` | `privacy-simple.sh` | Tip officiel → patches privacy → finalize → build/install local (cible : + publish GH) |
| `grok update` | `privacy-client-update.sh` | Met à jour le binaire depuis **nos** GitHub Releases |
| (wrapper) | `grok-wrapper.sh` | Route `rebuild` / `update` / sinon `~/.grok/bin/grok` |

## Menu

Sans argument et dans un terminal, `grok rebuild` ouvre un menu : en-tête d'état
(branche, lock, tip officiel, binaire installé, aval), puis les mêmes choix que
les flags. Il ne fait que remplir les réglages, affiche la commande équivalente,
et lance le pipeline habituel — il n'existe pas de second chemin d'exécution.

Navigation : **↑ ↓** (ou `j`/`k`) pour se déplacer, **Entrée** pour valider,
**Échap** pour revenir, **q** pour sortir. Les chiffres restent des
accélérateurs directs. Le sous-menu quitté garde la ligne où on l'avait laissé.

Hors terminal (CI, pipe, cron) ou dès qu'un argument est passé, aucun menu :
comportement strictement inchangé. `--menu` / `--no-menu` forcent l'un ou
l'autre, `GROK_PRIVACY_MENU=1|0` fait pareil par l'environnement.

Le menu ne montre que ce qui a du sens : quand un portage est interrompu, il ne
propose plus que « reprendre » ou « abandonner », parce qu'un apply neuf serait
refusé de toute façon.

Son sous-menu **Aval** est le seul endroit d'où ce script pousse : publier la
branche courante, ou remplacer `main` par elle. Le remplacement crée d'abord une
branche `backup/main-<version>`, exige de taper `REMPLACER`, pousse avec
`--force-with-lease`, et ne déplace `main` en local qu'une fois le distant
accepté.

`ALLOW_CONTRACT_FAIL` et `GROK_PRIVACY_MAX_SHRINK_PCT` restent volontairement
hors du menu : ils relâchent une gate, ça ne doit pas tenir en une frappe.

`GROK_PRIVACY_DRY_RUN=1` résout les réglages, imprime le plan, n'exécute rien.

## Usage courant

```bash
# Atelier (machine de build)
grok rebuild                 # menu
grok rebuild --check
grok rebuild --no-menu       # apply + build + install local, sans menu

# Client (toute machine, une fois les releases publiées)
grok update --check
grok update
```

## Conflit sur un nouveau tip amont

`grok rebuild` échoue fermé (code 3) dès qu'un patch critique conflicte : le tip
amont a bougé sous la file. Le rapport de conflit est écrit sous le git dir
(`grok-apply-conflict.diff`) même quand l'apply est annulé.

```bash
grok rebuild --port        # rejoue l'apply et laisse le conflit dans le worktree
# résoudre les fichiers listés, puis
git add <fichiers résolus>
grok rebuild --continue    # reprend la série, finalize, contrats, build, install
```

`--continue` rejoue aussi la queue d'apply (overlays, lock policy, commits de
control plane) : sans elle, le roundtrip de `finalize-sync` ne peut pas passer.

## Compat

| Ancien | Comportement |
|--------|----------------|
| `privacy-rebuild.sh` | Stub → `privacy-simple.sh` |
| `privacy-update.sh` | Avertissement → rebuild (pas le client) |
| `README-rebuild.md` | Redirige vers ce fichier + DISTRIBUTION |

## Assets attendus (GH Release)

- `grok-linux-x86_64`
- `grok-windows-x86_64.exe`

Voir CI [`.github/workflows/release.yml`](../.github/workflows/release.yml).
