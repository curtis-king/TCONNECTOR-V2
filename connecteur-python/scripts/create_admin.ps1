<#
.SYNOPSIS
    Cree ou reinitialise un administrateur T-CONNECTOR.

.DESCRIPTION
    Wrapper PowerShell autour de scripts/create_admin.py.

    Le mot de passe n'est jamais passe en argument : il est demande de facon
    masquee par Python, ou lu depuis la variable d'environnement
    TCONNECTOR_NEW_PASSWORD.

.EXAMPLE
    .\scripts\create_admin.ps1 -Email admin@client.cg -Diagnose

.EXAMPLE
    .\scripts\create_admin.ps1 -Email admin@client.cg -Reset

.EXAMPLE
    .\scripts\create_admin.ps1 -Email admin@client.cg -Reset -Db "C:\TConnector\data\tconnector.db"

.NOTES
    Si PowerShell bloque le script :
        powershell -ExecutionPolicy Bypass -File .\scripts\create_admin.ps1 -Email admin@client.cg -Reset
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Email,

    [string]$Nom = "Administrateur",
    [string]$Prenom = "",

    [switch]$Reset,
    [switch]$MustChange,
    [switch]$Diagnose,
    [switch]$NoMatrix,
    [switch]$NoUnlock,
    [switch]$NoBackup,
    [switch]$CreateDb,
    [switch]$KeepConfigPassword,

    [string]$Config = "",
    [string]$Db = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $PSScriptRoot "create_admin.py"

if (-not (Test-Path -LiteralPath $Script)) {
    Write-Host "ERREUR: script Python introuvable: $Script" -ForegroundColor Red
    exit 1
}

function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            Write-Host "ERREUR: Python introuvable: $Python" -ForegroundColor Red
            exit 1
        }
        return @{ Exe = $Python; Prefix = @() }
    }

    $candidates = @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root "python\python.exe"),
        (Join-Path $Root "runtime\python.exe"),
        (Join-Path $Root "_internal\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return @{ Exe = $candidate; Prefix = @() }
        }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -and $pythonCmd.Source -notlike "*\WindowsApps\*") {
        return @{ Exe = $pythonCmd.Source; Prefix = @() }
    }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        return @{ Exe = $pyCmd.Source; Prefix = @("-3") }
    }

    return $null
}

$resolved = Resolve-Python
if ($null -eq $resolved) {
    Write-Host "ERREUR: Python introuvable sur ce poste." -ForegroundColor Red
    Write-Host ""
    Write-Host "Solutions :"
    Write-Host "  1. Installer Python 3.10+ et relancer ce script."
    Write-Host "  2. Copier le dossier .venv de l'application dans : $Root"
    Write-Host "  3. Utiliser -Python `"C:\chemin\python.exe`""
    Write-Host ""
    Write-Host "Si le client n'a que TConnector.exe (PyInstaller), il faut une"
    Write-Host "version autonome / une commande --create-admin dans l'exe."
    exit 1
}

$arguments = @($Script, "--email", $Email)

if ($Nom) {
    $arguments += @("--nom", $Nom)
}
if ($Prenom) {
    $arguments += @("--prenom", $Prenom)
}
if ($Config) {
    $arguments += @("--config", $Config)
}
if ($Db) {
    $arguments += @("--db", $Db)
}
if ($Reset) {
    $arguments += "--reset"
}
if ($MustChange) {
    $arguments += "--must-change"
}
if ($Diagnose) {
    $arguments += "--diagnose"
}
if ($NoMatrix) {
    $arguments += "--no-matrix"
}
if ($NoUnlock) {
    $arguments += "--no-unlock"
}
if ($NoBackup) {
    $arguments += "--no-backup"
}
if ($CreateDb) {
    $arguments += "--create-db"
}
if ($KeepConfigPassword) {
    $arguments += "--keep-config-password"
}

Write-Host "Python : $($resolved.Exe)" -ForegroundColor DarkGray
$allArguments = @()
$allArguments += $resolved.Prefix
$allArguments += $arguments
& $resolved.Exe @allArguments
exit $LASTEXITCODE
