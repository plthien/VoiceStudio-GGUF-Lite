#!/usr/bin/env bash
# Lite-only Windows x86_64 Vulkan source-build, pinned to 886fc0798.
# No legacy VoiceStudio dependency tree or fallback build for non-Vulkan targets.
set -euo pipefail

PLATFORM=""
COMMIT_SHA=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform)
            PLATFORM="$2"
            shift 2
            ;;
        --commit-sha)
            COMMIT_SHA="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '1,30p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$PLATFORM" || -z "$COMMIT_SHA" ]]; then
    echo "Both --platform and --commit-sha are required." >&2
    exit 1
fi

if [[ "$COMMIT_SHA" != "886fc079838ca7400cb2b42b36e2a65aa1daabe8" ]]; then
    echo "--commit-sha must be a 40-char hex SHA (got: $COMMIT_SHA)" >&2
    exit 1
fi

case "$PLATFORM" in
    windows-x86_64) ;;
    *)
        echo "Unknown --platform: $PLATFORM" >&2
        exit 1
        ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# A bare `-j` runs every compile at once. With GGML_CPU_ALL_VARIANTS that is
# dozens of cc1plus processes, which the kernel OOM-kills on a 16 GB runner
# ("Killed signal terminated program cc1plus"). Bound it by the CPU count.
BUILD_JOBS="${OMNIVOICE_BUILD_JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo "${NUMBER_OF_PROCESSORS:-2}")}"
source "$REPO_ROOT/scripts/internal/omnivoice-vulkan.sh"
BIN_DIR="$REPO_ROOT/bin"
mkdir -p "$BIN_DIR"

WORK="$(mktemp -d -t omnivoice-cpp-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "→ Cloning omnivoice.cpp at $COMMIT_SHA into $WORK"
git clone --recurse-submodules https://github.com/ServeurpersoCom/omnivoice.cpp.git "$WORK/src"
git -C "$WORK/src" checkout --detach "$COMMIT_SHA"
[[ "$(git -C "$WORK/src" rev-parse HEAD)" == "886fc079838ca7400cb2b42b36e2a65aa1daabe8" ]] || exit 2
git -C "$WORK/src" submodule update --init --recursive
python "$REPO_ROOT/scripts/internal/instrument-attestation.py" "$WORK/src/src/backend.h"

cd "$WORK/src"

# A dynamically-linked build (the portable x86-64 CPU or Vulkan build)
# leaves the ggml runtime as shared libraries inside the build tree; only
# copying the executable ships a binary that dies on first spawn with
# exit 127 — "libggml.so.0: cannot open shared object file" — because the
# EXIT trap deletes the build tree, .so files and all (#1348). Copy them
# next to the binary; a static build simply has nothing matching. The
# backend puts bin/ on the loader path when it spawns the binary.
# Every library copied here is also recorded in COPIED_LIBS so the manifest
# step below checksums it: the runtime loads these at spawn time (with
# GGML_BACKEND_DL it picks its CPU/Vulkan backend from them), so they are
# verified like the executable.
COPIED_LIBS="$WORK/copied-libs"
: > "$COPIED_LIBS"
copy_shared_libs() {
    find build \( -name 'libggml*.so*' -o -name 'libggml*.dylib' -o -name 'ggml*.dll' \) \
        -type f -print0 2>/dev/null |
        while IFS= read -r -d '' lib; do
            cp -v "$lib" "$BIN_DIR/"
            basename "$lib" >> "$COPIED_LIBS"
        done
    # Dereference any symlinked SONAMEs (libggml.so.0 -> libggml.so) so the
    # copies in bin/ are real files under every name the loader asks for.
    find build \( -name 'libggml*.so*' -o -name 'libggml*.dylib' \) \
        -type l -print0 2>/dev/null |
        while IFS= read -r -d '' lib; do
            cp -vL "$lib" "$BIN_DIR/"
            basename "$lib" >> "$COPIED_LIBS"
        done
}

