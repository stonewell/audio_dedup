from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from ..cache import FingerprintCache
from ..models import AudioFile

_MAX_OFFSET = 20  # frames (~2.5s at chromaprint's ~0.128s/frame) searched to absorb lead-in/trim drift

NO_BACKEND_WARNING = (
    "No fingerprinting backend available — skipping duplicate detection.\n"
    "  Library path (no binary needed):\n"
    "    pip install chromaprint audioread pyacoustid\n"
    "    Linux:  apt install libchromaprint1\n"
    "    macOS:  brew install chromaprint\n"
    "  Binary path (all platforms):\n"
    "    Download fpcalc from https://acoustid.org/chromaprint and add to PATH\n"
    "    (or set the FPCALC environment variable to its full path)"
)


def _try_chromaprint_library(path: Path) -> tuple[float, list[int]] | None:
    """Fingerprint via libchromaprint with explicit PCM chunk alignment.

    audioread may return chunks whose byte length is not divisible by
    channels*2 (one int16 frame).  Feeding such a chunk to
    chromaprint.Fingerprinter.feed() triggers a hard C-level abort:
      audio_processor.cpp: Assertion `length % m_num_channels == 0' failed
    We buffer across chunks and only feed aligned slices.
    """
    try:
        import audioread
        import chromaprint as _cp

        with audioread.audio_open(str(path)) as af:
            samplerate: int = af.samplerate
            channels: int = af.channels
            duration: float = float(af.duration)

            fper = _cp.Fingerprinter()
            fper.start(samplerate, channels)

            frame_bytes = channels * 2  # bytes per interleaved int16 frame
            leftover = b""
            for chunk in af:
                data = leftover + chunk
                aligned = len(data) - (len(data) % frame_bytes)
                if aligned:
                    fper.feed(data[:aligned])
                leftover = data[aligned:]
            # trailing sub-frame bytes are dropped (< 1 sample, inaudible)

            fp_encoded = fper.finish()

        fp_ints, _ = _cp.decode_fingerprint(fp_encoded)
        return duration, [int(x) for x in fp_ints]
    except Exception:
        return None


