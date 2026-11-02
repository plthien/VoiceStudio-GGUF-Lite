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
      try {
        $killer.Kill()
        if (!$killer.WaitForExit(1000)) {
          throw "NS04: taskkill.exe did not terminate after forced kill"
        }
      } catch {
        throw "NS04: taskkill.exe exceeded cleanup bound and termination failed: $($_.Exception.Message)"
      }
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
      Write-Warning "NS04: taskkill returned $killCode after original candidate exited (natural-exit race)."
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
    if (!$proc.HasExited) {
      try {
        $startTicks=$proc.StartTime.ToUniversalTime().Ticks
      } catch {
        if (!$proc.HasExited) { throw "NS04: cannot capture candidate process identity: $($_.Exception.Message)" }
      }
    }
    if (!$proc.WaitForExit(8000)) {
      throw "NS04: Native Speed candidate --help timed out after 8 seconds"
    }
    $proc.Refresh()
    if (!$proc.HasExited) { throw "NS04: candidate is still running after WaitForExit" }
    $exitCode=[int]$proc.ExitCode
    if ($exitCode -ne 0) {
      throw "Native Speed candidate --help returned nonzero exit code $exitCode"
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
          if ($startTicks -le 0L) { throw "NS04: cannot terminate candidate without verified start identity" }
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

