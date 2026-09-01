#Requires -Version 5.1
<#
    privacy-client-update.ps1 — Mise à jour du binaire client installé (Windows)

    Équivalent PowerShell de scripts/privacy-client-update.sh, pour les machines
    où il n'y a ni bash ni wrapper : sous PowerShell, `grok` résout directement
    vers grok.exe, dont la sous-commande `update` est celle de xAI — verrouillée
    par le patch vendor-updater-hard-off. Ce script est le chemin du fork.

    Ce n'est PAS l'atelier mainteneur (sync officiel / patches / push repo).
    Pour ça : grok rebuild, sur la machine atelier.

    Stratégie : lire la dernière GitHub Release de lycaos/grok-privacy, choisir
    l'asset de cette plateforme, le télécharger, remplacer le binaire installé.

    Divergence assumée avec la version bash : ici on compare la version installée
    au tag avant de télécharger, et on s'arrête si c'est déjà à jour. L'asset
    Windows pèse ~263 Mo ; le retélécharger à chaque `-Yes` (tâche planifiée)
    serait un coût inutile. -Force passe outre.

    Usage :
      .\privacy-client-update.ps1              # met à jour
      .\privacy-client-update.ps1 --check      # versions seulement, aucune écriture
      .\privacy-client-update.ps1 -y           # non interactif
      .\privacy-client-update.ps1 --force      # retélécharge même si à jour

    Variables d'environnement :
      GROK_PRIVACY_BIN        défaut : %USERPROFILE%\.grok\bin\grok.exe
      GROK_PRIVACY_GH_REPO    défaut : lycaos/grok-privacy
#>

# 2.0 plutôt que Latest : « Latest » suit la version de PowerShell installée et
# peut durcir les règles d'une version à l'autre, sur des machines clientes où
# ce script n'est pas testé.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ScriptVersion = '1.0.0'

# ── Réglages ────────────────────────────────────────────────────────────────
$BinInstall = if ($env:GROK_PRIVACY_BIN) { $env:GROK_PRIVACY_BIN }
              else { Join-Path $env:USERPROFILE '.grok\bin\grok.exe' }
$GhRepo     = if ($env:GROK_PRIVACY_GH_REPO) { $env:GROK_PRIVACY_GH_REPO }
              else { 'lycaos/grok-privacy' }

# ── Sortie ──────────────────────────────────────────────────────────────────
function Write-Step { param([string]$m) Write-Host ''; Write-Host "── $m ──" -ForegroundColor White }
function Write-Info { param([string]$m) Write-Host '>> ' -ForegroundColor Cyan -NoNewline; Write-Host $m }
function Write-Ok   { param([string]$m) Write-Host 'OK  ' -ForegroundColor Green -NoNewline; Write-Host $m }
function Write-Warn { param([string]$m) Write-Host '!!  ' -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Write-Err  { param([string]$m) Write-Host 'ÉCHEC ' -ForegroundColor Red -NoNewline; Write-Host $m }

function Show-Banner {
    Write-Host "╭─ grok-privacy · update client (Windows) · v$ScriptVersion ─"
    Write-Host "│  Met à jour le binaire installé ($BinInstall)."
    Write-Host "│  Atelier mainteneur (repo / contrats / push) : grok rebuild"
    Write-Host "╰"
}

function Show-Usage {
    @"
grok-privacy update client (Windows) v$ScriptVersion

Met à jour le binaire Grok Privacy installé sur CETTE machine.
Ne touche ni à la série de patches ni au lock upstream.

Usage :
  .\privacy-client-update.ps1 [OPTIONS]

Options :
  --check, check     Afficher versions installée / disponible (aucune mutation)
  --yes, -y          Non interactif
  --force            Télécharger même si la version installée est déjà la bonne
  --from-source      Non supporté sous Windows (voir ci-dessous)
  -h, --help         Cette aide
  --version          Version du script

Variables :
  GROK_PRIVACY_BIN        défaut : %USERPROFILE%\.grok\bin\grok.exe
  GROK_PRIVACY_GH_REPO    défaut : lycaos/grok-privacy

Séparation :
  update   → binaire client (ce script)
  rebuild  → atelier Linux : sonde, sync officiel, contrats, finalize, release
"@
}

# ── Arguments (grammaire du script bash, pas celle de PowerShell) ────────────
$Mode       = 'update'
$AutoYes    = $false
$ForceDl    = $false

foreach ($a in $args) {
    switch -Regex ("$a") {
        '^(--check|check)$'   { $Mode = 'check' }
        '^(--yes|-y)$'        { $AutoYes = $true }
        '^(--force|-f)$'      { $ForceDl = $true }
        '^(-h|--help|aide)$'  { Show-Usage; exit 0 }
        '^--version$'         { Write-Host "grok-privacy-client-update $ScriptVersion"; exit 0 }
        '^--from-source$'     {
            Write-Err 'Non supporté sous Windows : --from-source compile depuis un clone local.'
            Write-Info 'Compile sur la machine atelier (grok rebuild), publie une release, puis relance ce script.'
            exit 2
        }
        '^(--upstream|--finalize|--push-repo|--all|--menu|--verify-only)$' {
            Write-Err "option d'atelier « $a » — elle appartient à grok rebuild, sur la machine atelier."
            exit 1
        }
        default { Write-Err "argument inconnu : $a (essayez --help)"; exit 1 }
    }
}

# ── Plateforme et assets attendus ───────────────────────────────────────────
function Get-PlatformName {
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'x86_64' }
        'ARM64' { 'aarch64' }
        'x86'   { 'i686' }
        default { 'x86_64' }
    }
    "windows-$arch"
}

