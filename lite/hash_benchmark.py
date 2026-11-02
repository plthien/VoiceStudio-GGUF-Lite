"""Small synthetic streaming-hash overhead probe for CI; NOT user-host 945 MB evidence."""
from __future__ import annotations
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from .runtime import sha256

def main():
    size=8*1024*1024
    with tempfile.TemporaryDirectory(prefix="lite-hash-measure-") as td:
        path=Path(td)/"fixture.bin"
        path.write_bytes(b"\0"*size)
        observed=[]
        start=time.perf_counter()
        for _ in range(3):
            t=time.perf_counter()
            value=sha256(path)
            observed.append(time.perf_counter()-t)
            if value!=hashlib.sha256(path.read_bytes()).hexdigest():
                raise AssertionError("Streaming SHA-256 mismatch")
        print(json.dumps({"type":"synthetic_hash_overhead","size_mib":8,
            "iterations":3,"mean_s":statistics.mean(observed),
            "median_s":statistics.median(observed),"total_s":time.perf_counter()-start,
            "real_host_model_hash":"NOT VERIFIED"}))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
