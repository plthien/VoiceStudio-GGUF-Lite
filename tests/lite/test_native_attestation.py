"""Native GPU attestation must reject self-reported env and ambiguous logs."""
import pytest
from lite.native_runner import native_attestation,NativeFailure

ADAPTER={"name":"NVIDIA GeForce GTX 1650","vendor_id":"0x10de"}
VALID=b"[LiteAttest/v1] backend=Vulkan0; device=Vulkan0; description=NVIDIA GeForce GTX 1650\n"

def test_genuine_schema_contract():
    result=native_attestation(VALID,ADAPTER)
    assert result["observed_native_backend"]=="Vulkan0"
    assert result["verification_status"]=="verified"

@pytest.mark.parametrize("stderr",[
    b"",b"GGML_BACKEND=Vulkan0\n",b"GPU0: NVIDIA Vulkan0\n",
    b"[LiteAttest/v1] backend=CPU; device=CPU; description=NVIDIA GeForce GTX 1650\n",
    b"[LiteAttest/v1] backend=Vulkan1; device=Vulkan1; description=NVIDIA GeForce GTX 1650\n",
    b"[LiteAttest/v1] backend=Vulkan0; device=Vulkan0; description=AMD Radeon 780M\n",
    VALID+VALID,VALID+b"[LiteAttest/v1] malformed\n",b"x"*131073,
], ids=["empty","requested-env-only","discovery-only","cpu",
        "vulkan1","amd","duplicate","malformed","oversize-131073"])
def test_rejects_missing_cpu_amd_vulkan1_duplicate_and_overflow(stderr):
    with pytest.raises(NativeFailure):
        native_attestation(stderr,ADAPTER)

def test_conflicting_preflight_fails():
    with pytest.raises(NativeFailure):
        native_attestation(VALID,{"name":"AMD Radeon","vendor_id":"0x1002"})