function Get-AssetCandidates {
    param([string]$Platform)
    @("grok-$Platform.exe", "grokp-$Platform.exe", "grok-$Platform", "grokp-$Platform",
      "grok-$Platform.zip", 'grok.exe', 'grokp.exe')
}

function Get-InstalledVersion {
    if (-not (Test-Path -LiteralPath $BinInstall)) { return "(absent : $BinInstall)" }
    try { (& $BinInstall --version 2>$null | Select-Object -First 1) }
    catch { '(illisible)' }
}

# ── GitHub ──────────────────────────────────────────────────────────────────
function Get-LatestRelease {
    # TLS 1.2 n'est pas le défaut sur Windows PowerShell 5.1 / Windows ancien.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    $uri = "https://api.github.com/repos/$GhRepo/releases/latest"
    try {
        Invoke-RestMethod -Uri $uri -Headers @{ 'User-Agent' = 'grok-privacy-client-update' }
    } catch {
        Write-Warn "Interrogation des releases impossible ($uri) : $($_.Exception.Message)"
        $null
    }
}

function Select-ReleaseAsset {
    param($Release, [string]$Platform)
    if (-not $Release -or -not $Release.assets) { return $null }
    foreach ($cand in (Get-AssetCandidates -Platform $Platform)) {
        $hit = $Release.assets | Where-Object { $_.name -eq $cand } | Select-Object -First 1
        if ($hit) { return $hit }
    }
    # Repli : n'importe quel asset qui nomme la plateforme.
    $Release.assets | Where-Object { $_.name -like '*windows*' } | Select-Object -First 1
}

function Confirm-Action {
    param([string]$Prompt)
    if ($AutoYes) { return $true }
    Write-Host $Prompt
    $ans = Read-Host 'Confirmer ? [o/N]'
    return ($ans -match '^(o|oui|y|yes)$')
}

# ── Installation ────────────────────────────────────────────────────────────
function Install-Binary {
    param([string]$Source)
    $dir = Split-Path -Parent $BinInstall
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    # Windows verrouille un exécutable en cours d'exécution : on ne peut pas
    # l'écraser, mais on peut le renommer. On décale l'ancien, on met le neuf en
    # place, puis on tente le nettoyage — qui échouera tant qu'il tourne, sans
    # conséquence.
    $old = "$BinInstall.old"
    if (Test-Path -LiteralPath $BinInstall) {
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue }
        try { Move-Item -LiteralPath $BinInstall -Destination $old -Force }
        catch {
            Write-Err "Impossible de déplacer le binaire en place — une session grok tourne-t-elle encore ?"
            Write-Info "Ferme toutes les fenêtres grok, puis relance."
            throw
        }
    }
    Move-Item -LiteralPath $Source -Destination $BinInstall -Force
    Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue

    Write-Ok "Installé : $BinInstall"
    & $BinInstall --version
}

