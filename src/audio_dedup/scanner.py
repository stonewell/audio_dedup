from __future__ import annotations

import itertools
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import mutagen

from .models import AudioFile

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus"}


def _tag_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]).strip() if val else ""
    return str(val).strip()


def _read_info(audio) -> tuple[float, int]:
    info = getattr(audio, "info", None)
    if info is None:
        return 0.0, 0
    duration = float(getattr(info, "length", 0) or 0)
    bitrate = int(getattr(info, "bitrate", 0) or 0)
    return duration, bitrate


def _read_file(path: Path, min_size: int) -> AudioFile | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size < min_size:
        return None
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None:
        return None

    duration, bitrate = _read_info(audio)
    raw_tags = dict(audio.tags) if audio.tags else {}
    tags = {
        "title": _tag_str(raw_tags.get("title")),
        "artist": _tag_str(raw_tags.get("artist")),
        "album": _tag_str(raw_tags.get("album")),
        "tracknumber": _tag_str(raw_tags.get("tracknumber")),
    }
    return AudioFile(path=path, size=stat.st_size, duration=duration, bitrate=bitrate, tags=tags)


def scan(directory: Path, min_size: int = 0, max_workers: int | None = None) -> list[AudioFile]:
    """Walk `directory` and read tags for every audio file.

    Parsing a tag header (struct unpacking, ID3/FLAC frame decoding, string
    handling in mutagen) is mostly pure-Python CPU work, not I/O wait, so a
    thread pool mostly serializes on the GIL instead of actually
    parallelizing it — a thread pool was tried here first and didn't scale.
    A process pool (same pattern as matchers/fingerprint.py's fingerprinting
    step) gives each worker its own interpreter/GIL, which is what actually
    makes a large collection scan faster with more cores.
    """
    paths = [p for p in directory.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS]
    workers = max_workers or (os.cpu_count() or 4)

    files: list[AudioFile] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_read_file, paths, itertools.repeat(min_size)):
            if result is not None:
                files.append(result)
    return files
