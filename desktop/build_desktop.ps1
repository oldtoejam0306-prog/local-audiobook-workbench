param(
    [Parameter(Mandatory = $true)][string]$SdkDir,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'bin'),
    [string]$IconPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\app-icon\audiobook-workbench.ico')
)

$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'WorkbenchDesktop.cs'
$flatCoreDll = Join-Path $SdkDir 'Microsoft.Web.WebView2.Core.dll'
$flatFormsDll = Join-Path $SdkDir 'Microsoft.Web.WebView2.WinForms.dll'
$nugetLib = Join-Path $SdkDir 'lib\net462'
$nugetCoreDll = Join-Path $nugetLib 'Microsoft.Web.WebView2.Core.dll'
$nugetFormsDll = Join-Path $nugetLib 'Microsoft.Web.WebView2.WinForms.dll'
$loaderDll = Join-Path $SdkDir 'runtimes\win-x64\native\WebView2Loader.dll'
$license = Join-Path $PSScriptRoot 'WEBVIEW2-LICENSE.txt'

if ((Test-Path -LiteralPath $flatCoreDll -PathType Leaf) -and (Test-Path -LiteralPath $flatFormsDll -PathType Leaf)) {
    $coreDll = $flatCoreDll
    $formsDll = $flatFormsDll
} else {
    $coreDll = $nugetCoreDll
    $formsDll = $nugetFormsDll
}

foreach ($path in @($compiler, $source, $coreDll, $formsDll, $loaderDll, $IconPath, $license)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required build input not found: $path" }
}

$out = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $out -Force | Out-Null
$exe = Join-Path $out 'AudiobookWorkbench.exe'
$outCore = Join-Path $out 'Microsoft.Web.WebView2.Core.dll'
$outForms = Join-Path $out 'Microsoft.Web.WebView2.WinForms.dll'
$outLoader = Join-Path $out 'WebView2Loader.dll'
$outIcon = Join-Path $out 'audiobook-workbench.ico'
$outLicense = Join-Path $out 'WEBVIEW2-LICENSE.txt'

& $compiler /nologo /target:winexe /platform:x64 /optimize+ /warn:4 /codepage:65001 `
    /out:$exe /win32icon:$([System.IO.Path]::GetFullPath($IconPath)) `
    /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll `
    /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll `
    /reference:$coreDll /reference:$formsDll $source
if ($LASTEXITCODE -ne 0) { throw "C# compilation failed with exit code $LASTEXITCODE" }

Copy-Item -LiteralPath $coreDll -Destination $outCore -Force
Copy-Item -LiteralPath $formsDll -Destination $outForms -Force
Copy-Item -LiteralPath $loaderDll -Destination $outLoader -Force
Copy-Item -LiteralPath $IconPath -Destination $outIcon -Force
Copy-Item -LiteralPath $license -Destination $outLicense -Force
Get-FileHash -Algorithm SHA256 -LiteralPath $exe, $outCore, $outForms, $outLoader, $outIcon, $outLicense
