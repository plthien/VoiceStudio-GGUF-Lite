"""Behavioral localhost browser and receive-stream protections."""
import pytest
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings

@pytest.fixture
def client(tmp_path):
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",
               timeout=30,max_ref_bytes=1024)
    with TestClient(create_app(s)) as c:
        yield c

def test_rejects_foreign_origin_for_json_and_form(client):
    for content_type,data in (("application/json",b"{}"),
                              ("application/x-www-form-urlencoded",b"name=x")):
        response=client.post("/models/licenses/accept",content=data,
            headers={"Origin":"https://attacker.example","Content-Type":content_type})
        assert response.status_code==403
    assert client.post("/models/licenses/accept",json={},headers={"Sec-Fetch-Site":"cross-site"}).status_code==403

def test_rejects_spoofed_host(client):
    response=client.get("/health",headers={"Host":"attacker.example"})
    assert response.status_code==403
    assert response.json()["error"]["code"]=="UNTRUSTED_HOST"

def test_body_bounds_even_with_no_content_length(client):
    result=client.post("/v1/audio/speech",content=b"x"*70000,headers={"Content-Type":"application/json"})
    assert result.status_code==413
    assert result.json()["error"]["code"]=="BODY_TOO_LARGE"

def test_browser_same_origin_does_not_block_legitimate_validation(client):
    response=client.post("/v1/audio/speech",json={},headers={"Origin":"http://testserver"})
    assert response.status_code==422  # payload invalid, not a forbidden Origin

def test_malformed_length_rejected(client):
    result=client.post("/models/licenses/accept",content=b"{}",
                       headers={"Content-Length":"-1","Content-Type":"application/json"})
    assert result.status_code in (400,422,403)


def test_chunked_payload_without_content_length_hits_receive_cap():
    import asyncio
    from lite.app import BoundedBodyASGI
    status=[]
    chunks=iter([b"x"*1024]*70)
    async def receive():
        try:
            piece=next(chunks)
            return {"type":"http.request","body":piece,"more_body":True}
        except StopIteration:
            return {"type":"http.request","body":b"","more_body":False}
    async def send(message):
        if message["type"]=="http.response.start":
            status.append(message["status"])
    async def read_body(scope,receive,send):
        while True:
            event=await receive()
            if not event.get("more_body"):
                break
        await send({"type":"http.response.start","status":200,"headers":[]})
        await send({"type":"http.response.body","body":b"OK"})
    guarded=BoundedBodyASGI(read_body,limit=1024*64)
    async def execute():
        await guarded({"type":"http","headers":[],"method":"POST","path":"/profiles"},receive,send)
    asyncio.run(execute())
    assert status==[413]
