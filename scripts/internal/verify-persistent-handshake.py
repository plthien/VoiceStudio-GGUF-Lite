"""Bounded pre-activation LitePersistent/v1 HELLO; never loads model/GPU."""
import argparse, os, struct, subprocess

MAGIC=0x3150544C
HEADER=struct.Struct("<IHHII")

def main():
    p=argparse.ArgumentParser();p.add_argument("exe");args=p.parse_args()
    proc=subprocess.Popen([args.exe,"--lite-worker-stdio"],stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          cwd=os.path.dirname(os.path.abspath(args.exe)))
    try:
        request=HEADER.pack(MAGIC,1,1,7,0)+HEADER.pack(MAGIC,1,5,8,0)
        output,stderr=proc.communicate(request,timeout=8)
        if len(output)!=HEADER.size*2+17 or len(stderr)>32768:
            raise RuntimeError("worker handshake output invalid")
        first=output[:HEADER.size]
        if HEADER.unpack(first)!=(MAGIC,1,101,7,17): raise RuntimeError("worker handshake header invalid")
        if output[HEADER.size:HEADER.size+17]!=b"LitePersistent/v1": raise RuntimeError("worker marker invalid")
        second=output[HEADER.size+17:]
        if HEADER.unpack(second)!=(MAGIC,1,104,8,0): raise RuntimeError("worker shutdown reply invalid")
        if proc.returncode: raise RuntimeError("worker handshake exit invalid")
        print("LITE_PERSISTENT_HANDSHAKE_PASS")
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=5)

if __name__=="__main__": main()
