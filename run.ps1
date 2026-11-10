[CmdletBinding()]
param(
    [switch]$Source,
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,63}$')]
    [string]$Name = 'Gugugu101',
    [ValidateNotNullOrEmpty()]
    [string]$BinaryName = '咕顾谷',
    [string]$OutputFolder = 'dist',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments = @()
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$runtimeOutput = if ([IO.Path]::IsPathRooted($OutputFolder)) { $OutputFolder } else { Join-Path $projectRoot $OutputFolder }
$binaryStem = if ($BinaryName.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { $BinaryName.Substring(0, $BinaryName.Length - 4) } else { $BinaryName }
if ([string]::IsNullOrWhiteSpace($binaryStem) -or $binaryStem.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
    $binaryStem.TrimEnd(' ', '.') -ne $binaryStem -or $binaryStem -in @('.', '..') -or
    $binaryStem -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { throw 'BinaryName must be a plain Windows filename without a path.' }
$packaged = Join-Path (Join-Path $runtimeOutput $Name) ($binaryStem + '.exe')
$sourcePython = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'

function Quote-WindowsArgument([string]$Value) {
    $quoted = [Text.StringBuilder]::new('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$quoted.Append(('\' * ($slashes * 2 + 1)))
            [void]$quoted.Append('"')
        }
        else {
            [void]$quoted.Append(('\' * $slashes))
            [void]$quoted.Append($character)
        }
        $slashes = 0
    }
    [void]$quoted.Append(('\' * ($slashes * 2)))
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

if (-not $Source -and (Test-Path -LiteralPath $packaged -PathType Leaf)) {
    $executable = $packaged
    $launchArguments = @($AppArguments)
}
else {
    if (-not (Test-Path -LiteralPath $sourcePython -PathType Leaf)) { throw 'The local runtime is unavailable. Run the packaged executable after building.' }
    $sourceFile = Join-Path $projectRoot 'app.py'
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw 'The assistant source entry point is not ready.' }
    $executable = $sourcePython
    $launchArguments = @($sourceFile) + @($AppArguments)
}

$startOptions = @{ FilePath = $executable; WorkingDirectory = $projectRoot; WindowStyle = 'Hidden' }
if ($launchArguments.Count -gt 0) {
    $startOptions.ArgumentList = (($launchArguments | ForEach-Object { Quote-WindowsArgument $_ }) -join ' ')
}
Start-Process @startOptions
