$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dll = Join-Path $projectDir "bin\AI.CAD.Agent.SolidWorks.dll"
$register = Join-Path $projectDir "Register-Addin.ps1"
if (-not (Test-Path $dll)) {
    & (Join-Path $projectDir "Build-Addin.ps1")
}
& $register

$solidWorks = New-Object -ComObject SldWorks.Application
$solidWorks.Visible = $true
$result = $solidWorks.LoadAddIn($dll)
if ($result -ne 0) {
    throw "SOLIDWORKS LoadAddIn failed with code $result. DLL: $dll"
}

Write-Host "SOLIDWORKS is ready and the AI CAD Agent Task Pane is loaded."
Write-Host "Add-in DLL: $dll"
