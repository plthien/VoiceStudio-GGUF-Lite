# Installs pinned Chatterbox source and pinned snapshot into D: only; owner-host opt-in.
[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-turbo',
      [switch]$SkipDownload,
      [switch]$VerifyOnly)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$SourceSha='5de7a54aa4e5e2baadb0182dde554908b48b85c2'
$ModelSha='74971fa28ff1cdf9a2cf4b41d102cb31fd32d411'
$ModelRepo='ResembleAI/chatterbox-turbo'
$Required=@('t3_turbo_v1.safetensors','s3gen_meanflow.safetensors','ve.safetensors',
  'added_tokens.json','merges.txt','vocab.json','special_tokens_map.json','tokenizer_config.json')
$Expected=@{
  't3_turbo_v1.safetensors'='fcf1f8c1d651bb7e3acd69ee5be269b4ac10c02980b7708213d598bc9f7cdf87'
  's3gen_meanflow.safetensors'='d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f'
}
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-turbo') {
  throw 'Root must be D:\VoiceStudioBuild\chatterbox-turbo (D:-only isolation)'
}
$repo=Split-Path -Parent $PSScriptRoot
$model=Join-Path $path ('models\'+$ModelSha)
$cache=Join-Path $path 'cache'
$venv=Join-Path $path '.venv-chatterbox'
$py=Join-Path $venv 'Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $path,$model,$cache,(Join-Path $cache 'tmp') | Out-Null
$env:CHATTERBOX_ROOT=$path
$env:HF_HOME=Join-Path $cache 'hf'
$env:HF_HUB_CACHE=Join-Path $cache 'hf\hub'
$env:TORCH_HOME=Join-Path $cache 'torch'
$env:TEMP=Join-Path $cache 'tmp'
$env:TMP=$env:TEMP
$env:PIP_CACHE_DIR=Join-Path $cache 'pip'
$manifest=Join-Path $model 'manifest.json'
function Verify-Files {
  if (!(Test-Path $manifest)) { throw 'MODEL_MANIFEST_UNAVAILABLE' }
  $m=Get-Content -Raw $manifest | ConvertFrom-Json
  if ($m.revision -ne $ModelSha -or $m.source_revision -ne $SourceSha) {
    throw 'MODEL_REVISION_MISMATCH'
  }
  foreach($name in $Required) {
    $file=Join-Path $model $name
    if(!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "MODEL_FILES_MISSING: $name" }
    $digest=(Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant()
    if($digest -ne $m.sha256.$name) { throw "MODEL_HASH_MISMATCH: $name" }
    if($Expected.ContainsKey($name) -and $digest -ne $Expected[$name]) { throw "MODEL_PIN_MISMATCH: $name" }
  }
}
if($VerifyOnly) { Verify-Files; Write-Output 'Verified pinned Turbo model files'; exit 0 }
if (!(Test-Path $py)) {
  & py -3.11 -m venv $venv
  if ($LASTEXITCODE -ne 0) { throw 'PYTHON_311_REQUIRED' }
}
& $py -m pip install --upgrade 'pip==25.1.1'
if($LASTEXITCODE -ne 0) { throw 'PIP_FAILED' }
# Preinstall the CUDA wheels separately; never mutate GGUF Lite's .venv-lite.
& $py -m pip install --index-url https://download.pytorch.org/whl/cu124 'torch==2.6.0' 'torchaudio==2.6.0'
if($LASTEXITCODE -ne 0) { throw 'TORCH_CUDA_WHEEL_UNAVAILABLE' }
& $py -m pip install -r (Join-Path $repo 'requirements-chatterbox.txt')
if($LASTEXITCODE -ne 0) { throw 'CHATTERBOX_DEPENDENCIES_FAILED' }
# Upstream source, and the Perth dependency's own immutable ref; exclude Gradio UI dependencies.
& $py -m pip install --no-deps ("git+https://github.com/resemble-ai/Perth.git@ff1c8ac55a976971245cdd53c18d6131ca00d993")
if($LASTEXITCODE -ne 0) { throw 'PINNED_PERTH_FAILED' }
& $py -m pip install --no-deps ("git+https://github.com/resemble-ai/chatterbox.git@"+$SourceSha)
if($LASTEXITCODE -ne 0) { throw 'PINNED_CHATTERBOX_FAILED' }
& $py -m pip freeze | Set-Content -Encoding ASCII (Join-Path $path 'private-dependency-lock.txt')
& $py (Join-Path $repo 'scripts\internal\chatterbox-dependency-inventory.py') --output (Join-Path $path 'private-dependency-inventory.json')
if ($LASTEXITCODE -ne 0) { throw 'DEPENDENCY_INVENTORY_FAILED' }
if (!$SkipDownload) {
  $downloader=Join-Path $repo 'scripts\internal\download-chatterbox-model.py'
  & $py $downloader --root $path
  if ($LASTEXITCODE -ne 0) { throw 'PINNED_MODEL_DOWNLOAD_FAILED' }
}
Verify-Files
$env:PYTHONPATH=$repo
& $py -m lite.chatterbox_config --verify
if($LASTEXITCODE -ne 0) { throw 'CHATTERBOX_PREFLIGHT_FAILED' }
Write-Output 'Chatterbox Turbo installed. Stop GGUF Vulkan worker before live CUDA use.'
