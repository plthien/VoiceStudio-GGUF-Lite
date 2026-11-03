param(
  [string]$Root="D:\VoiceStudioBuild\lite",
  [string]$GitBash="bash",
  [string]$OutputRuntime="",
  [ValidateSet("profiled","ref-cache","steps-32","steps-24","steps-16","steps-12","steps-8","fa-off")][string]$CandidateFlavor="profiled",
  [switch]$BuildFeasibilityProbe
)
# Lite-only, pinned Windows x64 Vulkan source build. Never overwrite the baseline.
$ErrorActionPreference="Stop"
# Require the Lite-owned Python before any clone, rather than relying on
# a generic Python executable in the Git Bash PATH.
$pythonExe=Join-Path $Root ".venv-lite\Scripts\python.exe"
if (!(Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
  throw "Lite Python 3.11 not found at $pythonExe. Run .\scripts\install-gguf-lite.ps1 -Root '$Root' first."
}
& $pythonExe -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 2)'
if ($LASTEXITCODE -ne 0) { throw "Lite build requires the installed Python 3.11 interpreter: $pythonExe" }
# WSL bash is not supported: the build requires Git Bash and cygpath.
& $GitBash -c 'command -v cygpath >/dev/null 2>&1'
if ($LASTEXITCODE -ne 0) { throw "Git Bash with cygpath is required. Pass -GitBash 'C:\Program Files\Git\bin\bash.exe'." }
$source=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
$stage=Join-Path $Root ("native-build-" + [guid]::NewGuid().ToString("N"))
$target=if ($OutputRuntime) { [IO.Path]::GetFullPath($OutputRuntime) } else {
  Join-Path $Root ("candidates\performance-q8-vulkan-" + $CandidateFlavor + "-" + [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8))
}
$installed=[IO.Path]::GetFullPath((Join-Path $Root "runtime"))
if ($OutputRuntime -and $target.TrimEnd('\') -ieq $installed.TrimEnd('\')) {
  throw "Candidate must not target installed runtime; omit -OutputRuntime for default no-overwrite path"
}
if ($OutputRuntime -and (Test-Path -LiteralPath $target)) {
  throw "Candidate runtime directory already exists; choose a fresh empty destination"
}
$stageScripts=Join-Path $stage "scripts\internal"
New-Item -ItemType Directory -Force -Path $stageScripts | Out-Null
foreach ($helper in @("build-gguf-lite.sh","omnivoice-vulkan.sh","instrument-attestation.py","instrument-native-speed.py","lite-native-speed.h","instrument-native-performance.py","lite-native-perf.h","instrument-ref-conditioning-cache.py","lite-reference-cache.h","instrument-maskgit-experiments.py","lite-maskgit-experiments.h","stage-residency-feasibility.py","native-residency-feasibility.cpp","stage-persistent-worker.py","lite-persistent-worker.cpp","verify-persistent-handshake.py")) {
  $from=Join-Path $source ("scripts\internal\" + $helper)
  if (!(Test-Path -PathType Leaf $from)) { throw "Lite build helper missing: $helper" }
  Copy-Item $from $stageScripts
}
$previousBuildPython=[Environment]::GetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON","Process")
$previousProbe=[Environment]::GetEnvironmentVariable("OMNIVOICE_RESIDENCY_FEASIBILITY","Process")
$previousFlavor=[Environment]::GetEnvironmentVariable("GGUF_LITE_CANDIDATE_FLAVOR","Process")
Push-Location $stage
try {
  # Git Bash converts this Windows path via cygpath before invoking Python.
  [Environment]::SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$pythonExe,"Process")
  [Environment]::SetEnvironmentVariable("GGUF_LITE_CANDIDATE_FLAVOR",$CandidateFlavor,"Process")
  if ($BuildFeasibilityProbe) {
    [Environment]::SetEnvironmentVariable("OMNIVOICE_RESIDENCY_FEASIBILITY","1","Process")
  } else {
    [Environment]::SetEnvironmentVariable("OMNIVOICE_RESIDENCY_FEASIBILITY",$null,"Process")
  }
  & $GitBash scripts/internal/build-gguf-lite.sh --platform windows-x86_64 --commit-sha $version
  if ($LASTEXITCODE -ne 0) { throw "Pinned Vulkan native build failed; no CPU fallback" }
  $bin=Join-Path $stage "bin"
  $exe=Join-Path $bin "omnivoice-tts-windows-x86_64.exe"
  $dll=Join-Path $bin "ggml-vulkan.dll"
  $manifest=Join-Path $bin "checksums.sha256"
  $receipt=Join-Path $bin "speed-capability.json"
  $persistentReceipt=Join-Path $bin "persistent-capability.json"
  $performanceReceipt=Join-Path $bin "performance-candidate.json"
  foreach ($required in @($exe,$dll,$manifest,$receipt,$persistentReceipt,$performanceReceipt,(Join-Path $bin "ggml.dll"),(Join-Path $bin "ggml-base.dll"))) {
    if (!(Test-Path -PathType Leaf $required)) { throw "Required Vulkan build artifact missing: $required" }
  }
  $records=@{}
  foreach ($line in (Get-Content $manifest)) {
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?([^\\/]+)$') {
      throw "Malformed native checksum manifest line"
    }
    $name=$Matches[2]
    if ($records.ContainsKey($name)) { throw "Duplicate native manifest entry: $name" }
    $records[$name]=$Matches[1].ToLowerInvariant()
  }
  $artifacts=@(Get-ChildItem $bin -File | Where-Object { $_.Extension -in @(".exe",".dll") })
  if ($artifacts.Count -lt 4 -or $records.Count -lt $artifacts.Count) {
    throw "Native manifest missing EXE/DLL or GGML dependencies"
  }
  foreach ($file in $artifacts) {
    $bytes=[IO.File]::ReadAllBytes($file.FullName)
    if ($bytes.Length -lt 64 -or $bytes[0] -ne 77 -or $bytes[1] -ne 90) {
      throw "Invalid PE executable or library: $($file.Name)"
    }
    $digest=(Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant()
    if (!$records.ContainsKey($file.Name) -or $records[$file.Name] -ne $digest) {
      throw "Native SHA256 mismatch: $($file.Name)"
    }
  }
  $speedReceipt=Get-Content -Raw $receipt | ConvertFrom-Json
  $actualExe=(Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
  if ($speedReceipt.schema -ne "lite-native-speed-v1" -or
      $speedReceipt.exe_sha256 -ne $actualExe -or
      $speedReceipt.runtime_revision -ne $version -or
      $speedReceipt.patch_sha256 -ne (Get-FileHash -Algorithm SHA256 (Join-Path $source "scripts/internal/instrument-native-speed.py")).Hash.ToLowerInvariant() -or
      $speedReceipt.header_sha256 -ne (Get-FileHash -Algorithm SHA256 (Join-Path $source "scripts/internal/lite-native-speed.h")).Hash.ToLowerInvariant()) {
      throw "Native Speed candidate receipt does not match pinned patched binary"
  }
  $persistent=Get-Content -Raw $persistentReceipt | ConvertFrom-Json
  if ($persistent.schema -ne "lite-persistent-v1" -or $persistent.marker -ne "LitePersistent/v1" -or
      $persistent.protocol_version -ne 1 -or $persistent.exe_sha256 -ne $actualExe -or
      $persistent.runtime_revision -ne $version -or
      $persistent.patch_sha256 -ne (Get-FileHash -Algorithm SHA256 (Join-Path $source "scripts/internal/stage-persistent-worker.py")).Hash.ToLowerInvariant() -or
      $persistent.source_sha256 -ne (Get-FileHash -Algorithm SHA256 (Join-Path $source "scripts/internal/lite-persistent-worker.cpp")).Hash.ToLowerInvariant()) {
    throw "Persistent worker candidate receipt does not match pinned binary"
  }
  $performance=Get-Content -Raw $performanceReceipt | ConvertFrom-Json
  if ($performance.schema -ne "lite-performance-candidate-v1" -or
      $performance.tag -ne "q8-vulkan-perf-opt-in" -or $performance.candidate_flavor -ne $CandidateFlavor -or $performance.exe_sha256 -ne $actualExe -or
      $performance.baseline_quant -ne "Q8_0" -or $performance.baseline_backend -ne "Vulkan0" -or
      $performance.default_ref_cache -ne $false -or $performance.default_maskgit_steps -ne 32 -or
      $performance.graph_reuse -ne "GRAPH_REUSE_DEFERRED") {
    throw "Performance candidate receipt is invalid"
  }
  foreach($check in @(
    @("perf_patch_sha256","instrument-native-performance.py"),@("perf_header_sha256","lite-native-perf.h"),
    @("cache_patch_sha256","instrument-ref-conditioning-cache.py"),@("cache_header_sha256","lite-reference-cache.h"),
    @("maskgit_patch_sha256","instrument-maskgit-experiments.py"),@("maskgit_header_sha256","lite-maskgit-experiments.h"))) {
    if($performance.($check[0]) -ne (Get-FileHash -Algorithm SHA256 (Join-Path $source ("scripts/internal/"+$check[1]))).Hash.ToLowerInvariant()) {throw "Performance candidate source receipt mismatch: $($check[0])"}
  }
  & $pythonExe (Join-Path $source "scripts/internal/verify-persistent-handshake.py") $exe
  if ($LASTEXITCODE -ne 0) { throw "Persistent worker pre-activation handshake failed" }
  if ($BuildFeasibilityProbe -and !(Test-Path -LiteralPath (Join-Path $bin "lite-reuse-probe.exe") -PathType Leaf)) {
    throw "Opt-in native A-B-A feasibility probe is missing"
  }
  $helpText=(& $exe --help 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0 -or !$helpText.Contains("LiteNativeSpeed/v1") -or !$helpText.Contains("LitePersistent/v1")) {
      throw "Compiled executable lacks required Lite native CLI help markers"
  }
  $targetBin=Join-Path $target "bin"
  if (Test-Path $targetBin) {
    throw "A Lite native runtime exists; back it up separately before rebuilding"
  }
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  Move-Item $bin $targetBin
  Write-Host "Pinned Vulkan-only runtime installed to $targetBin"
} finally {
  [Environment]::SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$previousBuildPython,"Process")
  [Environment]::SetEnvironmentVariable("OMNIVOICE_RESIDENCY_FEASIBILITY",$previousProbe,"Process")
  [Environment]::SetEnvironmentVariable("GGUF_LITE_CANDIDATE_FLAVOR",$previousFlavor,"Process")
  Pop-Location
  if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
}
