function Invoke-ChatterboxOnlineCommand {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory=$true)][scriptblock]$Command,
    [Parameter(Mandatory=$true)][string]$FailureCode
  )
  $previousHub=[Environment]::GetEnvironmentVariable('HF_HUB_OFFLINE','Process')
  $previousTransformers=[Environment]::GetEnvironmentVariable('TRANSFORMERS_OFFLINE','Process')
  try {
    # Verify/runtime deliberately use offline mode. Installation downloads are
    # the one explicit online boundary and must not inherit a stale offline flag.
    $env:HF_HUB_OFFLINE='0'
    $env:TRANSFORMERS_OFFLINE='0'
    & $Command
    if ($LASTEXITCODE -ne 0) { throw $FailureCode }
  }
  finally {
    if ($null -eq $previousHub) {
      Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
    } else {
      $env:HF_HUB_OFFLINE=$previousHub
    }
    if ($null -eq $previousTransformers) {
      Remove-Item Env:TRANSFORMERS_OFFLINE -ErrorAction SilentlyContinue
    } else {
      $env:TRANSFORMERS_OFFLINE=$previousTransformers
    }
  }
}
