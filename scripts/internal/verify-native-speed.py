"""Stage exact upstream pin, apply BOTH source patches and compile/run estimator harness.

Windows/Ubuntu CI. No full GGUF build, no GPU claims, no vendored upstream.
"""
from pathlib import Path
import hashlib,os,shutil,subprocess,sys,tempfile

ROOT=Path(__file__).resolve().parents[2]
PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
SOURCES=["src/duration-estimator.h","src/omnivoice.h","src/omnivoice.cpp",
         "src/pipeline-tts.cpp","src/pipeline-tts.h","src/backend.h","tools/omnivoice-tts.cpp"]
def command(args,**kwargs):
    return subprocess.run([str(a) for a in args],check=True,timeout=180,**kwargs)

def main():
    with tempfile.TemporaryDirectory(prefix="lite-native-speed-ci-") as t:
        work=Path(t)
        upstream=work/"upstream"
        command(["git","init","-q",upstream])
        command(["git","-C",upstream,"fetch","--no-tags","--depth","1",
                 "https://github.com/ServeurpersoCom/omnivoice.cpp.git",PIN])
        sha=command(["git","-C",upstream,"rev-parse","FETCH_HEAD"],capture_output=True,text=True).stdout.strip()
        assert sha==PIN,(sha,PIN)
        command(["git","-C",upstream,"checkout","FETCH_HEAD","--",*SOURCES])
        before={name:hashlib.sha256((upstream/name).read_bytes()).hexdigest() for name in SOURCES}
        v2=work/"legacy-v2"
        v2.mkdir()
        (v2/"omnivoice.h").write_bytes((upstream/"src/omnivoice.h").read_bytes())
        def run_patch():
            check=subprocess.run([sys.executable,ROOT/"scripts/internal/instrument-native-speed.py",upstream],
                                 capture_output=True,text=True,timeout=120)
            if check.returncode!=0:
                raise RuntimeError("Native pinned patch failed: "+check.stderr+check.stdout)
            return check
        response=run_patch()
        assert "LiteNativeSpeed/v1 patched" in response.stdout
        assert "LiteNativeSpeed/v1" in (upstream/"tools/omnivoice-tts.cpp").read_text()
        assert "OV_ABI_VERSION 2" in (upstream/"src/omnivoice.h").read_text()
        assert "ov_synthesize_with_speed(" in (upstream/"src/omnivoice.h").read_text()
        assert "float speed;" not in (upstream/"src/omnivoice.h").read_text()
        assert "p->speed" not in (upstream/"src/omnivoice.cpp").read_text()
        assert "ov_synthesize_with_speed(" in (upstream/"src/omnivoice.cpp").read_text()
        assert "float                 lite_speed);" in (upstream/"src/pipeline-tts.h").read_text()
        p=(upstream/"src/pipeline-tts.cpp").read_text()
        assert p.count("duration_estimate_tokens_speed(")==4
        assert p.count("lite_speed, params->denoise")==2
        assert p.count("params->chunk_threshold_sec, lite_speed")==1
        assert "LiteAttest/v1" not in p
        rejected=subprocess.run([sys.executable,ROOT/"scripts/internal/instrument-native-speed.py",upstream],
                               capture_output=True,text=True,timeout=60)
        assert rejected.returncode!=0
        # Patch of a drifted upstream must not alter ANY source path.
        drift=work/"drift"
        for name in SOURCES:
            path=drift/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((upstream/name).read_bytes())
        (drift/"src/lite-native-speed.h").unlink(missing_ok=True)
        path=drift/"tools/omnivoice-tts.cpp"
        path.write_text(path.read_text().replace("LiteNativeSpeed/v1","OutdatedPatch"),encoding="utf8")
        fail=subprocess.run([sys.executable,ROOT/"scripts/internal/instrument-native-speed.py",drift],
                           capture_output=True,text=True,timeout=60)
        assert fail.returncode!=0
        # Prepare fresh pinned source for attestation exact anchor smoke.
        backend=(upstream/"src/backend.h")
        command([sys.executable,ROOT/"scripts/internal/instrument-attestation.py",backend])
        assert "[LiteAttest/v1]" in backend.read_text()
        compiler=shutil.which("clang++") or shutil.which("g++")
        if compiler is None:
            raise RuntimeError("A C++17 compiler (clang++/g++) is mandatory for native speed CI")
        exe=work/("speed-check.exe" if os.name=="nt" else "speed-check")
        command([compiler,"-std=c++17","-O0","-I",upstream/"src",
                 ROOT/"scripts/internal/native-speed-ci.cpp","-o",exe])
        command([exe])
        # Compile real upstream-patched initializer body with the original
        # (saved) ABI-v2 caller, and a new-header translation unit. The
        # initializer is extracted from the actual C++ file, not rewritten.
        source=(upstream/"src/omnivoice.cpp").read_text(encoding="utf-8")
        start=source.index("void ov_tts_default_params(struct ov_tts_params * p) {")
        stop=source.index("\n}",start)+2
        initializer=source[start:stop]
        implementation=work/"native-default-params.cpp"
        implementation.write_text('#include "omnivoice.h"\nextern "C" {\n'+initializer+'\n}\n',encoding="utf8")
        oldcaller=work/"legacy-v2-caller.cpp"
        oldcaller.write_text('''#include "omnivoice.h"
#include <cstdint>
extern "C" int legacy_v2_compatible() {
    struct Guard { ov_tts_params params; uint64_t canary; };
    Guard v = {};
    v.canary = UINT64_C(0xa55a04eed034ee5a);
    ov_tts_default_params(&v.params);
    if (v.canary!=UINT64_C(0xa55a04eed034ee5a)) return -1;
    if (v.params.abi_version!=2 || v.params.chunk_threshold_sec!=30.0f) return -2;
    return (int)sizeof(v.params);
}
''',encoding="utf8")
        fresh=work/"native-abi-new.cpp"
        fresh.write_text('''#include "omnivoice.h"
#include <cassert>
extern "C" int legacy_v2_compatible();
int main() { assert(OV_ABI_VERSION==2); assert(legacy_v2_compatible()==(int)sizeof(ov_tts_params)); }
''',encoding="utf8")
        objects=[]
        for name,src,include in (("legacy",oldcaller,v2),
                                  ("init",implementation,upstream/"src"),
                                  ("new",fresh,upstream/"src")):
            obj=work/(name+".o")
            command([compiler,"-std=c++17","-O0","-c","-I",include,src,"-o",obj])
            objects.append(obj)
        abi_exe=work/("legacy-abi.exe" if os.name=="nt" else "legacy-abi")
        command([compiler,*objects,"-o",abi_exe])
        command([abi_exe])
        print("PINNED_NATIVE_ABI_V2_GUARDED_INITIALIZER_PASS",PIN)
        print("PINNED_NATIVE_SPEED_PATCH_AND_COMPILED_ESTIMATOR_PASS",PIN)
        print("PINNED_SOURCE_HASHES",before)
if __name__=="__main__":
    main()
