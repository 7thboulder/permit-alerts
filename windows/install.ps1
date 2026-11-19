# Installs Permit Alerts for the current Windows user.
#  - installs Python 3.12 with winget if it isn't already there
#  - creates a private Python environment in %LOCALAPPDATA%\permit-alerts
#  - installs this folder's copy of Permit Alerts into it
#  - adds "Permit Alerts" to the Desktop and Start menu, and (optionally) to Startup
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$home_dir = Join-Path $env:LOCALAPPDATA "permit-alerts"
$venv = Join-Path $home_dir "venv"

function Find-Python {
    foreach ($cmd in @("py -3.12", "py -3.11", "py -3", "python")) {
        try {
            $parts = $cmd.Split(" ")
            $v = & $parts[0] $parts[1..($parts.Length-1)] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { return $cmd }
        } catch {}
    }
    return $null
}

Write-Host "Permit Alerts installer" -ForegroundColor Green
$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.11+ not found. Installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + [System.Environment]::GetEnvironmentVariable("Path","Machine")
    $py = Find-Python
    if (-not $py) { throw "Python install didn't work. Install Python 3.12 from python.org, then run this again." }
}
Write-Host "Using $py"

New-Item -ItemType Directory -Force -Path $home_dir | Out-Null
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    $parts = $py.Split(" ")
    & $parts[0] $parts[1..($parts.Length-1)] -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install --upgrade "$repo" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$exe = Join-Path $venv "Scripts\permit-alerts.exe"
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $args_) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.Arguments = $args_
    $s.WorkingDirectory = $home_dir
    $s.Description = "Permit Alerts"
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Permit Alerts.lnk") "run"
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Permit Alerts.lnk") "run"

$answer = Read-Host "Start Permit Alerts automatically when you sign in to Windows? (Y/n)"
if ($answer -eq "" -or $answer -match "^[Yy]") {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath("Startup")) "Permit Alerts.lnk") "run --no-browser"
    Write-Host "Added to Startup."
}

Write-Host ""
Write-Host "Installed. Starting Permit Alerts; the dashboard opens at http://localhost:8645" -ForegroundColor Green
Write-Host "Keep the Permit Alerts window open (minimize it) - closing it stops the alerts."
Start-Process $exe "run"
