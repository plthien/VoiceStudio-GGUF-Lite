param([string]$Root="D:\VoiceStudioBuild\lite",[int]$Port=3900,[switch]$Preflight)
$ErrorActionPreference="Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ($Port -lt 1024 -or $Port -gt 65535) { throw "Invalid port" }
$env:GGUF_LITE_DATA_DIR = Join-Path $Root "data"
$env:OMNIVOICE_CACHE_DIR = "D:\VoiceStudioBuild\models"
$env:GGUF_LITE_BIN_DIR = Join-Path $Root "runtime\bin"
$python = Join-Path $Root ".venv-lite\Scripts\python.exe"
if (!(Test-Path $python)) { throw "Run install-gguf-lite.ps1 first" }
if (!(Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg missing from PATH" }
if (!(Get-Command vulkaninfo -ErrorAction SilentlyContinue)) { throw "vulkaninfo missing from PATH" }
Set-Location $Repo
if ($Preflight) {
  & $python -m lite.preflight
  exit $LASTEXITCODE
}
& $python -m uvicorn lite.app:app --host 127.0.0.1 --port $Port --workers 1
exit $LASTEXITCODE
