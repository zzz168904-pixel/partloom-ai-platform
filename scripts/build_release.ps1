param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$buildVenv = Join-Path $root ".build-venv"
$python = Join-Path $buildVenv "Scripts\python.exe"
$artifacts = Join-Path $root "artifacts"
$dist = Join-Path $root "dist\PartLoomAI"

if (-not (Test-Path -LiteralPath $python)) {
    & py -3.13 -m venv $buildVenv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the build environment." }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $python -m pip install -r (Join-Path $root "requirements-dev.txt") "pyinstaller>=6.14,<7"
if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies." }

& $python (Join-Path $root "scripts\release_audit.py") $root
if ($LASTEXITCODE -ne 0) { throw "Release audit failed." }

if (-not $SkipTests) {
    & $python -m pytest (Join-Path $root "tests")
    if ($LASTEXITCODE -ne 0) { throw "Release tests failed." }
}

Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --clean (Join-Path $root "packaging\partloom.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
} finally {
    Pop-Location
}

$docs = @(
    "LICENSE",
    "NOTICE",
    "README.md",
    "CAPABILITIES.md",
    "INSTALL.md",
    "CONTRIBUTING.md",
    "CONTRIBUTOR_POLICY.md",
    "DISCLAIMER.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "SOURCE_AVAILABLE_SCOPE.md",
    "COMMERCIAL_LICENSE.md",
    "LICENSE_HISTORY.md",
    "VERSION"
)
foreach ($name in $docs) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $dist -Force
}

$docsDir = Join-Path $root "docs"
if (Test-Path -LiteralPath $docsDir) {
    $distDocs = Join-Path $dist "docs"
    New-Item -ItemType Directory -Path $distDocs -Force | Out-Null
    Get-ChildItem -LiteralPath $docsDir -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $distDocs -Force
    }
}

New-Item -ItemType Directory -Path $artifacts -Force | Out-Null
$zipPath = Join-Path $artifacts "PartLoom-AI-Platform-0.1.0-Beta.2-Windows-x64.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -LiteralPath $dist -DestinationPath $zipPath -CompressionLevel Optimal

if (-not $SkipInstaller) {
    $isccCandidates = @(
        $env:PARTLOOM_ISCC,
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($iscc) {
        & $iscc (Join-Path $root "packaging\PartLoomAIPlatform.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
    } else {
        Write-Warning "Inno Setup 6 was not found. ZIP created; installer compilation skipped."
    }
}

Write-Host "Standalone folder: $dist"
Write-Host "Release ZIP: $zipPath"
