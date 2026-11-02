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
    if ((Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant() -ne $entries[$file.Name]) {
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
        $info.exe_sha256 -ne (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant() -or
        $info.patch_sha256 -ne (Get-FileHash -Algorithm SHA256 $patch).Hash.ToLowerInvariant() -or
        $info.header_sha256 -ne (Get-FileHash -Algorithm SHA256 $header).Hash.ToLowerInvariant()) {
      throw "Candidate speed receipt or patched EXE SHA invalid"
    }
  }
}
# Windows PowerShell 5.1 and PowerShell 7 both use the same bounded Windows
# process-tree termination. Never call Process.Kill(bool), a .NET 5+ API.
function Stop-SpeedHelpTree([System.Diagnostics.Process]$process, [long]$startTicks) {
  if ($process.HasExited) { return }
  $pidToStop=[int]$process.Id
  # Avoid acting on a recycled PID. Keep the original Process handle alive
  # throughout cleanup and compare the live process creation time.
  try {
    $live=[System.Diagnostics.Process]::GetProcessById($pidToStop)
    try {
      $actualTicks=$live.StartTime.ToUniversalTime().Ticks
    } finally { $live.Dispose() }
  } catch [ArgumentException] {
    if ($process.HasExited) { return }
    throw "NS04: candidate PID $pidToStop disappeared but original handle is still active"
  }
  if ($actualTicks -ne $startTicks) {
    throw "NS04: refused to terminate recycled PID $pidToStop"
  }
  if ($process.HasExited) { return }
  $taskkill=Join-Path ([Environment]::SystemDirectory) "taskkill.exe"
  if (!(Test-Path -LiteralPath $taskkill -PathType Leaf)) {
    throw "NS04: taskkill.exe unavailable; candidate process cleanup unverified"
  }
  # The only dynamic argv token is an integer obtained from a Process handle.
  # Avoid cmd.exe, interpolated shell statements and pipeline invocation.
  $psi=New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName=$taskkill
  $psi.Arguments="/PID $pidToStop /T /F"
  $psi.UseShellExecute=$false
  $psi.CreateNoWindow=$true
  $killer=New-Object System.Diagnostics.Process
  $killer.StartInfo=$psi
  try {
    if (!$killer.Start()) { throw "NS04: taskkill.exe did not start" }
    if (!$killer.WaitForExit(4000)) {
      try { $killer.Kill(); $killer.WaitForExit(1000) | Out-Null } catch {}
      throw "NS04: taskkill.exe exceeded 4-second cleanup bound"
    }
    # A zero exit-code alone is not proof the parent has exited. Conversely
    # taskkill can return nonzero after the target exits naturally in a race.
    $killCode=$killer.ExitCode
    if (!$process.WaitForExit(3000) -or !$process.HasExited) {
      throw "NS04: candidate PID $pidToStop still alive after taskkill (exit=$killCode)"
    }
    if ($killCode -ne 0) {
      # A natural-exit race is acceptable only after verifying the original
      # process handle exited. Do not claim taskkill successfully killed it.
      Write-Verbose "NS04: taskkill returned $killCode; original candidate already exited."
    }
  } finally {
    $killer.Dispose()
  }
}

function Assert-ExecutableSpeedHelp([string]$directory) {
  $exe=Join-Path $directory "omnivoice-tts-windows-x86_64.exe"
  $stdout=[IO.Path]::GetTempFileName()
  $stderr=[IO.Path]::GetTempFileName()
  $proc=$null
  $startTicks=0L
  $problem=$null
  try {
    # Fixed --help argument; Start-Process uses the literal executable path.
    $proc=Start-Process -FilePath $exe -ArgumentList @("--help") -WorkingDirectory $directory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -NoNewWindow -PassThru -ErrorAction Stop
    $startTicks=$proc.StartTime.ToUniversalTime().Ticks
    if (!$proc.WaitForExit(8000)) {
      throw "NS04: Native Speed candidate --help timed out after 8 seconds"
    }
    if ($proc.ExitCode -ne 0) {
      throw "Native Speed candidate --help returned nonzero exit code"
    }
    $outSize=(Get-Item -LiteralPath $stdout).Length
    $errSize=(Get-Item -LiteralPath $stderr).Length
    if ($outSize+$errSize -gt 32768) {
      throw "Native Speed candidate help output exceeded 32 KiB"
    }
    $helpText=(Get-Content -Raw -LiteralPath $stdout)+[Environment]::NewLine+(Get-Content -Raw -LiteralPath $stderr)
    if (!$helpText.Contains("LiteNativeSpeed/v1") -or !$helpText.Contains("--speed")) {
      throw "Native Speed candidate help capability marker/option missing"
    }
  } catch {
    $problem=$_.Exception.Message
  } finally {
    if ($null -ne $proc) {
      try {
        if (!$proc.HasExited) {
          Stop-SpeedHelpTree $proc $startTicks
        }
        if (!$proc.HasExited) { throw "NS04: original candidate process is still running" }
      } catch {
        $cleanupError=$_.Exception.Message
        $problem= if ($problem) { "$problem; PROCESS CLEANUP FAILED: $cleanupError" } else { "PROCESS CLEANUP FAILED: $cleanupError" }
      } finally { $proc.Dispose() }
    }
    # These are owned temp files. A failure to delete is reported and cannot
    # turn into an apparent successful executable validation.
    foreach ($tempFile in @($stdout,$stderr)) {
      try {
        if (Test-Path -LiteralPath $tempFile) {
          Remove-Item -LiteralPath $tempFile -Force -ErrorAction Stop
        }
      } catch {
        $tempError="NS04: temporary capture cleanup failed: $($_.Exception.Message)"
        $problem=if ($problem) { "$problem; $tempError" } else { $tempError }
      }
    }
  }
  if ($problem) { throw $problem }
}

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
