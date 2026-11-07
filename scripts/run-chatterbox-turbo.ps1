[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-nano',[int]$Port=3901)
Write-Warning 'Deprecated name; this wrapper launches Nano only.'
& (Join-Path $PSScriptRoot 'run-chatterbox-nano.ps1') -Root $Root -Port $Port
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
