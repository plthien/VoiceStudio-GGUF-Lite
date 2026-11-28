[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\vieneu-v3nano',[switch]$Smoke)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'install-vieneu-v3nano.ps1') -Root $Root -VerifyOnly -Smoke:$Smoke
if($LASTEXITCODE -and $LASTEXITCODE -ne 0){throw 'VIENEU_VERIFY_FAILED'}
