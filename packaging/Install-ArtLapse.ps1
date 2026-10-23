param(
    [string]$Source = (Join-Path $PSScriptRoot 'ArtLapse'),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Programs\ArtLapse'),
    [string]$SettingsFrom = ''
)
$ErrorActionPreference = 'Stop'
$sourcePath = [IO.Path]::GetFullPath($Source)
$targetPath = [IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'ArtLapse.exe'))) {
    throw 'Source must contain the complete verified ArtLapse onedir build.'
}
if ((Test-Path -LiteralPath $targetPath) -and
    (Get-ChildItem -LiteralPath $targetPath -Force | Select-Object -First 1)) {
    throw 'Destination must be empty. Existing applications and projects are never overwritten.'
}
if ($targetPath.StartsWith($sourcePath + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase) -or $targetPath -eq $sourcePath) {
    throw 'Destination must be outside the source build.'
}
$settingsPath = $null
if ($SettingsFrom) {
    $settingsPath = [IO.Path]::GetFullPath($SettingsFrom)
    if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
        throw 'SettingsFrom must be an existing settings.ini file.'
    }
    if (Test-Path -LiteralPath (Join-Path $sourcePath 'data\settings.ini')) {
        throw 'Source already contains settings; refusing to overwrite them.'
    }
}
$requiredBytes = (Get-ChildItem -LiteralPath $sourcePath -File -Recurse |
    Measure-Object -Property Length -Sum).Sum
$driveRoot = [IO.Path]::GetPathRoot($targetPath)
$drive = [IO.DriveInfo]::new($driveRoot)
if ($drive.AvailableFreeSpace -lt $requiredBytes + 10485760) {
    throw 'Insufficient free disk space for installation.'
}
New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
foreach ($item in Get-ChildItem -LiteralPath $sourcePath -Force) {
    Copy-Item -LiteralPath $item.FullName -Destination $targetPath -Recurse
}
if ($settingsPath) {
    $stateDirectory = Join-Path $targetPath 'data'
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    Copy-Item -LiteralPath $settingsPath -Destination (Join-Path $stateDirectory 'settings.ini')
}
Write-Output "Installed to $targetPath. Open ArtLapse.exe. Projects and videos are stored separately."
