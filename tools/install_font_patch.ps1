param(
    [string]$GamePaks = 'M:\XboxGames\Avowed\Content\Alabama\Content\Paks'
)
$ErrorActionPreference = 'Stop'
if (Get-Process '*Avowed*' -ErrorAction SilentlyContinue) {
    throw 'Close Avowed before installing the font patch.'
}
$sourceDir = Join-Path $PSScriptRoot '..\build\fonts'
$patchName = 'Alabama-WinGDK_UkrFonts_P'
$extensions = @('.pak', '.utoc', '.ucas')
if (-not (Test-Path -LiteralPath (Join-Path $GamePaks 'Alabama-WinGDK.utoc'))) {
    throw 'Original Avowed container was not found.'
}
foreach ($extension in $extensions) {
    $sourcePath = Join-Path $sourceDir ($patchName + $extension)
    if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Build missing: $sourcePath" }
}
$backupDir = Join-Path $sourceDir ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
foreach ($extension in $extensions) {
    $destination = Join-Path $GamePaks ($patchName + $extension)
    if (Test-Path -LiteralPath $destination) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        Copy-Item -LiteralPath $destination -Destination $backupDir
    }
}
foreach ($extension in $extensions) {
    $sourcePath = Join-Path $sourceDir ($patchName + $extension)
    $destination = Join-Path $GamePaks ($patchName + $extension)
    Copy-Item -LiteralPath $sourcePath -Destination $destination
    if ((Get-FileHash -LiteralPath $sourcePath).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
        throw "Installed hash mismatch: $destination"
    }
    Write-Output "Installed and verified: $destination"
}
