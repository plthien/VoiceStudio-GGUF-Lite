import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_direct_downloader_clears_stale_offline_flags_before_hub_import(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    observed = {}

    def fake_snapshot_download(**_kwargs):
        observed.update(hub=os.environ.get("HF_HUB_OFFLINE"),
                        transformers=os.environ.get("TRANSFORMERS_OFFLINE"))

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(snapshot_download=fake_snapshot_download))
    source = ROOT / "scripts/internal/download-chatterbox-model.py"
    spec = importlib.util.spec_from_file_location("download_chatterbox_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "0"
    module.snapshot_download()
    assert observed == {"hub":"0", "transformers":"0"}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe"])
def test_installer_online_boundary_overrides_and_restores_offline_flags(shell):
    exe = shutil.which(shell)
    if not exe:
        pytest.skip("PowerShell host unavailable")
    helper = ROOT / "scripts/internal/chatterbox-environment.ps1"
    command = (
        f". {_quote(helper)}; "
        "$env:HF_HUB_OFFLINE='1';$env:TRANSFORMERS_OFFLINE='1';"
        "Invoke-ChatterboxOnlineCommand -FailureCode 'DOWNLOAD_FAILED' -Command {"
        "if($env:HF_HUB_OFFLINE -ne '0' -or $env:TRANSFORMERS_OFFLINE -ne '0'){throw 'NOT_ONLINE'};"
        "& $env:ComSpec /c exit 0};"
        "if($env:HF_HUB_OFFLINE -ne '1' -or $env:TRANSFORMERS_OFFLINE -ne '1'){throw 'NOT_RESTORED'};"
        "$failed=$false;try {"
        "Invoke-ChatterboxOnlineCommand -FailureCode 'DOWNLOAD_FAILED' -Command {& $env:ComSpec /c exit 7}"
        "} catch {if($_.Exception.Message -ne 'DOWNLOAD_FAILED'){throw};$failed=$true};"
        "if(-not $failed){throw 'FAILURE_NOT_PROPAGATED'};"
        "if($env:HF_HUB_OFFLINE -ne '1' -or $env:TRANSFORMERS_OFFLINE -ne '1'){throw 'FAILURE_NOT_RESTORED'};"
        "Remove-Item Env:HF_HUB_OFFLINE,Env:TRANSFORMERS_OFFLINE;"
        "Invoke-ChatterboxOnlineCommand -FailureCode 'DOWNLOAD_FAILED' -Command {"
        "if($env:HF_HUB_OFFLINE -ne '0' -or $env:TRANSFORMERS_OFFLINE -ne '0'){throw 'NOT_ONLINE'};"
        "& $env:ComSpec /c exit 0};"
        "if(Test-Path Env:HF_HUB_OFFLINE){throw 'HF_LEAKED'};"
        "if(Test-Path Env:TRANSFORMERS_OFFLINE){throw 'TRANSFORMERS_LEAKED'};"
        "'ONLINE_BOUNDARY_OK'"
    )
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", command], capture_output=True, text=True, timeout=30,
        )
    except OSError as exc:
        pytest.skip(f"PowerShell host is an unlaunchable app alias: {exc}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ONLINE_BOUNDARY_OK" in result.stdout


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe"])
def test_ps51_ps7_positive_and_negative_snapshot_control_flow(tmp_path, shell):
    exe = shutil.which(shell)
    if not exe:
        pytest.skip("PowerShell host unavailable")
    try:
        probe = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            capture_output=True, text=True, timeout=10,
        )
    except OSError as exc:
        pytest.skip(f"PowerShell host is an unlaunchable app alias: {exc}")
    if probe.returncode != 0:
        pytest.skip("PowerShell host cannot execute commands")
    model = tmp_path / "model"; model.mkdir()
    names = ["one.safetensors", "tokenizer.json"]
    hashes = {}
    for name in names:
        payload = ("bounded-fixture:" + name).encode()
        (model / name).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    manifest = {"schema":"chatterbox-nano-model-manifest-v1", "model_id":"chatterbox-nano",
                "repo_id":"fixture/repo", "loader_mode":"nano=True", "revision":"fixture-model",
                "source_revision":"fixture-source", "sha256":hashes}
    (model / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    python_stub = tmp_path / "python.exe"; python_stub.write_bytes(b"fixture")
    ffmpeg = shutil.which("ffmpeg") or os.environ.get("CHATTERBOX_FFMPEG")
    if not ffmpeg:
        pytest.skip("FFmpeg unavailable")
    sha = ROOT / "scripts/internal/chatterbox-sha256.ps1"
    verify = ROOT / "scripts/internal/chatterbox-verify-model.ps1"
    table = "@{" + ";".join(f"{_quote(k)}={_quote(v)}" for k, v in hashes.items()) + "}"
    array = "@(" + ",".join(_quote(n) for n in names) + ")"
    common = (f". {_quote(sha)}; . {_quote(verify)}; $expected={table}; $required={array}; "
              f"Assert-ChatterboxVerifyPrerequisites -PythonPath {_quote(python_stub)} -FfmpegCommand {_quote(ffmpeg)} | Out-Null; "
              f"Test-ChatterboxModelSnapshot -ModelDirectory {_quote(model)} -ManifestPath {_quote(model/'manifest.json')} "
              f"-RequiredFiles $required -ExpectedHashes $expected -ModelRepo 'fixture/repo' "
              "-ModelRevision 'fixture-model' -SourceRevision 'fixture-source' | Out-Null; 'VERIFY_CONTROL_FLOW_OK'")
    def invoke():
        return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", common], capture_output=True, text=True, timeout=30)

    result = invoke()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERIFY_CONTROL_FLOW_OK" in result.stdout
    # The fixture is isolated to the helper. Production pins remain literal in
    # install-chatterbox-nano.ps1 and cannot be overridden by these arguments.
    production = (ROOT / "scripts/install-chatterbox-nano.ps1").read_text("utf8")
    assert hashes["one.safetensors"] not in production

    def rejected(code):
        bad = invoke()
        assert bad.returncode != 0 and code in bad.stdout + bad.stderr

    (model / "unexpected.bin").write_bytes(b"x")
    rejected("MODEL_MANIFEST_INVALID")
    (model / "unexpected.bin").unlink()

    original = (model / names[0]).read_bytes()
    (model / names[0]).write_bytes(original + b"tamper")
    rejected("MODEL_HASH_MISMATCH")
    (model / names[0]).write_bytes(original)

    (model / names[1]).unlink()
    rejected("MODEL_MANIFEST_INVALID")
    (model / names[1]).write_bytes(("bounded-fixture:" + names[1]).encode())

    stale = dict(manifest); stale["revision"] = "stale-model"
    (model / "manifest.json").write_text(json.dumps(stale), encoding="utf8")
    rejected("MODEL_MANIFEST_INVALID")
    (model / "manifest.json").write_text("{malformed", encoding="utf8")
    rejected("MODEL_MANIFEST_INVALID")