# MSVC builds link the C++ runtime dynamically, and a Windows host without the
# Visual C++ Redistributable cannot start the binary (exit 0xC0000135). The
# desktop installer ships it (#2780), so deploy the runtime app-locally next
# to the executable, as Microsoft documents for redistribution. These x64
# copies also serve Windows on ARM, which runs this binary under emulation.
copy_msvc_runtime() {
    # OMNIVOICE_MSVC_RUNTIME_DIR overrides the source directory (tests).
    local runtime_dir="${OMNIVOICE_MSVC_RUNTIME_DIR:-}" dll
    if [[ -z "$runtime_dir" ]]; then
        runtime_dir="$(cygpath -u "${SYSTEMROOT:-C:\\Windows}")/System32"
    fi
    for dll in vcruntime140.dll vcruntime140_1.dll msvcp140.dll msvcp140_1.dll \
        msvcp140_2.dll vcomp140.dll; do
        if [[ -f "$runtime_dir/$dll" ]]; then
            cp -v "$runtime_dir/$dll" "$BIN_DIR/"
            printf '%s\n' "$dll" >> "$COPIED_LIBS"
        fi
    done
    if [[ ! -f "$BIN_DIR/msvcp140.dll" || ! -f "$BIN_DIR/vcruntime140.dll" ]]; then
        echo "Visual C++ runtime DLLs not found in $runtime_dir" >&2
        return 1
    fi
}

# Multi-config generators (Visual Studio) put the executable under
# build/Release/; single-config ones (Ninja, Makefiles) put it in build/.
built_windows_exe() {
    local candidate
    for candidate in build/Release/omnivoice-tts.exe build/omnivoice-tts.exe; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    echo "omnivoice-tts.exe not found in build/Release/ or build/" >&2
    return 1
}





OUT_NAME="omnivoice-tts-$PLATFORM"
case "$PLATFORM" in
    windows-x86_64) OUT_NAME="$OUT_NAME.exe" ;;
esac

# Absolutely no CPU fallback or other platforms.
if ! try_vulkan_build; then
    echo "Vulkan compilation failed; CPU fallback forbidden" >&2
    exit 2
fi
exe="$(built_windows_exe)"
cp -v "$exe" "$BIN_DIR/$OUT_NAME"
copy_shared_libs
copy_msvc_runtime
for lib in ggml-vulkan.dll ggml.dll ggml-base.dll; do
    [[ -s "$BIN_DIR/$lib" ]] || { echo "Missing required $lib" >&2; exit 2; }
done

# Compute SHA-256 for the binary and every library copied above, and write
# them to bin/checksums.sha256.
cd "$BIN_DIR"
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        echo "No sha256sum/shasum found; cannot compute checksum." >&2
        return 1
    fi
}
DIGEST="$(sha256_of "$OUT_NAME")"

# Rewrite the manifest atomically, replacing (not accumulating) the entries
# this build owns, so renamed platforms and rebuilt libraries never leave a
# stale digest behind.
MANIFEST="$BIN_DIR/checksums.sha256"
TMP_MANIFEST="$(mktemp)"
OWNED="$(mktemp)"
{ printf '%s\n' "$OUT_NAME"; sort -u "$COPIED_LIBS"; } > "$OWNED"
if [[ -f "$MANIFEST" ]]; then
    awk 'NR == FNR { owned[$0] = 1; next } !($2 in owned)' "$OWNED" "$MANIFEST" > "$TMP_MANIFEST"
fi
while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    printf '%s  %s\n' "$(sha256_of "$name")" "$name" >> "$TMP_MANIFEST"
done < "$OWNED"
sort -k2 -o "$MANIFEST" "$TMP_MANIFEST"
rm -f "$TMP_MANIFEST" "$OWNED"

echo "✓ Built $OUT_NAME ($DIGEST)"
