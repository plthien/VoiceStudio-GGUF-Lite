"""Native 24 kHz PCM16 audio measurements; listening is still mandatory."""
import array
import json
import math
import sys
import wave
from pathlib import Path

def measure(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, 24000, 2, "NONE"):
            raise ValueError("Expected 24k mono PCM16")
        raw = wav.readframes(wav.getnframes())
        rate = wav.getframerate()
    pcm = array.array("h")
    pcm.frombytes(raw)
    if sys.byteorder == "big":
        pcm.byteswap()
    if not pcm:
        raise ValueError("No audio frames")
    amplitude = [abs(s) / 32768 for s in pcm]
    rms = math.sqrt(sum(float(s)*float(s) for s in pcm) / len(pcm)) / 32768
    silence_limit = int(0.01*32768)
    start = next((i for i,s in enumerate(pcm) if abs(s)>silence_limit),len(pcm))
    end = next((i for i,s in enumerate(reversed(pcm)) if abs(s)>silence_limit),len(pcm))
    return {"duration_s":len(pcm)/rate,"sample_rate":rate,"channels":1,"bit_depth":16,
        "peak":max(amplitude),"rms":rms,
        "clipped_frames":sum(abs(s)>=32767 for s in pcm),
        "begin_silence_s":start/rate,"end_silence_s":end/rate}

if __name__=="__main__":
    print(json.dumps(measure(Path(sys.argv[1]))))
