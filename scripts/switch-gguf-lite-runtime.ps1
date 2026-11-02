param(
  [ValidateSet("Activate","Rollback")][string]$Mode,
  [string]$CandidateBin="",
  [string]$ActiveBin="D:\VoiceStudioBuild\lite\runtime\bin",
  [string]$BackupBin="",
  [switch]$OwnerApproves
)
# Owner-only, explicit, non-destructive runtime directory switching.
# Never deletes/migrates data, cache, models, licence receipts or private WAV.
$ErrorActionPreference="Stop"
if (!$OwnerApproves) { throw "Explicit -OwnerApproves required; no automatic activation or rollback" }
$active=[IO.Path]::GetFullPath($ActiveBin)
if ($Mode -eq "Activate") {
  if (!$CandidateBin -or !$BackupBin) { throw "CandidateBin and BackupBin are required" }
  $source=[IO.Path]::GetFullPath($CandidateBin)
  $backup=[IO.Path]::GetFullPath($BackupBin)
  if ($source -ieq $active -or $backup -ieq $active -or $backup -ieq $source) {
    throw "Candidate, active, and backup paths must differ"
  }
  if (!(Test-Path -LiteralPath $source -PathType Container)) { throw "Candidate missing" }
  if (!(Test-Path -LiteralPath $active -PathType Container)) { throw "Active baseline missing" }
  if (Test-Path -LiteralPath $backup) { throw "Backup destination already exists" }
  if ([IO.Path]::GetPathRoot($source) -ine [IO.Path]::GetPathRoot($active) -or
      [IO.Path]::GetPathRoot($backup) -ine [IO.Path]::GetPathRoot($active)) {
    throw "Require same filesystem root for directory moves; no copy/delete fallback"
  }
  foreach ($p in @("omnivoice-tts-windows-x86_64.exe","ggml-vulkan.dll","checksums.sha256","speed-capability.json")) {
    if (!(Test-Path (Join-Path $source $p))) { throw "Candidate missing $p" }
  }
} else {
  if (!$BackupBin) { throw "BackupBin required for explicit rollback" }
  $backup=[IO.Path]::GetFullPath($BackupBin)
  if (!(Test-Path -LiteralPath $backup -PathType Container)) { throw "Original backup missing" }
  if ([IO.Path]::GetPathRoot($backup) -ine [IO.Path]::GetPathRoot($active)) {
    throw "Backup and active must be on same volume"
  }
}
# Refuse to touch either directory if the server still listens on localhost.
$socket=[Net.Sockets.TcpClient]::new()
try {
  $pending=$socket.BeginConnect("127.0.0.1",3900,$null,$null)
  if ($pending.AsyncWaitHandle.WaitOne(500)) {
    try { $socket.EndConnect($pending); throw "Lite server still running; stop it first" }
    catch [Net.Sockets.SocketException] {}
  }
} finally { $socket.Dispose() }
if ($Mode -eq "Activate") {
  Move-Item -LiteralPath $active -Destination $backup -ErrorAction Stop
  try {
    Move-Item -LiteralPath $source -Destination $active -ErrorAction Stop
    Write-Host "Activated candidate. Baseline preserved at $backup"
  } catch {
    Move-Item -LiteralPath $backup -Destination $active -ErrorAction Stop
    throw
  }
} else {
  if (Test-Path -LiteralPath $active) {
    throw "Rollback refuses to overwrite current runtime. Move it to an owner-chosen quarantine first"
  }
  Move-Item -LiteralPath $backup -Destination $active
  Write-Host "Restored original native runtime; data/models untouched"
}
