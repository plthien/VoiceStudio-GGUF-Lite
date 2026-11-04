"""V3 fail-first regressions: genuine Store/filesystem and final transaction behavior."""
from __future__ import annotations
import hashlib
import io
import json
import os
import wave
from pathlib import Path

import pytest
from .test_contracts import api, profile, audio
from lite.chatterbox_assembly import do_job


def wire_http(monkeypatch, client):
    """Actual app routes + real disk Store; only the inference response is fake."""
    import lite.chatterbox_assembly as asm
    posts=[]
    def routed(_base,path,payload=None,timeout=10):
        response=(client.post(path,json=payload) if payload is not None else client.get(path))
        if response.status_code != 200:
            raise RuntimeError("REFERENCE_CHANGED")
        return response.json()
    class Speech:
        headers={"X-Lite-Pause-Count":"0","X-TTS-Backend":"MOCK"}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return audio(.5)
    def speech(request,**kwargs):
        posts.append(json.loads(request.data))
        return Speech()
    monkeypatch.setattr(asm,"http_json",routed)
    monkeypatch.setattr(asm.urllib.request,"urlopen",speech)
    return posts


def manifest_at(tmp_path,voice):
    manifest=tmp_path/"owner-input.json"
    manifest.write_text(json.dumps({"voice":voice,"sections":[
        {"id":"a","input":"One speech."},{"id":"b","input":"Other speech."}]}))
    return manifest


def test_v3_n01_clean_completed_resume_preserves_pair_receipt_and_no_post(api,tmp_path,monkeypatch):
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    assert len(posts)==2
    paths=[output/"final.wav",output/"summary.json",output/"final-commit.json"]
    before=[p.read_bytes() for p in paths]
    result=do_job(manifest,output,resume=True)
    assert result["state"]=="PASS"
    assert [p.read_bytes() for p in paths]==before
    assert len(posts)==2


def test_v3_n01_summary_failure_on_rebuild_preserves_prior_pair(api,tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    client,_,_=api
    voice=profile(client)["id"]
    wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    paths=[output/"final.wav",output/"summary.json",output/"final-commit.json"]
    old=[p.read_bytes() for p in paths]
    original=asm.atomic
    def fail_summary(path,data):
        if path==output/"summary.json":raise OSError("INJECTED_SUMMARY_REPLACE")
        return original(path,data)
    monkeypatch.setattr(asm,"atomic",fail_summary)
    # Direct forced rebuild is an internal recovery operation; prior trusted
    # receipt must retain all three public bytes when a new publish fails.
    with pytest.raises(OSError,match="INJECTED_SUMMARY_REPLACE"):
        asm.publish_final_pair(output,old[0],json.loads(old[1]),
                               job_key=json.loads((output/"resume-manifest.json").read_text())["job_key"])
    assert [p.read_bytes() for p in paths]==old


def test_v3_n02_on_disk_corruption_rejected_on_all_pass_resume(api,tmp_path,monkeypatch):
    client,_,settings=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    assert len(posts)==2
    row=client.get("/profiles/"+voice).json()
    target=settings.data_dir/"voices"/row["ref_path"]
    target.write_bytes(audio(6.3))  # Change real bytes; DB row unchanged.
    status=client.post("/profiles/"+voice+"/integrity/verify")
    assert status.status_code==409 and status.json()["error"]["code"]=="REFERENCE_CHANGED"
    with pytest.raises(RuntimeError,match="REFERENCE_CHANGED"):
        do_job(manifest,output,resume=True)
    assert len(posts)==2
    assert (output/"final.wav").is_file()


def test_v3_n02_attestation_stable_is_opaque_and_without_cuda(api):
    client,svc,settings=api
    row=profile(client)
    voice=row["id"]
    result=client.post("/profiles/"+voice+"/integrity/verify")
    assert result.status_code==200,result.text
    value=result.json()
    assert value["verified"] is True and len(value["binding"])==64
    assert row["ref_sha256"] not in json.dumps(value)
    assert row["source_sha256"] not in json.dumps(value)
    assert svc.loads==0 and svc.syntheses==0
    again=client.post("/profiles/"+voice+"/integrity/verify")
    assert again.json()==value


def test_v3_n02_file_replace_symlink_missing_fail_closed(api,tmp_path):
    client,svc,settings=api
    row=profile(client)
    path=settings.data_dir/"voices"/row["ref_path"]
    original=path.read_bytes()
    import shutil
    path.unlink()
    missing=client.post("/profiles/"+row["id"]+"/integrity/verify")
    assert missing.status_code==409
    path.write_bytes(original)
    outside=tmp_path/"outside.wav"
    outside.write_bytes(original)
    path.unlink()
    try:
        path.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("OS does not allow creating symlink")
    attack=client.post("/profiles/"+row["id"]+"/integrity/verify")
    assert attack.status_code==409
    assert str(outside) not in attack.text
    assert svc.loads==0
