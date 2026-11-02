from pathlib import Path
import subprocess
root=Path(__file__).resolve().parents[2]
paths=set(subprocess.run(["git","ls-files"],cwd=root,check=True,capture_output=True,text=True).stdout.splitlines())
for prefix in ("backend/","frontend/","electron/","tauri/","mobile/","deploy/","examples/"):
    assert not any(p.startswith(prefix) for p in paths),prefix
for name in ("pyproject.toml","uv.lock","bun.lock","package.json"):
    assert name not in paths,name
for name in ("LICENSE","LICENSE-NOTICE.md","lite/resources/q8_pins.json","lite/resources/model_notice.json"):
    assert name in paths,name
for p in (root/"lite").rglob("*.py"):
    s=p.read_text(encoding="utf-8")
    assert 'backend/' not in s and 'import backend' not in s,str(p)
print(f"Lite layout pass: {len(paths)} tracked files")
