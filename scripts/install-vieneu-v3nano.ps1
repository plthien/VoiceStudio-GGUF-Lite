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
    # Compare pinned packages/versions/VCS revisions, not PowerShell line endings.
    # Windows PowerShell 5.1 serializes text as UTF-8 BOM + CRLF while pip
    # freeze returns a line array. Keep exact case-sensitive entries and fail closed.
    $lock=Join-Path $path 'private-dependency-lock.txt'
    if(!(Test-Path -LiteralPath $lock -PathType Leaf)){throw 'VIENEU_DEPENDENCY_LOCK_MISSING'}
    $pinned=@(Get-Content -LiteralPath $lock -Encoding UTF8 |
      ForEach-Object { $_.Trim().TrimStart([char]0xFEFF) } |
      Where-Object { $_ -ne '' } | Sort-Object -CaseSensitive)
    $current=@(& $py -m pip freeze)
    if($LASTEXITCODE -ne 0){throw 'VIENEU_PIP_FREEZE_FAILED'}
    $current=@($current | ForEach-Object { $_.Trim().TrimStart([char]0xFEFF) } |
      Where-Object { $_ -ne '' } | Sort-Object -CaseSensitive)
    if($pinned.Count -eq 0){throw 'VIENEU_DEPENDENCY_LOCK_EMPTY'}
    $difference=@(Compare-Object -ReferenceObject $pinned -DifferenceObject $current -CaseSensitive)
    if($difference.Count -gt 0){
      # <= appears only in saved lock, => appears only in live environment.
      Write-Warning ("VieNeu dependency differences:" + [Environment]::NewLine +
        (($difference | ForEach-Object { $_.SideIndicator + ' ' + $_.InputObject }) -join [Environment]::NewLine))
      throw 'VIENEU_DEPENDENCY_LOCK_CHANGED'
    }
    $args=@('-m','lite.vieneu_install','verify')
    if($Smoke){$args+='--smoke'}
    & $py @args
    if($LASTEXITCODE -ne 0){throw 'VIENEU_OFFLINE_VERIFICATION_FAILED'}
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
