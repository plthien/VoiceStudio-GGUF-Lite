param(
  [Parameter(Mandatory=$true)][string]$VoiceId,
  [ValidateSet("warm","cold")][string]$Policy="warm",
  [int]$Repetitions=3,
  [int]$Port=3900,
  [string]$Root="D:\VoiceStudioBuild\lite",
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks"
)
$ErrorActionPreference="Stop"
if ($Repetitions -ne 3) { throw "Ink benchmark is frozen at exactly 3 runs" }
if ($Port -lt 1024 -or $Port -gt 65535) { throw "Invalid localhost port" }
$python=Join-Path $Root ".venv-lite\Scripts\python.exe"
if (!(Test-Path -PathType Leaf $python)) { throw "Install Lite Python 3.11 venv first" }
$Repo=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$env:GGUF_LITE_DATA_DIR=Join-Path $Root "data"
$env:GGUF_LITE_BIN_DIR=Join-Path $Root "runtime\bin"
$env:OMNIVOICE_CACHE_DIR="D:\VoiceStudioBuild\models"
Push-Location $Repo
try {
  & $python -m lite.benchmark --voice-id $VoiceId --output $Output --port $Port --repetitions $Repetitions --policy $Policy
  if ($LASTEXITCODE -ne 0) { throw "Frozen 3-run benchmark failed: inspect private output/benchmark.json" }
} finally {
  Pop-Location
}
