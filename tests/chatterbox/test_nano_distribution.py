from pathlib import Path
import ast

from lite.chatterbox_config import MODEL_HASHES, REQUIRED_FILES, MODEL_REPO

ROOT = Path(__file__).resolve().parents[2]


def test_nano_active_entrypoints_and_no_turbo_toggle():
    runner = (ROOT / "scripts" / "run-chatterbox-nano.ps1").read_text("utf8")
    installer = (ROOT / "scripts" / "install-chatterbox-nano.ps1").read_text("utf8")
    worker = (ROOT / "lite" / "chatterbox_worker.py").read_text("utf8")
    assert "127.0.0.1" in runner and "--port $Port" in runner and "0.0.0.0" not in runner
    assert "D:\\VoiceStudioBuild\\chatterbox-nano" in runner + installer
    assert "from_local(str(folder), device=\"cuda\", nano=True)" in worker
    assert "from_pretrained" not in worker and "device=\"cpu\"" not in worker
    diagnostic=(ROOT / "lite" / "chatterbox_cpu_diagnostic.py").read_text("utf8")
    assert "CPU_FALLBACK_UNSUPPORTED" in diagnostic and "import torch" not in diagnostic
    assert "lite.app" not in runner and "run-gguf" not in runner.lower()


def test_model_allowlist_has_full_cryptographic_pins():
    assert MODEL_REPO == "ResembleAI/chatterbox-nano"
    assert set(MODEL_HASHES) == set(REQUIRED_FILES)
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in MODEL_HASHES.values())
    assert "t3_turbo_v1.safetensors" not in MODEL_HASHES


def test_nano_production_import_tree_has_no_gguf_runtime_imports():
    modules = ["chatterbox_app.py", "chatterbox_service.py", "chatterbox_worker.py",
               "chatterbox_config.py", "profiles.py", "chatterbox_migration.py"]
    banned = {"lite.app", "lite.native_runner", "lite.runtime", "lite.resident_worker"}
    for name in modules:
        tree = ast.parse((ROOT / "lite" / name).read_text("utf8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not (imports & banned), (name, imports & banned)
