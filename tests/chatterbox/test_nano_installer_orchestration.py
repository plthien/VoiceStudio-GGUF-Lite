import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


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
