# Installs or verifies the immutable Nano runtime in its isolated D: root.
[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-nano',
      [switch]$SkipDownload, [switch]$VerifyOnly)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$SourceSha='ab55923c27757c90cfcc407baa57dc25a7d25217'
$ModelSha='71ccd1d0081b430592cea481f4307e764e07bc64'
$ModelRepo='ResembleAI/chatterbox-nano'
$Required=@('t3_nano_v1.safetensors','s3gen_meanflow.safetensors','ve.safetensors',
  'added_tokens.json','merges.txt','vocab.json','special_tokens_map.json','tokenizer_config.json')
$Expected=@{
  't3_nano_v1.safetensors'='72b110185087d945dbdf54dee4e333848e1811bdd5fd6cb16ceb8da50006f0c9'
  's3gen_meanflow.safetensors'='d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f'
  've.safetensors'='f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c'
  'added_tokens.json'='72e4ab6acb0d9309ac3df4b526ae5fd80a2da5bc5ab7bb02d85096a374f69193'
  'merges.txt'='1ce1664773c50f3e0cc8842619a93edc4624525b728b188a9e0be33b7726adc5'
  'vocab.json'='f6bd25a65e4e63ca31360e9fb11c7e4f9a391a78385d640acd814092dd6eee4f'
  'special_tokens_map.json'='92ba8063bf40aa163eadebbfe0de07c2aebe44cf0d4a9e8726580b0781fd2640'
  'tokenizer_config.json'='bca16a2ac1ddbd78b8d6228f0031884cc74b6ea54b967d6f6d2ebae9ccde23e6'
}
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-nano') {throw 'NANO_D_DRIVE_ROOT_REQUIRED'}
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'internal\chatterbox-sha256.ps1')
. (Join-Path $PSScriptRoot 'internal\chatterbox-verify-model.ps1')
$model=Join-Path $path ('models\'+$ModelSha)
$cache=Join-Path $path 'cache'
$venv=Join-Path $path '.venv-chatterbox'
$py=Join-Path $venv 'Scripts\python.exe'
$manifest=Join-Path $model 'manifest.json'
function Set-NanoEnvironment {
  $env:CHATTERBOX_ROOT=$path
  $env:HF_HOME=Join-Path $cache 'hf'
  $env:HF_HUB_CACHE=Join-Path $cache 'hf\hub'
  $env:TORCH_HOME=Join-Path $cache 'torch'
  $env:TEMP=Join-Path $cache 'tmp'; $env:TMP=$env:TEMP
  $env:PIP_CACHE_DIR=Join-Path $cache 'pip'
}
function Verify-Files {
  Test-ChatterboxModelSnapshot -ModelDirectory $model -ManifestPath $manifest `
    -RequiredFiles $Required -ExpectedHashes $Expected -ModelRepo $ModelRepo `
    -ModelRevision $ModelSha -SourceRevision $SourceSha | Out-Null
}
Set-NanoEnvironment
if($VerifyOnly) {
  $env:HF_HUB_OFFLINE='1';$env:TRANSFORMERS_OFFLINE='1'
  Verify-Files
  Assert-ChatterboxVerifyPrerequisites -PythonPath $py | Out-Null
  $env:PYTHONPATH=$repo
  & $py -m lite.chatterbox_config --verify
  if ($LASTEXITCODE -ne 0) {throw 'NANO_PREFLIGHT_FAILED'}
  Write-Output 'Verified immutable Chatterbox-Nano snapshot offline'
  exit 0
}
New-Item -ItemType Directory -Force -Path $path,$model,$cache,(Join-Path $cache 'tmp') | Out-Null
if (!(Test-Path -LiteralPath $py)) {
  & py -3.11 -m venv $venv
  if ($LASTEXITCODE -ne 0) {throw 'PYTHON_311_REQUIRED'}
}
& $py -m pip install --upgrade 'pip==25.1.1'
if($LASTEXITCODE -ne 0) {throw 'PIP_FAILED'}
& $py -m pip install --index-url https://download.pytorch.org/whl/cu124 'torch==2.6.0' 'torchaudio==2.6.0'
if($LASTEXITCODE -ne 0) {throw 'TORCH_CUDA_WHEEL_UNAVAILABLE'}
& $py -m pip install -r (Join-Path $repo 'requirements-chatterbox.txt')
if($LASTEXITCODE -ne 0) {throw 'NANO_DEPENDENCIES_FAILED'}
& $py -m pip install --no-deps ("git+https://github.com/resemble-ai/Perth.git@ff1c8ac55a976971245cdd53c18d6131ca00d993")
if($LASTEXITCODE -ne 0) {throw 'PINNED_PERTH_FAILED'}
& $py -m pip install --no-deps ("git+https://github.com/resemble-ai/chatterbox.git@"+$SourceSha)
if($LASTEXITCODE -ne 0) {throw 'PINNED_CHATTERBOX_FAILED'}
& $py -m pip freeze | Set-Content -Encoding ASCII (Join-Path $path 'private-dependency-lock.txt')
& $py (Join-Path $repo 'scripts\internal\chatterbox-dependency-inventory.py') --output (Join-Path $path 'private-dependency-inventory.json')
if ($LASTEXITCODE -ne 0) {throw 'DEPENDENCY_INVENTORY_FAILED'}
if (!$SkipDownload) {
  & $py (Join-Path $repo 'scripts\internal\download-chatterbox-model.py') --root $path
  if ($LASTEXITCODE -ne 0) {throw 'PINNED_MODEL_DOWNLOAD_FAILED'}
}
Verify-Files
$env:PYTHONPATH=$repo
& $py -m lite.chatterbox_config --verify
if($LASTEXITCODE -ne 0) {throw 'NANO_PREFLIGHT_FAILED'}
Write-Output 'Chatterbox-Nano installed. GGUF is not started or required.'
