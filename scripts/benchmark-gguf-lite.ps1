param([Parameter(Mandatory=$true)][string]$VoiceId,[int]$Repetitions=3,[int]$Port=3900,[string]$Output="D:\VoiceStudioBuild\lite\benchmarks")
$ErrorActionPreference="Stop"
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$uri = "http://127.0.0.1:$Port"
$ready = Invoke-RestMethod "$uri/lite/readiness"
if (!$ready.ready -or $ready.quant.in_use -ne "omnivoice-base-Q8_0.gguf" -or $ready.gpu.backend -ne "Vulkan0") { throw "Lite not ready as pinned Vulkan0/Q8_0" }
$results = @()
for ($i=1; $i -le $Repetitions; $i++) {
  $body = @{model="omnivoice-gguf";voice=$VoiceId;input="A quiet morning begins beside the river.";response_format="wav";speed=1.0;language="en";seed=42} | ConvertTo-Json -Compress
  $dest = Join-Path $Output ("run-{0:D2}.wav" -f $i)
  $start = Get-Date
  Invoke-WebRequest -Method Post -Uri "$uri/v1/audio/speech" -ContentType "application/json" -Body $body -OutFile $dest
  $elapsed = ((Get-Date)-$start).TotalSeconds
  $bytes = [IO.File]::ReadAllBytes($dest)
  if ($bytes.Length -lt 46 -or [Text.Encoding]::ASCII.GetString($bytes,0,4) -ne "RIFF") { throw "Bad WAV output $i" }
  $sampleRate = [BitConverter]::ToInt32($bytes,24)
  if ($sampleRate -ne 24000) { throw "Unexpected native sample rate" }
  # Standard PCM16 WAV output; use Python wav_info for final audio duration/RTF.
  $python = Join-Path "D:\VoiceStudioBuild\lite" ".venv-lite\Scripts\python.exe"
  if (!(Test-Path $python)) { throw "Missing isolated Lite Python for PCM16 metrics" }
  $metricsText = & $python -m lite.audio_metrics $dest
  if ($LASTEXITCODE -ne 0) { throw "WAV metrics failed" }
  $metrics = $metricsText | ConvertFrom-Json
  $rtf = $elapsed / $metrics.duration_s
  $results += @{run=$i;elapsed_seconds=$elapsed;bytes=$bytes.Length;wav=$dest;audio_seconds=$metrics.duration_s;rtf=$rtf;metrics=$metrics}
}
$meanRTF = ($results | Measure-Object -Property rtf -Average).Average
$report = @{preflight=$ready;voice_id=$VoiceId;runs=$results;repetitions=$Repetitions;mean_rtf=$meanRTF;baseline_rtf=3.291;baseline_source="Prior full VoiceStudio Q8_0 3 runs; NOT Lite"}
$report | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 (Join-Path $Output "benchmark.json")
Write-Host "Generated $Repetitions WAV(s). Results are private under $Output."
