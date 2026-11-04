param([Parameter(Mandatory=$true)][string]$CandidateBin)
# Read-only candidate verification. This script never activates or moves a runtime.
$ErrorActionPreference="Stop"
$bin=[IO.Path]::GetFullPath($CandidateBin)
$receipt=Join-Path $bin "performance-candidate.json"
$manifest=Join-Path $bin "checksums.sha256"
$exe=Join-Path $bin "omnivoice-tts-windows-x86_64.exe"
foreach($path in @($receipt,$manifest,$exe)){if(!(Test-Path -LiteralPath $path -PathType Leaf)){throw "Candidate artifact missing: $path"}}
$info=Get-Content -Raw -LiteralPath $receipt|ConvertFrom-Json
if($info.schema -ne "lite-performance-candidate-v1" -or $info.tag -ne "q8-vulkan-perf-opt-in" -or
   $info.candidate_flavor -notin @("profiled","ref-cache","steps-32","steps-24","steps-16","steps-12","steps-8","fa-off") -or
   $info.baseline_quant -ne "Q8_0" -or $info.baseline_backend -ne "Vulkan0" -or
   $info.default_ref_cache -ne $false -or $info.default_maskgit_steps -ne 32 -or
   $info.graph_reuse -ne "GRAPH_REUSE_DEFERRED" -or $info.cuda_status -ne "CUDA_NOT_AVAILABLE" -or
   $info.q4_status -ne "NOT_AVAILABLE") {throw "Candidate provenance/defaults invalid"}
$sha=(Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
if($sha -ne $info.exe_sha256){throw "Candidate executable does not match receipt"}
$repo=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sources=@{
  perf_patch_sha256="scripts\internal\instrument-native-performance.py";perf_header_sha256="scripts\internal\lite-native-perf.h";
  cache_patch_sha256="scripts\internal\instrument-ref-conditioning-cache.py";cache_header_sha256="scripts\internal\lite-reference-cache.h";
  maskgit_patch_sha256="scripts\internal\instrument-maskgit-experiments.py";maskgit_header_sha256="scripts\internal\lite-maskgit-experiments.h"
}
foreach($field in $sources.Keys){$actual=(Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repo $sources[$field])).Hash.ToLowerInvariant();if($info.$field -ne $actual){throw "Candidate source receipt mismatch: $field"}}
$line=Get-Content -LiteralPath $manifest|Where-Object{$_ -match 'omnivoice-tts-windows-x86_64\.exe$'}
if(@($line).Count -ne 1 -or $line -notmatch '^([0-9a-fA-F]{64})\s+' -or $Matches[1].ToLowerInvariant() -ne $sha){throw "Candidate manifest mismatch"}
Write-Output "PERFORMANCE_CANDIDATE_PASS tag=q8-vulkan-perf-opt-in activation=OWNER_ONLY"
