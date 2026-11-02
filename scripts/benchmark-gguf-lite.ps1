param([Parameter(Mandatory=$true)][string]$VoiceId,[int]$Repetitions=3,[int]$Port=3900,[string]$Output="D:\VoiceStudioBuild\lite\benchmarks")
$ErrorActionPreference="Stop"
if ($Repetitions -ne 3) { throw "Frozen Ink benchmark requires exactly 3 runs" }
$narration="You lift the lid, toss in a banana peel, and forget about it. But when Paris tried to get people to use covered garbage bins in the eighteen eighties, people got angry. Landlords complained. Newspapers made jokes. And workers who earned their living from discarded stuff worried they might lose their jobs."
if ($narration.Length -ne 306) { throw "Narration must remain exactly 306 characters" }
$uri="http://127.0.0.1:$Port"
$ready=Invoke-RestMethod "$uri/lite/readiness"
if (!$ready.ready -or !$ready.quant.verified) { throw "Pinned Q8_0 Lite readiness not verified" }
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$results=@()
for ($i=1;$i -le 3;$i++) {
  $body=@{model="omnivoice-gguf";voice=$VoiceId;input=$narration;response_format="wav";speed=1.0;language="en";seed=42}|ConvertTo-Json -Compress
  $dest=Join-Path $Output ("run-{0:D2}.wav" -f $i)
  $watch=[Diagnostics.Stopwatch]::StartNew()
  Invoke-WebRequest -Method Post -Uri "$uri/v1/audio/speech" -ContentType "application/json" -Body $body -OutFile $dest
  $watch.Stop()
  $python="D:\VoiceStudioBuild\lite\.venv-lite\Scripts\python.exe"
  $metrics=& $python -m lite.audio_metrics $dest | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or !$metrics.duration_s) { throw "Invalid output WAV" }
  $results+=@{run=$i;elapsed_s=$watch.Elapsed.TotalSeconds;audio_s=$metrics.duration_s;rtf=$watch.Elapsed.TotalSeconds/$metrics.duration_s;sha256=(Get-FileHash $dest -Algorithm SHA256).Hash.ToLowerInvariant();metrics=$metrics}
}
$mean=($results|Measure-Object -Property rtf -Average).Average
$report=@{benchmark_case_id="Ink-Explainer-306";character_count=306;seed=42;profile_id=$VoiceId;runs=$results;mean_rtf=$mean;historical_full_voice_studio_mean_rtf=3.291;comparability="not_comparable";reason="Same reference audio/transcript and DSP policy not yet evidenced";listening_review="NOT VERIFIED"}
$report|ConvertTo-Json -Depth 12|Set-Content -Encoding utf8 (Join-Path $Output "benchmark.json")
Write-Host "Saved private 3-run benchmark; original A/B comparability NOT VERIFIED"

