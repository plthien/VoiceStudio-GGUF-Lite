"""python -m lite.preflight: conservative dry run (no downloads)."""
import json
import shutil
from .config import Settings
from .runtime import preflight
from .licence import Licence

def main():
    settings = Settings.load()
    report = preflight(settings)
    report["ffmpeg"] = bool(shutil.which("ffmpeg"))
    report["licence"] = Licence(settings).status()
    report["ready"] = report["ready"] and report["licence"]["accepted"] and report["licence"]["provenance_reconciled"] and report["ffmpeg"]
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
