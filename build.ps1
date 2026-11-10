[CmdletBinding()]
param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,63}$')]
    [string]$Name = 'Gugugu101',
    [ValidateNotNullOrEmpty()]
    [string]$DisplayName = '咕顾谷',
    [ValidateNotNullOrEmpty()]
    [string]$BinaryName = '咕顾谷',
    [ValidatePattern('^\d+\.\d+\.\d+(?:\.\d+)?$')]
    [string]$ProductVersion = '1.0.1',
    [string]$IconPath = 'assets\app-icon.png',
    [string]$OutputFolder = 'dist',
    [switch]$SourceQaOnly,
    [switch]$SkipQaLauncher
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$launcherSource = Join-Path $projectRoot 'interactive-qa-launcher.cs'
$binaryStem = if ($BinaryName.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { $BinaryName.Substring(0, $BinaryName.Length - 4) } else { $BinaryName }
if ([string]::IsNullOrWhiteSpace($binaryStem) -or $binaryStem.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
    $binaryStem.TrimEnd(' ', '.') -ne $binaryStem -or $binaryStem -in @('.', '..') -or
    $binaryStem -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
    throw 'BinaryName must be a plain Windows filename without a path.'
}
$binaryFileName = $binaryStem + '.exe'
if (@($ProductVersion.Split('.') | Where-Object { [int]$_ -gt 65535 }).Count) { throw 'Version components must be at most 65535.' }

function CSharp-Literal([string]$Value) {
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n') + '"'
}

function Build-QaLauncher([string]$Destination, [switch]$SourceMode) {
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw 'The local .NET C# compiler is unavailable; the QA launcher could not be built.'
    }
    $compilerArguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001', "/out:$Destination")
    if ($SourceMode) { $compilerArguments += '/define:SOURCE_MODE' }
    $generatedDirectory = Join-Path $projectRoot 'build'
    New-Item -ItemType Directory -Path $generatedDirectory -Force | Out-Null
    $sourceKind = if ($SourceMode) { 'source' } else { $Name }
    $effectiveSource = Join-Path $generatedDirectory ("qa-launcher-$sourceKind.cs")
    $template = [IO.File]::ReadAllText($launcherSource)
    $template = $template.Replace('__APP_BINARY_LITERAL__', (CSharp-Literal $binaryFileName))
    $template = $template.Replace('__APP_NAME_LITERAL__', (CSharp-Literal $DisplayName))
    $template = $template.Replace('__APP_QA_TITLE_LITERAL__', (CSharp-Literal ($DisplayName + ' · 验证启动器')))
    $template = $template.Replace('__APP_VERSION_LITERAL__', (CSharp-Literal $ProductVersion))
    if (-not (Test-Path -LiteralPath $effectiveSource -PathType Leaf) -or [IO.File]::ReadAllText($effectiveSource) -ne $template) {
        [IO.File]::WriteAllText($effectiveSource, $template, [Text.UTF8Encoding]::new($false))
    }
    if ($SourceMode -and (Test-Path -LiteralPath $Destination -PathType Leaf) -and
        (Get-Item -LiteralPath $effectiveSource).LastWriteTimeUtc -le (Get-Item -LiteralPath $Destination).LastWriteTimeUtc) { return }
    $compilerArguments += $effectiveSource
    & $compiler @compilerArguments
    if ($LASTEXITCODE -ne 0) { throw "QA launcher compilation failed: $LASTEXITCODE" }
}

# Reuse the unchanged source launcher: a source QA run may still be using that
# executable while the separate packaged application is being built.
$sourceLauncher = Join-Path $projectRoot 'Interactive-Source-QA.exe'
if ($SourceQaOnly -and $SkipQaLauncher) { throw 'SourceQaOnly cannot be combined with SkipQaLauncher.' }
if (-not $SkipQaLauncher) { Build-QaLauncher -Destination $sourceLauncher -SourceMode }
if ($SourceQaOnly) {
    Write-Output (Join-Path $projectRoot 'Interactive-Source-QA.exe')
    return
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python environment missing: $Python" }
$guideName = '使用前请先阅读.txt'
$guide = Join-Path $projectRoot $guideName
if (-not (Test-Path -LiteralPath $guide -PathType Leaf)) { throw "Build input missing: $guideName" }
foreach ($required in @('app.py', 'catalog.json', 'assets', 'samples')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $required))) { throw "Build input missing: $required" }
}

$buildRoot = Join-Path $projectRoot 'build'
$distributionRoot = if ([IO.Path]::IsPathRooted($OutputFolder)) {
    [IO.Path]::GetFullPath($OutputFolder)
} else {
    [IO.Path]::GetFullPath((Join-Path $projectRoot $OutputFolder))
}
if (-not $distributionRoot.StartsWith(($projectRoot + '\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output must stay in a subfolder of this project.'
}
$prospectiveBundle = Join-Path $distributionRoot $Name
if (Test-Path -LiteralPath (Join-Path $prospectiveBundle 'data')) {
    throw 'This output already contains saved user data. Choose another -Name or -OutputFolder for a side-by-side build.'
}
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
$resolvedIcon = if ([IO.Path]::IsPathRooted($IconPath)) { [IO.Path]::GetFullPath($IconPath) } else { [IO.Path]::GetFullPath((Join-Path $projectRoot $IconPath)) }
if (-not (Test-Path -LiteralPath $resolvedIcon -PathType Leaf)) { throw 'The application icon is missing.' }
$versionFile = Join-Path $buildRoot ($Name + '-version-info.txt')
$versionGenerator = @'
from pathlib import Path
import sys
from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo,
    StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct)
display, binary, version, output = sys.argv[1:]
parts = tuple(int(part) for part in version.split('.'))
parts += (0,) * (4 - len(parts))
fields = {'FileDescription': display, 'ProductName': display, 'InternalName': display,
          'OriginalFilename': binary, 'FileVersion': version, 'ProductVersion': version,
          'CompanyName': '', 'LegalCopyright': ''}
resource = VSVersionInfo(ffi=FixedFileInfo(filevers=parts, prodvers=parts,
    mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('080404B0', [StringStruct(k, v) for k, v in fields.items()])]),
          VarFileInfo([VarStruct('Translation', [2052, 1200])])])
