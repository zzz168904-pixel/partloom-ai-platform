param(
    [string]$SolidWorksInteropDir = $env:SOLIDWORKS_INTEROP_DIR
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($SolidWorksInteropDir)) {
    $candidates = @(
        (Join-Path $env:ProgramFiles "SOLIDWORKS Corp\SOLIDWORKS\api\redist"),
        (Join-Path ${env:ProgramFiles(x86)} "SOLIDWORKS Corp\SOLIDWORKS\api\redist")
    )
    $SolidWorksInteropDir = $candidates |
        Where-Object { $_ -and (Test-Path (Join-Path $_ "SolidWorks.Interop.sldworks.dll")) } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($SolidWorksInteropDir)) {
    throw "SolidWorks interop directory was not found. Set SOLIDWORKS_INTEROP_DIR or pass -SolidWorksInteropDir."
}
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputDir = Join-Path $projectDir "bin"
$compiler = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$framework = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
$sources = @(
    (Join-Path $projectDir "Properties\AssemblyInfo.cs"),
    (Join-Path $projectDir "SolidWorksAddin.cs"),
    (Join-Path $projectDir "GatewayProcessManager.cs"),
    (Join-Path $projectDir "GatewayClient.cs"),
    (Join-Path $projectDir "AgentTaskPaneControl.cs")
)
$references = @(
    (Join-Path $framework "System.dll"),
    (Join-Path $framework "System.Core.dll"),
    (Join-Path $framework "System.Drawing.dll"),
    (Join-Path $framework "System.Net.Http.dll"),
    (Join-Path $framework "System.Web.Extensions.dll"),
    (Join-Path $framework "System.Windows.Forms.dll"),
    (Join-Path $SolidWorksInteropDir "SolidWorks.Interop.sldworks.dll"),
    (Join-Path $SolidWorksInteropDir "SolidWorks.Interop.swpublished.dll")
)

if (-not (Test-Path $compiler)) { throw "C# compiler not found: $compiler" }
foreach ($path in $references) { if (-not (Test-Path $path)) { throw "Reference not found: $path" } }
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$output = Join-Path $outputDir "AI.CAD.Agent.SolidWorks.dll"
$buildOutput = Join-Path $outputDir "AI.CAD.Agent.SolidWorks.build.dll"
if (Test-Path $buildOutput) { Remove-Item -LiteralPath $buildOutput -Force }
$arguments = @("/nologo", "/target:library", "/platform:x64", "/optimize+", "/out:$buildOutput")
$arguments += $references | ForEach-Object { "/reference:$_" }
$arguments += $sources
& $compiler $arguments
if ($LASTEXITCODE -ne 0) { throw "C# compilation failed with exit code $LASTEXITCODE" }
if (Test-Path $output) { Remove-Item -LiteralPath $output -Force }
Move-Item -LiteralPath $buildOutput -Destination $output -Force
Copy-Item (Join-Path $SolidWorksInteropDir "SolidWorks.Interop.sldworks.dll") $outputDir -Force
Copy-Item (Join-Path $SolidWorksInteropDir "SolidWorks.Interop.swpublished.dll") $outputDir -Force
Write-Host "Built: $output"
