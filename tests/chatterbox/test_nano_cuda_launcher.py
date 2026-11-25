"""CUDA launcher regression tests; no GPU, downloads, or model weights required."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "run-chatterbox-nano.ps1"
SOURCE = LAUNCHER.read_text(encoding="utf-8")
PROBE = re.search(r"\$cudaPreflight = @'\n(.*?)\n'@", SOURCE, re.S).group(1)


def test_launcher_static_contract():
    before, server = SOURCE.split("& $python -m uvicorn", 1)
    assert before.index('$env:CUDA_DEVICE_ORDER = "PCI_BUS_ID"') < before.index("& $python -c")
    assert before.index('$env:CUDA_VISIBLE_DEVICES = "0"') < before.index("& $python -c")
    assert before.index("Remove-Item Env:PYTORCH_NVML_BASED_CUDA_CHECK") < before.index("& $python -c")
    assert before.index("& $python -c") < before.index("NANO_CUDA_PREFLIGHT_FAILED")
    assert "if ($LASTEXITCODE -ne 0)" in before
    for setting in (
        "[string]$Root='D:\\VoiceStudioBuild\\chatterbox-nano'",
        "[int]$Port=3901",
        ".venv-chatterbox\\Scripts\\python.exe",
        "$env:HF_HUB_OFFLINE='1'",
        "$env:TRANSFORMERS_OFFLINE='1'",
        "$env:HF_HOME=", "$env:HF_HUB_CACHE=", "$env:TORCH_HOME=",
        "$env:TMP=", "$env:TEMP=", "$env:PYTHONPATH=",
        "$env:CHATTERBOX_ROOT=", "$env:CHATTERBOX_PORT=",
        "ENV_NOT_INSTALLED", "INVALID_PORT",
    ):
        assert setting in before
    assert "lite.chatterbox_app:app --host 127.0.0.1 --port $Port --no-access-log" in server
    assert 'NANO_SERVICE_EXIT_$LASTEXITCODE' in server
    assert "SetEnvironmentVariable" not in SOURCE


@pytest.mark.parametrize("mode,expected", [
    ("ok", "CUDA preflight: PASS"),
    ("unavailable", "CUDA preflight: FAIL"),
    ("zero", "count=0"),
    ("multiple", "count=2"),
    ("invalid_index", "Invalid device id"),
    ("wrong_current", "CUDA device 0 is not current"),
    ("bad_properties", "properties unavailable"),
    ("empty_name", "has no name"),
    ("import_error", "mock torch import failure"),
])
def test_probe_mocked_cuda(tmp_path, mode, expected):
    # Deliberately expose only the permitted CUDA API; unexpected model/inference
    # or network-related torch calls fail. Run the actual embedded Python probe.
    (tmp_path / "torch.py").write_text(
        """
import os
from types import SimpleNamespace
mode = os.environ['NANO_TEST_MODE']
if mode == 'import_error':
    raise ImportError('mock torch import failure')
class CUDA:
    def is_available(self):
        return mode != 'unavailable'
    def device_count(self):
        return {'zero': 0, 'multiple': 2}.get(mode, 1)
    def set_device(self, index):
        assert index == 0
        if mode == 'invalid_index':
            raise AssertionError('Invalid device id')
    def current_device(self):
        return 1 if mode == 'wrong_current' else 0
    def get_device_properties(self, index):
        assert index == 0
        if mode == 'bad_properties':
            raise RuntimeError('properties unavailable')
        return SimpleNamespace(name='' if mode == 'empty_name' else 'Mock GPU')