def _to_signed32(x: int) -> int:
    """Normalize a 32-bit value to Python's signed two's-complement range.

    fpcalc's -json output emits some sub-fingerprints as unsigned literals
    (up to 2**32-1), which struct.pack('i', ...) rejects outright. The
    chromaprint library path already returns signed ints, so normalizing
    here makes every fingerprint element consistently representable as a
    32-bit two's-complement value regardless of which backend produced it.
    """
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _try_fpcalc(path: Path) -> tuple[float, list[int]] | None:
    """Fingerprint by calling the fpcalc binary with -raw -json.

    Returns raw integer arrays directly — no chromaprint decode step needed.
    Works on Windows, Linux, and macOS as long as fpcalc is in PATH.
    """
    fpcalc = shutil.which("fpcalc") or os.environ.get("FPCALC")
    if not fpcalc:
        return None
    try:
        result = subprocess.run(
            [fpcalc, "-raw", "-json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        fingerprint = [_to_signed32(x) for x in (data.get("fingerprint") or [])]
        return float(data.get("duration") or 0), fingerprint
    except Exception:
        return None


def _compute_fingerprint(path: Path) -> tuple[float, list[int]] | None:
    """Try library path first, fall back to fpcalc binary. Runs in a worker process."""
    return _try_chromaprint_library(path) or _try_fpcalc(path)


def _similarity_at_offset(a: list[int], b: list[int], offset: int) -> float:
    a2, b2 = (a[offset:], b) if offset >= 0 else (a, b[-offset:])
    length = min(len(a2), len(b2))
    if length == 0:
        return 0.0
    # Mask to 32 bits before popcount: Python ints are arbitrary-precision,
    # so XOR-ing a negative and a non-negative value does NOT yield the
    # 32-bit two's-complement XOR pattern .bit_count() needs — it silently
    # produces a nonsense result (e.g. (-5 ^ 3).bit_count() == 1 instead of
    # the correct 29), badly under-counting differing bits about half the
    # time in practice, since sign is essentially a coin flip per frame.
    diff = sum(((a2[i] ^ b2[i]) & 0xFFFFFFFF).bit_count() for i in range(length))
    return 1.0 - diff / (length * 32)


def fingerprint_similarity(
    a: list[int],
    b: list[int],
    threshold: float = 0.85,
    max_offset: int = _MAX_OFFSET,
) -> float:
    """Best Hamming similarity between two fingerprints across a small range
    of frame offsets. A naive index-aligned compare misses matches where one
    file has extra lead-in silence, ID3 padding, or a slightly different
    trim — a fraction-of-a-second shift is enough to desync frame-by-frame
    comparison otherwise. Stops early once `threshold` is met.
    """
    best = _similarity_at_offset(a, b, 0)
    if best >= threshold:
        return best
    for offset in range(1, max_offset + 1):
        best = max(best, _similarity_at_offset(a, b, offset), _similarity_at_offset(a, b, -offset))
        if best >= threshold:
            return best
    return best


def ensure_fingerprints(
    files: list[AudioFile],
    cache: FingerprintCache,
    max_workers: int | None = None,
    verbose: bool = False,
    force_refresh: bool = False,
) -> tuple[list[str], int]:
    """Populate .fingerprint (and backfill unknown .duration) on every file.

    Cache hits are resolved sequentially (cheap DB lookups); cache misses —
    the expensive decode+fingerprint step — are computed in a process pool
    so a large collection isn't bottlenecked on a single core.

    With force_refresh=True, every file is recomputed regardless of what's
    cached (the cache is still updated with the fresh results). Without it,
    only files whose cache entry is missing or stale (mtime changed) are
    recomputed.

    Returns (warnings, new_count) — new_count is how many fingerprints were
    actually (re)computed this run, so callers can skip work (like rebuilding
    the term-index stats) when a repeat run changed nothing.
    """
    warnings: list[str] = []
    total = len(files)
    if total == 0:
        return warnings, 0

    to_compute: list[AudioFile] = []
    cached_count = 0
    for f in files:
        if force_refresh:
            to_compute.append(f)
            continue
        try:
            mtime = f.path.stat().st_mtime
        except OSError:
            continue
        cached = cache.get(str(f.path), mtime)
        if cached is None:
            to_compute.append(f)
            continue
        cached_duration, fp_data = cached
        f.fingerprint = fp_data
        if f.duration == 0.0 and cached_duration:
            f.duration = cached_duration
        cached_count += 1

    new_count = 0
    workers = max_workers or (os.cpu_count() or 4)

    if to_compute:
        if verbose:
            print(
                f"  Fingerprinting {len(to_compute)} files "
                f"({cached_count} already cached, {workers} workers)...",
                flush=True,
            )
        with ProcessPoolExecutor(max_workers=workers) as pool:
            future_to_file = {pool.submit(_compute_fingerprint, f.path): f for f in to_compute}
            try:
                for i, future in enumerate(as_completed(future_to_file), 1):
                    f = future_to_file[future]
                    result = future.result()
                    if result is not None:
                        fp_duration, fp_data = result
                        f.fingerprint = fp_data
                        if f.duration == 0.0 and fp_duration:
                            f.duration = fp_duration
                        try:
                            mtime = f.path.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        cache.set(str(f.path), mtime, f.duration, fp_data)
                        new_count += 1
                    if verbose:
                        print(f"  [{i}/{len(to_compute)}] {new_count} computed\r", end="", flush=True)
            except KeyboardInterrupt:
                pool.shutdown(cancel_futures=True)
                if verbose:
                    print(f"\n  Interrupted — {new_count} fingerprints saved to cache.")
                raise

    if verbose:
        print(f"  Done: {new_count} computed, {cached_count} from cache" + " " * 20)

    if total > 0 and not any(f.fingerprint for f in files):
        warnings.append(NO_BACKEND_WARNING)

    return warnings, new_count
