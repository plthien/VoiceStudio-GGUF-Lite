param(
  [Parameter(Mandatory=$true)][string]$VoiceA,
  [Parameter(Mandatory=$true)][string]$VoiceB,
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks\persistent-worker",
  [int]$Port=3900
)
# OWNER LIVE ONLY: real Q8_0/GTX1650 metrics. Never run in CI or export voice IDs/text/audio.
$ErrorActionPreference="Stop"
$base="http://127.0.0.1:$Port"
$dir=Join-Path $Output ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$rows=New-Object System.Collections.Generic.List[object]
function Get-Vram {
  $raw=& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  $total=0;foreach($line in $raw){if($line -match '^\s*(\d+)\s*$'){$total += [int]$Matches[1]}};return $total
}
function Set-Worker([string]$Mode,$Idle){$body=@{mode=$Mode;idle_timeout_seconds=$Idle}|ConvertTo-Json -Compress;Invoke-RestMethod -Method Put -Uri "$base/lite/worker/settings" -ContentType "application/json" -Body $body|Out-Null}
function Status { Invoke-RestMethod -Uri "$base/lite/worker/status" }
function Unload { Invoke-RestMethod -Method Post -Uri "$base/lite/worker/unload"|Out-Null }
function Invoke-Sample([string]$Label,[string]$Voice,[string]$Text,[double]$Speed=1.0,[int]$Seed=42) {
  $token=[guid]::NewGuid().ToString("N");$temp=Join-Path $dir ("private-"+$token+".wav");$vramLog=Join-Path $dir ("private-"+$token+".vram");$before=Get-Vram;$clock=[Diagnostics.Stopwatch]::StartNew();$monitor=$null
  try {
    $monitor=Start-Process -FilePath "nvidia-smi.exe" -ArgumentList @('--query-gpu=memory.used','--format=csv,noheader,nounits','--loop-ms=250') -WindowStyle Hidden -RedirectStandardOutput $vramLog -RedirectStandardError (Join-Path $dir ("private-"+$token+".err")) -PassThru
    $payload=@{model="omnivoice-gguf";voice=$Voice;input=$Text;response_format="wav";speed=$Speed;seed=$Seed}|ConvertTo-Json -Compress
    $response=Invoke-WebRequest -Method Post -Uri "$base/v1/audio/speech" -ContentType "application/json" -Body $payload -OutFile $temp -PassThru
    $clock.Stop();if($monitor -and !$monitor.HasExited){$monitor.Kill();$monitor.WaitForExit()};$state=Status;$after=Get-Vram
    $peak=$after;if(Test-Path -LiteralPath $vramLog){foreach($line in (Get-Content -LiteralPath $vramLog)){if($line -match '^\s*(\d+)\s*$' -and [int]$Matches[1] -gt $peak){$peak=[int]$Matches[1]}}}
    if((Get-Item $temp).Length -lt 46 -or $response.Headers['X-Lite-Verification-Status'] -ne 'verified'){throw "Unverified or truncated WAV"}
    $rows.Add([pscustomobject]@{case=$Label;utc=[DateTime]::UtcNow.ToString("o");speed=$Speed;seed=$Seed;http_ms=[math]::Round($clock.Elapsed.TotalMilliseconds,1);server_elapsed_s=$response.Headers['X-Lite-Elapsed-S'];audio_s=$response.Headers['X-Lite-Audio-S'];speech_s=$response.Headers['X-Lite-Speech-S'];pause_s=$response.Headers['X-Lite-Pause-S'];rtf=$response.Headers['X-Lite-RTF'];pid=$state.pid;generation=$state.worker_generation;cold_loads=$state.cold_load_count;syntheses=$state.synthesis_count;reuse=$state.reuse_count;vram_before_mib=$before;vram_peak_mib=$peak;vram_after_mib=$after;state=$state.state})
  } finally {if($monitor -and !$monitor.HasExited){$monitor.Kill();$monitor.WaitForExit()};foreach($private in @($temp,$vramLog,(Join-Path $dir ("private-"+$token+".err")))){if(Test-Path -LiteralPath $private){Remove-Item -LiteralPath $private -Force}}}
}
$original=Invoke-RestMethod -Uri "$base/lite/worker/settings"
try {
  Set-Worker "persistent" 60;Unload
  Invoke-Sample "cold-a" $VoiceA "Persistent worker cold request."
  Invoke-Sample "warm-a-1" $VoiceA "Persistent worker warm request one."
  Invoke-Sample "warm-a-2" $VoiceA "Persistent worker warm request two."
  Invoke-Sample "two-pause" $VoiceA "First. [pause 800ms] Second. [pause 1.5s] Third."
  $ten=(1..11|ForEach-Object{"Section $_"}) -join " [pause 500ms] ";Invoke-Sample "ten-pause" $VoiceA $ten
  foreach($speed in @(0.85,1.0,1.15)){Invoke-Sample ("speed-"+$speed.ToString("0.00",[cultureinfo]::InvariantCulture)) $VoiceA "A fixed sentence measures native speed." $speed}
  Invoke-Sample "aba-a1" $VoiceA "A B A isolation sentence." 1.0 42;Invoke-Sample "aba-b" $VoiceB "A B A isolation sentence." 1.0 43;Invoke-Sample "aba-a2" $VoiceA "A B A isolation sentence." 1.0 42
  1..10|ForEach-Object{Invoke-Sample ("warm-cycle-"+$_.ToString("00")) $VoiceA "Warm repeated stability cycle." 1.0 (100+$_)}
  Start-Sleep -Seconds 65;$idle=Status;$rows.Add([pscustomobject]@{case="idle-60s";utc=[DateTime]::UtcNow.ToString("o");state=$idle.state;pid=$idle.pid;generation=$idle.worker_generation;vram_after_mib=(Get-Vram)})
  Set-Worker "persistent" $null;Invoke-Sample "never-start" $VoiceA "Never idle policy request.";Start-Sleep -Seconds 65;$never=Status;$rows.Add([pscustomobject]@{case="never-after-65s";utc=[DateTime]::UtcNow.ToString("o");state=$never.state;pid=$never.pid;generation=$never.worker_generation;vram_after_mib=(Get-Vram)})
  Unload;$manual=Status;$rows.Add([pscustomobject]@{case="manual-unload";utc=[DateTime]::UtcNow.ToString("o");state=$manual.state;pid=$manual.pid;generation=$manual.worker_generation;vram_after_mib=(Get-Vram)})
  Invoke-Sample "recovery-cold" $VoiceA "Explicit request after unload recovers."
} finally {
  try{Set-Worker $original.mode $original.idle_timeout_seconds}catch{Write-Warning "Restore settings manually: $($_.Exception.Message)"}
  $rows|Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $dir "owner-metrics.csv")
  $report=@{schema="lite-persistent-owner-benchmark-v1";created_utc=[DateTime]::UtcNow.ToString("o");privacy="No voice IDs, input text, reference bytes, or generated audio retained";rows=$rows}|ConvertTo-Json -Depth 6
  [IO.File]::WriteAllText((Join-Path $dir "owner-metrics.json"),$report,(New-Object Text.UTF8Encoding($false)))
}
Write-Host "Owner-only sanitized metrics: $dir"
Write-Host "Manual gates remain: listen to A/B/A; stop/restart service; test cancellation; inspect peak VRAM, growth and OOM."
