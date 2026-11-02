param(
  [string]$Root="D:\VoiceStudioBuild\lite",
  [string]$GitBash="bash"
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
$target=Join-Path $Root "runtime"
$stageScripts=Join-Path $stage "scripts\internal"
New-Item -ItemType Directory -Force -Path $stageScripts | Out-Null
foreach ($helper in @("build-gguf-lite.sh","omnivoice-vulkan.sh","instrument-attestation.py")) {
  $from=Join-Path $source ("scripts\internal\" + $helper)
  if (!(Test-Path -PathType Leaf $from)) { throw "Lite build helper missing: $helper" }
  Copy-Item $from $stageScripts
}
$previousBuildPython=[Environment]::GetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON","Process")
Push-Location $stage
try {
  # Git Bash converts this Windows path via cygpath before invoking Python.
  [Environment]::SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$pythonExe,"Process")
  & $GitBash scripts/internal/build-gguf-lite.sh --platform windows-x86_64 --commit-sha $version
  if ($LASTEXITCODE -ne 0) { throw "Pinned Vulkan native build failed; no CPU fallback" }
  $bin=Join-Path $stage "bin"
  $exe=Join-Path $bin "omnivoice-tts-windows-x86_64.exe"
  $dll=Join-Path $bin "ggml-vulkan.dll"
  $manifest=Join-Path $bin "checksums.sha256"
  foreach ($required in @($exe,$dll,$manifest,(Join-Path $bin "ggml.dll"),(Join-Path $bin "ggml-base.dll"))) {
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
  $targetBin=Join-Path $target "bin"
  if (Test-Path $targetBin) {
    throw "A Lite native runtime exists; back it up separately before rebuilding"
  }
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  Move-Item $bin $targetBin
  Write-Host "Pinned Vulkan-only runtime installed to $targetBin"
} finally {
  [Environment]::SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$previousBuildPython,"Process")
  Pop-Location
  if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
}
