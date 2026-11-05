[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$VoiceId,
  [ValidateSet('Smoke','ColdWarm','Pause','LongForm','All')][string]$Mode='Smoke',
  [string]$Root='D:\VoiceStudioBuild\chatterbox-nano',
  [string]$Manifest='',
  [string]$Output='')
Write-Warning 'Deprecated name; this wrapper benchmarks Nano only.'
& (Join-Path $PSScriptRoot 'benchmark-chatterbox-nano.ps1') -VoiceId $VoiceId -Mode $Mode -Root $Root -Manifest $Manifest -Output $Output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