function Test-PathShadowing {
    $onPath = Get-Command grok -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -and ($onPath.Source -ne $BinInstall)) {
        Write-Warn "« grok » sur ton PATH pointe ailleurs : $($onPath.Source)"
        Write-Info "Ce script a mis à jour $BinInstall. Aligne le PATH, ou lance ce script avec GROK_PRIVACY_BIN=<ce chemin>."
    }
}

# ── Modes ───────────────────────────────────────────────────────────────────
function Invoke-Check {
    Show-Banner
    $platform = Get-PlatformName
    Write-Step 'État client'
    Write-Host ("  Installé     {0}" -f (Get-InstalledVersion))
    Write-Host ("  Chemin       {0}" -f $BinInstall)
    Write-Host ("  Plateforme   {0}" -f $platform)
    Write-Host ("  Dépôt GH     {0}" -f $GhRepo)
    Write-Host ''

    $rel = Get-LatestRelease
    if (-not $rel) { Write-Warn "Pas de release exploitable pour l'instant."; return }
    $asset = Select-ReleaseAsset -Release $rel -Platform $platform
    if ($asset) {
        $mb = [math]::Round($asset.size / 1MB)
        Write-Ok "Release disponible : $($rel.tag_name)  asset=$($asset.name)  ${mb} Mo"
    } else {
        Write-Warn "Release $($rel.tag_name) sans asset pour $platform"
    }
    Write-Info 'Atelier mainteneur : grok rebuild (machine Linux)'
}

function Invoke-Update {
    Show-Banner
    $platform = Get-PlatformName
    Write-Step "Recherche d'une release GitHub ($GhRepo)"
    Write-Info "Plateforme détectée : $platform"
    $installed = Get-InstalledVersion
    Write-Info "Binaire actuel     : $installed"

    $rel = Get-LatestRelease
    if (-not $rel) { Write-Err 'Aucune release lisible — réseau ou dépôt inaccessible.'; exit 2 }
    Write-Info "Dernière release   : $($rel.tag_name)"

    $asset = Select-ReleaseAsset -Release $rel -Platform $platform
    if (-not $asset) {
        Write-Err "Release $($rel.tag_name) trouvée, mais aucun asset pour $platform."
        Write-Info 'Assets attendus : grok-windows-x86_64.exe'
        Write-Info 'Publier les binaires depuis la machine atelier : grok rebuild → Publier une release.'
        exit 2
    }

    # Déjà à jour ? Le tag est « vX.Y.Z », la version binaire « grok X.Y.Z (sha) ».
    $tagVersion = $rel.tag_name -replace '^v', ''
    if (-not $ForceDl -and $installed -match [regex]::Escape($tagVersion)) {
        Write-Ok "Déjà à jour ($installed)."
        Write-Info 'Forcer le retéléchargement : --force'
        Test-PathShadowing
        exit 0
    }

    $mb = [math]::Round($asset.size / 1MB)
    if (-not (Confirm-Action "Télécharger $($asset.name) ($($rel.tag_name), ${mb} Mo) → $BinInstall ?")) {
        Write-Warn 'Mise à jour annulée.'
        exit 1
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("grok-privacy-{0}.exe" -f [guid]::NewGuid())
    Write-Info 'Téléchargement…'
    # Invoke-WebRequest rend une barre de progression qui divise le débit par dix
    # sur les gros fichiers en PowerShell 5.1 ; on la coupe le temps du transfert.
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp `
            -Headers @{ 'User-Agent' = 'grok-privacy-client-update' }
    } finally {
        $ProgressPreference = $prev
    }

    $got = (Get-Item -LiteralPath $tmp).Length
    if ($got -ne $asset.size) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Write-Err "Téléchargement incomplet : $got octets reçus, $($asset.size) attendus."
        exit 1
    }

    Install-Binary -Source $tmp
    Write-Ok "Mise à jour client depuis la release $($rel.tag_name)"
    Test-PathShadowing
}

switch ($Mode) {
    'check'  { Invoke-Check }
    'update' { Invoke-Update }
}
