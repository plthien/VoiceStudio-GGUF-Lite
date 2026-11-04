param(
  [string]$VoiceA="",
  [string]$VoiceB="",
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks\persistent-worker",
  [int]$Port=3900,
  [string]$CiMockSpeechUrl="",
  [string[]]$Cases=@(),
  [int]$IdleObservationSeconds=65,
  [switch]$ContinueOnFailure
)
# OWNER LIVE ONLY: real Q8_0/GTX1650 metrics. Never run in CI or export voice IDs/text/audio.
$ErrorActionPreference="Stop"
$base="http://127.0.0.1:$Port"
$benchmarkStarted=[Diagnostics.Stopwatch]::StartNew()
$failures=New-Object System.Collections.Generic.List[string]
$groupCases=@{
  cold=@("cold-a")
  warm=@("warm-a-1","warm-a-2")
  twopause=@("two-pause")
  tenpause=@("ten-pause")
  aba=@("aba-a1","aba-b","aba-a2")
  idle=@("idle-start","idle-60s","never-start","never-after-65s","manual-unload")
}
$lowLevel=@("cold-a","warm-a-1","warm-a-2","two-pause","ten-pause","aba-a1","aba-b","aba-a2","idle-start","idle-60s","never-after-65s","never-start","manual-unload","recovery-cold","ci-http")
foreach($value in @(0.85,1.0,1.15)){$lowLevel+=("speed-"+$value.ToString("0.00",[cultureinfo]::InvariantCulture))}
$lowLevel+=@(1..10|ForEach-Object{"warm-cycle-"+$_.ToString("00")})
$selected=@{}
foreach($selector in $Cases) {
  foreach($part in ([string]$selector -split ',')){
    $name=$part.Trim().ToLowerInvariant()
    if($groupCases.ContainsKey($name)){foreach($member in $groupCases[$name]){$selected[$member]=$true}}
    elseif($lowLevel -contains $name){
      $selected[$name]=$true
      if($name -eq "idle-60s"){$selected["idle-start"]=$true}
      if($name -eq "never-after-65s"){$selected["never-start"]=$true}
    }
    else{throw "UNKNOWN_CASE_SELECTOR: $name"}
  }
}
if($IdleObservationSeconds -ne 65 -and $env:CI -ne "true"){
  throw "IdleObservationSeconds is CI-only unless it is the owner value 65"
}
function Test-Selected([string]$Label){return $Cases.Count -eq 0 -or $selected.ContainsKey($Label)}
function Invoke-SpeechRequest([string]$Uri,[string]$Payload,[string]$OutFile) {
  # -UseBasicParsing prevents Windows PowerShell 5.1 from invoking the legacy
  # Internet Explorer engine or displaying its first-run security prompt.
  Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Uri -ContentType "application/json" -Body $Payload -OutFile $OutFile -PassThru
}
function Get-HeaderValue([object]$Response,[string]$Name) {
  foreach($key in $Response.Headers.Keys){
    if([string]$key -ieq $Name){return [string]$Response.Headers[$key]}
  }
  return ""
}
function Get-SafeErrorCode([object]$Record) {
  $message=[string]$Record.Exception.Message
  foreach($code in @("PERF_NOT_ATTRIBUTABLE","REQUEST_CORRELATION_MISSING",
      "PROFILE_BINDING_CHANGED","PROFILE_BINDING_UNAVAILABLE","WARM_PRECONDITION_NOT_MET")){
    if($message -match [regex]::Escape($code)){return $code}
  }
  return $Record.Exception.GetType().Name
}
if ($CiMockSpeechUrl) {
  # CI-only localhost exercise of the exact request helper. It neither changes
  # worker settings nor writes an owner report, and its fixture contains no
  # private voice, transcript, audio or filesystem data.
  $smoke=[IO.Path]::GetTempFileName()
  if($Cases.Count -and !$selected.ContainsKey("ci-http")){throw "UNKNOWN_CASE_SELECTOR: ci-http fixture requires ci-http"}
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
if (!$VoiceA) { throw "VoiceA is required for the owner benchmark" }
$needsVoiceB=($Cases.Count -eq 0 -or (Test-Selected "aba-b"))
if($needsVoiceB -and !$VoiceB){throw "VoiceB is required for the selected A-B-A benchmark"}
$dir=Join-Path $Output ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$rows=New-Object System.Collections.Generic.List[object]
function Publish-TextAtomic([string]$Destination,[string]$Content) {
  $temp=$Destination+"."+[guid]::NewGuid().ToString("N")+".partial"
  $backup=$Destination+"."+[guid]::NewGuid().ToString("N")+".backup"
  $bytes=[Text.Encoding]::UTF8.GetBytes($Content)
  $stream=New-Object IO.FileStream($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
  try {
    if([IO.File]::Exists($Destination)){[IO.File]::Replace($temp,$Destination,$backup);[IO.File]::Delete($backup)}
    else{[IO.File]::Move($temp,$Destination)}
  } finally {if([IO.File]::Exists($temp)){[IO.File]::Delete($temp)};if([IO.File]::Exists($backup)){[IO.File]::Delete($backup)}}
}
function Flush-Results {
  $rowSnapshot=@($rows|ForEach-Object{$_})
  $json=@{schema="lite-persistent-owner-benchmark-v1";created_utc=[DateTime]::UtcNow.ToString("o");privacy="No voice IDs, input text, reference bytes, or generated audio retained";rows=$rowSnapshot}|ConvertTo-Json -Depth 8
  Publish-TextAtomic (Join-Path $dir "owner-metrics.json") $json
  $names=@($rows|ForEach-Object{$_.PSObject.Properties.Name}|Select-Object -Unique)
  if(!$names.Count){$names=@("case","state")}
  $csv=(@($rows|Select-Object -Property $names|ConvertTo-Csv -NoTypeInformation) -join "`r`n")+"`r`n"
  Publish-TextAtomic (Join-Path $dir "owner-metrics.csv") $csv
}
function Get-Vram {
  try {$raw=& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null}
  catch {return $null}
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
$profileBindings=@{}
function Get-ProfileSnapshot([string]$Voice) {
  try {$profile=Invoke-RestMethod -Uri ($base+"/profiles/"+[uri]::EscapeDataString($Voice))}
  catch {throw "PROFILE_BINDING_UNAVAILABLE"}
  $version=0
  if(!$profile -or $profile.id -cne $Voice -or
     ![int]::TryParse([string]$profile.version,[ref]$version) -or $version -le 0 -or
     [string]$profile.ref_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
     [string]$profile.source_sha256 -cnotmatch '^[0-9a-f]{64}$'){
    throw "PROFILE_BINDING_UNAVAILABLE"
  }
  return [pscustomobject]@{id=[string]$profile.id;version=$version;
    ref_sha256=[string]$profile.ref_sha256;source_sha256=[string]$profile.source_sha256}
}
function Assert-ProfileUnchanged([object]$Expected) {
  $actual=Get-ProfileSnapshot $Expected.id
  if($actual.id -cne $Expected.id -or $actual.version -ne $Expected.version -or
     $actual.ref_sha256 -cne $Expected.ref_sha256 -or
     $actual.source_sha256 -cne $Expected.source_sha256){
    throw "PROFILE_BINDING_CHANGED"
  }
}
function Assert-VoiceStable([string]$Voice) {
  if(!$profileBindings.ContainsKey($Voice)){throw "PROFILE_BINDING_UNAVAILABLE"}
  Assert-ProfileUnchanged $profileBindings[$Voice]
}
function Get-RequestPerformance([object]$Response,[object]$State) {
  $requestId=Get-HeaderValue $Response "X-Lite-Request-Id"
  $candidate=($state.runtime_binding -and $state.runtime_binding.candidate_kind -eq "verified_candidate")
  $claimsPerf=($state.performance_status -eq "VERIFIED" -or $null -ne $state.performance)
  if($candidate -or $claimsPerf){
    if([string]::IsNullOrWhiteSpace($requestId)){throw "REQUEST_CORRELATION_MISSING"}
    $correlated=($state.last_completed_request_id -ceq $requestId -and
      $null -ne $state.last_completed_worker_generation -and
      [int]$state.last_completed_worker_generation -eq [int]$state.worker_generation)
    $required=@("native_total_us","ref_preprocess_us","ref_codec_encode_us",
      "maskgit_lm_forward_us","maskgit_non_lm_overhead_us","codec_decode_us",
      "postprocess_us","ref_cache_hit","maskgit_steps")
    $usable=($null -ne $state.performance)
    foreach($field in $required){
      if(!$usable -or $state.performance.PSObject.Properties.Name -notcontains $field -or
         $null -eq $state.performance.$field){$usable=$false;break}
      $metricValue=[uint64]0
      if(![uint64]::TryParse([string]$state.performance.$field,
          [Globalization.NumberStyles]::Integer,[cultureinfo]::InvariantCulture,
          [ref]$metricValue)){$usable=$false;break}
    }
    if(!$correlated -or $state.performance_status -ne "VERIFIED" -or !$usable){
      throw "PERF_NOT_ATTRIBUTABLE"
    }
    return [pscustomobject]@{status="VERIFIED";performance=$state.performance}
  }
  if(![string]::IsNullOrWhiteSpace([string]$state.performance_status) -and
     $state.performance_status -ne "NOT_AVAILABLE"){throw "PERF_NOT_ATTRIBUTABLE"}
  return [pscustomobject]@{status="NOT_AVAILABLE";performance=$null}
}
function Invoke-Sample([string]$Label,[string]$Voice,[string]$Text,[double]$Speed=1.0,[int]$Seed=42) {
  if(!(Test-Selected $Label)){return}
  $caseClock=[Diagnostics.Stopwatch]::StartNew();Write-Output ("START case="+$Label+" utc="+[DateTime]::UtcNow.ToString("o"))
  $stage="precondition"
  $token=[guid]::NewGuid().ToString("N");$temp=Join-Path $dir ("private-"+$token+".wav");$vramLog=Join-Path $dir ("private-"+$token+".vram");$before=Get-Vram;$clock=[Diagnostics.Stopwatch]::StartNew();$monitor=$null
  try {
    Assert-VoiceStable $Voice
    $beforeState=Status
    if($Label -like "warm-a-*" -and ($beforeState.state -ne "READY" -or !$beforeState.resident -or [int]$beforeState.cold_load_count -lt 1)){
      throw "WARM_PRECONDITION_NOT_MET"
    }
    $stage="POST"
    try {
      $monitor=Start-Process -FilePath "nvidia-smi.exe" -ArgumentList @('--query-gpu=memory.used','--format=csv,noheader,nounits','--loop-ms=250') -WindowStyle Hidden -RedirectStandardOutput $vramLog -RedirectStandardError (Join-Path $dir ("private-"+$token+".err")) -PassThru
    } catch { $monitor=$null }
    $payload=@{model="omnivoice-gguf";voice=$Voice;input=$Text;response_format="wav";speed=$Speed;seed=$Seed}|ConvertTo-Json -Compress
    $response=Invoke-SpeechRequest "$base/v1/audio/speech" $payload $temp
    $clock.Stop();Stop-Monitor $monitor;$stage="verify";Assert-VoiceStable $Voice;$state=Status;$after=Get-Vram
    $perfResult=Get-RequestPerformance $response $state;$perf=$perfResult.performance
    if($Label -like "warm-a-*" -and [int]$state.cold_load_count -ne [int]$beforeState.cold_load_count){throw "WARM_PRECONDITION_NOT_MET"}
    $peak=$after;if(Test-Path -LiteralPath $vramLog){foreach($line in (Get-Content -LiteralPath $vramLog)){if($line -match '^\s*(\d+)\s*$' -and [int]$Matches[1] -gt $peak){$peak=[int]$Matches[1]}}}
    if((Get-Item $temp).Length -lt 46 -or $response.Headers['X-Lite-Verification-Status'] -ne 'verified'){throw "Unverified or truncated WAV"}
    $rows.Add([pscustomobject]@{case=$Label;utc=[DateTime]::UtcNow.ToString("o");speed=$Speed;seed=$Seed;http_ms=[math]::Round($clock.Elapsed.TotalMilliseconds,1);server_elapsed_s=(Get-HeaderValue $response 'X-Lite-Elapsed-S');audio_s=(Get-HeaderValue $response 'X-Lite-Audio-S');speech_s=(Get-HeaderValue $response 'X-Lite-Speech-S');pause_s=(Get-HeaderValue $response 'X-Lite-Pause-S');rtf=(Get-HeaderValue $response 'X-Lite-RTF');pid=$state.pid;generation=$state.worker_generation;cold_loads=$state.cold_load_count;syntheses=$state.synthesis_count;reuse=$state.reuse_count;performance_status=$perfResult.status;perf_native_total_us=$perf.native_total_us;perf_ref_preprocess_us=$perf.ref_preprocess_us;perf_ref_codec_encode_us=$perf.ref_codec_encode_us;perf_maskgit_lm_forward_us=$perf.maskgit_lm_forward_us;perf_maskgit_non_lm_overhead_us=$perf.maskgit_non_lm_overhead_us;perf_sampling_semantics=$(if($perf){"derived_non_lm_overhead"}else{$null});perf_codec_decode_us=$perf.codec_decode_us;perf_postprocess_us=$perf.postprocess_us;perf_cache_hit=$perf.ref_cache_hit;perf_steps=$perf.maskgit_steps;vram_before_mib=$before;vram_peak_mib=$peak;vram_after_mib=$after;state=$state.state})
    Flush-Results
    Write-Output ("PASS case="+$Label+" elapsed_s="+$caseClock.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture))
  } catch {
    $code=Get-SafeErrorCode $_
    $failures.Add($Label+":"+$code)
    $rows.Add([pscustomobject]@{case=$Label;utc=[DateTime]::UtcNow.ToString("o");state="FAIL";stage=$stage;error=$code;elapsed_s=$caseClock.Elapsed.TotalSeconds})
    Flush-Results
    Write-Output ("FAIL case="+$Label+" elapsed_s="+$caseClock.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture)+" error="+$code)
    if(!$ContinueOnFailure){throw}
  } finally {Stop-Monitor $monitor;foreach($private in @($temp,$vramLog,(Join-Path $dir ("private-"+$token+".err")))){if(Test-Path -LiteralPath $private){Remove-Item -LiteralPath $private -Force}}}
}
$original=Invoke-RestMethod -Uri "$base/lite/worker/settings"
$restoreFailed=$false
try {
  $profileBindings[$VoiceA]=Get-ProfileSnapshot $VoiceA
  if($needsVoiceB){$profileBindings[$VoiceB]=Get-ProfileSnapshot $VoiceB}
  Set-Worker "persistent" 60;Unload
  Invoke-Sample "cold-a" $VoiceA "Persistent worker comparable request."
  if(!(Test-Selected "cold-a") -and ((Test-Selected "warm-a-1") -or (Test-Selected "warm-a-2"))) {
    $warmFile=Join-Path $dir ("private-warmup-"+[guid]::NewGuid().ToString("N")+".wav")
    Write-Output ("START case=warmup-unmeasured utc="+[DateTime]::UtcNow.ToString("o"))
    try {
      Assert-VoiceStable $VoiceA
      $payload=@{model="omnivoice-gguf";voice=$VoiceA;input="Persistent worker comparable request.";response_format="wav";speed=1.0;seed=42}|ConvertTo-Json -Compress
      $response=Invoke-SpeechRequest "$base/v1/audio/speech" $payload $warmFile
      Assert-VoiceStable $VoiceA
      $warmed=Status
      if($response.Headers['X-Lite-Verification-Status'] -ne 'verified' -or !$warmed.resident -or $warmed.state -ne 'READY'){throw "WARM_PRECONDITION_NOT_MET"}
      Write-Output "PASS case=warmup-unmeasured"
    } catch {
      $warmupError=Get-SafeErrorCode $_
      $failures.Add($warmupError)
      Write-Output ("FAIL case=warmup-unmeasured error="+$warmupError)
      if(!$ContinueOnFailure){throw}
    } finally {if(Test-Path -LiteralPath $warmFile){Remove-Item -LiteralPath $warmFile -Force}}
  }
  Invoke-Sample "warm-a-1" $VoiceA "Persistent worker comparable request."
  Invoke-Sample "warm-a-2" $VoiceA "Persistent worker comparable request."
  Invoke-Sample "two-pause" $VoiceA "First. [pause 800ms] Second. [pause 1.5s] Third."
  $ten=(1..11|ForEach-Object{"Section $_"}) -join " [pause 500ms] ";Invoke-Sample "ten-pause" $VoiceA $ten
  foreach($speed in @(0.85,1.0,1.15)){Invoke-Sample ("speed-"+$speed.ToString("0.00",[cultureinfo]::InvariantCulture)) $VoiceA "A fixed sentence measures native speed." $speed}
  Invoke-Sample "aba-a1" $VoiceA "A B A isolation sentence." 1.0 42;Invoke-Sample "aba-b" $VoiceB "A B A isolation sentence." 1.0 43;Invoke-Sample "aba-a2" $VoiceA "A B A isolation sentence." 1.0 42
  1..10|ForEach-Object{Invoke-Sample ("warm-cycle-"+$_.ToString("00")) $VoiceA "Warm repeated stability cycle." 1.0 (100+$_)}
  if(Test-Selected "idle-60s"){
    Set-Worker "persistent" 60;Invoke-Sample "idle-start" $VoiceA "Idle unload precondition request."
    $beforeIdle=Status;if(!$beforeIdle.resident -or $beforeIdle.state -ne "READY"){throw "IDLE_PRECONDITION_NOT_MET"}
    Start-Sleep -Seconds $IdleObservationSeconds;$idle=Status
    if($IdleObservationSeconds -ge 65 -and ($idle.resident -or $idle.pid)){throw "IDLE_UNLOAD_NOT_OBSERVED"}
    $rows.Add([pscustomobject]@{case="idle-60s";utc=[DateTime]::UtcNow.ToString("o");state=$idle.state;pid=$idle.pid;generation=$idle.worker_generation;vram_after_mib=(Get-Vram)})
  }
  if(Test-Selected "never-after-65s"){
    Set-Worker "persistent" $null;Invoke-Sample "never-start" $VoiceA "Never idle policy request."
    $beforeNever=Status;if(!$beforeNever.resident -or $beforeNever.state -ne "READY"){throw "NEVER_IDLE_PRECONDITION_NOT_MET"}
    Start-Sleep -Seconds $IdleObservationSeconds;$never=Status
    if(!$never.resident -or $never.pid -ne $beforeNever.pid){throw "NEVER_IDLE_RESIDENCY_LOST"}
    $rows.Add([pscustomobject]@{case="never-after-65s";utc=[DateTime]::UtcNow.ToString("o");state=$never.state;pid=$never.pid;generation=$never.worker_generation;vram_after_mib=(Get-Vram)})
  }
  if(Test-Selected "manual-unload"){
    $beforeManual=Status
    if(!$beforeManual.resident){
      Set-Worker "persistent" $null
      $selected["never-start"]=$true
      Invoke-Sample "never-start" $VoiceA "Manual unload precondition request."
      $beforeManual=Status
    }
    if(!$beforeManual.resident){throw "MANUAL_UNLOAD_PRECONDITION_NOT_MET"}
    Unload;$manual=Status
    if($manual.resident -or $manual.pid){throw "MANUAL_UNLOAD_FAILED"}
    $rows.Add([pscustomobject]@{case="manual-unload";utc=[DateTime]::UtcNow.ToString("o");state=$manual.state;pid=$manual.pid;generation=$manual.worker_generation;vram_after_mib=(Get-Vram)})
  }
  Invoke-Sample "recovery-cold" $VoiceA "Explicit request after unload recovers."
} catch {
  $benchmarkError=Get-SafeErrorCode $_
  $failures.Add("benchmark:"+$benchmarkError)
  Write-Output ("FAIL benchmark stage=execution error="+$benchmarkError)
} finally {
  try{Set-Worker $original.mode $original.idle_timeout_seconds;Write-Output "PASS settings-restore"}catch{
    $restoreFailed=$true
    $failures.Add("SETTINGS_RESTORE_FAILED")
    Write-Output "FAIL settings-restore error=SETTINGS_RESTORE_FAILED"
  }
  Flush-Results
}
Write-Host "Owner-only sanitized metrics: $dir"
Write-Host "Manual gates remain: listen to A/B/A; stop/restart service; test cancellation; inspect peak VRAM, growth and OOM."
$benchmarkStarted.Stop()
if($failures.Count){Write-Output ("FAIL benchmark elapsed_s="+$benchmarkStarted.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture)+" failures="+$failures.Count);exit 2}
Write-Output ("PASS benchmark elapsed_s="+$benchmarkStarted.Elapsed.TotalSeconds.ToString("0.000",[cultureinfo]::InvariantCulture))
