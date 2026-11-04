"""Stage validated candidate-only MaskGIT/FA controls without changing ABI-v2."""
from pathlib import Path
import hashlib,subprocess,sys
PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
def stage(root:Path):
    root=root.resolve(strict=True)
    ref="HEAD" if subprocess.run(["git","-C",root,"rev-parse","--verify","HEAD"],capture_output=True).returncode==0 else "FETCH_HEAD"
    if subprocess.run(["git","-C",root,"rev-parse",ref],capture_output=True,text=True,check=True).stdout.strip()!=PIN:raise ValueError("Pinned native revision mismatch")
    if "mg_cfg.num_step             = params->mg_num_step;" not in (root/"src/pipeline-tts.cpp").read_text(encoding="utf-8"):
        raise ValueError("MaskGIT setting anchor drift")
    if "p->use_fa      = true;" not in (root/"src/omnivoice.cpp").read_text(encoding="utf-8"):
        raise ValueError("flash-attention default anchor drift")
    source=Path(__file__).with_name("lite-maskgit-experiments.h").read_bytes(); target=root/"src/lite-maskgit-experiments.h"
    if target.exists():raise ValueError("MaskGIT experiments already staged")
    target.write_bytes(source)
    print("LITE_MASKGIT_EXPERIMENTS_STAGED",PIN,hashlib.sha256(source).hexdigest(),"GRAPH_REUSE_DEFERRED")
if __name__=="__main__":
    if len(sys.argv)!=2:raise SystemExit("usage: instrument-maskgit-experiments.py <pinned-source-root>")
    stage(Path(sys.argv[1]))
