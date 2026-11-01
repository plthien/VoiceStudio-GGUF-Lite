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

from .audio import wav_file
from .config import Settings
from .runtime import model_pair, native_files, native_env, preflight, RuntimeErrorLite

LOG = logging.getLogger("gguf_lite")

class NativeBusy(RuntimeError):
    pass
class NativeTimeout(RuntimeError):
    pass
class NativeFailure(RuntimeError):
    pass

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
                       request_id: str) -> tuple[bytes, dict]:
        if self.lock.locked():
            raise NativeBusy("GPU is busy with another request")
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
                    self.process = await asyncio.create_subprocess_exec(
                        *argv, cwd=str(self.settings.bin_dir), env=native_env(self.settings),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=(os.name != "nt"),
                        creationflags=(getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
                                       if os.name == "nt" else 0))
                    try:
                        _, stderr = await asyncio.wait_for(
                            self.process.communicate(text.encode("utf-8")),
                            timeout=self.settings.timeout)
                    except asyncio.TimeoutError as exc:
                        await self._terminate(self.process)
                        raise NativeTimeout("Native generation timed out") from exc
                    except BaseException:
                        await self._terminate(self.process)
                        raise
                    if self.process.returncode != 0:
                        raise NativeFailure("Native inference failed")
                    info = wav_file(out, max_bytes=self.settings.max_output_bytes)
                    data = out.read_bytes()
                    elapsed = time.monotonic() - started
                    details = {"request_id": request_id, "profile_id": profile["id"],
                               "quant": "Q8_0", "gpu": "Vulkan0",
                               "elapsed_s": round(elapsed, 3),
                               "audio_s": info["seconds"],
                               "rtf": round(elapsed / info["seconds"], 3),
                               "sha256": info["sha256"]}
                    LOG.info("native_complete request=%s profile=%s quant=Q8_0 gpu=Vulkan0 elapsed=%.3f audio=%.3f rtf=%.3f",
                             request_id, profile["id"], elapsed, info["seconds"], details["rtf"])
                    return data, details
            finally:
                self.process = None
                self.active_id = None
