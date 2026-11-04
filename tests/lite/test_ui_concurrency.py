"""Mutation must reject while native GPU one-process lock is held."""
import asyncio
from pathlib import Path
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings

def test_profile_mutations_reject_busy_inference(tmp_path):
    settings=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    app=create_app(settings)
    row={"id":"voice","ref_text":"hi","language":"en","name":"Test","version":1}
    app.state.store.get=lambda id:row if id=="voice" else None
    async def simulate():
        await app.state.runner.lock.acquire()
    asyncio.run(simulate())
    # An asyncio.Lock held from a different loop still reports locked synchronously.
    try:
        with TestClient(app) as c:
            assert c.put("/profiles/voice",json={"name":"Changed"}).status_code==409
            assert c.delete("/profiles/voice").status_code==409
            assert c.delete("/profiles/voice?purge=true").status_code==409
            assert c.put("/profiles/voice/audio",files={"ref_audio":("a.wav",b"x"*128)}).status_code==409
    finally:
        app.state.runner.lock.release()
