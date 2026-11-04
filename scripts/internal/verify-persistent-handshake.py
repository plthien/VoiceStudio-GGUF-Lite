"""Bounded pre-activation LitePersistent/v1 HELLO; never loads model/GPU."""
import argparse, os, struct, subprocess

MAGIC=0x3150544C
HEADER=struct.Struct("<IHHII")

def main():
    p=argparse.ArgumentParser();p.add_argument("exe");args=p.parse_args()
    env={**os.environ,"GGUF_LITE_PERF":"1"}
    proc=subprocess.Popen([args.exe,"--lite-worker-stdio"],stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          cwd=os.path.dirname(os.path.abspath(args.exe)),env=env)
    try:
        request=(HEADER.pack(MAGIC,1,1,7,0)+HEADER.pack(MAGIC,1,6,8,0)+
                 HEADER.pack(MAGIC,1,5,9,0))
        output,stderr=proc.communicate(request,timeout=8)
        perf_size=11+struct.calcsize("<III16QIII")
        if len(output)!=HEADER.size*3+17+perf_size or len(stderr)>32768:
            raise RuntimeError("worker handshake output invalid")
        first=output[:HEADER.size]
        if HEADER.unpack(first)!=(MAGIC,1,101,7,17): raise RuntimeError("worker handshake header invalid")
        if output[HEADER.size:HEADER.size+17]!=b"LitePersistent/v1": raise RuntimeError("worker marker invalid")
        perf_at=HEADER.size+17
        perf_header=output[perf_at:perf_at+HEADER.size]
        if HEADER.unpack(perf_header)!=(MAGIC,1,105,8,perf_size): raise RuntimeError("worker PERF header invalid")
        perf=output[perf_at+HEADER.size:perf_at+HEADER.size+perf_size]
        if (not perf.startswith(b"LitePerf/v1") or struct.unpack_from("<I",perf,11)[0]!=1 or
                struct.unpack_from("<I",perf,15)[0]!=struct.calcsize("<III16QIII")):
            raise RuntimeError("worker PERF payload invalid")
        second=output[perf_at+HEADER.size+perf_size:]
        if HEADER.unpack(second)!=(MAGIC,1,104,9,0): raise RuntimeError("worker shutdown reply invalid")
        if proc.returncode: raise RuntimeError("worker handshake exit invalid")
        print("LITE_PERSISTENT_HANDSHAKE_AND_PERF_PASS")
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=5)

if __name__=="__main__": main()
