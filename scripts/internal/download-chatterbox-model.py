"""Download exactly the approved HF snapshot; private SHA256 manifest, never Git."""
import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

SOURCE = "ab55923c27757c90cfcc407baa57dc25a7d25217"
REV = "71ccd1d0081b430592cea481f4307e764e07bc64"
REPO = "ResembleAI/chatterbox-nano"
REQUIRED = ("t3_nano_v1.safetensors","s3gen_meanflow.safetensors","ve.safetensors",
            "added_tokens.json","merges.txt","vocab.json","special_tokens_map.json","tokenizer_config.json")
EXPECTED = {
    "t3_nano_v1.safetensors":"72b110185087d945dbdf54dee4e333848e1811bdd5fd6cb16ceb8da50006f0c9",
    "s3gen_meanflow.safetensors":"d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f",
    "ve.safetensors":"f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c",
    "added_tokens.json":"72e4ab6acb0d9309ac3df4b526ae5fd80a2da5bc5ab7bb02d85096a374f69193",
    "merges.txt":"1ce1664773c50f3e0cc8842619a93edc4624525b728b188a9e0be33b7726adc5",
    "vocab.json":"f6bd25a65e4e63ca31360e9fb11c7e4f9a391a78385d640acd814092dd6eee4f",
    "special_tokens_map.json":"92ba8063bf40aa163eadebbfe0de07c2aebe44cf0d4a9e8726580b0781fd2640",
    "tokenizer_config.json":"bca16a2ac1ddbd78b8d6228f0031884cc74b6ea54b967d6f6d2ebae9ccde23e6"
}
def run(root):
    target=root/"models"/REV
    target.mkdir(parents=True,exist_ok=True)
    snapshot_download(repo_id=REPO, revision=REV,
                      allow_patterns=list(REQUIRED), local_dir=str(target))
    files={}
    for name in REQUIRED:
        path=target/name
        if not path.is_file() or path.is_symlink() or path.resolve().parent!=target.resolve():
            raise RuntimeError("MODEL_FILES_MISSING")
        h=hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024*1024),b""):
                h.update(block)
        digest=h.hexdigest()
        if digest!=EXPECTED[name]:
            raise RuntimeError("MODEL_HASH_MISMATCH")
        files[name]=digest
    payload=json.dumps({"schema":"chatterbox-nano-model-manifest-v1",
                        "model_id":"chatterbox-nano","repo_id":REPO,
                        "loader_mode":"nano=True","revision":REV,
                        "source_revision":SOURCE,"sha256":files},sort_keys=True,indent=2).encode()
    import os,tempfile
    fd,temp=tempfile.mkstemp(prefix=".manifest-",dir=target)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(temp,target/"manifest.json")
    finally:
        if os.path.exists(temp):os.unlink(temp)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--root",type=Path,required=True)
    run(p.parse_args().root)
