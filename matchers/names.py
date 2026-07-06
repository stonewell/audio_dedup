from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz

from ..models import AudioFile, DuplicateGroup

_TRACK_PREFIX = re.compile(r"^\d{1,3}[\s.\-_]+")
_TRAILING_JUNK = re.compile(
    r"[\s_\-]+(\(\d+\)|\[\d+\]|copy|duplicate|\d+)$",
    re.IGNORECASE,
)


def _norm_stem(path: Path) -> str:
    stem = path.stem
    stem = _TRACK_PREFIX.sub("", stem)
    stem = _TRAILING_JUNK.sub("", stem)
    return stem.lower().strip()


def find_name_duplicates(
    files: list[AudioFile],
    score_threshold: float = 85.0,
    duration_tolerance: float = 5.0,
) -> tuple[list[DuplicateGroup], set[Path]]:
    norms = {f.path: _norm_stem(f.path) for f in files}

    parent: dict[Path, Path] = {f.path: f.path for f in files}

    def find(x: Path) -> Path:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: Path, y: Path) -> None:
        parent[find(x)] = find(y)

    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            a, b = files[i], files[j]
            if abs(a.duration - b.duration) > duration_tolerance:
                continue
            score = fuzz.token_sort_ratio(norms[a.path], norms[b.path])
            if score >= score_threshold:
                union(a.path, b.path)

    buckets: dict[Path, list[AudioFile]] = {}
    for f in files:
        root = find(f.path)
        buckets.setdefault(root, []).append(f)

    groups: list[DuplicateGroup] = []
    matched: set[Path] = set()

    for members in buckets.values():
        if len(members) < 2:
            continue
        anchor_norm = norms[members[0].path]
        avg_score = sum(
            fuzz.token_sort_ratio(anchor_norm, norms[m.path]) for m in members[1:]
        ) / (len(members) - 1)
        groups.append(DuplicateGroup(
            tier="name",
            confidence="medium",
            files=members,
            score=avg_score / 100.0,
        ))
        for f in members:
            matched.add(f.path)

    return groups, matched
