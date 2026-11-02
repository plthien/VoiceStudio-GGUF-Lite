"""Reviewed Lite-only source instrumentation; exact pinned C++ anchor required."""
from pathlib import Path
import sys

path=Path(sys.argv[1])
source=path.read_text(encoding="utf-8")
old='    ov_log(OV_LOG_INFO, "[Load] %s backend: %s (CPU threads: %d)", label, ggml_backend_name(bp.backend), n_threads);'
new='''    // LiteAttest/v1: native-selected GGML device, not requested env or vulkaninfo.
    ggml_backend_dev_t lite_dev = ggml_backend_get_device(bp.backend);
    if (!lite_dev) {
        ov_log(OV_LOG_ERROR, "[LiteAttest/v1] missing native GGML device");
        return BackendPair{};
    }
    ov_log(OV_LOG_INFO, "[LiteAttest/v1] backend=%s; device=%s; description=%s",
           ggml_backend_name(bp.backend),
           ggml_backend_dev_name(lite_dev),
           ggml_backend_dev_description(lite_dev));
''' + old
if source.count(old)!=1 or "[LiteAttest/v1]" in source:
    raise SystemExit("Pinned C++ source does not match attestation patch anchor")
path.write_text(source.replace(old,new),encoding="utf-8")

