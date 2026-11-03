param(
  [Parameter(Mandatory=$true)][string]$Voice,
  [Parameter(Mandatory=$true)][string]$SectionsFile,
  [string]$Output="D:\VoiceStudioBuild\lite\benchmarks\longform",
  [string]$RunDir="",
  [int]$Port=3900,
  [int]$VoiceVersion=0,
  [int]$Seed=42,
  [double]$Speed=1.0,
  [switch]$ContinueOnFailure,
  [int[]]$RetryUncertainSections=@()
)
# OWNER LIVE ONLY. Section sources, manifests and WAVs stay in the owner-selected directory.
$ErrorActionPreference="Stop"
$started=[DateTime]::UtcNow
$clock=[Diagnostics.Stopwatch]::StartNew()
$root=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$base="http://127.0.0.1:$Port"
$resuming=-not [string]::IsNullOrWhiteSpace($RunDir)

function Get-ProfileSnapshot {
  $encoded=[Uri]::EscapeDataString($Voice)
  $profile=Invoke-RestMethod -Uri "$base/profiles/$encoded"
  if([string]$profile.id -cne $Voice -or [int]$profile.version -lt 1 -or
     [string]$profile.ref_sha256 -notmatch '^[0-9a-f]{64}$' -or
     [string]$profile.source_sha256 -notmatch '^[0-9a-f]{64}$'){
    throw "Voice profile identity is incomplete or invalid"
  }
  return [pscustomobject]@{
    id=[string]$profile.id
    version=[int]$profile.version
    ref_sha256=[string]$profile.ref_sha256
    source_sha256=[string]$profile.source_sha256
  }
}

function Get-ProfileBinding([object]$profile) {
  $canonical=$profile.id+"`n"+$profile.version.ToString([cultureinfo]::InvariantCulture)+"`n"+$profile.ref_sha256+"`n"+$profile.source_sha256
  $sha=[Security.Cryptography.SHA256]::Create()
  try {
    $bytes=[Text.Encoding]::UTF8.GetBytes($canonical)
    return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-","").ToLowerInvariant()
  } finally {$sha.Dispose()}
}

function Get-FileSha256([string]$path) {
  $sha=[Security.Cryptography.SHA256]::Create()
  $stream=[IO.File]::OpenRead($path)
  try {
    return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-","").ToLowerInvariant()
  } finally {
    $stream.Dispose()
    $sha.Dispose()
  }
}

