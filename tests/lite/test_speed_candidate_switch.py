"""Windows-only real PowerShell candidate activation and rollback, using fake PE fixtures.

No owner data, GPU, licence or live installed bin touched. Failure paths
must keep active runtime bytes intact and never auto-enable experimental speed.
"""
import hashlib
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
import pytest
from lite.config import ROOT,RUNTIME_REVISION,NATIVE_NAME,VULKAN_NAME

SCRIPT=ROOT/"scripts/switch-gguf-lite-runtime.ps1"

def make_bin(folder:Path,speed:bool,compiler=None):
    folder.mkdir(parents=True,exist_ok=False)
    paths=[folder/NATIVE_NAME,folder/VULKAN_NAME,folder/"ggml.dll",folder/"ggml-base.dll"]
    for idx,p in enumerate(paths):
        p.write_bytes(b"MZ"+bytes([idx+9])*300)
    if speed and compiler:
        # A genuinely runnable tiny Windows PE. Not a fake MZ blob.
        source=folder/"speed-help-fixture.cpp"
        source.write_text(textwrap.dedent(r'''
        #include <iostream>
        #include <fstream>
        #include <string>
        #include <thread>
        #include <chrono>
        #include <cstdlib>
        int main(int argc,char** argv) {
            if(argc!=2 || std::string(argv[1])!="--help") return 17;
            std::ifstream fd("help-mode.txt");
            std::string mode="valid";
            if(fd) fd>>mode;
            if(mode=="hang") { std::this_thread::sleep_for(std::chrono::seconds(14)); return 0; }
            if(mode=="crash") std::abort();
            if(mode=="nonzero") return 3;
            if(mode=="large") { for(int i=0;i<40000;i++) std::cout<<"x"; return 0; }
            if(mode=="missing") { std::cout<<"--speed"; return 0; }
            if(mode=="wrong") { std::cout<<"LiteNativeSpeeds/v1 --speed"; return 0; }
            std::cout<<"LiteNativeSpeed/v1 --speed <float>\n";
            return 0;
        }
        '''),encoding="utf8")
        subprocess.run([compiler,"-std=c++17","-O0",str(source),"-o",str(paths[0])],
                       check=True,timeout=90,capture_output=True)
        (folder/"help-mode.txt").write_text("valid",encoding="utf8")
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
    compiler=shutil.which("clang++") or shutil.which("g++")
    assert compiler,"Windows runner must have a real C++ compiler"
    base=tmp_path/"runtime"/"bin"
    candidate=tmp_path/"candidate"/"bin"
    back=tmp_path/"backups"/"old-bin"
    original=make_bin(base,False)
    make_bin(candidate,True,compiler=compiler)
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
    # Never permit a forged or corrupted candidate manifest.
    tamper=candidate/VULKAN_NAME
    tamper.write_bytes(b"MZ"+b"modified"*60)
    failed=call("Activate")
    assert failed.returncode!=0 and base.exists() and not back.exists()
    tamper.write_bytes(b"MZ"+bytes([10])*300)
    # All negative executable probes keep source, baseline and backup intact.
    receipt=candidate/"speed-capability.json"
    original_receipt=receipt.read_bytes()
    receipt.write_text(receipt.read_text().replace("lite-native-speed-v1","forged-receipt"))
    refused_receipt=call("Activate")
    assert refused_receipt.returncode!=0 and base.exists() and candidate.exists() and not back.exists()
    receipt.write_bytes(original_receipt)
    mode=candidate/"help-mode.txt"
    for behavior in ("missing","wrong","nonzero","crash","large","hang"):
        mode.write_text(behavior,encoding="utf8")
        failed=call("Activate")
        assert failed.returncode!=0,(behavior,failed.stdout,failed.stderr)
        assert base.exists() and candidate.exists() and not back.exists(),behavior
        assert hashlib.sha256((base/NATIVE_NAME).read_bytes()).hexdigest()==known
    mode.write_text("valid",encoding="utf8")
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