cuda = CUDA()
""", encoding="utf-8")
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="0", CUDA_DEVICE_ORDER="PCI_BUS_ID",
               NANO_TEST_MODE=mode, PYTHONPATH=str(tmp_path))
    env.pop("PYTORCH_NVML_BASED_CUDA_CHECK", None)
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=20)
    assert expected in result.stdout
    assert "CUDA_VISIBLE_DEVICES: 0" in result.stdout
    if mode == "ok":
        assert result.returncode == 0
        assert "CUDA device count: 1" in result.stdout
        assert "CUDA device: Mock GPU" in result.stdout
    else:
        assert result.returncode == 1
        assert "CUDA preflight: PASS" not in result.stdout
        assert "Check nvidia-smi" in result.stdout


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"),
                    reason="requires Windows PowerShell")
@pytest.mark.parametrize("visibility,probe_exit,server_exit", [
    (None, 0, 0), ("", 0, 0), ("99", 0, 0), ("0", 0, 0),
    ("99", 7, 0), ("0", 0, 9),
])
def test_windows_launcher_with_mock_python(tmp_path, visibility, probe_exit, server_exit):
    # Redirect only the pinned executable path to a harmless PowerShell script.
    # All other launcher code executes unchanged in a separate Windows process.
    fake = tmp_path / "fake-python.ps1"
    fake.write_text(r"""
if ($env:CUDA_VISIBLE_DEVICES -ne '0') { throw 'bad visibility' }
if ($env:CUDA_DEVICE_ORDER -ne 'PCI_BUS_ID') { throw 'bad order' }
if (Test-Path Env:PYTORCH_NVML_BASED_CUDA_CHECK) { throw 'NVML override inherited' }
if ($env:HF_HUB_OFFLINE -ne '1' -or $env:TRANSFORMERS_OFFLINE -ne '1') { throw 'not offline' }
if ($env:CHATTERBOX_PORT -ne '4391') { throw 'port not preserved' }
if ($args[0] -eq '-c') {
    Write-Output 'MOCK_PREFLIGHT'
    exit ([int]$env:NANO_TEST_PROBE_EXIT)
}
if ($args[0] -ne '-m' -or $args[1] -ne 'uvicorn') { throw 'unexpected command' }
if (($args -join ' ') -ne '-m uvicorn lite.chatterbox_app:app --host 127.0.0.1 --port 4391 --no-access-log') { throw 'bad server args' }
Write-Output 'MOCK_UVICORN'
exit ([int]$env:NANO_TEST_SERVER_EXIT)
""", encoding="utf-8")
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(r"""
$ErrorActionPreference = 'Stop'
$names = @('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'PYTORCH_NVML_BASED_CUDA_CHECK')
$before = @{}
foreach ($scope in @('User', 'Machine')) {
    foreach ($name in $names) {
        $before["$scope/$name"] = [Environment]::GetEnvironmentVariable($name, $scope)
    }
}
function Join-Path {
    param([string]$Path, [string]$ChildPath)
    if ($ChildPath -eq '.venv-chatterbox\Scripts\python.exe') { return $env:NANO_TEST_PYTHON }
    Microsoft.PowerShell.Management\Join-Path -Path $Path -ChildPath $ChildPath
}
try {
    & $env:NANO_TEST_LAUNCHER -Port 4391
} finally {
    foreach ($scope in @('User', 'Machine')) {
        foreach ($name in $names) {
            if ([Environment]::GetEnvironmentVariable($name, $scope) -cne $before["$scope/$name"]) {
                throw "Persistent environment changed: $scope/$name"
            }
        }
    }
    Write-Output 'MOCK_PERSISTENT_ENV_UNCHANGED'
}
""", encoding="utf-8")
    env = dict(os.environ, CUDA_DEVICE_ORDER="FASTEST_FIRST",
               PYTORCH_NVML_BASED_CUDA_CHECK="1", NANO_TEST_PYTHON=str(fake),
               NANO_TEST_LAUNCHER=str(LAUNCHER), NANO_TEST_PROBE_EXIT=str(probe_exit),
               NANO_TEST_SERVER_EXIT=str(server_exit))
    if visibility is None:
        env.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        env["CUDA_VISIBLE_DEVICES"] = visibility
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                            env=env, capture_output=True, text=True, timeout=40)
    assert "MOCK_PREFLIGHT" in result.stdout
    assert "MOCK_PERSISTENT_ENV_UNCHANGED" in result.stdout
    if probe_exit:
        assert result.returncode != 0
        assert "MOCK_UVICORN" not in result.stdout
        assert "NANO_CUDA_PREFLIGHT_FAILED" in result.stderr
    else:
        assert result.stdout.index("MOCK_PREFLIGHT") < result.stdout.index("MOCK_UVICORN")
        if server_exit:
            assert result.returncode != 0
            assert "NANO_SERVICE_EXIT_9" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
