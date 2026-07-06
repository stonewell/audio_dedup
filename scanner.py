from __future__ import annotations

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


def scan(directory: Path, min_size: int = 0) -> list[AudioFile]:
    files: list[AudioFile] = []
    for path in directory.rglob("*"):
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < min_size:
            continue
        try:
            audio = mutagen.File(path, easy=True)
        except Exception:
            continue
        if audio is None:
            continue

        duration, bitrate = _read_info(audio)
        raw_tags = dict(audio.tags) if audio.tags else {}
        tags = {
            "title": _tag_str(raw_tags.get("title")),
            "artist": _tag_str(raw_tags.get("artist")),
            "album": _tag_str(raw_tags.get("album")),
            "tracknumber": _tag_str(raw_tags.get("tracknumber")),
        }
        files.append(AudioFile(
            path=path,
            size=stat.st_size,
            duration=duration,
            bitrate=bitrate,
            tags=tags,
        ))
    return files
