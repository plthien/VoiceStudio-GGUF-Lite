[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-turbo',[int]$Port=3901)
$ErrorActionPreference='Stop'
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-turbo') {
  throw 'Chatterbox installation must be on D:'
}
if($Port -lt 1 -or $Port -gt 65535){throw 'Invalid port'}
$python=Join-Path $path '.venv-chatterbox\Scripts\python.exe'
if (!(Test-Path $python)){throw 'Environment not installed; run install-chatterbox-turbo.ps1'}
$env:CHATTERBOX_ROOT=$path
$env:CHATTERBOX_PORT=[string]$Port
$env:PYTHONPATH=Split-Path -Parent $PSScriptRoot
$env:HF_HOME=Join-Path $path 'cache\hf'
$env:HF_HUB_CACHE=Join-Path $path 'cache\hf\hub'
$env:TORCH_HOME=Join-Path $path 'cache\torch'
$env:TMP=Join-Path $path 'cache\tmp'
$env:TEMP=$env:TMP
Write-Output "Local service: http://127.0.0.1:$Port/ui"
& $python -m uvicorn lite.chatterbox_app:app --host 127.0.0.1 --port $Port --no-access-log
if ($LASTEXITCODE -ne 0) { throw "Chatterbox service exited with code $LASTEXITCODE" }
