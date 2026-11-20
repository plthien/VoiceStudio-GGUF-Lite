# Launch the EXISTING one-server UI and both independent providers.
[CmdletBinding()]
param([string]$VieNeuRoot='D:\VoiceStudioBuild\vieneu-v3nano',
      [string]$ChatterboxRoot='D:\VoiceStudioBuild\chatterbox-nano',
      [int]$Port=3901)
$ErrorActionPreference='Stop'
$env:VIENEU_ROOT=[IO.Path]::GetFullPath($VieNeuRoot).TrimEnd('\')
& (Join-Path $PSScriptRoot 'verify-vieneu-v3nano.ps1') -Root $env:VIENEU_ROOT
if($LASTEXITCODE -and $LASTEXITCODE -ne 0){throw 'VIENEU_PREFLIGHT_FAILED'}
& (Join-Path $PSScriptRoot 'run-chatterbox-nano.ps1') -Root $ChatterboxRoot -Port $Port
