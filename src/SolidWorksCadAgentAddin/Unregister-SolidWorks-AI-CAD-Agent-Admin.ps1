$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator rights are required. Right-click PowerShell and choose Run as administrator."
}

$guid = "{D2A1EBA5-75E3-4BB6-923C-7B92E944FC89}"
$machineAddin = "HKLM:\SOFTWARE\SolidWorks\Addins\$guid"
if (Test-Path $machineAddin) {
    Remove-Item -LiteralPath $machineAddin -Recurse -Force
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $projectDir "Unregister-Addin.ps1")
Write-Host "AI CAD Agent was removed from the SOLIDWORKS Add-In Manager."
