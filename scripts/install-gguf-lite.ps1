param([string]$Python="py", [string]$Root="D:\VoiceStudioBuild\lite")
$ErrorActionPreference="Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$venv = Join-Path $Root ".venv-lite"
& $Python -3.11 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 venv creation failed" }
& (Join-Path $venv "Scripts\python.exe") -m pip install --requirement (Join-Path $Repo "requirements-lite.txt")
if ($LASTEXITCODE -ne 0) { throw "Lite-only dependency installation failed" }
Write-Host "Installed Lite-only venv to $venv. No model or binary was downloaded."
