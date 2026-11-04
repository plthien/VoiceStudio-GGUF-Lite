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


@pytest.mark.parametrize("fault",[
    "stage_wav","stage_summary","final_replace","summary_replace",
    "receipt_replace","fsync","cleanup"])
def test_v3_n01_fault_matrix_preserves_committed_pair(api,tmp_path,monkeypatch,fault):
    import lite.chatterbox_assembly as asm
    client,_,_=api
    voice=profile(client)["id"]
    wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    public=[output/"final.wav",output/"summary.json",output/"final-commit.json"]
    old=[p.read_bytes() for p in public]
    state=json.loads((output/"resume-manifest.json").read_text())
    original=asm.atomic
    calls=[0]
    def injected(path,data):
        calls[0]+=1
        stage=asm.FINAL_VERSIONS in str(path)
        label=("stage_wav" if stage and path.name=="final.wav"
               else "stage_summary" if stage and path.name=="summary.json"
               else "final_replace" if path==public[0]
               else "summary_replace" if path==public[1]
               else "receipt_replace" if path==public[2]
               else "")
        if fault==label:
            raise OSError("INJECTED_"+fault)
        return original(path,data)
    monkeypatch.setattr(asm,"atomic",injected)
    if fault=="fsync":
        old_fsync=asm.os.fsync
        fail=[False]
        def raise_once(fd):
            if not fail[0]:
                fail[0]=True
                raise OSError("INJECTED_fsync")
            return old_fsync(fd)
        monkeypatch.setattr(asm.os,"fsync",raise_once)
    elif fault=="cleanup":
        old_unlink=asm.os.unlink
        fail=[False]
        def unlink_once(file,*args,**kwargs):
            if ".chatterbox-" in str(file) and not fail[0]:
                fail[0]=True
                raise PermissionError("INJECTED_cleanup")
            return old_unlink(file,*args,**kwargs)
        monkeypatch.setattr(asm.os,"unlink",unlink_once)
    with pytest.raises((OSError,RuntimeError)):
        asm.publish_final_pair(output,old[0],json.loads(old[1]),job_key=state["job_key"])
    # If the newly staged receipt was not committed, old owner-visible files
    # must remain byte-identical and the old immutable receipt authoritative.
    assert [p.read_bytes() for p in public] == old
    assert json.loads(public[2].read_text())["schema"]==asm.FINAL_SCHEMA


