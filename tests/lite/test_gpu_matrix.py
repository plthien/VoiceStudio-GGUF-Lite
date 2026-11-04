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


# Matches the actual Vulkan SDK 1.4.363 Windows --summary layout after the
# host disabled only VK_LAYER_AMD_switchable_graphics. GPU0 is GTX 1650;
# AMD Radeon remains GPU1. Ignore unrelated warning and instance metadata.
WINDOWS_INDENTED_SUMMARY = """==========
VULKANINFO
==========

Vulkan Instance Version: 1.4.363
Instance Layers: count = 11
VK_LAYER_NV_optimus NVIDIA Optimus layer 1.4.351

Devices:
========
GPU0:
        apiVersion         = 1.4.351
        vendorID           = 0x10de
        deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
        deviceName         = NVIDIA GeForce GTX 1650
GPU1:
        apiVersion         = 1.2.133
        vendorID           = 0x1002
        deviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
        deviceName         = AMD Radeon(TM) Graphics
"""

def test_windows_indented_vulkan_summary_identifies_gpu0_gtx1650():
    from lite.runtime import parse_vulkan_devices
    devices = parse_vulkan_devices(WINDOWS_INDENTED_SUMMARY)
    assert len(devices) == 2
    assert [(d["vk_index"], d["name"], d["vendor_id"]) for d in devices] == [
        (0, "NVIDIA GeForce GTX 1650", "0x10de"),
        (1, "AMD Radeon(TM) Graphics", "0x1002"),
    ]
    gpu = nvidia_device(run=fake_probe(WINDOWS_INDENTED_SUMMARY), which=which)
    assert gpu["backend"] == "Vulkan0"
    assert gpu["verified"] is True
    assert gpu["vk_index"] == 0
    assert gpu["vram_mib"] == 4096

def test_indented_gpu1_nvidia_does_not_bypass_gpu0_amd():
    summary = """GPU0:
        vendorID = 0x1002
        deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
        deviceName = AMD Radeon(TM) Graphics
GPU1:
        vendorID = 0x10de
        deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
        deviceName = NVIDIA GeForce GTX 1650
"""
    with pytest.raises(RuntimeErrorLite, match="Vulkan0 is not the verified NVIDIA GTX 1650"):
        nvidia_device(run=fake_probe(summary), which=which)

def test_incomplete_gpu0_fails_closed_even_if_gpu1_is_nvidia():
    summary = """GPU0:
        vendorID = 0x1002
        deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
GPU1:
        vendorID = 0x10de
        deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
        deviceName = NVIDIA GeForce GTX 1650
"""
    with pytest.raises(RuntimeErrorLite, match="Vulkan0 is not unambiguously identified"):
        nvidia_device(run=fake_probe(summary), which=which)
