"""Positive dependency closure and local documentation links; no heavy full-app imports."""
from pathlib import Path
import re
import subprocess
import sys
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
paths=set(subprocess.run(["git","ls-files"],cwd=root,check=True,capture_output=True,text=True).stdout.splitlines())
for prefix in ("backend/","frontend/","electron/","tauri/","mobile/","deploy/","examples/"):
    assert not any(p.startswith(prefix) for p in paths),prefix
for name in ("pyproject.toml","uv.lock","bun.lock","package.json"):
    assert name not in paths,name
required=("LICENSE","LICENSE-NOTICE.md","README.md","lite/resources/q8_pins.json",
          "lite/resources/model_notice.json","scripts/internal/build-gguf-lite.sh",
          "scripts/internal/omnivoice-vulkan.sh","scripts/internal/instrument-attestation.py",
          "scripts/internal/instrument-native-speed.py","scripts/internal/lite-native-speed.h",
          "scripts/internal/native-speed-ci.cpp","scripts/internal/verify-native-speed.py",
          "scripts/switch-gguf-lite-runtime.ps1","lite/speed.py",
          "scripts/build-gguf-lite-vulkan.ps1","scripts/benchmark-gguf-lite.ps1",
          "lite/ui/index.html","lite/ui/assets/app.css","lite/ui/assets/app.js",
          "lite/ui/assets/api.js","lite/ui_routes.py","lite/ui_support.py")
for name in required:
    assert name in paths,name
for file in (root/"lite").rglob("*.py"):
    text=file.read_text(encoding="utf-8")
    assert 'backend/' not in text and 'import backend' not in text,str(file)
for p in ("README.md","LICENSE-NOTICE.md","docs/gguf-lite.md",
          "docs/gguf-lite-ui.md","docs/gguf-lite-native-speed.md",
          "docs/integrations/youtube-ai-studio-tts-contract.md"):
    text=(root/p).read_text(encoding="utf-8")
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if "://" in target or target.startswith("#") or target.startswith("mailto:"):
            continue
        dest=((root/p).parent/target.split("#")[0]).resolve()
        assert dest.exists() and dest.is_relative_to(root), f"Broken local link in {p}: {target}"
import lite.app
from fastapi.testclient import TestClient
from lite.config import Settings
# App imported without Torch/Torchaudio/WhisperX, Electron, or external service.
import sys,tempfile
assert not any(x in sys.modules for x in ("torch","torchaudio","whisperx"))
with tempfile.TemporaryDirectory() as tmp:
    here=Path(tmp)
    app=lite.app.create_app(Settings(data_dir=here/"data",cache_dir=here/"cache",bin_dir=here/"bin",timeout=30))
    with TestClient(app) as c:
        assert c.get("/health").status_code==200
        routes=set(c.get("/openapi.json").json()["paths"])
        assert routes=={"/health","/lite/readiness","/engines/omnivoice-gguf/quants",
          "/v1/audio/voices","/profiles","/profiles/{profile_id}",
          "/profiles/{profile_id}/audio","/models/licenses/details/{repo_id}",
          "/models/licenses/acceptance/{repo_id}","/models/licenses/accept",
          "/v1/audio/speech","/lite/status","/profiles/{profile_id}/reference",
          "/lite/migration/preview","/lite/migration/apply",
          "/lite/speed-capability"},routes
        assert c.get("/ui").status_code==200
        assert c.get("/ui/assets/app.js").status_code==200
        assert c.get("/ui/assets/missing.js").status_code==404
print(f"Lite layout, links, imports and OpenAPI PASS: {len(paths)} tracked files")
