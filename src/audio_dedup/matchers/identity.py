from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz

from ..models import AudioFile

_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_NONWORD_RE = re.compile(r"[^\w\s]")
_TRACK_PREFIX = re.compile(r"^\d{1,3}[\s.\-_]+")
_TRAILING_JUNK = re.compile(
    r"[\s_\-]+(\(\d+\)|\[\d+\]|copy|duplicate|\d+)$",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = s.lower()
    s = _NONWORD_RE.sub("", s)
    return " ".join(s.split())


def _norm_artist(s: str) -> str:
    s = _FEAT_RE.sub("", s)
    return _norm(s)


def _norm_stem(path: Path) -> str:
    stem = path.stem
    stem = _TRACK_PREFIX.sub("", stem)
    stem = _TRAILING_JUNK.sub("", stem)
    return stem.lower().strip()


def _tag_key(f: AudioFile) -> str | None:
    title = _norm(f.tags.get("title", ""))
    artist = _norm_artist(f.tags.get("artist", ""))
    if not title or not artist:
        return None
    return f"{title} {artist}"


def identity_score(a: AudioFile, b: AudioFile) -> float:
    """Fuzzy tag/filename similarity (0-100) between two files.

    This is purely corroborating evidence attached to an already
    fingerprint-confirmed match — see matchers/duplicates.py — so the report
    can distinguish "tags and audio agree" from "audio matches but tags
    differ" (e.g. re-ripped or re-downloaded files with unrelated names).
    It never gates whether a fingerprint check happens.
    """
    score = fuzz.token_sort_ratio(_norm_stem(a.path), _norm_stem(b.path))
    a_tag, b_tag = _tag_key(a), _tag_key(b)
    if a_tag and b_tag:
        score = max(score, fuzz.token_sort_ratio(a_tag, b_tag))
    return score
