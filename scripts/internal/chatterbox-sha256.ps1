# Portable offline SHA256: no dependency on module auto-loading of Get-FileHash.
function Get-ChatterboxSha256 {
  [CmdletBinding()]
  param([Parameter(Mandatory=$true)][string]$LiteralPath)
  $resolved=[IO.Path]::GetFullPath($LiteralPath)
  $stream=$null
  $hash=$null
  try {
    $stream=[IO.File]::Open($resolved,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $hash=[Security.Cryptography.SHA256]::Create()
    $bytes=$hash.ComputeHash($stream)
    return ([BitConverter]::ToString($bytes) -replace '-','').ToLowerInvariant()
  } finally {
    if ($null -ne $hash) { $hash.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
  }
}
