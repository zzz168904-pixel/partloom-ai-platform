$ErrorActionPreference = "Stop"
$guid = "{D2A1EBA5-75E3-4BB6-923C-7B92E944FC89}"
$paths = @(
    "HKCU:\Software\SolidWorks\Addins\$guid",
    "HKCU:\Software\SolidWorks\AddInsStartup\$guid",
    "HKCU:\Software\Classes\CLSID\$guid",
    "HKCU:\Software\Classes\AICADAgent.SolidWorks.Addin"
)
foreach ($path in $paths) {
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Write-Host "Unregistered AI CAD Agent add-in for the current Windows user."
