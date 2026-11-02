"""Frozen OpenAI-shaped HTTP request contract for future YouTube backend, not UI duplication."""
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings

def test_external_speech_schema_and_dedicated_local_routes(tmp_path):
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(s)) as c:
        openapi=c.get("/openapi.json").json()
        paths=openapi["paths"]
        assert "/v1/audio/speech" in paths
        assert "post" in paths["/v1/audio/speech"]
        fields=openapi["components"]["schemas"]["Speech"]["properties"]
        assert set(fields)=={"model","voice","input","response_format","speed","language","seed"}
        assert not set(fields)&{"wpm","duration","alignment","output_rate","chunking","provider"}
        assert not any("/stt" in p or "/cloud" in p or "/transcribe" in p or "/dubbing" in p for p in paths)
        assert "/ui" not in paths and "/ui/assets/{asset_path}" not in paths
        for path in ("/health","/lite/readiness","/v1/audio/voices","/profiles"):
            assert path in paths
        assert c.post("/v1/audio/speech",json={"model":"other",
            "voice":"test","input":"hello","speed":1.0}).status_code==422
        assert c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"test","input":"hello","speed":1.5}).status_code==422
        assert c.get("/health").json()["ready"] is False