function Write-AtomicJson([string]$path,[object]$value) {
  $temp=$path+"."+[guid]::NewGuid().ToString("N")+".partial"
  $bytes=[Text.Encoding]::UTF8.GetBytes(($value|ConvertTo-Json -Depth 12 -Compress))
  $stream=New-Object IO.FileStream($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
  try {
    if([IO.File]::Exists($path)){[IO.File]::Replace($temp,$path,$null)}
    else{[IO.File]::Move($temp,$path)}
  } finally {if([IO.File]::Exists($temp)){[IO.File]::Delete($temp)}}
}
function Write-AtomicText([string]$path,[string]$text){
  $temp=$path+"."+[guid]::NewGuid().ToString("N")+".partial"
  $bytes=[Text.Encoding]::UTF8.GetBytes($text)
  $stream=New-Object IO.FileStream($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
  try {
    if([IO.File]::Exists($path)){[IO.File]::Replace($temp,$path,$null)}
    else{[IO.File]::Move($temp,$path)}
  } finally {if([IO.File]::Exists($temp)){[IO.File]::Delete($temp)}}
}
function Sync-File([string]$path){
  $stream=New-Object IO.FileStream($path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
  try {$stream.Flush($true)} finally {$stream.Dispose()}
}
function Get-HeaderValue([object]$response,[string]$name){
  foreach($key in $response.Headers.Keys){
    if([string]$key -ieq $name){return [string]$response.Headers[$key]}
  }
  return ""
}
function Get-RuntimeBinding {
  $state=Invoke-RestMethod -Uri "$base/lite/worker/status"
  $binding=$state.runtime_binding
  if(!$binding -or $binding.schema -ne "lite-longform-runtime-binding-v1" -or
     $binding.identity_sha256 -notmatch '^[0-9a-f]{64}$' -or
     $binding.declared_backend -ne "Vulkan0" -or
     $binding.status -ne "verified_capability"){
    throw "RUNTIME_BINDING_UNAVAILABLE"
  }
  return $binding
}
function Assert-RuntimeUnchanged([object]$expected){
  $now=Get-RuntimeBinding
  if($now.identity_sha256 -cne $expected.identity_sha256 -or
     $now.exe_sha256 -cne $expected.exe_sha256 -or
     $now.candidate_receipt_sha256 -cne $expected.candidate_receipt_sha256){
    throw "RUNTIME_BINDING_CHANGED: refusing mixed-runtime benchmark artifacts"
  }
  return $now
}
function Assert-ProfileUnchanged([object]$expected) {
  $actual=Get-ProfileSnapshot
  if($actual.id -cne $expected.id -or $actual.version -ne $expected.version -or
     $actual.ref_sha256 -cne $expected.ref_sha256 -or
     $actual.source_sha256 -cne $expected.source_sha256){
    throw "Voice profile changed during benchmark; refusing mixed-identity artifacts"
  }
}

$profile=Get-ProfileSnapshot
if($VoiceVersion -gt 0 -and $VoiceVersion -ne $profile.version){
  throw "Requested VoiceVersion does not match the current voice profile"
}
$actualVoiceVersion=$profile.version
$profileBinding=Get-ProfileBinding $profile

if($resuming){
  if(!(Test-Path -LiteralPath $RunDir -PathType Container)){throw "RunDir must be an existing benchmark directory"}
  $run=(Resolve-Path -LiteralPath $RunDir).Path
} else {
  New-Item -ItemType Directory -Force -Path $Output | Out-Null
  $name=$started.ToString("yyyyMMdd-HHmmss")+"-"+[Guid]::NewGuid().ToString("N").Substring(0,8)
  $run=Join-Path (Resolve-Path -LiteralPath $Output).Path $name
  New-Item -ItemType Directory -Path $run | Out-Null
}
$manifest=Join-Path $run "manifest.json"
Write-Output ("START longform utc="+$started.ToString("o")+" resume="+$resuming.ToString().ToLowerInvariant()+" run="+$run)
python -m lite.longform_benchmark prepare --sections-file $SectionsFile --output $manifest --voice-version $actualVoiceVersion --profile-binding-sha256 $profileBinding --seed $Seed --speed $Speed
if($LASTEXITCODE -ne 0){throw "Manifest preparation or resume validation failed"}
$spec=Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
if($spec.schema -ne "lite-longform-manifest-v2" -or $spec.profile_binding_sha256 -cne $profileBinding){throw "Manifest profile binding mismatch"}
$rows=New-Object System.Collections.Generic.List[object]
$wavs=New-Object System.Collections.Generic.List[string]
$original=Invoke-RestMethod -Uri "$base/lite/worker/settings"
$final=Join-Path $run "final.wav"
if(Test-Path -LiteralPath $final){Remove-Item -LiteralPath $final -Force}
$restoreFailed=$false
$runError=$null
try {
  $settings=@{mode="persistent";idle_timeout_seconds=$null}|ConvertTo-Json -Compress
  Invoke-RestMethod -Method Put -Uri "$base/lite/worker/settings" -ContentType "application/json" -Body $settings|Out-Null
  foreach($section in $spec.sections){
    Assert-ProfileUnchanged $profile
    $wav=Join-Path $run ("section-{0:d4}.wav" -f [int]$section.index)
    $sectionReceipt=$wav+".json"
    $journalPath=$wav+".attempt.json"
    $tmp=$wav+".partial"
    $journal=$null
    if(Test-Path -LiteralPath $journalPath){
      $journal=Get-Content -Raw -LiteralPath $journalPath|ConvertFrom-Json
      if($journal.schema -ne "lite-longform-attempt-v1" -or
         [int]$journal.index -ne [int]$section.index -or
         $journal.text_sha256 -cne $section.text_sha256 -or
         $journal.manifest_sha256 -cne $spec.manifest_sha256 -or
         $journal.profile_binding_sha256 -cne $profileBinding){
        throw "UNCERTAIN_SECTION_REQUIRES_OWNER_DECISION: invalid section journal"
      }
    } elseif((Test-Path -LiteralPath $wav) -or (Test-Path -LiteralPath $sectionReceipt) -or (Test-Path -LiteralPath $tmp)){
      throw "UNCERTAIN_SECTION_REQUIRES_OWNER_DECISION: orphan artifacts for section $($section.index)"
    }
    if($journal -and $journal.state -ne "VERIFIED_COMMITTED"){
      if(!$resuming -or $RetryUncertainSections -notcontains [int]$section.index){
        throw "UNCERTAIN_SECTION_REQUIRES_OWNER_DECISION: section $($section.index)"
      }
      foreach($orphan in @($wav,$sectionReceipt,$tmp,$sectionReceipt+".partial")){
        if(Test-Path -LiteralPath $orphan){
          Move-Item -LiteralPath $orphan -Destination ($orphan+".quarantine-"+[guid]::NewGuid().ToString("N"))
        }
      }
      $journal.prior_attempt_id=$journal.attempt_id
      $journal.state="OWNER_APPROVED_RETRY"
      $journal.owner_retry_approved=$true
      Write-AtomicJson $journalPath $journal
    }
    if($journal -and $journal.state -eq "VERIFIED_COMMITTED"){
      if(!(Test-Path -LiteralPath $wav) -or !(Test-Path -LiteralPath $sectionReceipt)){
        throw "UNCERTAIN_SECTION_REQUIRES_OWNER_DECISION: committed artifact missing"
      }
      if(!(Test-Path -LiteralPath $sectionReceipt)){throw "Resume receipt missing for section $($section.index)"}
      $saved=Get-Content -Raw -LiteralPath $sectionReceipt|ConvertFrom-Json
      $digest=Get-FileSha256 $wav
      if($journal.wav_sha256 -cne $digest -or $journal.attempt_id -cne $saved.attempt_id){
        throw "Resume journal/receipt mismatch for section $($section.index)"
      }
      $wavInfoJson=python -m lite.longform_benchmark inspect --wav $wav
      if($LASTEXITCODE -ne 0){throw "Resume WAV validation failed for section $($section.index)"}
      $wavInfo=$wavInfoJson|ConvertFrom-Json
      if($saved.schema -ne "lite-longform-section-v2" -or $saved.wav_sha256 -cne $digest -or
         $saved.text_sha256 -cne $section.text_sha256 -or $saved.manifest_sha256 -cne $spec.manifest_sha256 -or
         $saved.profile_binding_sha256 -cne $profileBinding -or $saved.verification_status -ne "verified" -or
         [Math]::Abs([double]$saved.audio_s-[double]$wavInfo.audio_s) -gt 0.001){
        throw "Resume integrity mismatch for section $($section.index)"
      }
      $wavs.Add($wav)
      $rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=[double]$saved.wall_s;audio_s=[double]$saved.audio_s;speech_s=[double]$saved.speech_s;rtf=[double]$saved.rtf;native_total_us=$saved.native_total_us;ref_codec_encode_us=$saved.ref_codec_encode_us;cache_hit=$saved.cache_hit;maskgit_steps=$saved.maskgit_steps;status="PASS";resumed=$true})
      Write-Output ("PASS resume section="+$section.index)
      continue
    }
    $timer=[Diagnostics.Stopwatch]::StartNew()
    try {
      $attemptId=[guid]::NewGuid().ToString("N")
      $record=@{schema="lite-longform-attempt-v1";index=[int]$section.index;
        text_sha256=[string]$section.text_sha256;manifest_sha256=[string]$spec.manifest_sha256;
        profile_binding_sha256=$profileBinding;attempt_id=$attemptId;
        prior_attempt_id=$(if($journal){$journal.prior_attempt_id}else{$null});
        attempts=$(if($journal){[int]$journal.attempts+1}else{1});
        owner_retry_approved=[bool]($journal -and $resuming);
        start_utc=[DateTime]::UtcNow.ToString("o");state="IN_FLIGHT"}
      Write-AtomicJson $journalPath $record
      $payload=@{model="omnivoice-gguf";voice=$Voice;input=$section.text;response_format="wav";speed=$Speed;seed=[int]$section.seed}|ConvertTo-Json -Compress
      $response=Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$base/v1/audio/speech" -ContentType "application/json" -Body $payload -OutFile $tmp -PassThru
      $timer.Stop()
      if($response.Headers['X-Lite-Verification-Status'] -ne 'verified'){throw "Unverified native response"}
      $wavInfoJson=python -m lite.longform_benchmark inspect --wav $tmp
      if($LASTEXITCODE -ne 0){throw "Section WAV integrity validation failed"}
      $wavInfo=$wavInfoJson|ConvertFrom-Json
      $audioSeconds=[double]$wavInfo.audio_s
      $headerAudio=[string]$response.Headers['X-Lite-Audio-S']
      if([string]::IsNullOrWhiteSpace($headerAudio) -or [Math]::Abs([double]$headerAudio-$audioSeconds) -gt 0.001){throw "Audio duration header does not match verified WAV"}
      $speechHeader=[string]$response.Headers['X-Lite-Speech-S']
      $speechSeconds=$(if([string]::IsNullOrWhiteSpace($speechHeader)){$audioSeconds}else{[double]$speechHeader})
      if($speechSeconds -le 0 -or $speechSeconds -gt ($audioSeconds+(1.0/24000.0))){throw "Invalid speech duration header"}
      Assert-ProfileUnchanged $profile
      Sync-File $tmp
      Move-Item -LiteralPath $tmp -Destination $wav
      $state=Invoke-RestMethod -Uri "$base/lite/worker/status"
      $digest=Get-FileSha256 $wav
      $receipt=@{schema="lite-longform-section-v2";index=[int]$section.index;text_sha256=$section.text_sha256;manifest_sha256=$spec.manifest_sha256;profile_binding_sha256=$profileBinding;wav_sha256=$digest;verification_status="verified";attempt_id=$attemptId;wall_s=$timer.Elapsed.TotalSeconds;audio_s=$audioSeconds;speech_s=$speechSeconds;rtf=$response.Headers['X-Lite-RTF'];native_total_us=$state.performance.native_total_us;ref_codec_encode_us=$state.performance.ref_codec_encode_us;cache_hit=$state.performance.ref_cache_hit;maskgit_steps=$state.performance.maskgit_steps}
      Write-AtomicJson $sectionReceipt $receipt
      $record.state="VERIFIED_COMMITTED"
      $record.wav_sha256=$digest
      Write-AtomicJson $journalPath $record
      $wavs.Add($wav)
      $rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=$timer.Elapsed.TotalSeconds;audio_s=$audioSeconds;speech_s=$speechSeconds;rtf=[double]$response.Headers['X-Lite-RTF'];native_total_us=$state.performance.native_total_us;ref_codec_encode_us=$state.performance.ref_codec_encode_us;cache_hit=$state.performance.ref_cache_hit;maskgit_steps=$state.performance.maskgit_steps;status="PASS";resumed=$false})
      Write-Output ("PASS section="+$section.index+" elapsed_s="+$timer.Elapsed.TotalSeconds.ToString('0.000',[cultureinfo]::InvariantCulture))
    } catch {
      if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force}
      $rows.Add([pscustomobject]@{index=[int]$section.index;wall_s=$timer.Elapsed.TotalSeconds;status="FAIL";error=$_.Exception.GetType().Name})
      Write-Output ("FAIL section="+$section.index+" error="+$_.Exception.GetType().Name)
      if(!$ContinueOnFailure){throw}
    }
  }
  Assert-ProfileUnchanged $profile
  if($wavs.Count -ne $spec.sections.Count){throw "Not all sections completed; final WAV withheld"}
  python -m lite.longform_benchmark assemble --output (Join-Path $run "final.wav") @wavs
  if($LASTEXITCODE -ne 0){throw "Final streaming assembly failed"}
} catch {
  $runError=$_.Exception.GetType().Name
  Write-Output ("FAIL longform stage=run error="+$runError)
} finally {
  $restore=@{mode=$original.mode;idle_timeout_seconds=$original.idle_timeout_seconds}|ConvertTo-Json -Compress
  try {
    Invoke-RestMethod -Method Put -Uri "$base/lite/worker/settings" -ContentType "application/json" -Body $restore|Out-Null
  } catch {
    $restoreFailed=$true
    Write-Output ("FAIL longform stage=restore error=SETTINGS_RESTORE_FAILED")
  }
  $rows|Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $run "metrics.csv")
  $audioTotal=($rows|Where-Object{$_.status -eq 'PASS'}|Measure-Object -Property audio_s -Sum).Sum
  $speechTotal=($rows|Where-Object{$_.status -eq 'PASS'}|Measure-Object -Property speech_s -Sum).Sum
  $elapsed=$clock.Elapsed.TotalSeconds
  $success=(-not $restoreFailed -and !$runError -and $wavs.Count -eq $spec.sections.Count -and (Test-Path -LiteralPath (Join-Path $run "final.wav")))
  $qualified=(-not $resuming -and $success -and @($rows|Where-Object{$_.status -ne 'PASS' -or $_.resumed -eq $true}).Count -eq 0)
  @{schema="lite-longform-owner-run-v2";utc=$started.ToString("o");elapsed_s=$elapsed;audio_s=$audioTotal;speech_s=$speechTotal;final_rtf=$(if($audioTotal){$elapsed/$audioTotal}else{$null});speech_rtf=$(if($speechTotal){$elapsed/$speechTotal}else{$null});qualified_fresh_run=$qualified;resumed_run=$resuming;overall_status=$(if($success){"PASS"}else{"FAIL"});restore_status=$(if($restoreFailed){"FAIL"}else{"PASS"});errors=@($runError,$(if($restoreFailed){"SETTINGS_RESTORE_FAILED"}else{$null})|Where-Object{$_});sections=$rows;privacy="voice id, text, profile fingerprints, reference bytes and audio paths omitted";owner_gates="OWNER TEST PENDING"}|ConvertTo-Json -Depth 5|Set-Content -Encoding UTF8 (Join-Path $run "metrics.json")
}
$clock.Stop()
if($restoreFailed -or $runError){
  Write-Output ("FAIL longform elapsed_s="+$clock.Elapsed.TotalSeconds.ToString('0.000',[cultureinfo]::InvariantCulture)+" output="+$run)
  exit 2
}
Write-Output ("PASS longform elapsed_s="+$clock.Elapsed.TotalSeconds.ToString('0.000',[cultureinfo]::InvariantCulture)+" output="+$run)
