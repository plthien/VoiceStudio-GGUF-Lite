# Run from Windows 10 x64 PowerShell; install is the only phase allowed internet access.
[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\vieneu-v3nano',
      [switch]$VerifyOnly, [switch]$SkipDownload, [switch]$Smoke)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$expected=[IO.Path]::GetFullPath('D:\VoiceStudioBuild\vieneu-v3nano').TrimEnd('\')
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne $expected) { throw 'VIENEU_FIXED_ROOT_REQUIRED' }
if (-not [Environment]::Is64BitOperatingSystem -or $env:OS -ne 'Windows_NT') { throw 'WINDOWS_X64_REQUIRED' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'FFMPEG_REQUIRED' }
$repo=Split-Path -Parent $PSScriptRoot
$venv=Join-Path $path '.venv-vieneu'
$py=Join-Path $venv 'Scripts\python.exe'
$env:VIENEU_ROOT=$path
$env:PYTHONPATH=$repo
$env:HF_HOME=Join-Path $path 'cache\hf'
$env:HF_HUB_CACHE=Join-Path $path 'cache\hf\hub'
$env:HF_HUB_DISABLE_TELEMETRY='1'
$env:PIP_CACHE_DIR=Join-Path $path 'cache\pip'
$env:CUDA_VISIBLE_DEVICES=''
if($VerifyOnly){
    if(!(Test-Path -LiteralPath $py -PathType Leaf)){throw 'ENV_NOT_INSTALLED'}
    $env:HF_HUB_OFFLINE='1'
    $env:TRANSFORMERS_OFFLINE='1'
    $args=@('-m','lite.vieneu_install','verify')
    if($Smoke){$args+='--smoke'}
    & $py @args
    if($LASTEXITCODE -ne 0){throw 'VIENEU_OFFLINE_VERIFICATION_FAILED'}
    $lock=Join-Path $path 'private-dependency-lock.txt'
    if(!(Test-Path -LiteralPath $lock)){throw 'VIENEU_DEPENDENCY_LOCK_MISSING'}
    $current=(& $py -m pip freeze) -join "`n"
    $pinned=(Get-Content -LiteralPath $lock -Raw).Trim()
    if($current.Trim() -ne $pinned){throw 'VIENEU_DEPENDENCY_LOCK_CHANGED'}
    Write-Output 'VieNeu v3 Nano verified OFFLINE'
    exit 0
}
$env:HF_HUB_OFFLINE='0'
$env:TRANSFORMERS_OFFLINE='0'
New-Item -ItemType Directory -Force -Path $path,(Join-Path $path 'models'),(Join-Path $path 'cache'),(Join-Path $path 'logs'),(Join-Path $path 'cache\staging'),(Join-Path $path 'cache\jobs') | Out-Null
if(!(Test-Path -LiteralPath $py)){
    & py -3.11 -m venv $venv
    if($LASTEXITCODE -ne 0){throw 'PYTHON_311_REQUIRED'}
}
& $py -m pip install -r (Join-Path $repo 'requirements-vieneu.txt')
if($LASTEXITCODE -ne 0){throw 'VIENEU_DEPENDENCIES_FAILED'}
& $py -m pip install --no-deps 'git+https://github.com/pnnbao97/VieNeu-TTS.git@6566468cf14974807050495036ab75cd041143f7'
if($LASTEXITCODE -ne 0){throw 'PINNED_VIENEU_SDK_FAILED'}
& $py -m lite.vieneu_install check-sdk
if($LASTEXITCODE -ne 0){throw 'VIENEU_PINNED_SDK_VERIFY_FAILED'}
if($SkipDownload) {
    Write-Output 'Skipped model download; offline verification is required before generation.'
} else {
    & $py -m lite.vieneu_install download
    if($LASTEXITCODE -ne 0){throw 'VIENEU_MODEL_DOWNLOAD_FAILED'}
}
if($Smoke -and !$SkipDownload) {
    $env:HF_HUB_OFFLINE='1'
    & $py -m lite.vieneu_install verify --smoke
    if($LASTEXITCODE -ne 0){throw 'VIENEU_LOCAL_SMOKE_FAILED'}
}
& $py -m pip freeze | Set-Content -Encoding UTF8 (Join-Path $path 'private-dependency-lock.txt')
Write-Output 'VieNeu runtime installed. Run scripts\verify-vieneu-v3nano.ps1 offline before launch.'
