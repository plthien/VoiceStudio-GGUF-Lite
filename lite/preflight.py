"""python -m lite.preflight: conservative dry run (no downloads)."""
import json
import shutil
from .config import Settings
from .runtime import preflight
from .licence import Licence
from .readiness import complete_readiness

def main():
    settings = Settings.load()
    engine = preflight(settings)
    status = Licence(settings).status()
    report = complete_readiness(engine,status,store_healthy=settings.data_dir.is_dir(),
                                ffmpeg_installed=bool(shutil.which("ffmpeg")))
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
