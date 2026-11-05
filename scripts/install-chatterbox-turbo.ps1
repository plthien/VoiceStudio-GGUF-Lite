# Deprecated filename retained for automation compatibility; installs Nano only.
[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-nano',
      [switch]$SkipDownload,
      [switch]$VerifyOnly)
Write-Warning 'Deprecated name; this wrapper installs Chatterbox-Nano only.'
$arguments=@('-Root',$Root)
if($SkipDownload){$arguments+='-SkipDownload'}
if($VerifyOnly){$arguments+='-VerifyOnly'}
& (Join-Path $PSScriptRoot 'install-chatterbox-nano.ps1') @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
