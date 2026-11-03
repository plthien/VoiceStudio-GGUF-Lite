"""Stage the exact pin and verify patches, ABI, and optional real target link.

Windows/Ubuntu CI. No model files, GPU inference, or vendored upstream.
"""
from pathlib import Path
import hashlib,os,shutil,subprocess,sys,tempfile

ROOT=Path(__file__).resolve().parents[2]
PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
SOURCES=["CMakeLists.txt","src/duration-estimator.h","src/omnivoice.h","src/omnivoice.cpp",
         "src/pipeline-tts.cpp","src/pipeline-tts.h","src/pipeline-codec.cpp","src/maskgit-tts.h",
         "src/backend.h","tools/omnivoice-tts.cpp"]
def command(args,**kwargs):
    timeout=kwargs.pop("timeout",180)
    return subprocess.run([str(a) for a in args],check=True,timeout=timeout,**kwargs)

def main():
    link_worker = sys.argv[1:] == ["--link-worker"]
    if sys.argv[1:] not in ([], ["--link-worker"]):
        raise SystemExit("usage: verify-native-speed.py [--link-worker]")
    with tempfile.TemporaryDirectory(prefix="lite-native-speed-ci-") as t:
        work=Path(t)
        upstream=work/"upstream"
        command(["git","init","-q",upstream])
        command(["git","-C",upstream,"fetch","--no-tags","--depth","1",
                 "https://github.com/ServeurpersoCom/omnivoice.cpp.git",PIN])
        sha=command(["git","-C",upstream,"rev-parse","FETCH_HEAD"],capture_output=True,text=True).stdout.strip()
        assert sha==PIN,(sha,PIN)
        if link_worker:
            command(["git","-C",upstream,"checkout","--detach","FETCH_HEAD"])
            command(["git","-C",upstream,"submodule","update","--init","--recursive"], timeout=900)
        else:
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
        # Staged feasibility target must compile against the real PINNED,
        # Speed ABI-v2 patched header; this is NOT a real model execution.
        staged=command([sys.executable,ROOT/"scripts/internal/stage-residency-feasibility.py",upstream],
                       capture_output=True,text=True)
        assert "LITE_REUSE_PROBE_STAGED" in staged.stdout
        cmake=(upstream/"CMakeLists.txt").read_text()
        assert "add_executable(lite-reuse-probe tools/lite-reuse-probe.cpp)" in cmake
        assert "target_link_libraries(lite-reuse-probe PRIVATE omnivoice-core)" in cmake
        denied=subprocess.run([sys.executable,ROOT/"scripts/internal/stage-residency-feasibility.py",upstream],
                              capture_output=True,text=True,timeout=30)
        assert denied.returncode!=0,"Double staging must fail closed"
        perf=command([sys.executable,ROOT/"scripts/internal/instrument-native-performance.py",upstream],
                     capture_output=True,text=True)
        assert "LITE_NATIVE_PERF_STAGED" in perf.stdout
        perf_files=[upstream/x for x in ("src/pipeline-tts.cpp","src/pipeline-codec.cpp","src/maskgit-tts.h","src/omnivoice.cpp","src/lite-native-perf.h")]
        perf_hashes={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in perf_files}
        perf_again=subprocess.run([sys.executable,ROOT/"scripts/internal/instrument-native-performance.py",upstream],capture_output=True,text=True,timeout=60)
        assert perf_again.returncode!=0 and perf_hashes=={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in perf_files}
        cache=command([sys.executable,ROOT/"scripts/internal/instrument-ref-conditioning-cache.py",upstream],
                      capture_output=True,text=True)
        assert "LITE_REFERENCE_CACHE_STAGED" in cache.stdout
        cache_files=[upstream/x for x in ("src/pipeline-tts.cpp","src/pipeline-tts.h","src/lite-reference-cache.h")]
        cache_hashes={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in cache_files}
        cache_again=subprocess.run([sys.executable,ROOT/"scripts/internal/instrument-ref-conditioning-cache.py",upstream],capture_output=True,text=True,timeout=60)
        assert cache_again.returncode!=0 and cache_hashes=={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in cache_files}
        experiments=command([sys.executable,ROOT/"scripts/internal/instrument-maskgit-experiments.py",upstream],
                            capture_output=True,text=True)
        assert "GRAPH_REUSE_DEFERRED" in experiments.stdout
        persistent=command([sys.executable,ROOT/"scripts/internal/stage-persistent-worker.py",upstream],
                           capture_output=True,text=True)
        assert "LITE_PERSISTENT_WORKER_STAGED" in persistent.stdout
        cli=(upstream/"tools/omnivoice-tts.cpp").read_text()
        assert "--lite-worker-stdio" in cli and "LitePersistent/v1" in cli
        assert "tools/lite-persistent-worker.cpp" in (upstream/"CMakeLists.txt").read_text()
        worker=(upstream/"tools/lite-persistent-worker.cpp").read_text()
        assert worker.count("ov_init(&ip)")==1
        assert worker.count("ov_synthesize_with_speed(ctx")==1
        assert "ov_audio_free(&audio)" in worker and "if(ctx)ov_free(ctx)" in worker
        compiler=shutil.which("clang++") or shutil.which("g++")
        if compiler is None:
            raise RuntimeError("A C++17 compiler (clang++/g++) is mandatory for native speed CI")
        command([compiler,"-std=c++17","-fsyntax-only","-I",upstream/"src",
                 upstream/"tools/lite-reuse-probe.cpp"])
        command([compiler,"-std=c++17","-fsyntax-only","-I",upstream/"src",
                 upstream/"tools/lite-persistent-worker.cpp"])
        print("PINNED_PERSISTENT_WORKER_COMPILES_MODEL_NOT_RUN",PIN)
        print("PINNED_REAL_CONTEXT_A_B_A_HARNESS_COMPILES_MODEL_NOT_RUN",PIN)
        exe=work/("speed-check.exe" if os.name=="nt" else "speed-check")
        command([compiler,"-std=c++17","-O0","-I",upstream/"src",
                 ROOT/"scripts/internal/native-speed-ci.cpp","-o",exe])
        command([exe])
        perf_exe=work/("performance-check.exe" if os.name=="nt" else "performance-check")
        command([compiler,"-std=c++17","-O0","-I",upstream/"src",
                 ROOT/"scripts/internal/native-performance-ci.cpp","-o",perf_exe])
        command([perf_exe])
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
        print("PINNED_NATIVE_PERFORMANCE_CACHE_FIXTURE_PASS",PIN)
        if link_worker:
            build=work/"real-linked-worker"
            command(["cmake","-S",upstream,"-B",build,"-DCMAKE_BUILD_TYPE=Release",
                     "-DGGML_VULKAN=OFF","-DGGML_BACKEND_DL=OFF",
                     "-DBUILD_SHARED_LIBS=OFF","-DGGML_NATIVE=OFF",
                     "-DGGML_CPU_ALL_VARIANTS=OFF"], timeout=900)
            command(["cmake","--build",build,"--config","Release","--target",
                     "omnivoice-tts","--parallel","2"], timeout=1200)
            names={"omnivoice-tts","omnivoice-tts.exe"}
            candidates=[p for p in build.rglob("omnivoice-tts*")
                        if p.is_file() and p.name in names]
            if len(candidates)!=1:
                raise RuntimeError(f"linked omnivoice-tts candidate not unique: {candidates}")
            linked=candidates[0]
            help_result=command([linked,"--help"],capture_output=True,text=True)
            help_text=help_result.stdout+help_result.stderr
            assert "LitePersistent/v1" in help_text and "LiteNativeSpeed/v1" in help_text
            handshake=command([sys.executable,ROOT/"scripts/internal/verify-persistent-handshake.py",linked],
                              capture_output=True,text=True)
            assert "HANDSHAKE_AND_PERF_PASS" in handshake.stdout
            print("PINNED_REAL_OMNIVOICE_TARGET_LINK_AND_HANDSHAKE_PASS_CPU_NO_MODEL",PIN)
        print("PINNED_SOURCE_HASHES",before)
if __name__=="__main__":
    main()
