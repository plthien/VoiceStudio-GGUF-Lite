[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-nano',[int]$Port=3901)
$ErrorActionPreference='Stop'
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-nano') { throw 'Nano installation must use D:\VoiceStudioBuild\chatterbox-nano' }
if($Port -lt 1 -or $Port -gt 65535){throw 'INVALID_PORT'}
$python=Join-Path $path '.venv-chatterbox\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)){throw 'ENV_NOT_INSTALLED'}
$env:CHATTERBOX_ROOT=$path
$env:CHATTERBOX_PORT=[string]$Port
$env:PYTHONPATH=Split-Path -Parent $PSScriptRoot
$env:HF_HOME=Join-Path $path 'cache\hf'
$env:HF_HUB_CACHE=Join-Path $path 'cache\hf\hub'
$env:TORCH_HOME=Join-Path $path 'cache\torch'
$env:TMP=Join-Path $path 'cache\tmp'
$env:TEMP=$env:TMP
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
Write-Output "VoiceStudio Nano Lite: http://127.0.0.1:$Port/ui"
& $python -m uvicorn lite.chatterbox_app:app --host 127.0.0.1 --port $Port --no-access-log
if ($LASTEXITCODE -ne 0) { throw "NANO_SERVICE_EXIT_$LASTEXITCODE" }
