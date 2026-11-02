"""Windows-only real PowerShell candidate activation and rollback, using fake PE fixtures.

No owner data, GPU, licence or live installed bin touched. Failure paths
must keep active runtime bytes intact and never auto-enable experimental speed.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from lite.config import ROOT,RUNTIME_REVISION,NATIVE_NAME,VULKAN_NAME

SCRIPT=ROOT/"scripts/switch-gguf-lite-runtime.ps1"

def make_bin(folder:Path,speed:bool):
    folder.mkdir(parents=True,exist_ok=False)
    paths=[folder/NATIVE_NAME,folder/VULKAN_NAME,folder/"ggml.dll",folder/"ggml-base.dll"]
    for idx,p in enumerate(paths):
        p.write_bytes(b"MZ"+bytes([idx+9])*300)
    folder.joinpath("checksums.sha256").write_text("".join(
        hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.name+"\n" for p in paths))
    if speed:
        sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        folder.joinpath("speed-capability.json").write_text(json.dumps({
            "schema":"lite-native-speed-v1","runtime_revision":RUNTIME_REVISION,
            "exe_sha256":sha(paths[0]),
            "patch_sha256":sha(ROOT/"scripts/internal/instrument-native-speed.py"),
            "header_sha256":sha(ROOT/"scripts/internal/lite-native-speed.h")
        }))
    return paths

@pytest.mark.skipif(os.name!="nt",reason="Owner switch is Windows PowerShell only")
def test_speed_candidate_owner_activation_and_rollback(tmp_path):
    pwsh=shutil.which("pwsh")
    assert pwsh, "Windows CI requires PowerShell 7"
    base=tmp_path/"runtime"/"bin"
    candidate=tmp_path/"candidate"/"bin"
    back=tmp_path/"backups"/"old-bin"
    original=make_bin(base,False)
    make_bin(candidate,True)
    known=hashlib.sha256(original[0].read_bytes()).hexdigest()
    untouched=tmp_path/"data"/"voice.bin";untouched.parent.mkdir();untouched.write_bytes(b"private-fixture")
    def call(mode,approval=True):
        cmd=[pwsh,"-NoProfile","-File",str(SCRIPT),"-Mode",mode,
             "-ActiveBin",str(base),"-BackupBin",str(back)]
        if mode=="Activate":cmd+=["-CandidateBin",str(candidate)]
        if approval:cmd+=["-OwnerApproves"]
        return subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    denied=call("Activate",False)
    assert denied.returncode!=0 and base.exists() and candidate.exists() and not back.exists()
    files=make_bin if False else None
    # Never permit a forged or corrupted candidate manifest.
    tamper=candidate/VULKAN_NAME
    tamper.write_bytes(b"MZ"+b"modified"*60)
    failed=call("Activate")
    assert failed.returncode!=0 and base.exists() and not back.exists()
    tamper.write_bytes(b"MZ"+bytes([10])*300)
    activated=call("Activate")
    assert activated.returncode==0,activated.stdout+activated.stderr
    assert base.exists() and back.exists() and not candidate.exists()
    assert hashlib.sha256((back/NATIVE_NAME).read_bytes()).hexdigest()==known
    assert untouched.read_bytes()==b"private-fixture"
    # Rollback must not overwrite an active runtime, not even the candidate.
    refused=call("Rollback")
    assert refused.returncode!=0 and back.exists()
    quarantined=tmp_path/"quarantined-new-native-bin"
    base.rename(quarantined)
    restored=call("Rollback")
    assert restored.returncode==0,restored.stdout+restored.stderr
    assert hashlib.sha256((base/NATIVE_NAME).read_bytes()).hexdigest()==known
    assert quarantined.exists() and untouched.read_bytes()==b"private-fixture"
