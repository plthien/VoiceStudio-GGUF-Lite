[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$VoiceId,
  [ValidateSet('Smoke','ColdWarm','Pause','LongForm','All')][string]$Mode='Smoke',
  [string]$Root='D:\VoiceStudioBuild\chatterbox-nano',
  [string]$Manifest='', [string]$Output='')
$ErrorActionPreference='Stop'
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-nano') {throw 'NANO_D_DRIVE_ROOT_REQUIRED'}
$python=Join-Path $path '.venv-chatterbox\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) {throw 'ENV_NOT_INSTALLED'}
$env:PYTHONPATH=Split-Path -Parent $PSScriptRoot
$env:CHATTERBOX_ROOT=$path
$argsList=@('-m','lite.chatterbox_benchmark','--voice-id',$VoiceId,'--mode',$Mode)
if ($Manifest) {$argsList+=@('--manifest',$Manifest)}
if ($Output) {$argsList+=@('--output',$Output)}
& $python @argsList
if ($LASTEXITCODE -ne 0) {throw 'NANO_BENCHMARK_FAILED'}
