"""Retired compatibility module: Nano production is CUDA-only."""


def cpu_diagnostic(*_args, **_kwargs):
    raise RuntimeError("CPU_FALLBACK_UNSUPPORTED")


def main():
    print("CPU_FALLBACK_UNSUPPORTED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
