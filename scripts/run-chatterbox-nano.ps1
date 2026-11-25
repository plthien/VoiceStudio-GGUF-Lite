[CmdletBinding()]
param([string]$Root='D:\VoiceStudioBuild\chatterbox-nano',[int]$Port=3901)
$ErrorActionPreference='Stop'
$path=[IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($path -ne 'D:\VoiceStudioBuild\chatterbox-nano') { throw 'Nano installation must use D:\VoiceStudioBuild\chatterbox-nano' }
if($Port -lt 1 -or $Port -gt 65535){throw 'INVALID_PORT'}
$python=Join-Path $path '.venv-chatterbox\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)){throw 'ENV_NOT_INSTALLED'}
$env:CHATTERBOX_ROOT=$path
$env:CHATTERBOX_PORT=[string]$Port
$env:PYTHONPATH=Split-Path -Parent $PSScriptRoot
$env:HF_HOME=Join-Path $path 'cache\hf'
$env:HF_HUB_CACHE=Join-Path $path 'cache\hf\hub'
$env:TORCH_HOME=Join-Path $path 'cache\torch'
$env:TMP=Join-Path $path 'cache\tmp'
$env:TEMP=$env:TMP
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
# Nano is a dedicated CUDA0-only runtime.
# Do not inherit incompatible GPU visibility from other AI environments.
$env:CUDA_DEVICE_ORDER = "PCI_BUS_ID"
$env:CUDA_VISIBLE_DEVICES = "0"
Remove-Item Env:PYTORCH_NVML_BASED_CUDA_CHECK -ErrorAction SilentlyContinue

# Probe the pinned runtime before starting the service. No model load or inference.
$cudaPreflight = @'
import os
import sys

print('VoiceStudio Nano CUDA preflight', flush=True)
visibility = os.environ.get('CUDA_VISIBLE_DEVICES', '')
print(f'CUDA_VISIBLE_DEVICES: {visibility}', flush=True)
try:
    import torch
    available = torch.cuda.is_available()
    count = torch.cuda.device_count()
    print(f'CUDA device count: {count}', flush=True)
    if not available or count != 1:
        raise RuntimeError(f'Expected exactly one available CUDA device, got available={available}, count={count}')
    torch.cuda.set_device(0)
    if torch.cuda.current_device() != 0:
        raise RuntimeError('CUDA device 0 is not current')
    properties = torch.cuda.get_device_properties(0)
    if not properties.name:
        raise RuntimeError('CUDA device 0 has no name')
    print(f'CUDA device: {properties.name}', flush=True)
except Exception as exc:
    print(f'CUDA preflight: FAIL ({type(exc).__name__}: {exc})', flush=True)
    print('Check nvidia-smi, the NVIDIA driver, and CUDA-enabled PyTorch in the pinned Nano environment.', flush=True)
    sys.exit(1)
print('CUDA preflight: PASS', flush=True)
'@
& $python -c $cudaPreflight
if ($LASTEXITCODE -ne 0) { throw "NANO_CUDA_PREFLIGHT_FAILED (exit $LASTEXITCODE): check the CUDA diagnostics above; Uvicorn was not started." }

Write-Output "VoiceStudio Nano Lite: http://127.0.0.1:$Port/ui"
& $python -m uvicorn lite.chatterbox_app:app --host 127.0.0.1 --port $Port --no-access-log
if ($LASTEXITCODE -ne 0) { throw "NANO_SERVICE_EXIT_$LASTEXITCODE" }
