"""Download exactly the approved HF snapshot; private SHA256 manifest, never Git."""
import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

SOURCE = "5de7a54aa4e5e2baadb0182dde554908b48b85c2"
REV = "74971fa28ff1cdf9a2cf4b41d102cb31fd32d411"
REQUIRED = ("t3_turbo_v1.safetensors","s3gen_meanflow.safetensors","ve.safetensors",
            "added_tokens.json","merges.txt","vocab.json","special_tokens_map.json","tokenizer_config.json")
EXPECTED = {
    "t3_turbo_v1.safetensors":"fcf1f8c1d651bb7e3acd69ee5be269b4ac10c02980b7708213d598bc9f7cdf87",
    "s3gen_meanflow.safetensors":"d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f"
}
def run(root):
    target=root/"models"/REV
    target.mkdir(parents=True,exist_ok=True)
    snapshot_download(repo_id="ResembleAI/chatterbox-turbo", revision=REV,
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
        if name in EXPECTED and digest!=EXPECTED[name]:
            raise RuntimeError("MODEL_HASH_MISMATCH")
        files[name]=digest
    payload=json.dumps({"schema":"chatterbox-model-manifest-v1","revision":REV,
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