def test_v3_n01_crash_between_views_restarts_from_old_receipt(api,tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    do_job(manifest,out)
    tracked=[out/"final.wav",out/"summary.json",out/"final-commit.json"]
    old=[p.read_bytes() for p in tracked]
    # Simulate process death directly after final.wav was replaced, before
    # summary.json/commit receipt. No cleanup handler executes.
    asm.atomic(out/"final.wav",audio(.5))
    assert tracked[0].read_bytes()!=old[0]
    recovered=do_job(manifest,out,resume=True)
    assert recovered["state"]=="PASS"
    assert [p.read_bytes() for p in tracked]==old
    assert len(posts)==2


def test_v3_n01_missing_receipt_first_publish_resume_no_replay(api,tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    original=asm.atomic
    failed=[False]
    def inject(path,data):
        if path.name=="summary.json" and not failed[0]:
            failed[0]=True
            raise OSError("INJECTED_FIRST_SUMMARY")
        return original(path,data)
    monkeypatch.setattr(asm,"atomic",inject)
    with pytest.raises(OSError,match="INJECTED_FIRST_SUMMARY"):
        do_job(manifest,out)
    assert len(posts)==2
    assert not (out/"final-commit.json").exists()
    assert not (out/"final.wav").exists()
    assert not (out/"summary.json").exists()
    monkeypatch.setattr(asm,"atomic",original)
    result=do_job(manifest,out,resume=True)
    assert result["state"]=="PASS"
    assert len(posts)==2
    assert (out/"final-commit.json").exists()


@pytest.mark.parametrize("artifact",["receipt","final","summary","section"])
def test_v3_n01_corrupted_committed_artifact_never_false_pass(api,tmp_path,monkeypatch,artifact):
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    do_job(manifest,out)
    mapping={"receipt":out/"final-commit.json","final":out/"final.wav",
             "summary":out/"summary.json","section":out/"section-a.wav"}
    path=mapping[artifact]
    path.write_bytes(b"corrupt"+path.read_bytes()[:10])
    if artifact in ("receipt","section"):
        with pytest.raises(RuntimeError,match="FINAL_PUBLICATION_RECOVERY_REQUIRED|RESUME_WAV_INVALID"):
            do_job(manifest,out,resume=True)
    else:
        # Receipt points at the previous immutable verified pair; a corrupted
        # projection can be restored safely without regeneration.
        assert do_job(manifest,out,resume=True)["state"]=="PASS"
    assert len(posts)==2


@pytest.mark.parametrize("mutation",["replace","truncate","delete","symlink"])
def test_v3_n02_out_of_band_reference_mutations(api,tmp_path,monkeypatch,mutation):
    client,_,settings=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    do_job(manifest,out)
    row=client.get("/profiles/"+voice).json()
    target=settings.data_dir/"voices"/row["ref_path"]
    if mutation=="replace":
        staged=tmp_path/"changed.wav";staged.write_bytes(audio(6.2))
        os.replace(staged,target)
    elif mutation=="truncate":
        target.write_bytes(target.read_bytes()[:200])
    elif mutation=="delete":
        target.unlink()
    else:
        target.unlink()
        external=tmp_path/"not-owned.wav"
        external.write_bytes(audio(6))
        try:target.symlink_to(external)
        except (OSError,NotImplementedError):
            pytest.skip("Insufficient symlink privileges")
    status=client.post("/profiles/"+voice+"/integrity/verify")
    assert status.status_code==409
    assert status.json()["error"]["code"]=="REFERENCE_CHANGED"
    with pytest.raises(RuntimeError,match="REFERENCE_CHANGED"):
        do_job(manifest,out,resume=True)
    assert len(posts)==2
    assert (out/"final-commit.json").is_file()


@pytest.mark.parametrize("field",["version","ref_sha256","source_sha256"])
def test_v3_n02_single_db_provenance_change_blocks_resume(api,tmp_path,monkeypatch,field):
    import sqlite3
    client,_,settings=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    do_job(manifest,out)
    with sqlite3.connect(settings.data_dir/"lite.sqlite3") as db:
        if field=="version":
            db.execute("UPDATE profiles SET version=version+1 WHERE id=?",(voice,))
        else:
            db.execute(f"UPDATE profiles SET {field}=? WHERE id=?",("f"*64,voice))
    with pytest.raises(RuntimeError,match="RESUME_PROVENANCE_CHANGED|REFERENCE_CHANGED"):
        do_job(manifest,out,resume=True)
    assert len(posts)==2


def test_v3_n02_attestation_busy_and_recovery_fail_closed(api):
    client,svc,_=api
    voice=profile(client)["id"]
    class Locked:
        def locked(self):return True
    previous=client.app.state.admission
    client.app.state.admission=Locked()
    response=client.post("/profiles/"+voice+"/integrity/verify")
    assert response.status_code==503
    assert response.json()["error"]["code"]=="REFERENCE_VERIFICATION_BUSY"
    client.app.state.admission=previous
    client.app.state.cache_trusted=False
    response=client.post("/profiles/"+voice+"/integrity/verify")
    assert response.status_code==503
    assert response.json()["error"]["code"]=="SERVICE_RECOVERY_REQUIRED"
    assert svc.loads==0


def test_v3_n01_os_process_kill_mid_projection_recovers(api,tmp_path,monkeypatch):
    import subprocess,sys
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    paths=[output/"final.wav",output/"summary.json",output/"final-commit.json"]
    originals=[p.read_bytes() for p in paths]
    code=r"""
import hashlib,json,os,sys
from pathlib import Path
import lite.chatterbox_assembly as asm
output=Path(sys.argv[1])
old=bytearray((output/"final.wav").read_bytes())
# Change an audio sample without changing WAV format/duration.
old[48]^=0x3f
summary=json.loads((output/"summary.json").read_text())
summary["final_sha256"]=hashlib.sha256(old).hexdigest()
key=json.loads((output/"resume-manifest.json").read_text())["job_key"]
native=asm.atomic
def die_after_first_public_view(path,data):
    native(path,data)
    if path==output/"final.wav":
        os._exit(97)
asm.atomic=die_after_first_public_view
asm.publish_final_pair(output,bytes(old),summary,job_key=key)
raise SystemExit(1)
"""
    proc=subprocess.run([sys.executable,"-c",code,str(output)],
                        cwd=Path(__file__).resolve().parents[2],timeout=20)
    assert proc.returncode==97
    assert paths[0].read_bytes()!=originals[0]
    assert paths[2].read_bytes()==originals[2]
    result=do_job(manifest,output,resume=True)
    assert result["state"]=="PASS"
    assert [p.read_bytes() for p in paths]==originals
    assert len(posts)==2


def test_v3_n01_legacy_v2_receiptless_resume_is_migration_free(api,tmp_path,monkeypatch):
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    output=tmp_path/"private"
    do_job(manifest,output)
    snapshot=[(output/p).read_bytes() for p in ("final.wav","summary.json")]
    (output/"final-commit.json").unlink()
    state=output/"resume-manifest.json"
    previous=json.loads(state.read_text())
    previous["schema"]="chatterbox-job-v1"
    state.write_text(json.dumps(previous))
    assert do_job(manifest,output,resume=True)["state"]=="PASS"
    assert [ (output/p).read_bytes() for p in ("final.wav","summary.json") ]==snapshot
    assert len(posts)==2


def test_v3_n01_legacy_forged_summary_cannot_claim_pass(api,tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    client,_,_=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    do_job(manifest,out)
    (out/"final-commit.json").unlink()
    state=out/"resume-manifest.json"
    payload=json.loads(state.read_text());payload["schema"]="chatterbox-job-v1"
    state.write_text(json.dumps(payload))
    final=out/"final.wav"
    changed=bytearray(final.read_bytes());changed[48]^=0x17
    final.write_bytes(changed)
    summary=out/"summary.json"
    claim=json.loads(summary.read_text())
    claim["final_sha256"]=hashlib.sha256(changed).hexdigest()
    summary.write_text(json.dumps(claim))
    # A receiptless legacy pair with matching SHA/summary is not enough:
    # reassemble verified sections and detect mismatched audio content.
    assert do_job(manifest,out,resume=True)["state"]=="PASS"
    assert hashlib.sha256(final.read_bytes()).hexdigest()!=claim["final_sha256"]
    assert len(posts)==2


def test_v3_n02_reference_modified_during_last_final_assembly_rejected(api,tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    client,_,settings=api
    voice=profile(client)["id"]
    posts=wire_http(monkeypatch,client)
    manifest=manifest_at(tmp_path,voice)
    out=tmp_path/"private"
    row=client.get("/profiles/"+voice).json()
    target=settings.data_dir/"voices"/row["ref_path"]
    native=asm.concatenate
    def mutate_after_assembly(parts,*args,**kwargs):
        result=native(parts,*args,**kwargs)
        target.write_bytes(audio(6.4))
        return result
    monkeypatch.setattr(asm,"concatenate",mutate_after_assembly)
    with pytest.raises(RuntimeError,match="REFERENCE_CHANGED"):
        do_job(manifest,out)
    assert len(posts)==2
    assert not (out/"final-commit.json").exists()
    assert not (out/"final.wav").exists()


def test_v3_n02_concurrent_profile_mutation_while_attesting_is_busy(api,monkeypatch):
    import concurrent.futures,threading
    client,_,_=api
    voice=profile(client)["id"]
    entered=threading.Event();release=threading.Event()
    store=client.app.state.store
    original=store.reference
    def gated(row):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("CI_BARRIER_TIMEOUT")
        return original(row)
    monkeypatch.setattr(store,"reference",gated)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        task=pool.submit(lambda:client.post("/profiles/"+voice+"/integrity/verify"))
        try:
            assert entered.wait(10)
            response=client.put("/profiles/"+voice,json={"name":"Changed"})
            assert response.status_code==409
            assert response.json()["error"]["code"]=="PROFILE_ACTIVE"
        finally:
            release.set()
        proof=task.result(timeout=10)
        assert proof.status_code==200
    assert client.get("/profiles/"+voice).json()["name"]=="Alpha"