Path(output).write_text(str(resource), encoding='utf-8')
'@
& $Python -X utf8 -c $versionGenerator $DisplayName $binaryFileName $ProductVersion $versionFile
if ($LASTEXITCODE -ne 0) { throw 'Windows application version resources could not be generated.' }
$oldCacheDirectory = $env:PYINSTALLER_CONFIG_DIR
$oldUtf8 = $env:PYTHONUTF8
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $buildRoot 'pyinstaller-cache'
    $env:PYTHONUTF8 = '1'
    $packagerArguments = @(
        '-X', 'utf8', '-m', 'PyInstaller',
        '--noconfirm', '--onedir', '--windowed', '--noupx',
        '--name', $Name,
        '--icon', $resolvedIcon,
        '--version-file', $versionFile,
        '--distpath', $distributionRoot,
        '--workpath', (Join-Path $buildRoot 'pyinstaller'),
        '--specpath', $buildRoot,
        '--paths', $projectRoot,
        '--add-data', ((Join-Path $projectRoot 'catalog.json') + ';.'),
        '--add-data', ((Join-Path $projectRoot 'assets') + ';assets'),
        '--add-data', ((Join-Path $projectRoot 'samples') + ';samples'),
        '--collect-all', 'rapidocr',
        '--collect-all', 'windows_capture',
        '--collect-binaries', 'onnxruntime',
        '--collect-data', 'onnxruntime',
        '--copy-metadata', 'rapidocr',
        '--copy-metadata', 'windows-capture',
        '--copy-metadata', 'onnxruntime',
        '--exclude-module', 'PySide6.QtWebEngineCore',
        '--exclude-module', 'PySide6.QtWebEngineWidgets',
        '--exclude-module', 'PySide6.QtWebEngineQuick',
        '--exclude-module', 'PySide6.QtWebChannel',
        '--exclude-module', 'PySide6.QtQml',
        '--exclude-module', 'PySide6.QtQuick',
        '--exclude-module', 'PySide6.QtQuickWidgets',
        (Join-Path $projectRoot 'app.py')
    )
    Push-Location -LiteralPath $projectRoot
    try {
        & $Python @packagerArguments
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
    }
    finally { Pop-Location }

    $bundle = Join-Path $distributionRoot $Name
    $workingExecutable = Join-Path $bundle ($Name + '.exe')
    if (-not (Test-Path -LiteralPath $workingExecutable -PathType Leaf)) { throw 'The packaged executable was not produced.' }
    $executable = Join-Path $bundle $binaryFileName
    if ($workingExecutable -ne $executable) { Move-Item -LiteralPath $workingExecutable -Destination $executable }

    # Qt6Core expects the Windows OS ICU exports (ucnv_open, etc.). The build
    # host's PATH also contains Poppler's incompatible version-suffixed ICU78.
    # It must not shadow the OS component. PE imports were checked: Qt6Core is
    # this bundle's only icuuc.dll consumer; icudt78 is only that ICU's data DLL.
    $internalRoot = [IO.Path]::GetFullPath((Join-Path $bundle '_internal'))
    foreach ($unrelatedIcu in @('icuuc.dll', 'icudt78.dll')) {
        $artifactPath = [IO.Path]::GetFullPath((Join-Path $internalRoot $unrelatedIcu))
        if (-not $artifactPath.StartsWith(($internalRoot + '\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Unexpected ICU artifact path.'
        }
        if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
            Remove-Item -LiteralPath $artifactPath
        }
    }
    if (-not $SkipQaLauncher) { Build-QaLauncher -Destination (Join-Path $bundle 'Interactive-QA.exe') }
    Copy-Item -LiteralPath $guide -Destination (Join-Path $bundle $guideName)
    $exampleSource = Join-Path $projectRoot 'samples\web-demo.json'
    if (Test-Path -LiteralPath $exampleSource -PathType Leaf) {
        $exampleDirectory = Join-Path $bundle 'samples'
        New-Item -ItemType Directory -Path $exampleDirectory -Force | Out-Null
        Copy-Item -LiteralPath $exampleSource -Destination (Join-Path $exampleDirectory 'web-demo.json')
    }
    Write-Output $executable
}
finally {
    if ($null -eq $oldCacheDirectory) { Remove-Item Env:\PYINSTALLER_CONFIG_DIR -ErrorAction SilentlyContinue }
    else { $env:PYINSTALLER_CONFIG_DIR = $oldCacheDirectory }
    if ($null -eq $oldUtf8) { Remove-Item Env:\PYTHONUTF8 -ErrorAction SilentlyContinue }
    else { $env:PYTHONUTF8 = $oldUtf8 }
}
