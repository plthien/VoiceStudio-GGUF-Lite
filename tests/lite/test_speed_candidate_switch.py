"""NS04: executable lifecycle under Windows PowerShell 5.1 and PowerShell 7.

Every candidate EXE is a real compiled Windows PE. No user files or installed
runtime are touched. Real Windows PIDs are inspected with kernel32 handles.
"""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from lite.config import ROOT, RUNTIME_REVISION, NATIVE_NAME, VULKAN_NAME

SWITCH=ROOT/"scripts/switch-gguf-lite-runtime.ps1"
HELPER=ROOT/"scripts/internal/speed-help-process.ps1"

@pytest.fixture(params=["powershell.exe","pwsh.exe"],ids=["WindowsPowerShell5_1","PowerShell7"])
def ps_exe(request):
    if os.name!="nt":
        pytest.skip("Windows-only real process tree test")
    exe=shutil.which(request.param)
    if not exe:
        pytest.skip("NOT VERIFIED: "+request.param+" unavailable")
    result=subprocess.run([exe,"-NoProfile","-Command",
                           "$PSVersionTable.PSVersion.ToString()"],
                          capture_output=True,text=True,timeout=12)
    assert result.returncode==0,result.stderr
    version=result.stdout.strip().splitlines()[-1]
    if request.param=="powershell.exe":
        assert version.startswith("5.1."),version
    else:
        assert int(version.split(".")[0])>=7,version
    return exe

def running(pid:int)->bool:
    if not pid: return False
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
    kernel.WaitForSingleObject.restype=ctypes.c_uint32
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.CloseHandle.restype=ctypes.c_int
    handle=kernel.OpenProcess(0x00100000|0x1000,False,pid)
    if not handle:
        error=ctypes.get_last_error()
        assert error in (87,1168),f"Unverifiable PID {pid}: Windows error {error}"
        return False
    try:
        return kernel.WaitForSingleObject(handle,0)==0x102
    finally:
        kernel.CloseHandle(handle)

