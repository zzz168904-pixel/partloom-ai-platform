$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator rights are required. Right-click PowerShell and choose Run as administrator."
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$guid = "{D2A1EBA5-75E3-4BB6-923C-7B92E944FC89}"
$userRegistration = Join-Path $projectDir "Register-Addin.ps1"
& $userRegistration

$addin = "HKLM:\SOFTWARE\SolidWorks\Addins\$guid"
New-Item -Path $addin -Force | Out-Null
Set-Item -Path $addin -Value 0
New-ItemProperty -Path $addin -Name "Title" -Value "AI CAD Agent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $addin -Name "Description" -Value "Natural language, PDF and CAD file automation through the shared AI CAD Agent Pipeline" -PropertyType String -Force | Out-Null

Write-Host "AI CAD Agent is registered in the SOLIDWORKS Add-In Manager."
Write-Host "Restart SOLIDWORKS and enable AI CAD Agent under Tools > Add-Ins."
