param(
  [Parameter(Mandatory=$true)][string]$Voice,
  [Parameter(Mandatory=$true)][string]$SectionsFile,
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks\longform",
  [int]$Port=3900,
  [int]$VoiceVersion=1,
  [int]$Seed=42,
  [double]$Speed=1.0,
  [switch]$ContinueOnFailure
)
# OWNER LIVE ONLY. The section source and WAVs remain in the owner-selected directory.
$ErrorActionPreference="Stop"
$started=[DateTime]::UtcNow
$clock=[Diagnostics.Stopwatch]::StartNew()
$root=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$run=Join-Path $Output $started.ToString("yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Force -Path $run | Out-Null
$manifest=Join-Path $run "manifest.json"
Write-Output ("START longform utc="+$started.ToString("o"))
python -m lite.longform_benchmark prepare --sections-file $SectionsFile --output $manifest --voice-version $VoiceVersion --seed $Seed --speed $Speed
if($LASTEXITCODE -ne 0){throw "Manifest preparation failed"}
$spec=Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
$rows=New-Object System.Collections.Generic.List[object]
$wavs=New-Object System.Collections.Generic.List[string]
$original=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/lite/worker/settings"
try {
  $settings=@{mode="persistent";idle_timeout_seconds=$null}|ConvertTo-Json -Compress
  Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$Port/lite/worker/settings" -ContentType "application/json" -Body $settings|Out-Null
  foreach($section in $spec.sections){
    $wav=Join-Path $run ("section-{0:d4}.wav" -f [int]$section.index)
    $sectionReceipt=$wav+".json"
    if(Test-Path -LiteralPath $wav){
      if(!(Test-Path -LiteralPath $sectionReceipt)){throw "Resume receipt missing for section $($section.index)"}
      $saved=Get-Content -Raw -LiteralPath $sectionReceipt|ConvertFrom-Json
      $digest=(Get-FileHash -Algorithm SHA256 -LiteralPath $wav).Hash.ToLowerInvariant()
      if($saved.schema -ne "lite-longform-section-v1" -or $saved.wav_sha256 -ne $digest -or $saved.text_sha256 -ne $section.text_sha256 -or $saved.verification_status -ne "verified"){throw "Resume integrity mismatch for section $($section.index)"}
      $wavs.Add($wav);$rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=[double]$saved.wall_s;audio_s=[double]$saved.audio_s;speech_s=[double]$saved.speech_s;rtf=[double]$saved.rtf;native_total_us=$saved.native_total_us;ref_codec_encode_us=$saved.ref_codec_encode_us;cache_hit=$saved.cache_hit;maskgit_steps=$saved.maskgit_steps;status="PASS";resumed=$true});Write-Output ("PASS resume section="+$section.index);continue
    }
    $tmp=$wav+".partial";$timer=[Diagnostics.Stopwatch]::StartNew()
    try {
      $payload=@{model="omnivoice-gguf";voice=$Voice;input=$section.text;response_format="wav";speed=$Speed;seed=[int]$section.seed}|ConvertTo-Json -Compress
      $response=Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$Port/v1/audio/speech" -ContentType "application/json" -Body $payload -OutFile $tmp -PassThru
      $timer.Stop()
      if($response.Headers['X-Lite-Verification-Status'] -ne 'verified' -or (Get-Item -LiteralPath $tmp).Length -lt 46){throw "unverified or truncated WAV"}
      Move-Item -LiteralPath $tmp -Destination $wav
      $state=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/lite/worker/status"
      $digest=(Get-FileHash -Algorithm SHA256 -LiteralPath $wav).Hash.ToLowerInvariant()
      $receiptTemp=$sectionReceipt+".partial"
      @{schema="lite-longform-section-v1";index=[int]$section.index;text_sha256=$section.text_sha256;wav_sha256=$digest;verification_status="verified";wall_s=$timer.Elapsed.TotalSeconds;audio_s=$response.Headers['X-Lite-Audio-S'];speech_s=$response.Headers['X-Lite-Speech-S'];rtf=$response.Headers['X-Lite-RTF'];native_total_us=$state.performance.native_total_us;ref_codec_encode_us=$state.performance.ref_codec_encode_us;cache_hit=$state.performance.ref_cache_hit;maskgit_steps=$state.performance.maskgit_steps}|ConvertTo-Json -Compress|Set-Content -Encoding UTF8 -LiteralPath $receiptTemp
      Move-Item -LiteralPath $receiptTemp -Destination $sectionReceipt
      $wavs.Add($wav)
      $rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=$timer.Elapsed.TotalSeconds;audio_s=[double]$response.Headers['X-Lite-Audio-S'];speech_s=[double]$response.Headers['X-Lite-Speech-S'];rtf=[double]$response.Headers['X-Lite-RTF'];native_total_us=$state.performance.native_total_us;ref_codec_encode_us=$state.performance.ref_codec_encode_us;cache_hit=$state.performance.ref_cache_hit;maskgit_steps=$state.performance.maskgit_steps;status="PASS";resumed=$false})
      Write-Output ("PASS section="+$section.index+" elapsed_s="+$timer.Elapsed.TotalSeconds.ToString('0.000',[cultureinfo]::InvariantCulture))
    } catch {
      if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force}
      $rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=$timer.Elapsed.TotalSeconds;status="FAIL";error=$_.Exception.GetType().Name})
      Write-Output ("FAIL section="+$section.index+" error="+$_.Exception.GetType().Name)
      if(!$ContinueOnFailure){throw}
    }
  }
  if($wavs.Count -ne $spec.sections.Count){throw "Not all sections completed; final WAV withheld"}
  python -m lite.longform_benchmark assemble --output (Join-Path $run "final.wav") @wavs
  if($LASTEXITCODE -ne 0){throw "Final streaming assembly failed"}
} finally {
  $restore=@{mode=$original.mode;idle_timeout_seconds=$original.idle_timeout_seconds}|ConvertTo-Json -Compress
  try{Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$Port/lite/worker/settings" -ContentType "application/json" -Body $restore|Out-Null}catch{Write-Warning "SETTINGS_RESTORE_FAILED"}
  $rows|Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $run "metrics.csv")
  $audioTotal=($rows|Where-Object{$_.status -eq 'PASS'}|Measure-Object -Property audio_s -Sum).Sum
  $speechTotal=($rows|Where-Object{$_.status -eq 'PASS'}|Measure-Object -Property speech_s -Sum).Sum
  $elapsed=$clock.Elapsed.TotalSeconds
  $qualified=(@($rows|Where-Object{$_.status -ne 'PASS' -or $_.resumed -eq $true}).Count -eq 0)
  @{schema="lite-longform-owner-run-v1";utc=$started.ToString("o");elapsed_s=$elapsed;audio_s=$audioTotal;speech_s=$speechTotal;final_rtf=$(if($audioTotal){$elapsed/$audioTotal}else{$null});speech_rtf=$(if($speechTotal){$elapsed/$speechTotal}else{$null});qualified_fresh_run=$qualified;sections=$rows;privacy="voice id, text and reference bytes omitted";owner_gates="OWNER TEST PENDING"}|ConvertTo-Json -Depth 5|Set-Content -Encoding UTF8 (Join-Path $run "metrics.json")
}
$clock.Stop();Write-Output ("PASS longform elapsed_s="+$clock.Elapsed.TotalSeconds.ToString('0.000',[cultureinfo]::InvariantCulture)+" output="+$run)
