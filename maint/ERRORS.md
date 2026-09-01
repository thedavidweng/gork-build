# Erreurs corrigées

Une entrée par erreur : cause → preuve → fix. Sous `maint/` parce que le control
plane est restauré après chaque apply ; à la racine, ce fichier disparaîtrait au
premier `grok rebuild`.

## 2026-07-31 — Patch restauré depuis gork sans réadapter l'identité

**Cause** — `0080-product-identity` a été repris tel quel du fork amont pour
récupérer sa substance, en supposant qu'un patch « identité » était neutre.
**Preuve** — `grok --version` affichait `gork 0.2.116`.
**Fix** — 172 substitutions sur la file, puis vérification par exécution du
binaire, pas par relecture du patch. Reprendre un patch amont, c'est reprendre
son identité : toute réutilisation passe par un diff des chaînes visibles.

## 2026-07-31 — Correction d'identité faite au mauvais étage

**Cause** — les chaînes ont d'abord été corrigées uniquement dans
`maint/patches/`, en croyant la file seule source de l'arbre.
**Preuve** — après apply, `maint/overlays/README.md` et
`maint/control/.github/workflows/` réintroduisaient `gork` ; le workflow CI
pointait encore sur `target/release/gork`.
**Fix** — trois étages écrivent l'arbre produit et doivent bouger ensemble :
patches (appliqués), overlays (écrasent après l'apply), control plane
(restauré après l'apply, avec `maint/control/` qui écrase les chemins live).
Après toute correction transverse : re-apply puis grep sur l'arbre, jamais sur
la seule source éditée.

## 2026-07-31 — `git add -A` a versionné `__pycache__`

**Cause** — un `import patchctl` depuis un script ponctuel a produit
`maint/scripts/__pycache__/`, ramassé par un `git add -A`.
**Preuve** — `git checkout --detach` refusé pendant l'apply : *« Your local
changes to maint/scripts/__pycache__/patchctl.cpython-312.pyc would be
overwritten »*.
**Fix** — `.gitignore` couvre `__pycache__/` et `*.pyc`. Importer un module du
repo depuis un one-liner produit des artefacts : préférer l'appel en
sous-processus, ou nettoyer avant de stager.

## 2026-07-31 — SHA256SUMS régénéré avant la dernière édition de patch

**Cause** — les sommes ont été recalculées puis un patch a encore été édité.
**Preuve** — `SHA256 mismatch for 0080-product-identity.patch` au premier apply.
**Fix** — la régénération est la toute dernière opération d'un lot d'édition de
patches. Le garde-fou a fonctionné : il n'a rien laissé passer.

## 2026-07-31 — `[[ cond ]] && cmd` en milieu de fonction sous `set -e`

**Cause** — écriture réflexe de `[[ -n "$x" ]] && cmd` pour une action
conditionnelle. Condition fausse ⇒ statut 1 ⇒ le script meurt sans message.
**Preuve** — repéré à la relecture avant exécution, sur deux sites ajoutés le
jour même (`do_finalize_if_needed`, `do_verify_contracts`).
**Fix** — `if … then … fi` dès qu'on n'est pas sur la dernière ligne d'une
fonction dont on veut propager le statut.

## 2026-07-31 — Critère de finalize trop strict

**Cause** — pour détecter un lock périmé, `product_tip` a été comparé à `HEAD`.
**Preuve** — après finalize, `product_tip=6d9b9cd` et `HEAD=21be712` : le lock
venait d'être écrit et se déclarait déjà périmé, refinalisant à chaque rebuild.
**Fix** — `product_tip` désigne le dernier commit du produit, donc le parent du
commit qui écrit le lock : à jour, il vaut `HEAD` **ou** `HEAD^`. Un pointeur
« dernier X » se compare à la position qu'il désigne, pas à la tête courante.

## 2026-07-31 — Éditions perdues : pipeline lancé sur un worktree sale

**Cause** — un lot d'éditions (renommage `grokp`) laissé non commité avant de
lancer `grok rebuild`. L'apply a échoué sur le worktree sale, et son
gestionnaire d'erreur `restore_branch` a fait `git checkout -f` : il a détruit
exactement le travail qui avait causé l'échec.
**Preuve** — `git commit` suivant : *« nothing to commit, working tree clean »*,
et `PRODUCT_CLI` revenu à sa valeur d'avant.
**Fix** — `require_clean_worktree` refuse de démarrer un apply et `restore_branch`
ne force plus. Côté méthode : commiter avant de lancer un pipeline qui manipule
git, sans attendre que le lot soit « fini ».

## 2026-07-31 — Recalculateur d'en-têtes de hunk faux sur les fichiers créés

**Cause** — pour propager le décalage après ajout de lignes, `+start` a été
recalculé comme `a + delta`. Sur un fichier créé, `a` vaut 0 : la formule
produisait `+0`, alors que la forme correcte est `-0,0 +1,N`.
**Preuve** — `git diff` sur quatre patchs non concernés par le lot :
`-@@ -0,0 +1,79 @@` / `+@@ -0,0 +0,79 @@`.
**Fix** — ne recalculer l'offset que si `a > 0`, sinon conserver la valeur
d'origine. Le signal d'alarme était que l'outil touchait des fichiers qui
n'auraient pas dû bouger : vérifier l'étendue d'une transformation automatique
avant d'en lire le résultat.

## 2026-08-08 — Asset Windows jamais publiable : deux noms pour la même chose

**Cause** — la matrice de `release.yml` téléversait `grokp-windows-x86_64.exe`,
le job de publication retéléchargeait `grok-windows-x86_64.exe`. L'asset étant
`optional: true`, rien ne cassait : la release sortait sans Windows.
**Preuve** — les cinq occurrences du fichier, dont une seule en `grokp-`, contre
`docs/DISTRIBUTION.md` qui se déclare canon et dit `grok-windows-x86_64.exe`.
Le garde-fou existait mais imputait l'absence à `MSVC LNK4319` : un bug d'une
ligne déguisé en bug dur connu, donc jamais cherché.
**Fix** — nom aligné sur le canon, dans le fichier live **et** dans
`maint/control/.github/workflows/release.yml` qui l'écrase après chaque apply ;
avertissement reformulé pour ne plus imputer de cause. Un message qui affirme
une cause qu'il n'a pas vérifiée coûte plus cher que pas de message du tout.

## 2026-08-03 — Diagnostic d'infra recopié en dur dans la sonde

**Cause** — la sonde aval suggérait une cause à un échec CI (« compte GitHub
bloqué ») dans une chaîne écrite en dur, et une session suivante a ajouté la
remontée du message d'annotation GitHub. Deux façons d'inscrire durablement,
dans un dépôt de code, un diagnostic sur l'état d'un compte.
**Preuve** — la chaîne survivait à chaque `grok rebuild` via le control plane
et s'affichait à chaque sonde, hors de tout contexte.
**Fix** — chaîne neutralisée, remontée d'annotation retirée. Un outil de build
rapporte ce qu'il observe (`failure`, l'horodatage, le titre) et renvoie vers
l'onglet Actions ; l'état d'un compte n'est ni son affaire, ni une chose à
graver dans un fichier versionné.

## 2026-08-03 — Dates GitHub affichées en UTC, prises pour l'heure locale

**Cause** — `downstream_state` imprimait `.createdAt` brut, or l'API GitHub date
tout en UTC (suffixe `Z`). Deux heures de moins que l'horloge, l'été à Paris.
**Preuve** — `timedatectl` : `Europe/Paris (CEST, +0200)`, NTP actif, RTC en UTC
— la machine était juste. Le run affiché `16:47:21Z` valait bien `18:47 CEST`.
**Fix** — helper `local_time` sur `date -d`, avec repli sur la valeur brute.
`gh --template` ne pouvait pas aider : son `timefmt` reformate sans changer de
fuseau (vérifié sur gh 2.45). Un horodatage qui vient du réseau porte son
fuseau : le convertir est le travail de l'affichage, pas celui du lecteur.

## 2026-08-03 — `IFS=$'\t'` : un champ vide en tête décale toute la ligne

**Cause** — `@tsv` produisait `""` pour un booléen faux en première colonne. La
tabulation est un caractère *blanc* : `read` fusionne les séparateurs qui se
suivent et ignore ceux de tête, donc chaque champ remontait d'un cran.
**Preuve** — la liste des releases testée sur `cli/cli` : la ligne `latest`
correcte, les deux suivantes avec le tag dans la colonne du drapeau et
l'horodatage brut dans celle du tag.
**Fix** — placeholder `-` au lieu de la chaîne vide. Un champ potentiellement
vide ne se met jamais en tête d'une ligne lue avec un IFS blanc. Découvert en
testant le chemin « releases » sur un dépôt qui en a : le dépôt du projet n'en
publie aucune, ce chemin n'aurait jamais été exercé localement.

## 2026-08-03 — Transition 0.2.118 : file périmée face à un resserrement de visibilité

**Cause** — 0.2.118 resserre massivement la visibilité amont (921 ajouts de
`pub(crate)` entre dd04f39 et 780d138), dont
`GrokAuth::is_data_collection_disabled`. `0030-retention-opt-out` insère des
lignes de part et d'autre de cette signature : le 3-way n'a plus de résolution.
**Preuve** — `git am --3way` de 0030 sur 780d138 : `CONFLICT (content)` dans
`auth/model.rs`, un seul bloc, exactement sur cette ligne ; le reflog s'arrête
sur `am --abort` après 3 patchs.
**Fix** — série ré-exportée sur 780d138 ; le portage tient en **une ligne de
contexte** (`pub` → `pub(crate)`, tous les appelants restant intra-crate). Le
pipeline n'était pas en cause : c'est la file qui datait de 0.2.116. Croiser
d'abord les fichiers des patchs avec `git diff --name-only <lock> <tip>` donne
la carte des risques avant de lancer quoi que ce soit.

## 2026-08-03 — Conflit critique : preuve détruite, aucune reprise possible

**Cause** — sur conflit critique, `patchctl apply` faisait `git am --abort`
*avant* de rendre la main, et `privacy-simple.sh` annonçait « Un port manuel est
requis. Branche restaurée. » Aucun des deux n'était vrai : plus rien à
inspecter, et rien pour finir la queue d'apply (overlays, lock policy, commits
de control plane) dont dépend le roundtrip de `finalize-sync`.
**Preuve** — reflog figé sur `am --abort`, worktree resté sur la branche sync
partielle ; `git checkout main` refusé (« untracked working tree files would be
overwritten ») parce que le control plane est **non suivi** sur les branches
sync/* et suivi sur `main` — vérifié : les 58 fichiers sont pourtant identiques
au bit près, git refuse quand même.
**Fix** — rapport de conflit écrit sous le git dir avant tout abort ;
`apply --keep-conflict` / `apply --continue` (état de reprise sous le git dir,
jamais dans l'arbre produit) ; `grok rebuild --port` / `--continue` ; message de
`restore_branch` aligné sur ce qui s'est réellement passé. Un gestionnaire
d'erreur qui affirme avoir restauré doit le prouver, pas le supposer.

## 2026-08-01 — Historique pollué : 88 commits, 5 identités, série empilée 5×

**Cause** — config git machine `test <test@test.com>` jamais imposée par le
pipeline, patchs exportés avec un `From:` étranger, et chaque rebuild mergé
dans `main` au lieu de le remplacer : la série complète s'empilait à chaque
passage (`privacy-core` présent 5 fois).
**Preuve** — `git log dd04f39..main --format='%an <%ae>' | sort -u` : 5
identités distinctes sur 88 commits, pour 12 patchs de contenu.
**Fix** — `main` reconstruit : base upstream dd04f39 + série appliquée une
fois + 2 commits d'outillage, arbre final identique au bit près (`git diff`
vide). Identité imposée repo-local et via env par privacy-simple.sh, défauts
patchctl → Lycaos, `From:` des 12 patchs réécrits. Règle : `main` est un
artefact régénérable — il se remplace, il ne se merge pas.
