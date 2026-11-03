param(
  [string]$VoiceA="",
  [string]$VoiceB="",
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks\persistent-worker",
  [int]$Port=3900,
  [string]$CiMockSpeechUrl="",
  [string[]]$Cases=@(),
  [switch]$ContinueOnFailure
)
# OWNER LIVE ONLY: real Q8_0/GTX1650 metrics. Never run in CI or export voice IDs/text/audio.
$ErrorActionPreference="Stop"
$base="http://127.0.0.1:$Port"
$benchmarkStarted=[Diagnostics.Stopwatch]::StartNew()
$failures=New-Object System.Collections.Generic.List[string]
function Test-Selected([string]$Label){return $Cases.Count -eq 0 -or $Cases -contains $Label}
function Invoke-SpeechRequest([string]$Uri,[string]$Payload,[string]$OutFile) {
  # -UseBasicParsing prevents Windows PowerShell 5.1 from invoking the legacy
  # Internet Explorer engine or displaying its first-run security prompt.
  Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Uri -ContentType "application/json" -Body $Payload -OutFile $OutFile -PassThru
}
if ($CiMockSpeechUrl) {
  # CI-only localhost exercise of the exact request helper. It neither changes
  # worker settings nor writes an owner report, and its fixture contains no
  # private voice, transcript, audio or filesystem data.
  $smoke=[IO.Path]::GetTempFileName()
  if($Cases.Count -and $Cases -notcontains 'ci-http'){Write-Output "PASS benchmark selected=0";exit 0}
  Write-Output ("START case=ci-http utc="+[DateTime]::UtcNow.ToString("o"))
  try {
    $payload=@{model="omnivoice-gguf";voice="ci-smoke";input="CI smoke";response_format="wav";speed=1.0;seed=42}|ConvertTo-Json -Compress
    $response=Invoke-SpeechRequest $CiMockSpeechUrl $payload $smoke
    if ((Get-Item -LiteralPath $smoke).Length -lt 46 -or $response.Headers['X-Lite-Verification-Status'] -ne 'verified') {
      throw "Mock speech response was unverified or truncated"
    }
    Write-Output "PASS case=ci-http"
  } catch {
    Write-Output ("FAIL case=ci-http error="+$_.Exception.GetType().Name)
    if(!$ContinueOnFailure){throw}
    Write-Output "PASS settings-restore unchanged"
    exit 2
  } finally {
    if (Test-Path -LiteralPath $smoke) { Remove-Item -LiteralPath $smoke -Force }
  }
  Write-Output "PASS settings-restore unchanged"
  Write-Output "BENCHMARK_HTTP_SMOKE_PASS"
  exit 0
}
if (!$VoiceA -or !$VoiceB) { throw "VoiceA and VoiceB are required for the owner benchmark" }
$dir=Join-Path $Output ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$rows=New-Object System.Collections.Generic.List[object]
function Get-Vram {
  $raw=& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  $total=0;foreach($line in $raw){if($line -match '^\s*(\d+)\s*$'){$total += [int]$Matches[1]}};return $total
}
function Stop-Monitor($Monitor) {
  if (!$Monitor) { return }
  try { if (!$Monitor.HasExited) { $Monitor.Kill() } } catch { }
  try { $Monitor.WaitForExit() } catch { }
}
function Set-Worker([string]$Mode,$Idle){$body=@{mode=$Mode;idle_timeout_seconds=$Idle}|ConvertTo-Json -Compress;Invoke-RestMethod -Method Put -Uri "$base/lite/worker/settings" -ContentType "application/json" -Body $body|Out-Null}
function Status { Invoke-RestMethod -Uri "$base/lite/worker/status" }
function Unload { Invoke-RestMethod -Method Post -Uri "$base/lite/worker/unload"|Out-Null }
function Invoke-Sample([string]$Label,[string]$Voice,[string]$Text,[double]$Speed=1.0,[int]$Seed=42) {
  if(!(Test-Selected $Label)){return}
  $caseClock=[Diagnostics.Stopwatch]::StartNew();Write-Output ("START case="+$Label+" utc="+[DateTime]::UtcNow.ToString("o"))
  $token=[guid]::NewGuid().ToString("N");$temp=Join-Path $dir ("private-"+$token+".wav");$vramLog=Join-Path $dir ("private-"+$token+".vram");$before=Get-Vram;$clock=[Diagnostics.Stopwatch]::StartNew();$monitor=$null
  try {
    $monitor=Start-Process -FilePath "nvidia-smi.exe" -ArgumentList @('--query-gpu=memory.used','--format=csv,noheader,nounits','--loop-ms=250') -WindowStyle Hidden -RedirectStandardOutput $vramLog -RedirectStandardError (Join-Path $dir ("private-"+$token+".err")) -PassThru
    $payload=@{model="omnivoice-gguf";voice=$Voice;input=$Text;response_format="wav";speed=$Speed;seed=$Seed}|ConvertTo-Json -Compress
    $response=Invoke-SpeechRequest "$base/v1/audio/speech" $payload $temp
    $clock.Stop();Stop-Monitor $monitor;$state=Status;$after=Get-Vram
    $peak=$after;if(Test-Path -LiteralPath $vramLog){foreach($line in (Get-Content -LiteralPath $vramLog)){if($line -match '^\s*(\d+)\s*$' -and [int]$Matches[1] -gt $peak){$peak=[int]$Matches[1]}}}
    if((Get-Item $temp).Length -lt 46 -or $response.Headers['X-Lite-Verification-Status'] -ne 'verified'){throw "Unverified or truncated WAV"}
    $rows.Add([pscustomobject]@{case=$Label;utc=[DateTime]::UtcNow.ToString("o");speed=$Speed;seed=$Seed;http_ms=[math]::Round($clock.Elapsed.TotalMilliseconds,1);server_elapsed_s=$response.Headers['X-Lite-Elapsed-S'];audio_s=$response.Headers['X-Lite-Audio-S'];speech_s=$response.Headers['X-Lite-Speech-S'];pause_s=$response.Headers['X-Lite-Pause-S'];rtf=$response.Headers['X-Lite-RTF'];pid=$state.pid;generation=$state.worker_generation;cold_loads=$state.cold_load_count;syntheses=$state.synthesis_count;reuse=$state.reuse_count;perf_native_total_us=$state.performance.native_total_us;perf_ref_preprocess_us=$state.performance.ref_preprocess_us;perf_ref_codec_encode_us=$state.performance.ref_codec_encode_us;perf_maskgit_lm_forward_us=$state.performance.maskgit_lm_forward_us;perf_maskgit_sampling_us=$state.performance.maskgit_sampling_us;perf_codec_decode_us=$state.performance.codec_decode_us;perf_postprocess_us=$state.performance.postprocess_us;perf_cache_hit=$state.performance.ref_cache_hit;perf_steps=$state.performance.maskgit_steps;vram_before_mib=$before;vram_peak_mib=$peak;vram_after_mib=$after;state=$state.state})
    Write-Output ("PASS case="+$Label+" elapsed_s="+$caseClock.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture))
  } catch {
    $failures.Add($Label+":"+$_.Exception.GetType().Name)
    $rows.Add([pscustomobject]@{case=$Label;utc=[DateTime]::UtcNow.ToString("o");state="FAIL";error=$_.Exception.GetType().Name})
    Write-Output ("FAIL case="+$Label+" elapsed_s="+$caseClock.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture)+" error="+$_.Exception.GetType().Name)
    if(!$ContinueOnFailure){throw}
  } finally {Stop-Monitor $monitor;foreach($private in @($temp,$vramLog,(Join-Path $dir ("private-"+$token+".err")))){if(Test-Path -LiteralPath $private){Remove-Item -LiteralPath $private -Force}}}
}
$original=Invoke-RestMethod -Uri "$base/lite/worker/settings"
try {
  Set-Worker "persistent" 60;Unload
  Invoke-Sample "cold-a" $VoiceA "Persistent worker comparable request."
  Invoke-Sample "warm-a-1" $VoiceA "Persistent worker comparable request."
  Invoke-Sample "warm-a-2" $VoiceA "Persistent worker comparable request."
  Invoke-Sample "two-pause" $VoiceA "First. [pause 800ms] Second. [pause 1.5s] Third."
  $ten=(1..11|ForEach-Object{"Section $_"}) -join " [pause 500ms] ";Invoke-Sample "ten-pause" $VoiceA $ten
  foreach($speed in @(0.85,1.0,1.15)){Invoke-Sample ("speed-"+$speed.ToString("0.00",[cultureinfo]::InvariantCulture)) $VoiceA "A fixed sentence measures native speed." $speed}
  Invoke-Sample "aba-a1" $VoiceA "A B A isolation sentence." 1.0 42;Invoke-Sample "aba-b" $VoiceB "A B A isolation sentence." 1.0 43;Invoke-Sample "aba-a2" $VoiceA "A B A isolation sentence." 1.0 42
  1..10|ForEach-Object{Invoke-Sample ("warm-cycle-"+$_.ToString("00")) $VoiceA "Warm repeated stability cycle." 1.0 (100+$_)}
  if(Test-Selected "idle-60s"){Start-Sleep -Seconds 65;$idle=Status;$rows.Add([pscustomobject]@{case="idle-60s";utc=[DateTime]::UtcNow.ToString("o");state=$idle.state;pid=$idle.pid;generation=$idle.worker_generation;vram_after_mib=(Get-Vram)})}
  if(Test-Selected "never-after-65s"){Set-Worker "persistent" $null;Invoke-Sample "never-start" $VoiceA "Never idle policy request.";Start-Sleep -Seconds 65;$never=Status;$rows.Add([pscustomobject]@{case="never-after-65s";utc=[DateTime]::UtcNow.ToString("o");state=$never.state;pid=$never.pid;generation=$never.worker_generation;vram_after_mib=(Get-Vram)})}
  if(Test-Selected "manual-unload"){Unload;$manual=Status;$rows.Add([pscustomobject]@{case="manual-unload";utc=[DateTime]::UtcNow.ToString("o");state=$manual.state;pid=$manual.pid;generation=$manual.worker_generation;vram_after_mib=(Get-Vram)})}
  Invoke-Sample "recovery-cold" $VoiceA "Explicit request after unload recovers."
} finally {
  try{Set-Worker $original.mode $original.idle_timeout_seconds;Write-Output "PASS settings-restore"}catch{$failures.Add("settings-restore:"+$_.Exception.GetType().Name);Write-Output ("FAIL settings-restore error="+$_.Exception.GetType().Name)}
  $rows|Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $dir "owner-metrics.csv")
  $report=@{schema="lite-persistent-owner-benchmark-v1";created_utc=[DateTime]::UtcNow.ToString("o");privacy="No voice IDs, input text, reference bytes, or generated audio retained";rows=$rows}|ConvertTo-Json -Depth 6
  [IO.File]::WriteAllText((Join-Path $dir "owner-metrics.json"),$report,(New-Object Text.UTF8Encoding($false)))
}
Write-Host "Owner-only sanitized metrics: $dir"
Write-Host "Manual gates remain: listen to A/B/A; stop/restart service; test cancellation; inspect peak VRAM, growth and OOM."
$benchmarkStarted.Stop()
if($failures.Count){Write-Output ("FAIL benchmark elapsed_s="+$benchmarkStarted.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture)+" failures="+$failures.Count);exit 2}
Write-Output ("PASS benchmark elapsed_s="+$benchmarkStarted.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture))
