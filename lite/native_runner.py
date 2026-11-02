"""Own exactly one native subprocess; never auto-replay or expose stderr."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
import re

from .audio import wav_file
from .config import Settings
from .runtime import model_pair, native_files, native_env, preflight, RuntimeErrorLite
from .speed import native_speed_capability, speed_to_native

LOG = logging.getLogger("gguf_lite")

class NativeBusy(RuntimeError):
    pass
class NativeTimeout(RuntimeError):
    pass
class NativeFailure(RuntimeError):
    pass

def native_command(exe: Path) -> list[str]:
    return [str(exe)]  # tests replace with [sys.executable, fake_py]

def native_attestation(stderr: bytes, detected: dict) -> dict:
    """Captured from this process, emitted by audited LiteAttest/v1 C++ instrumentation."""
    if not isinstance(stderr,bytes) or len(stderr)>131072:
        raise NativeFailure("Native stderr missing/truncated/oversize")
    lines=stderr.decode("utf-8","replace").splitlines()
    matches=[s.strip() for s in lines if "[LiteAttest/v1]" in s]
    if len(matches)!=1:
        raise NativeFailure("Native GPU evidence missing/ambiguous")
    match=re.fullmatch(r"\[LiteAttest/v1\] backend=Vulkan0; device=Vulkan0; description=(NVIDIA[^\r\n]{1,160})",matches[0])
    if not match:
        raise NativeFailure("Native did not use NVIDIA Vulkan0")
    native_name=match.group(1).upper()
    preflight_name=detected.get("name","").upper()
    if ("GTX 1650" not in native_name or "GTX 1650" not in preflight_name or
        detected.get("vendor_id") not in ("0x10de","4318")):
        raise NativeFailure("Native device conflicts with GTX 1650 evidence")
    return {"requested_backend":"Vulkan0","observed_native_backend":"Vulkan0",
            "observed_device_identity":match.group(1),
            "verification_evidence":"native-stderr:LiteAttest/v1",
            "verification_status":"verified"}

async def _bounded_communicate(proc: asyncio.subprocess.Process, payload: bytes) -> bytes:
    """Pipe is drained incrementally; never buffer arbitrary native stderr."""
    async def read_stderr():
        pieces=[]
        total=0
        while True:
            chunk=await proc.stderr.read(8192)
            if not chunk:
                return b"".join(pieces)
            total+=len(chunk)
            if total>131072:
                raise NativeFailure("Native verification stderr exceeded 128 KiB")
            pieces.append(chunk)

    async def send_stdin():
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError,ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError,ConnectionResetError):
                await proc.stdin.wait_closed()

    _,data,_=await asyncio.gather(send_stdin(),read_stderr(),proc.wait())
    return data

class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.active_id: str | None = None
        self.process: asyncio.subprocess.Process | None = None

    async def stop(self):
        if self.process is not None and self.process.returncode is None:
            await self._terminate(self.process)

    async def _terminate(self, proc):
        if proc.returncode is not None:
            return
        if os.name == "nt":
            # Kill entire Windows process tree (native can launch children).
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=8)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=10)

    async def generate(self, *, profile: dict, reference: Path,
                       text: str, language: str | None, seed: int | None,
                       request_id: str, speed: float = 1.0) -> tuple[bytes, dict]:
        if self.lock.locked():
            raise NativeBusy("GPU is busy with another request")
        effective_speed, native_speed_arg=speed_to_native(speed)
        if speed!=1.0 and not native_speed_capability(self.settings)["supported"]:
            raise RuntimeErrorLite("NATIVE_SPEED_UNAVAILABLE: verified patched native runtime required")
        async with self.lock:
            self.active_id = profile["id"]
            started = time.monotonic()
            try:
                state = await asyncio.to_thread(preflight, self.settings)
                if not state["ready"]:
                    raise RuntimeErrorLite("Runtime is not ready")
                exe, dll = native_files(self.settings)
                base, codec = model_pair(self.settings)
                # Rechecked under the lock immediately before spawn.
                with tempfile.TemporaryDirectory(prefix="lite-", dir=self.settings.data_dir) as work:
                    root = Path(work)
                    out, ref_text = root / "out.wav", root / "reference.txt"
                    ref_text.write_text(profile["ref_text"], encoding="utf-8")
                    argv = [str(exe), "--model", str(base), "--codec", str(codec),
                            "-o", str(out), "--ref-wav", str(reference),
                            "--ref-text", str(ref_text)]
                    lang = (language or profile["language"]).strip().lower()
                    if lang not in {"auto", "en", "english", "fr", "french", "es", "spanish",
                                   "de", "german", "it", "italian", "pt", "portuguese",
                                   "ja", "japanese", "zh", "chinese", "ko", "korean"}:
                        raise NativeFailure("Unsupported GGUF language")
                    if lang not in ("auto", ""):
                        labels = {"en":"English", "fr":"French", "es":"Spanish", "de":"German",
                                  "it":"Italian", "pt":"Portuguese", "ja":"Japanese",
                                  "zh":"Chinese", "ko":"Korean"}
                        argv += ["--lang", labels.get(lang, lang.capitalize())]
                    if seed is not None:
                        argv += ["--seed", str(seed)]
                    if speed!=1.0:
                        argv += ["--speed", native_speed_arg]
                    self.process = await asyncio.create_subprocess_exec(
                        *native_command(exe), *argv[1:], cwd=str(self.settings.bin_dir), env=native_env(self.settings),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=(os.name != "nt"),
                        creationflags=(getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
                                       if os.name == "nt" else 0))
                    try:
                        stderr = await asyncio.wait_for(
                            _bounded_communicate(self.process, text.encode("utf-8")),
                            timeout=self.settings.timeout)
                    except asyncio.TimeoutError as exc:
                        await self._terminate(self.process)
                        raise NativeTimeout("Native generation timed out") from exc
                    except BaseException:
                        await self._terminate(self.process)
                        raise
                    if self.process.returncode != 0:
                        raise NativeFailure("Native inference failed")
                    proof=native_attestation(stderr,state["gpu"])
                    # Check both pinned GGUFs again: change-during-request is not successful.
                    model_pair(self.settings)
                    info = wav_file(out, max_bytes=self.settings.max_output_bytes)
                    data = out.read_bytes()
                    elapsed = time.monotonic() - started
                    details = {"request_id": request_id, "profile_id": profile["id"],
                               "quant": "Q8_0", **proof,
                               "elapsed_s": round(elapsed, 3),
                               "audio_s": info["seconds"],
                               "rtf": round(elapsed / info["seconds"], 3),
                               "sha256": info["sha256"]}
                    LOG.info("native_complete request=%s profile=%s quant=Q8_0 gpu=Vulkan0 elapsed=%.3f audio=%.3f rtf=%.3f",
                             request_id, profile["id"], elapsed, info["seconds"], details["rtf"])
                    return data, details
            finally:
                # Reap the whole owned tree even if cancellation happens before communicate().
                if self.process is not None and self.process.returncode is None:
                    cleanup=asyncio.create_task(self._terminate(self.process))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
                self.process = None
                self.active_id = None
