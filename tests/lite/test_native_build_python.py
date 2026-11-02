"""Native Vulkan build: explicit Lite Python is required before clone."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
SH=ROOT/"scripts/internal/build-gguf-lite.sh"
PS=ROOT/"scripts/build-gguf-lite-vulkan.ps1"
PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"

def bash_exe():
    if os.name=="nt":
        exe=Path(os.environ.get("ProgramFiles",r"C:\Program Files"))/"Git/bin/bash.exe"
        if exe.is_file():
            return str(exe)
    found=shutil.which("bash")
    if not found:
        pytest.skip("Bash unavailable")
    return found

def run_shell(tmp_path,python_value=None):
    env=os.environ.copy()
    env.pop("GGUF_LITE_BUILD_PYTHON",None)
    if python_value is not None:
        env["GGUF_LITE_BUILD_PYTHON"]=python_value
    return subprocess.run([bash_exe(),str(SH),"--platform","windows-x86_64",
                           "--commit-sha",PIN],cwd=tmp_path,env=env,
                          capture_output=True,text=True,timeout=20)

def test_missing_python_fails_before_clone(tmp_path):
    result=run_shell(tmp_path)
    assert result.returncode!=0
    assert "GGUF_LITE_BUILD_PYTHON is missing" in result.stderr
    assert "Cloning omnivoice.cpp" not in result.stdout

def test_missing_explicit_python_fails_before_clone(tmp_path):
    result=run_shell(tmp_path,"/missing/python")
    assert result.returncode!=0
    assert "Lite Python executable is unavailable" in result.stderr
    assert "Cloning omnivoice.cpp" not in result.stdout

@pytest.mark.skipif(os.name=="nt",reason="Linux stub exercises PATH independence without network")
def test_explicit_python_not_from_path(tmp_path):
    fake=tmp_path/"fakebin"
    fake.mkdir()
    git=fake/"git"
    git.write_text("#!/bin/sh\necho SYNTHETIC_GIT_CLONE_BLOCKED >&2\nexit 42\n")
    git.chmod(0o755)
    env=os.environ.copy()
    env["GGUF_LITE_BUILD_PYTHON"]=sys.executable
    env["PATH"]=str(fake)+":/usr/bin:/bin"
    result=subprocess.run([bash_exe(),str(SH),"--platform","windows-x86_64",
                           "--commit-sha",PIN],cwd=tmp_path,env=env,
                          capture_output=True,text=True,timeout=20)
    assert result.returncode!=0
    assert "Cloning omnivoice.cpp" in result.stdout
    assert "SYNTHETIC_GIT_CLONE_BLOCKED" in result.stderr

@pytest.mark.skipif(os.name!="nt",reason="Windows PowerShell launcher check")
def test_windows_launcher_fails_fast_without_lite_python(tmp_path):
    pwsh=shutil.which("pwsh")
    if pwsh is None:
        pytest.fail("Windows runner requires pwsh")
    result=subprocess.run([pwsh,"-NoProfile","-File",str(PS),"-Root",str(tmp_path/"no-lite")],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode!=0
    assert "Lite Python 3.11 not found" in (result.stdout+result.stderr)
    assert "Cloning omnivoice.cpp" not in (result.stdout+result.stderr)

def test_instrumentation_uses_only_explicit_interpreter():
    bash=SH.read_text()
    powershell=PS.read_text()
    assert '"$PYTHON_BIN" "$REPO_ROOT/scripts/internal/instrument-attestation.py"' in bash
    assert '\npython "$REPO_ROOT/scripts/internal/instrument-attestation.py"' not in bash
    assert 'SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$pythonExe,"Process")' in powershell
    assert 'SetEnvironmentVariable("GGUF_LITE_BUILD_PYTHON",$previousBuildPython,"Process")' in powershell


def test_native_speed_patch_uses_pinned_python_and_not_untracked_cli():
    bash=SH.read_text()
    ps=PS.read_text()
    assert '"$PYTHON_BIN" "$REPO_ROOT/scripts/internal/instrument-native-speed.py"' in bash
    assert "instrument-native-speed.py" in ps and "lite-native-speed.h" in ps
    assert "speed-capability.json" in bash and "speed-capability.json" in ps
    assert "-OutputRuntime" not in bash  # only explicit owner-controlled PowerShell staging option
    assert "$OutputRuntime" in ps
    assert "A Lite native runtime exists" in ps
