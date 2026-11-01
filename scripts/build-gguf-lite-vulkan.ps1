param(
  [string]$Root="D:\VoiceStudioBuild\lite",
  [string]$GitBash="bash"
)
# Build into a fresh Lite-owned tree, never overwriting the benchmarked
# VoiceStudio fork bin/ artifacts or its existing model cache.
$ErrorActionPreference="Stop"
$source = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = "886fc079838ca7400cb2b42b36e2a65aa1daabe8"
$stage = Join-Path $Root ("native-build-" + [guid]::NewGuid().ToString("N"))
$target = Join-Path $Root "runtime"
New-Item -ItemType Directory -Force -Path (Join-Path $stage "scripts/lib") | Out-Null
Copy-Item (Join-Path $source "scripts/build-omnivoice-tts.sh") (Join-Path $stage "scripts")
Copy-Item (Join-Path $source "scripts/lib/omnivoice-vulkan.sh") (Join-Path $stage "scripts/lib")
Push-Location $stage
try {
  & $GitBash scripts/build-omnivoice-tts.sh --platform windows-x86_64 --commit-sha $version
  if ($LASTEXITCODE -ne 0) { throw "Pinned native build failed" }
  $bin = Join-Path $stage "bin"
  $exe = Join-Path $bin "omnivoice-tts-windows-x86_64.exe"
  $dll = Join-Path $bin "ggml-vulkan.dll"
  $manifest = Join-Path $bin "checksums.sha256"
  foreach ($file in @($exe,$dll,$manifest)) {
    if (!(Test-Path -PathType Leaf $file)) { throw "Vulkan build did not produce $file; CPU fallback is forbidden" }
  }
  foreach ($file in @($exe,$dll)) {
    $bytes = [IO.File]::ReadAllBytes($file)
    if ($bytes.Length -lt 64 -or $bytes[0] -ne 77 -or $bytes[1] -ne 90) {
      throw "Native build emitted an invalid Windows PE artifact"
    }
    $name = Split-Path $file -Leaf
    $digest = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant()
    $lines = Get-Content $manifest
    if (!($lines | Where-Object { $_.ToLowerInvariant().StartsWith($digest) -and $_.Contains($name) })) {
      throw "Native SHA256 manifest lacks a valid entry for $name"
    }
  }
  $targetBin = Join-Path $target "bin"
  # Prevent replacement of previously installed known-good Lite artifacts.
  if (Test-Path $targetBin) { throw "Target runtime already installed; back it up separately before rebuilding" }
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  Move-Item $bin $targetBin
  Write-Host "Pinned Vulkan-only runtime installed to $targetBin"
} finally {
  Pop-Location
  if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
}
