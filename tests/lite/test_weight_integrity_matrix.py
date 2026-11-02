"""No model downloads: synthetic trusted digest, revision and GGUF negative matrix."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import pytest
from lite.config import Settings, BASE_NAME,TOKENIZER_NAME,MODEL_REVISION
from lite import runtime

def gguf(magic=b"GGUF",version=3):
    return magic+version.to_bytes(4,"little")+(1).to_bytes(8,"little")+(1).to_bytes(8,"little")+b"test"*32

@pytest.fixture
def pair(tmp_path,monkeypatch):
    s=Settings(data_dir=tmp_path/"lite",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    folder=s.cache_dir/"models--Serveurperso--OmniVoice-GGUF"/"snapshots"/MODEL_REVISION
    folder.mkdir(parents=True)
    base=folder/BASE_NAME
    token=folder/TOKENIZER_NAME
    base.write_bytes(gguf()+b"base")
    token.write_bytes(gguf()+b"token")
    pinned={"base":{"sha256":hashlib.sha256(base.read_bytes()).hexdigest()},
            "tokenizer":{"sha256":hashlib.sha256(token.read_bytes()).hexdigest()}}
    monkeypatch.setattr(runtime,"_expected_quantmap",lambda: pinned)
    return s,base,token,pinned

def test_synthetic_pair_matches_only_explicit_trusted_hash(pair):
    settings,base,token,_=pair
    assert runtime.model_pair(settings)==(base.resolve(),token.resolve())

@pytest.mark.parametrize("mutated",["base-hash","tokenizer-hash","header","bad-version","truncated"])
def test_wrong_weights_rejected_even_with_pinned_filename(pair,mutated):
    settings,base,token,pins=pair
    if mutated=="base-hash":
        base.write_bytes(base.read_bytes()+b"mutated")
    elif mutated=="tokenizer-hash":
        token.write_bytes(token.read_bytes()+b"mutated")
    elif mutated=="header":
        token.write_bytes(gguf(b"FAKE")+b"token")
    elif mutated=="bad-version":
        base.write_bytes(gguf(version=0)+b"base")
    else:
        token.write_bytes(b"GGUF")
    with pytest.raises(runtime.RuntimeErrorLite):
        runtime.model_pair(settings)

def test_wrong_quant_and_missing_tokenizer_are_not_fallback(pair):
    settings,base,token,pins=pair
    token.unlink()
    (base.parent/"omnivoice-tokenizer-Q4_K_M.gguf").write_bytes(gguf())
    with pytest.raises(runtime.RuntimeErrorLite):
        runtime.model_pair(settings)

def test_cache_symlink_outside_is_rejected(pair,tmp_path):
    settings,base,token,_=pair
    outside=tmp_path/"outside.gguf"
    outside.write_bytes(base.read_bytes())
    base.unlink()
    try:
        base.symlink_to(outside)
    except (OSError,NotImplementedError):
        # Windows runner can lack unprivileged symlink permissions.
        # The path check remains exercised on Linux; no test silently passes.
        base.write_bytes(gguf(b"FAKE"))
    with pytest.raises(runtime.RuntimeErrorLite):
        runtime.model_pair(settings)

def test_revision_and_source_provenance_status_fail_closed(tmp_path,monkeypatch):
    root=tmp_path
    resource=root/"lite/resources"
    resource.mkdir(parents=True)
    true=json.loads((runtime.ROOT/"lite/resources/q8_pins.json").read_text())
    for field,value in (("model_revision","deadbeef"*5),
                        ("source_hash_provenance","NOT_VERIFIED"),
                        ("quant","Q4_K_M")):
        broken=dict(true)
        broken[field]=value
        (resource/"q8_pins.json").write_text(json.dumps(broken))
        monkeypatch.setattr(runtime,"ROOT",root)
        with pytest.raises(runtime.RuntimeErrorLite):
            runtime._expected_quantmap()
    monkeypatch.undo()

def test_installed_does_not_claim_in_use_or_verified_before_hashes(pair,monkeypatch):
    s,base,token,_=pair
    monkeypatch.setattr(runtime,"native_files",lambda settings:(Path("exe"),Path("dll")))
    monkeypatch.setattr(runtime,"nvidia_device",lambda:{"verified":True,"backend":"Vulkan0"})
    state=runtime.preflight(s)
    assert state["ready"] and state["quant"]["installed"]
    assert state["quant"]["verified"] and state["quant"]["prepared_quant"]=="Q8_0"
    assert state["quant"]["in_use"] is None
