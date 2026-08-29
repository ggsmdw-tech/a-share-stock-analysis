$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

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

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed. Check the network and try again."
}

$serverArgs = @(
    "-m", "streamlit", "run", "app.py",
    "--server.headless=true", "--server.port=8501"
)
$server = Start-Process -FilePath $venvPython -ArgumentList $serverArgs -WorkingDirectory $PSScriptRoot -PassThru
$ready = $false
try {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:8501/" -UseBasicParsing -TimeoutSec 2
            $ready = $true
            break
        } catch {
            if ($server.HasExited) {
                throw "Streamlit failed to start. Run python -m streamlit run app.py to view logs."
            }
        }
    }

    if ($ready) {
        Start-Process "http://localhost:8501/"
        Write-Host "App started at http://localhost:8501/"
        Write-Host "Closing this window will stop the app."
    } else {
        throw "App startup timed out. Check whether port 8501 is already in use."
    }
    Wait-Process -Id $server.Id
} finally {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
    }
}
