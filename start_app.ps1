$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$port = 8501
$localUrl = "http://localhost:$port/"

function Test-AppReady {
    try {
        $response = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-AppReady) {
    Start-Process $localUrl
    Write-Host "Streamlit is already running at $localUrl"
    exit 0
}

$portOwner = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -ne $portOwner) {
    throw "Port $port is already used by another process. Close it or run the app on a different port."
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $created = $false
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3.12 -m venv (Join-Path $PSScriptRoot ".venv")
        $created = ($LASTEXITCODE -eq 0)
    }

    if (-not $created) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw "Python was not found. Install Python 3.12, then run start_app.bat again."
        }
        & $pythonCommand.Source -m venv (Join-Path $PSScriptRoot ".venv")
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Python virtual environment."
        }
    }
}

$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
$dependencyMarker = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $dependencyMarker) {
    (Get-Content -LiteralPath $dependencyMarker -Raw).Trim()
} else {
    ""
}

$dependenciesReady = $installedHash -eq $requirementsHash
if ($dependenciesReady) {
    & $venvPython -m pip check | Out-Null
    $dependenciesReady = $LASTEXITCODE -eq 0
}

if (-not $dependenciesReady) {
    Write-Host "Installing or updating Python dependencies..."
    & $venvPython -m pip install --disable-pip-version-check -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Check the network and try again."
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Installed dependencies have conflicts. Check requirements.txt."
    }
    Set-Content -LiteralPath $dependencyMarker -Value $requirementsHash -Encoding ascii
}

$serverArgs = @(
    "-m", "streamlit", "run", "app.py",
    "--server.headless=true", "--server.port=$port"
)
$server = Start-Process -FilePath $venvPython -ArgumentList $serverArgs -WorkingDirectory $PSScriptRoot -PassThru
$ready = $false
try {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-AppReady) {
            $ready = $true
            break
        }
        if ($server.HasExited) {
            throw "Streamlit failed to start. Run python -m streamlit run app.py to view logs."
        }
    }

    if ($ready) {
        Start-Process $localUrl
        Write-Host "App started at $localUrl"
        Write-Host "Closing this window will stop the app."
    } else {
        throw "App startup timed out. Check whether port $port is already in use."
    }
    Wait-Process -Id $server.Id
} finally {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
    }
}
