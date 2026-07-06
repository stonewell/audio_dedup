from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..cache import FingerprintCache
from ..models import AudioFile, DuplicateGroup

_DURATION_RATIO_LIMIT = 1.15  # skip pair if durations differ by more than 15%

_NO_BACKEND_WARNING = (
    "No fingerprinting backend available — skipping fingerprint tier.\n"
    "  Library path (no binary needed):\n"
    "    pip install chromaprint audioread pyacoustid\n"
    "    Linux:  apt install libchromaprint1\n"
    "    macOS:  brew install chromaprint\n"
    "  Binary path (all platforms):\n"
    "    Download fpcalc from https://acoustid.org/chromaprint and add to PATH\n"
    "    (or set the FPCALC environment variable to its full path)"
)


def _try_chromaprint_library(path: Path) -> tuple[float, list[int]] | None:
    """Fingerprint via libchromaprint (requires chromaprint + audioread + pyacoustid).

    pyacoustid handles audio decoding via audioread; chromaprint provides ctypes
    bindings to libchromaprint and the pure-Python decode_fingerprint() decoder.
    """
    try:
        import acoustid
        import chromaprint as _cp

        if not (acoustid.have_chromaprint and acoustid.have_audioread):
            return None
        duration, fp_encoded = acoustid.fingerprint_file(str(path))
        fp_ints, _ = _cp.decode_fingerprint(fp_encoded)
        return float(duration), [int(x) for x in fp_ints]
    except Exception:
        return None


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
        return float(data.get("duration") or 0), data.get("fingerprint") or []
    except Exception:
        return None


def _compute_fingerprint(path: Path) -> tuple[float, list[int]] | None:
    """Try library path first, fall back to fpcalc binary."""
    return _try_chromaprint_library(path) or _try_fpcalc(path)


def _hamming_bits(a: int, b: int) -> int:
    xor = a ^ b
    count = 0
    while xor:
        xor &= xor - 1
        count += 1
    return count


def _similarity(a: list[int], b: list[int]) -> float:
    length = min(len(a), len(b))
    if length == 0:
        return 0.0
    diff = sum(_hamming_bits(a[i], b[i]) for i in range(length))
    return 1.0 - diff / (length * 32)


def find_fingerprint_duplicates(
    files: list[AudioFile],
    cache: FingerprintCache,
    threshold: float = 0.85,
) -> tuple[list[DuplicateGroup], set[Path], list[str]]:
    warnings: list[str] = []

    # Compute / load fingerprints
    for f in files:
        key = str(f.path)
        try:
            mtime = f.path.stat().st_mtime
        except OSError:
            continue
        fp_data = cache.get(key, mtime)
        if fp_data is None:
            result = _compute_fingerprint(f.path)
            if result is not None:
                fp_duration, fp_data = result
                cache.set(key, mtime, fp_data)
                if f.duration == 0.0 and fp_duration:
                    f.duration = fp_duration
        f.fingerprint = fp_data

    cache.save()

    eligible = [f for f in files if f.fingerprint]

    if not eligible:
        warnings.append(_NO_BACKEND_WARNING)
        return [], set(), warnings

    # Union-Find
    parent: dict[Path, Path] = {f.path: f.path for f in eligible}

    def find(x: Path) -> Path:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: Path, y: Path) -> None:
        parent[find(x)] = find(y)

    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            if a.duration > 0 and b.duration > 0:
                ratio = max(a.duration, b.duration) / min(a.duration, b.duration)
                if ratio > _DURATION_RATIO_LIMIT:
                    continue
            sim = _similarity(a.fingerprint, b.fingerprint)
            if sim >= threshold:
                union(a.path, b.path)

    buckets: dict[Path, list[AudioFile]] = {}
    for f in eligible:
        root = find(f.path)
        buckets.setdefault(root, []).append(f)

    groups: list[DuplicateGroup] = []
    matched: set[Path] = set()

    for members in buckets.values():
        if len(members) < 2:
            continue
        anchor = members[0].fingerprint
        avg_score = sum(_similarity(anchor, m.fingerprint) for m in members[1:]) / (len(members) - 1)
        groups.append(DuplicateGroup(
            tier="fingerprint",
            confidence="high",
            files=members,
            score=avg_score,
        ))
        for f in members:
            matched.add(f.path)

    return groups, matched, warnings
