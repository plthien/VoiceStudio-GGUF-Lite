"""As-installed dependency metadata inventory. Private D: file only."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path


def inventory():
    rows=[]
    for d in importlib.metadata.distributions():
        meta=d.metadata
        name=meta.get("Name") or "UNKNOWN"
        version=d.version
        rows.append({"name":name, "version":version,
                     "license_expression":meta.get("License-Expression"),
                     "license":(meta.get("License") or "")[:250],
                     "license_classifiers":[s for s in meta.get_all("Classifier",[])
                                           if "License ::" in s],
                     "project_urls":[str(v)[:240] for v in meta.get_all("Project-URL",[])][:12]})
    return {"schema":"chatterbox-installed-dependencies-v1",
            "application_license":"AGPL-3.0",
            "chatterbox_upstream_source_model_license":"MIT",
            "data_source":"installed Python distribution metadata",
            "build_dependent_notice":"FFmpeg executable license depends on its build; inspect locally",
            "packages":sorted(rows,key=lambda r:r["name"].lower())}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    out=args.output.resolve()
    if os.name=="nt" and out.drive.upper()!="D:":
        raise ValueError("D_DRIVE_ONLY")
    out.parent.mkdir(parents=True,exist_ok=True)
    import tempfile
    payload=(json.dumps(inventory(),indent=2,ensure_ascii=False)+"\n").encode("utf-8")
    fd,temp=tempfile.mkstemp(prefix=".chatterbox-inventory-",suffix=".tmp",dir=out.parent)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(temp,out)
    finally:
        if os.path.exists(temp):os.unlink(temp)


if __name__=="__main__":
    main()
