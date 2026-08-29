$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("a_share_" + $stamp)
$archive = Join-Path $PSScriptRoot ("a-share-package-" + $stamp + ".zip")

New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "app.py") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "DEPLOY.md") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "requirements.txt") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "start_app.bat") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "start_app.ps1") -Destination $staging
    $packageSource = Join-Path $PSScriptRoot "stock_analysis"
    $packageTarget = Join-Path $staging "stock_analysis"
    New-Item -ItemType Directory -Path $packageTarget | Out-Null
    Get-ChildItem -LiteralPath $packageSource -File -Filter "*.py" |
        Copy-Item -Destination $packageTarget
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".streamlit\config.toml")) {
        New-Item -ItemType Directory -Path (Join-Path $staging ".streamlit") | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".streamlit\config.toml") -Destination (Join-Path $staging ".streamlit")
    }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $archive
    Write-Host "Share package created: $archive"
    Write-Host "Send it to the recipient, extract it, and double-click start_app.bat."
} finally {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
