param([Parameter(Mandatory=$true)][string]$LegacyDb,
[Parameter(Mandatory=$true)][string]$LegacyVoices,
[Parameter(Mandatory=$true)][string]$TargetData,[switch]$Apply)
$ErrorActionPreference="Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$env:GGUF_LITE_DATA_DIR = $TargetData
$env:GGUF_LITE_LEGACY_DB = $LegacyDb
$env:GGUF_LITE_LEGACY_VOICES = $LegacyVoices
$env:GGUF_LITE_MIGRATE_APPLY = if ($Apply) {"1"} else {"0"}
Set-Location $Repo
& python -m lite.migrate
exit $LASTEXITCODE