def digest(path:Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def make_bin(folder:Path, speed:bool, compiler=None):
    folder.mkdir(parents=True,exist_ok=False)
    files=[folder/NATIVE_NAME,folder/VULKAN_NAME,folder/"ggml.dll",folder/"ggml-base.dll"]
    for idx,p in enumerate(files):
        p.write_bytes(b"MZ"+bytes([idx+9])*300)
    if speed:
        assert compiler
        cpp=folder/"speed-help-fixture.cpp"
        cpp.write_text(textwrap.dedent(r'''
        #include <windows.h>
        #include <fstream>
        #include <string>
        #include <iostream>
        #include <thread>
        #include <chrono>
        #include <cstdlib>
        int main(int argc,char**argv){
            if (argc==2 && std::string(argv[1])=="--child") {
                std::this_thread::sleep_for(std::chrono::seconds(40));
                return 0;
            }
            if (argc!=2 || std::string(argv[1])!="--help") return 17;
            std::ifstream fd("help-mode.txt");
            std::string mode="valid";
            if(fd)fd>>mode;
            DWORD pid=GetCurrentProcessId();
            {std::ofstream f("parent-pid.txt");f<<pid;}
            if(mode=="hang") {
                std::this_thread::sleep_for(std::chrono::seconds(40));
                return 0;
            }
            if(mode=="spawn"){
                char self[MAX_PATH]={0};
                if(!GetModuleFileNameA(nullptr,self,MAX_PATH))return 19;
                std::string cmd=std::string("\"")+self+"\" --child";
                STARTUPINFOA si={};si.cb=sizeof(si);
                PROCESS_INFORMATION pi={};
                if(!CreateProcessA(self,&cmd[0],nullptr,nullptr,FALSE,CREATE_NO_WINDOW,
                                   nullptr,nullptr,&si,&pi))return 20;
                {std::ofstream f("child-pid.txt");f<<pi.dwProcessId;}
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                std::this_thread::sleep_for(std::chrono::seconds(40));
                return 0;
            }
            if(mode=="race") {
                std::this_thread::sleep_for(std::chrono::milliseconds(8000));
                return 3;
            }
            if(mode=="crash") std::abort();
            if(mode=="nonzero")return 3;
            if(mode=="large"){for(int i=0;i<40000;i++)std::cout<<"x";return 0;}
            if(mode=="missing"){std::cout<<"--speed";return 0;}
            if(mode=="wrong"){std::cout<<"LiteNativeSpeeds/v1 --speed";return 0;}
            std::cout<<"LiteNativeSpeed/v1 --speed <float>\n";
            return 0;
        }
        '''),encoding="utf8")
        subprocess.run([compiler,"-std=c++17","-O0",str(cpp),"-o",str(files[0])],
                       capture_output=True,text=True,check=True,timeout=90)
        (folder/"help-mode.txt").write_text("valid",encoding="utf8")
    (folder/"checksums.sha256").write_text("".join(
        digest(p)+"  "+p.name+"\n" for p in files))
    if speed:
        sha=digest
        (folder/"speed-capability.json").write_text(json.dumps({
           "schema":"lite-native-speed-v1","runtime_revision":RUNTIME_REVISION,
           "exe_sha256":sha(files[0]),
           "patch_sha256":sha(ROOT/"scripts/internal/instrument-native-speed.py"),
           "header_sha256":sha(ROOT/"scripts/internal/lite-native-speed.h")
        }))
    return files

@pytest.fixture
def case(tmp_path,ps_exe):
    compiler=shutil.which("clang++") or shutil.which("g++")
    assert compiler,"Windows CI must have C++ compiler"
    base=tmp_path/"runtime"/"bin"
    cand=tmp_path/"candidate"/"bin"
    backup=tmp_path/"backup"/"old"
    original=make_bin(base,False)
    make_bin(cand,True,compiler)
    # Prove the compiled fixture itself is a runnable PE with a clean exit,
    # not merely an MZ prefix or an assumed help marker.
    direct=subprocess.run([str(cand/NATIVE_NAME),"--help"],cwd=cand,
                          capture_output=True,text=True,timeout=8)
    assert direct.returncode==0,(direct.returncode,direct.stderr)
    assert "LiteNativeSpeed/v1" in direct.stdout and "--speed" in direct.stdout
    private=tmp_path/"data"/"original-reference.wav"
    private.parent.mkdir()
    private.write_bytes(b"PRIVATE_FIXTURE_NEVER_MODIFIED")
    tempfile=tmp_path/"isolated-temp"
    tempfile.mkdir()
    env=os.environ.copy()
    env.update({"TEMP":str(tempfile),"TMP":str(tempfile)})
    context={
        "ps":ps_exe,"base":base,"cand":cand,"backup":backup,
        "private":private,"tmp":tempfile,"env":env,
        "baseline_sha":digest(original[0]),"candidate_sha":digest(cand/NATIVE_NAME),
    }
    try:
        yield context
    finally:
        # A broken cleanup implementation must not leave the test runner with
        # orphaned binaries. This teardown is independent of the production
        # helper; it still makes the regression FAIL with actual orphan PIDs.
        orphaned=[]
        for name in ("parent-pid.txt","child-pid.txt"):
            file=cand/name
            if file.is_file():
                process_id=int(file.read_text())
                if running(process_id):
                    orphaned.append(process_id)
                    subprocess.run(["taskkill.exe","/PID",str(process_id),"/T","/F"],
                                   capture_output=True,text=True,timeout=10)
        if orphaned:
            pytest.fail("NS04 leaked real Windows candidate/child PID(s): "+repr(orphaned))

def run_switch(c,mode="Activate",approve=True,timeout=25):
    command=[c["ps"],"-NoProfile","-File",str(SWITCH),
             "-Mode",mode,"-ActiveBin",str(c["base"]),
             "-BackupBin",str(c["backup"])]
    if mode=="Activate":command+=["-CandidateBin",str(c["cand"])]
    if approve:command+=["-OwnerApproves"]
    return subprocess.run(command,env=c["env"],capture_output=True,text=True,timeout=timeout)

def assert_pristine(c):
    assert c["base"].is_dir() and c["cand"].is_dir()
    assert not c["backup"].exists(),"Activation began despite failed validation"
    assert digest(c["base"]/NATIVE_NAME)==c["baseline_sha"]
    assert digest(c["cand"]/NATIVE_NAME)==c["candidate_sha"]
    assert c["private"].read_bytes()==b"PRIVATE_FIXTURE_NEVER_MODIFIED"
    assert list(c["tmp"].iterdir())==[], "Temporary stdout/stderr were not removed"

def pid(c,name):
    file=c["cand"]/name
    return int(file.read_text()) if file.exists() else 0

def run_timeout(c,child=False,race=False):
    mode="spawn" if child else "race" if race else "hang"
    (c["cand"]/"help-mode.txt").write_text(mode)
    cmd=[c["ps"],"-NoProfile","-File",str(SWITCH),"-Mode","Activate",
         "-ActiveBin",str(c["base"]),"-BackupBin",str(c["backup"]),
         "-CandidateBin",str(c["cand"]),"-OwnerApproves"]
    probe=subprocess.Popen(cmd,env=c["env"],stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,text=True)
    seen_parent=False
    seen_child=False
    try:
        until=time.monotonic()+6
        while time.monotonic()<until and probe.poll() is None:
            p=pid(c,"parent-pid.txt")
            q=pid(c,"child-pid.txt") if child else 0
            if p and running(p):seen_parent=True
            if q and running(q):seen_child=True
            if seen_parent and (not child or seen_child):break
            time.sleep(.04)
        output,errors=probe.communicate(timeout=25)
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.communicate(timeout=5)
    assert seen_parent,"Real candidate parent never started"
    if child: assert seen_child,"Real child process was never observed alive"
    assert probe.returncode!=0,(output,errors)
    assert not running(pid(c,"parent-pid.txt")),"Orphaned parent PID"
    if child: assert not running(pid(c,"child-pid.txt")),"Orphaned child PID"
    assert_pristine(c)
    return output+errors

def test_ns04_owner_approval_activation_and_rollback(case):
    c=case
    denied=run_switch(c,approve=False)
    assert denied.returncode!=0
    assert_pristine(c)
    activated=run_switch(c)
    assert activated.returncode==0,activated.stdout+activated.stderr
    assert c["base"].exists() and c["backup"].exists() and not c["cand"].exists()
    assert digest(c["backup"]/NATIVE_NAME)==c["baseline_sha"]
    assert c["private"].read_bytes()==b"PRIVATE_FIXTURE_NEVER_MODIFIED"
    assert list(c["tmp"].iterdir())==[]
    refused=run_switch(c,mode="Rollback")
    assert refused.returncode!=0
    quarantined=c["base"].parent/"quarantined-bin"
    c["base"].rename(quarantined)
    restored=run_switch(c,mode="Rollback")
    assert restored.returncode==0,restored.stdout+restored.stderr
    assert digest(c["base"]/NATIVE_NAME)==c["baseline_sha"]
    assert quarantined.exists() and c["private"].read_bytes()==b"PRIVATE_FIXTURE_NEVER_MODIFIED"

@pytest.mark.parametrize("mode",["nonzero","crash","missing","wrong","large"])
def test_ns04_bad_executable_rejected_without_mutations(case,mode):
    c=case
    (c["cand"]/"help-mode.txt").write_text(mode)
    failed=run_switch(c)
    assert failed.returncode!=0,(failed.stdout,failed.stderr)
    assert not running(pid(c,"parent-pid.txt"))
    assert_pristine(c)

def test_ns04_hung_parent_reaped(case):
    result=run_timeout(case)
    assert "timed out" in result.lower(),result

def test_ns04_hung_parent_and_child_tree_reaped(case):
    result=run_timeout(case,child=True)
    assert "timed out" in result.lower(),result

def test_ns04_natural_exit_race_is_safe(case):
    result=run_timeout(case,race=True)
    assert "timed out" in result.lower() or "nonzero" in result.lower(),result

def test_ns04_cleanup_failure_explicitly_reported(case,tmp_path):
    c=case
    (c["cand"]/"help-mode.txt").write_text("hang")
    # The production helper is dot-sourced and exercised with a REAL PROCESS.
    # Give the helper a deliberately stale start tick to simulate a safety-
    # critical cleanup failure, and verify it THROWS. Then clean up the same
    # real process with its true ticks. No kill of a mismatched PID is allowed.
    harness=tmp_path/"check-cleanup.ps1"
    harness.write_text(textwrap.dedent(r'''
        param([string]$Helper,[string]$Exe,[string]$Dir)
        $ErrorActionPreference="Stop"
        . $Helper
        $p=Start-Process -FilePath $Exe -ArgumentList @("--help") -WorkingDirectory $Dir -PassThru -NoNewWindow
        $ticks=$p.StartTime.ToUniversalTime().Ticks
        try {
            try {
                Stop-SpeedHelpTree $p ($ticks + 1L)
                throw "NS04 failure was silently accepted"
            } catch {
                if (!$_.Exception.Message.Contains("recycled PID")) { throw }
                Write-Output "NS04_CLEANUP_FAILURE_REPORTED"
            }
        } finally {
            Stop-SpeedHelpTree $p $ticks
            if (!$p.HasExited) { throw "Original candidate still active" }
            $p.Dispose()
        }
    '''),encoding="utf8")
    run=subprocess.run([c["ps"],"-NoProfile","-File",str(harness),
                        "-Helper",str(HELPER),"-Exe",str(c["cand"]/NATIVE_NAME),
                        "-Dir",str(c["cand"])],env=c["env"],capture_output=True,
                       text=True,timeout=25)
    assert run.returncode==0,run.stdout+run.stderr
    assert "NS04_CLEANUP_FAILURE_REPORTED" in run.stdout
    assert not running(pid(c,"parent-pid.txt"))
    assert_pristine(c)
