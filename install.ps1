# periodika-agent uzstādīšana uz Windows (PowerShell).
#
#   .\install.ps1              # virtuālā vide + pamatuzstādīšana + pārbaude
#   .\install.ps1 -All         # arī attēlu apstrāde un Anthropic SDK
#   .\install.ps1 -NoVenv      # uzstādīt pašreizējā Python vidē
param(
    [switch]$All,
    [switch]$Images,
    [switch]$NoVenv
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$extras = ""
if ($Images) { $extras = "[images]" }
if ($All)    { $extras = "[images,claude]" }

$python = "python"
if (-not (Get-Command $python -ErrorAction SilentlyContinue)) {
    Write-Error "Nav atrasts python. Uzstādi Python 3.9+ no python.org vai: winget install Python.Python.3.12"
}
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Error "Vajadzīgs Python 3.9 vai jaunāks." }

if (-not $NoVenv) {
    if (-not (Test-Path ".venv")) {
        Write-Host "==> Veidoju virtuālo vidi .venv"
        & $python -m venv .venv
    }
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
}

Write-Host "==> Uzstādu periodika-agent$extras"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e ".$extras"

Write-Host "==> Sistēmas rīki (neobligāti)"
foreach ($tool in @("tesseract", "pdftoppm")) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) {
        Write-Host "    [ok]  $tool"
    } else {
        Write-Host "    [--]  $tool nav atrasts"
    }
}
if (-not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    Write-Host "          winget install UB-Mannheim.TesseractOCR"
}
if (-not (Get-Command pdftoppm -ErrorAction SilentlyContinue)) {
    Write-Host "          winget install oschwartz10612.Poppler"
}

Write-Host ""
& $python -m periodika doctor --offline

Write-Host @"

==> Talak:
    .\.venv\Scripts\Activate.ps1
    periodika doctor
    periodika probe --sample-issue <laidiena-ID>
    periodika mcp-install --target claude-desktop --write
"@
