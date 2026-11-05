# Pure PowerShell 5.1-compatible snapshot verifier.  The production installer
# always supplies its compiled-in immutable pins; tests exercise this function
# with a separate synthetic fixture without adding a production hash bypass.
function Test-ChatterboxModelSnapshot {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory=$true)][string]$ModelDirectory,
    [Parameter(Mandatory=$true)][string]$ManifestPath,
    [Parameter(Mandatory=$true)][string[]]$RequiredFiles,
    [Parameter(Mandatory=$true)][hashtable]$ExpectedHashes,
    [Parameter(Mandatory=$true)][string]$ModelRepo,
    [Parameter(Mandatory=$true)][string]$ModelRevision,
    [Parameter(Mandatory=$true)][string]$SourceRevision
  )
  if (!(Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {throw 'MODEL_MANIFEST_INVALID'}
  try {$m=Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json}
  catch {throw 'MODEL_MANIFEST_INVALID'}
  if ($m.schema -ne 'chatterbox-nano-model-manifest-v1' -or $m.model_id -ne 'chatterbox-nano' -or
      $m.repo_id -ne $ModelRepo -or $m.loader_mode -ne 'nano=True' -or
      $m.revision -ne $ModelRevision -or $m.source_revision -ne $SourceRevision) {
    throw 'MODEL_MANIFEST_INVALID'
  }
  $rootFiles=@(Get-ChildItem -LiteralPath $ModelDirectory -File | Select-Object -ExpandProperty Name)
  $allowed=@($RequiredFiles)+@('manifest.json')
  if(@($rootFiles | Where-Object {$_ -notin $allowed}).Count -or
     @($allowed | Where-Object {$_ -notin $rootFiles}).Count){throw 'MODEL_MANIFEST_INVALID'}
  foreach($name in $RequiredFiles) {
    $file=Join-Path $ModelDirectory $name
    if(!(Test-Path -LiteralPath $file -PathType Leaf)) {throw "MODEL_FILES_MISSING: $name"}
    $digest=Get-ChatterboxSha256 -LiteralPath $file
    if($digest -ne $ExpectedHashes[$name] -or $digest -ne $m.sha256.$name) {
      throw "MODEL_HASH_MISMATCH: $name"
    }
  }
  return $true
}

function Assert-ChatterboxVerifyPrerequisites {
  [CmdletBinding()]
  param([Parameter(Mandatory=$true)][string]$PythonPath,
        [string]$FfmpegCommand='ffmpeg')
  if (!(Test-Path -LiteralPath $PythonPath -PathType Leaf)) {throw 'ENV_NOT_INSTALLED'}
  if (!(Get-Command $FfmpegCommand -ErrorAction SilentlyContinue)) {throw 'FFMPEG_UNAVAILABLE'}
  return $true
}
