from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class AudioFile:
    path: Path
    size: int
    duration: float
    bitrate: int  # bps; 0 if unknown
    tags: dict[str, str]  # normalized: title, artist, album, tracknumber
    fingerprint: list[int] | None = None


@dataclass
class DuplicateGroup:
    tier: Literal["tags", "fingerprint", "name"]
    confidence: Literal["high", "medium"]
    files: list[AudioFile] = field(default_factory=list)
    score: float = 1.0
