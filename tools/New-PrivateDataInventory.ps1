[CmdletBinding()]
param(
    [string]$RepositoryRoot,
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $RepositoryRoot 'docs\PRIVATE_DATA_INVENTORY.json'
}
$dataRoot = Join-Path $RepositoryRoot 'local-data\playlists'
if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
    throw "Private playlist root does not exist: $dataRoot"
}

$files = @(
    Get-ChildItem -LiteralPath $dataRoot -File -Recurse -Force |
        Where-Object { $_.Extension -in @('.m3u', '.m3u8') } |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject]@{
                relativePath = $_.FullName.Substring($RepositoryRoot.Length + 1).Replace('\', '/')
                sizeBytes = $_.Length
                lastWriteTimeUtc = $_.LastWriteTimeUtc.ToString('o')
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
)

$uniqueHashes = @($files.sha256 | Sort-Object -Unique)
$inventory = [ordered]@{
    schemaVersion = 1
    generatedAtUtc = [DateTime]::UtcNow.ToString('o')
    classification = 'private-local-operational-data'
    contentIncluded = $false
    totalFiles = $files.Count
    uniqueSha256 = $uniqueHashes.Count
    totalBytes = [long](($files | Measure-Object sizeBytes -Sum).Sum)
    files = $files
}

$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$json = $inventory | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($OutputPath, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Host "Private inventory written: $OutputPath" -ForegroundColor Green
Write-Host "Files: $($files.Count); unique hashes: $($uniqueHashes.Count)" -ForegroundColor Green
