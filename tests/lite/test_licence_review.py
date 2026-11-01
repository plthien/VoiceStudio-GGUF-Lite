"""Review receipt is bound to exact revision, document bytes, and terms fingerprint."""
import hashlib
import json
from lite.config import Settings, MODEL_REVISION, REPO_ID
from lite.profiles import Store
from lite.licence import Licence

def test_review_receipt_requires_document_and_new_manual_acceptance(tmp_path):
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"models",bin_dir=tmp_path/"bin",timeout=30)
    Store(s)
    license=Licence(s)
    before=license.status()
    assert not before["provenance_reconciled"]
    root=s.data_dir/"provenance"
    root.mkdir()
    card=root/"README.md"
    content=b"Reviewed full pinned document for synthetic test; not a live legal review"
    card.write_bytes(content)
    receipt={
        "repo_id":REPO_ID, "model_revision":MODEL_REVISION,
        "evidence_url":"https://huggingface.co/"+REPO_ID+"/blob/"+MODEL_REVISION+"/README.md",
        "model_licence":"CC-BY-NC-4.0","commercial_inference":"restricted",
        "reviewed_by":"Synthetic test reviewer", "reviewed_at":"2026-11-01",
        "document_sha256":hashlib.sha256(content).hexdigest()
    }
    (root/"review.json").write_text(json.dumps(receipt))
    new=license.status()
    assert new["provenance_reconciled"]
    assert new["fingerprint"]!=before["fingerprint"]
    assert not new["accepted"]
    assert not license.is_ready()
    license.change(expected=new["fingerprint"],action="accepted")
    assert license.is_ready()
    assert not license.status()["commercial_cleared"]
    card.write_bytes(b"mutated after review")
    assert not license.is_ready()
    assert license.status()["state"]=="terms_updated"

def test_review_receipt_wrong_model_and_document_hash_block(tmp_path):
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"models",bin_dir=tmp_path/"bin",timeout=30)
    Store(s)
    license=Licence(s)
    d=s.data_dir/"provenance"
    d.mkdir()
    (d/"README.md").write_bytes(b"card")
    record={"repo_id":REPO_ID,"model_revision":"0"*40,
            "evidence_url":"https://example.invalid/file",
            "model_licence":"CC-BY-NC-4.0",
            "commercial_inference":"restricted","reviewed_by":"test",
            "reviewed_at":"2026-11-01","document_sha256":"f"*64}
    (d/"review.json").write_text(json.dumps(record))
    assert not license.status()["provenance_reconciled"]
    assert not license.is_ready()
