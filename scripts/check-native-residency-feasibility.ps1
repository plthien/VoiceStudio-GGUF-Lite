param(
  [Parameter(Mandatory=$true)][string]$CandidateBin,
  [Parameter(Mandatory=$true)][string]$BaseGguf,
  [Parameter(Mandatory=$true)][string]$TokenizerGguf,
  [Parameter(Mandatory=$true)][string]$ReferenceAWav,
  [Parameter(Mandatory=$true)][string]$TranscriptAFile,
  [Parameter(Mandatory=$true)][string]$ReferenceBWav,
  [Parameter(Mandatory=$true)][string]$TranscriptBFile,
  [string]$EvidenceDir="D:\VoiceStudioBuild\lite\benchmarks\residency-feasibility",
  [ValidateRange(0,2147483647)][int]$Seed=42,
  [ValidateRange(0.75,1.25)][double]$Speed=1.0
)
# Owner only. Does not start/stop Lite or modify installed native/model/voice files.
$ErrorActionPreference="Stop"
function SHA256([string]$path) {
  $hash=[Security.Cryptography.SHA256]::Create()
  $stream=[IO.File]::OpenRead($path)
  try { [BitConverter]::ToString($hash.ComputeHash($stream)).Replace("-","").ToLowerInvariant() }
  finally { $stream.Dispose(); $hash.Dispose() }
}
foreach($f in @($BaseGguf,$TokenizerGguf,$ReferenceAWav,$ReferenceBWav,$TranscriptAFile,$TranscriptBFile)) {
  if (!(Test-Path -LiteralPath $f -PathType Leaf)) { throw "Model/reference/transcript input unavailable" }
}
$repo=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pins=Get-Content -Raw (Join-Path $repo "lite\resources\q8_pins.json") | ConvertFrom-Json
if ([IO.Path]::GetFileName($BaseGguf) -cne $pins.base.name -or
    [IO.Path]::GetFileName($TokenizerGguf) -cne $pins.tokenizer.name -or
    $pins.model_revision -ne "361609388ae572a820d085185bbbe2a2aac4b30e" -or
    $pins.runtime_revision -ne "886fc079838ca7400cb2b42b36e2a65aa1daabe8") {
  throw "Wrong pinned Q8_0 model metadata"
}
if ((SHA256 $BaseGguf) -ne $pins.base.sha256 -or (SHA256 $TokenizerGguf) -ne $pins.tokenizer.sha256) {
  throw "Pinned Q8_0 hash mismatch"
}
$bin=[IO.Path]::GetFullPath($CandidateBin)
$exe=Join-Path $bin "lite-reuse-probe.exe"
$manifest=Join-Path $bin "checksums.sha256"
foreach($path in @($exe,$manifest,(Join-Path $bin "ggml-vulkan.dll"))) {
  if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing opt-in isolated feasibility candidate" }
}
$record=@{}
foreach($line in (Get-Content -LiteralPath $manifest)) {
  if ($line -notmatch '^([a-fA-F0-9]{64})\s+\*?([^\\/]+)$') { throw "Malformed checksum manifest" }
  if ($record.ContainsKey($Matches[2])) { throw "Duplicate checksum entry" }
  $record[$Matches[2]]=$Matches[1].ToLowerInvariant()
}
foreach($file in @($exe,(Join-Path $bin "ggml-vulkan.dll"))) {
  $name=[IO.Path]::GetFileName($file)
  if (!$record.ContainsKey($name) -or $record[$name] -ne (SHA256 $file)) {
    throw "Untrusted opt-in feasibility candidate"
  }
}
if ((SHA256 $ReferenceAWav) -eq (SHA256 $ReferenceBWav)) {
  throw "Reference A and B must differ to test voice isolation"
}
$ffmpeg=(Get-Command ffmpeg -ErrorAction Stop).Source
$private=Join-Path $EvidenceDir ("private-" + [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force -Path $private | Out-Null
$rawA=Join-Path $private "A.s16le"
$rawB=Join-Path $private "B.s16le"
$stdout=Join-Path $private "stdout.private.log"
$stderr=Join-Path $private "stderr.private.log"
$oldBackend=[Environment]::GetEnvironmentVariable("OMNIVOICE_GGUF_BACKEND","Process")
$oldDevice=[Environment]::GetEnvironmentVariable("OMNIVOICE_GGUF_DEVICE","Process")
$proc=$null
try {
  & $ffmpeg -nostdin -hide_banner -loglevel error -y -i $ReferenceAWav -vn -ac 1 -ar 24000 -c:a pcm_s16le -f s16le $rawA
  if ($LASTEXITCODE -ne 0) { throw "Failed to normalize reference A" }
  & $ffmpeg -nostdin -hide_banner -loglevel error -y -i $ReferenceBWav -vn -ac 1 -ar 24000 -c:a pcm_s16le -f s16le $rawB
  if ($LASTEXITCODE -ne 0) { throw "Failed to normalize reference B" }
  [Environment]::SetEnvironmentVariable("OMNIVOICE_GGUF_BACKEND","vulkan","Process")
  [Environment]::SetEnvironmentVariable("OMNIVOICE_GGUF_DEVICE","0","Process")
  $args=@($BaseGguf,$TokenizerGguf,$rawA,$TranscriptAFile,$rawB,$TranscriptBFile,
          [string]$Seed,$Speed.ToString("G9",[cultureinfo]::InvariantCulture))
  $quote=@()
  foreach($arg in $args) {
    if ($arg.Contains('"')) { throw "Unsupported quote in path" }
    $quote+=('"' + $arg + '"')
  }
  # Explicit EXE path and fixed ordered argv. No cmd.exe or shell evaluation.
  $proc=Start-Process -FilePath $exe -ArgumentList $quote -WorkingDirectory $bin -NoNewWindow -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr -ErrorAction Stop
  if (!$proc.WaitForExit(900000)) {
    $processId=[int]$proc.Id
    & ([IO.Path]::Combine([Environment]::SystemDirectory,"taskkill.exe")) /PID $processId /T /F | Out-Null
    if (!$proc.WaitForExit(5000)) { throw "Probe timeout: process termination unverified" }
    throw "Probe exceeded 15-minute deadline"
  }
  $proc.Refresh()
  if ($proc.ExitCode -ne 0) { throw "Native repeated synthesis failed; inspect private logs" }
  $text=Get-Content -Raw -LiteralPath $stdout
  $evidence=Get-Content -Raw -LiteralPath $stderr
  if (!$text.Contains("LITE_NATIVE_CONTEXT_REUSE_FEASIBILITY_PASS init_count=1 synth_count=3 free_count=1")) {
    throw "Native repeated-synthesis PASS marker missing"
  }
  $device=[regex]::Matches($evidence,'(?m)^\[LiteAttest/v1\] backend=Vulkan0; device=Vulkan0; description=NVIDIA[^\r\n]*GTX 1650[^\r\n]*$')
  if ($device.Count -ne 1) { throw "Real native NVIDIA GTX1650 Vulkan0 proof missing or duplicated" }
  Write-Host "PASS native reuse: same context, A-B-A, one ov_init, one ov_free, GTX1650 Vulkan0"
  Write-Host "PRIVATE evidence folder: $private"
  Write-Host "NOT a production resident-worker acceptance or VRAM stability result"
}
finally {
  [Environment]::SetEnvironmentVariable("OMNIVOICE_GGUF_BACKEND",$oldBackend,"Process")
  [Environment]::SetEnvironmentVariable("OMNIVOICE_GGUF_DEVICE",$oldDevice,"Process")
  if ($null -ne $proc) {
    if (!$proc.HasExited) {
      $processId=[int]$proc.Id
      try {
        & ([IO.Path]::Combine([Environment]::SystemDirectory,"taskkill.exe")) /PID $processId /T /F | Out-Null
        if (!$proc.WaitForExit(5000)) { Write-Warning "Orphan probe PID: $processId" }
      } catch { Write-Warning "Probe cleanup not verified: $($_.Exception.Message)" }
    }
    $proc.Dispose()
  }
  foreach($raw in @($rawA,$rawB)) {
    if (Test-Path -LiteralPath $raw) { Remove-Item -LiteralPath $raw -Force -ErrorAction Stop }
  }
}
