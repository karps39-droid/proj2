# periodika-agent uzstādīšana uz Windows (PowerShell).
#
#   .\install.ps1              # virtuālā vide + pamatuzstādīšana + pārbaude
#   .\install.ps1 -All         # arī attēlu apstrāde un Anthropic SDK
#   .\install.ps1 -NoVenv      # uzstādīt pašreizējā Python vidē
#
# SVARĪGI: šis fails jāglabā kā UTF-8 AR BOM. Windows PowerShell 5.1 lasa .ps1
# bez BOM sistēmas ANSI kodējumā (CP1252), un tad latviešu burti sabojājas tā,
# ka daži pārtop par pēdiņām (ē = C4 93; 0x93 CP1252 ir “), kas sagrauj visa
# faila parsēšanu. Ja rediģē šo failu, saglabā to ar BOM.
param(
    [switch]$All,
    [switch]$Images,
    [switch]$Browser,
    [switch]$NoVenv
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$extras = ""
if ($Images)  { $extras = "[images]" }
if ($Browser) { $extras = "[browser]" }
if ($All)     { $extras = "[images,claude,browser]" }

# Atrod derīgu Python. Nosaukums "python" uz Windows mēdz nebūt: Microsoft Store
# instalācija liek tikai "python3.12", bet "py" var rādīt uz izdzēstu vidi.
# Tāpēc katru kandidātu tiešām palaižam, nevis tikai pārbaudām, vai tas eksistē.
function Find-Python {
    $candidates = @(
        @("python"), @("python3"),
        @("python3.14"), @("python3.13"), @("python3.12"),
        @("python3.11"), @("python3.10"), @("python3.9"),
        @("py", "-3")
    )
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        foreach ($cand in $candidates) {
            $exe = $cand[0]
            $pre = @($cand | Select-Object -Skip 1)
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            try {
                $global:LASTEXITCODE = 0
                & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { return @{ Exe = $exe; Pre = $pre } }
            } catch { }
        }
    } finally { $ErrorActionPreference = $prevEA }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Error "Nav atrasts Python 3.9+. Uzstādi to no python.org vai: winget install Python.Python.3.12"
}
$pythonExe = $py.Exe
$pythonPre = $py.Pre
Write-Host ("==> Python: " + (& $pythonExe @pythonPre -c "import sys; print(sys.executable or 'python'); " ) )

if (-not $NoVenv) {
    if (-not (Test-Path ".venv")) {
        Write-Host "==> Veidoju virtuālo vidi .venv"
        & $pythonExe @pythonPre -m venv .venv
        if ($LASTEXITCODE -ne 0) { Write-Error "Neizdevās izveidot .venv" }
    }
    $pythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    $pythonPre = @()
}

Write-Host "==> Uzstādu periodika-agent$extras"
& $pythonExe @pythonPre -m pip install --quiet --upgrade pip
& $pythonExe @pythonPre -m pip install --quiet -e ".$extras"

if ($All -or $Browser) {
    Write-Host "==> Lejupielādēju Chromium (SPA lapu lasīšanai)"
    & $pythonExe @pythonPre -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    Neizdevās. Ja Chromium jau ir, norādi PERIODIKA_CHROMIUM=C:\ceļš\uz\chrome.exe"
    }
}

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
& $pythonExe @pythonPre -m periodika doctor --offline

Write-Host @"

==> Tālāk:
    .\.venv\Scripts\Activate.ps1
    periodika doctor
    periodika probe --sample-issue <laidiena-ID>
    periodika mcp-install --target claude-code-lietotaja --write
"@
