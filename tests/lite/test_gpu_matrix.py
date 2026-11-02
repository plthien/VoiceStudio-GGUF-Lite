"""Deterministic Vulkan/NVIDIA inventory regressions."""
import subprocess
import pytest
from lite.runtime import nvidia_device,RuntimeErrorLite

NVIDIA="""GPU0:
deviceName = NVIDIA GeForce GTX 1650
deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
vendorID = 0x10de
"""
AMD_FIRST="""GPU0:
deviceName = AMD Radeon 780M
deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
vendorID = 0x1002
GPU1:
deviceName = NVIDIA GeForce GTX 1650
deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
vendorID = 0x10de
"""

def fake_probe(vulkan,rows="NVIDIA GeForce GTX 1650, 4096\n",returncode=0):
    def run(args,**kwargs):
        if "vulkaninfo" in args[0]:
            return subprocess.CompletedProcess(args,returncode,vulkan,"")
        return subprocess.CompletedProcess(args,0,rows,"")
    return run

def which(x): return x

def test_verified_single_nvidia_inventory():
    result=nvidia_device(run=fake_probe(NVIDIA),which=which)
    assert result["vram_mib"]==4096
    assert result["vendor_id"]=="0x10de"

@pytest.mark.parametrize("vulkan,rows",[
    (AMD_FIRST,"NVIDIA GeForce GTX 1650, 4096\n"),
    (NVIDIA,"NVIDIA GeForce GTX 1650, 0\n"),
    (NVIDIA,"NVIDIA GeForce GTX 1650, 8192\n"),
    (NVIDIA,"NVIDIA GeForce GTX 1650, 4096\nNVIDIA RTX 3070, 8192\n"),
    ("","NVIDIA GeForce GTX 1650, 4096\n"),
])
def test_ambiguous_or_wrong_gpu_fails(vulkan,rows):
    with pytest.raises(RuntimeErrorLite):
        nvidia_device(run=fake_probe(vulkan,rows),which=which)

