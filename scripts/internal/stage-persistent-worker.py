"""Anchor the real one-context stdio mode into the exact pinned native CLI."""
from pathlib import Path
import hashlib
import subprocess
import sys

PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
CMAKE_OLD="add_executable(omnivoice-tts tools/omnivoice-tts.cpp)"
CMAKE_NEW="add_executable(omnivoice-tts tools/omnivoice-tts.cpp tools/lite-persistent-worker.cpp)"
MAIN_ANCHOR="static int main_impl(int argc, char ** argv) {\n    if (argc <= 1) {"
MAIN_PATCH="""int lite_persistent_worker_main();
static int main_impl(int argc, char ** argv) {
    if (argc == 2 && strcmp(argv[1], "--lite-worker-stdio") == 0) {
        return lite_persistent_worker_main();
    }
    if (argc <= 1) {"""
HELP_ANCHOR='            "Optional:\\n"'
HELP_PATCH='            "Persistent worker:\\n  --lite-worker-stdio   LitePersistent/v1 private framed stdio mode\\n\\n"\n            "Optional:\\n"'

def stage(root: Path):
    root=root.resolve(strict=True)
    ref="HEAD" if subprocess.run(["git","-C",root,"rev-parse","--verify","HEAD"],capture_output=True).returncode==0 else "FETCH_HEAD"
    rev=subprocess.run(["git","-C",root,"rev-parse",ref],capture_output=True,text=True,check=True).stdout.strip()
    if rev!=PIN: raise ValueError("Pinned native revision mismatch")
    source=root/"tools/omnivoice-tts.cpp"; cmake=root/"CMakeLists.txt"
    cli=source.read_text(encoding="utf-8"); build=cmake.read_text(encoding="utf-8")
    if cli.count(MAIN_ANCHOR)!=1 or cli.count(HELP_ANCHOR)!=1 or build.count(CMAKE_OLD)!=1:
        raise ValueError("Persistent worker anchor drift")
    worker=Path(__file__).with_name("lite-persistent-worker.cpp").read_bytes()
    if b"ov_init(&ip)" not in worker or b"ov_synthesize_with_speed(ctx" not in worker or b"LitePersistent/v1" not in worker:
        raise ValueError("Persistent worker source contract missing")
    target=root/"tools/lite-persistent-worker.cpp"
    if target.exists() or "--lite-worker-stdio" in cli: raise ValueError("Persistent worker already staged")
    target.write_bytes(worker)
    source.write_text(cli.replace(MAIN_ANCHOR,MAIN_PATCH).replace(HELP_ANCHOR,HELP_PATCH),encoding="utf-8")
    cmake.write_text(build.replace(CMAKE_OLD,CMAKE_NEW),encoding="utf-8")
    print("LITE_PERSISTENT_WORKER_STAGED",PIN,hashlib.sha256(worker).hexdigest())

if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit("usage: stage-persistent-worker.py <pinned-source-root>")
    stage(Path(sys.argv[1]))
