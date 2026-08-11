$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dll = Join-Path $projectDir "bin\AI.CAD.Agent.SolidWorks.dll"
$guid = "{D2A1EBA5-75E3-4BB6-923C-7B92E944FC89}"
if (-not (Test-Path $dll)) { throw "Add-in DLL not found. Run Build-Addin.ps1 first: $dll" }

$assemblyName = [Reflection.AssemblyName]::GetAssemblyName($dll).FullName
$codeBase = ([Uri]$dll).AbsoluteUri
$className = "AICADAgent.SolidWorks.SolidWorksAddin"
$runtime = "v4.0.30319"
$inproc = "HKCU:\Software\Classes\CLSID\$guid\InprocServer32"
$version = Join-Path $inproc "1.0.0.0"
$category = "HKCU:\Software\Classes\CLSID\$guid\Implemented Categories\{62C8FE65-4EBB-45E7-B440-6E39B2CDBF29}"

foreach ($path in @($inproc, $version, $category)) { New-Item -Path $path -Force | Out-Null }
foreach ($path in @($inproc, $version)) {
    Set-Item -Path $path -Value "mscoree.dll"
    New-ItemProperty -Path $path -Name "ThreadingModel" -Value "Both" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $path -Name "Class" -Value $className -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $path -Name "Assembly" -Value $assemblyName -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $path -Name "RuntimeVersion" -Value $runtime -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $path -Name "CodeBase" -Value $codeBase -PropertyType String -Force | Out-Null
}
New-Item -Path "HKCU:\Software\Classes\CLSID\$guid\ProgId" -Force | Out-Null
Set-Item -Path "HKCU:\Software\Classes\CLSID\$guid\ProgId" -Value "AICADAgent.SolidWorks.Addin"
$progId = "HKCU:\Software\Classes\AICADAgent.SolidWorks.Addin"
New-Item -Path $progId -Force | Out-Null
Set-Item -Path $progId -Value "AI CAD Agent for SOLIDWORKS"
New-Item -Path (Join-Path $progId "CLSID") -Force | Out-Null
Set-Item -Path (Join-Path $progId "CLSID") -Value $guid

$addin = "HKCU:\Software\SolidWorks\Addins\$guid"
$startup = "HKCU:\Software\SolidWorks\AddInsStartup\$guid"
New-Item -Path $addin -Force | Out-Null
Set-Item -Path $addin -Value 0
New-ItemProperty -Path $addin -Name "Title" -Value "AI CAD Agent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $addin -Name "Description" -Value "Natural language, PDF and CAD file automation through the shared AI CAD Agent Pipeline" -PropertyType String -Force | Out-Null
New-Item -Path $startup -Force | Out-Null
Set-Item -Path $startup -Value 1
[Environment]::SetEnvironmentVariable("CAD_AGENT_PROJECT_ROOT", (Resolve-Path (Join-Path $projectDir "..\..")).Path, "User")
Write-Host "Registered AI CAD Agent add-in for the current Windows user."
Write-Host "Restart SOLIDWORKS, then enable AI CAD Agent under Tools > Add-Ins if it is not loaded automatically."
