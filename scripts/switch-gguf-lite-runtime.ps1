param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("Activate","Rollback")][string]$Mode,
  [string]$CandidateBin="",
  [string]$ActiveBin="D:\VoiceStudioBuild\lite\runtime\bin",
  [string]$BackupBin="",
  [switch]$OwnerApproves
)
# Owner-controlled, same-volume, integrity-gated native switch; no data deletion.
$ErrorActionPreference="Stop"
if (!$OwnerApproves) { throw "Explicit -OwnerApproves required; no unattended native switch" }
$active=[IO.Path]::GetFullPath($ActiveBin)
if (!$BackupBin) { throw "An unused owner BackupBin path is required" }
$backup=[IO.Path]::GetFullPath($BackupBin)
if ($backup -ieq $active) { throw "Backup and active bins must differ" }
if ([IO.Path]::GetPathRoot($backup) -ine [IO.Path]::GetPathRoot($active)) {
  throw "Backup and active must be on the same filesystem volume"
}
if ($Mode -eq "Activate") {
  if (!$CandidateBin) { throw "CandidateBin required" }
  $source=[IO.Path]::GetFullPath($CandidateBin)
  if ($source -ieq $backup -or $source -ieq $active) {
    throw "Candidate cannot be active bin or backup"
  }
  if ([IO.Path]::GetPathRoot($source) -ine [IO.Path]::GetPathRoot($active)) {
    throw "Candidate must be on the active filesystem volume"
  }
  if (!(Test-Path -LiteralPath $source -PathType Container) -or
      !(Test-Path -LiteralPath $active -PathType Container) -or
      (Test-Path -LiteralPath $backup)) {
    throw "Candidate/active bin missing, or backup destination already exists"
  }
} else {
  if (!(Test-Path -LiteralPath $backup -PathType Container)) {
    throw "Original backup bin does not exist"
  }
  if (Test-Path -LiteralPath $active) {
    throw "Rollback refuses to overwrite active bin; quarantine the candidate first"
  }
}
function Get-NativeSha256([string]$file) {
  # .NET Framework 4.8 / .NET: independent of PowerShell module discovery.
  $sha=[System.Security.Cryptography.SHA256]::Create()
  $stream=[System.IO.File]::OpenRead($file)
  try {
    return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-","").ToLowerInvariant()
  } finally {
    $stream.Dispose()
    $sha.Dispose()
  }
}
function Assert-NativeManifest([string]$directory,[bool]$requireSpeed) {
  $manifest=Join-Path $directory "checksums.sha256"
  if (!(Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "Native checksums manifest missing"
  }
  $entries=@{}
  foreach ($line in (Get-Content -LiteralPath $manifest)) {
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?([^\\/]+)$') {
      throw "Malformed checksum manifest entry"
    }
    $hash=$Matches[1].ToLowerInvariant()
    $name=$Matches[2]
    if ($entries.ContainsKey($name)) { throw "Duplicate native manifest entry" }
    $entries[$name]=$hash
  }
  foreach ($name in @("omnivoice-tts-windows-x86_64.exe","ggml-vulkan.dll","ggml.dll","ggml-base.dll")) {
    if (!(Test-Path -LiteralPath (Join-Path $directory $name) -PathType Leaf)) {
      throw "Native component missing: $name"
    }
  }
  $binaries=Get-ChildItem -LiteralPath $directory -File |
    Where-Object { $_.Extension -in @('.exe','.dll') }
  foreach ($file in $binaries) {
    if (!$entries.ContainsKey($file.Name)) { throw "Unmanifested native binary $($file.Name)" }
    if ($file.Length -lt 64) { throw "Truncated PE executable or DLL" }
    $stream=[IO.File]::OpenRead($file.FullName)
    try {
      if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90) {
        throw "Native component is not PE: $($file.Name)"
      }
    } finally { $stream.Dispose() }
    if ((Get-NativeSha256 $file.FullName) -ne $entries[$file.Name]) {
      throw "Native file checksum mismatch: $($file.Name)"
    }
  }
  if ($requireSpeed) {
    $receipt=Join-Path $directory "speed-capability.json"
    if (!(Test-Path -LiteralPath $receipt -PathType Leaf)) {
      throw "Lite native speed capability receipt missing"
    }
    $info=Get-Content -Raw -LiteralPath $receipt | ConvertFrom-Json
    $repo=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $exe=Join-Path $directory "omnivoice-tts-windows-x86_64.exe"
    $patch=Join-Path $repo "scripts\internal\instrument-native-speed.py"
    $header=Join-Path $repo "scripts\internal\lite-native-speed.h"
    if ($info.schema -ne "lite-native-speed-v1" -or
        $info.runtime_revision -ne "886fc079838ca7400cb2b42b36e2a65aa1daabe8" -or
        $info.exe_sha256 -ne (Get-NativeSha256 $exe) -or
        $info.patch_sha256 -ne (Get-NativeSha256 $patch) -or
        $info.header_sha256 -ne (Get-NativeSha256 $header)) {
      throw "Candidate speed receipt or patched EXE SHA invalid"
    }
    $persistentReceipt=Join-Path $directory "persistent-capability.json"
    if (!(Test-Path -LiteralPath $persistentReceipt -PathType Leaf)) {
      throw "Lite persistent capability receipt missing"
    }
    $persistent=Get-Content -Raw -LiteralPath $persistentReceipt | ConvertFrom-Json
    $persistentPatch=Join-Path $repo "scripts\internal\stage-persistent-worker.py"
    $persistentSource=Join-Path $repo "scripts\internal\lite-persistent-worker.cpp"
    if ($persistent.schema -ne "lite-persistent-v1" -or
        $persistent.marker -ne "LitePersistent/v1" -or
        $persistent.protocol_version -ne 1 -or
        $persistent.runtime_revision -ne "886fc079838ca7400cb2b42b36e2a65aa1daabe8" -or
        $persistent.exe_sha256 -ne (Get-NativeSha256 $exe) -or
        $persistent.patch_sha256 -ne (Get-NativeSha256 $persistentPatch) -or
        $persistent.source_sha256 -ne (Get-NativeSha256 $persistentSource)) {
      throw "Candidate persistent receipt or patched EXE SHA invalid"
    }
  }
}
. (Join-Path $PSScriptRoot "internal/speed-help-process.ps1")

# Do not swap while the HTTP server is accepting requests.
$socket=[Net.Sockets.TcpClient]::new()
try {
  $pending=$socket.BeginConnect("127.0.0.1",3900,$null,$null)
  if ($pending.AsyncWaitHandle.WaitOne(500)) {
    try {
      $socket.EndConnect($pending)
      throw "Lite service is still running; shut it down before switching"
    } catch [Net.Sockets.SocketException] {}
  }
} finally { $socket.Dispose() }
if ($Mode -eq "Activate") {
  Assert-NativeManifest $source $true
  Assert-NativeManifest $active $false
  Assert-ExecutableSpeedHelp $source
  $parent=Split-Path -Parent $backup
  if (!(Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  Move-Item -LiteralPath $active -Destination $backup -ErrorAction Stop
  try {
    Move-Item -LiteralPath $source -Destination $active -ErrorAction Stop
    Write-Host "Candidate active; pristine native bin preserved at $backup"
  } catch {
    Move-Item -LiteralPath $backup -Destination $active -ErrorAction Stop
    throw
  }
} else {
  Assert-NativeManifest $backup $false
  Move-Item -LiteralPath $backup -Destination $active -ErrorAction Stop
  Write-Host "Original native bin restored; voice/model data unchanged"
}
